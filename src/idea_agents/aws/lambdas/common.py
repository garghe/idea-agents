"""Shared helpers for the Lambda handlers."""

from __future__ import annotations

import json
import logging
import os
import urllib.request

import boto3
from botocore.config import Config

from ..store import AwsCaseStore

log = logging.getLogger()
log.setLevel(logging.INFO)

_store: AwsCaseStore | None = None
_portal_url: str | None = None


def store() -> AwsCaseStore:
    global _store
    if _store is None:
        _store = AwsCaseStore()
    return _store


def pipeline_summary() -> dict:
    """The pipeline's stages and gates, passed in by the stack (generated from pipeline.yaml)."""
    return json.loads(os.environ["PIPELINE_JSON"])


def upload_targets(atts: list[dict]) -> list[dict]:
    """Short-lived signed S3 links, one per file, so the browser uploads straight to the case bucket.

    The regional endpoint avoids a redirect (which would break the browser's CORS check). The link is
    valid for 15 minutes and only for that key and content type. GuardDuty scans the file on arrival.
    """
    region = os.environ.get("AWS_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=region, endpoint_url=f"https://s3.{region}.amazonaws.com",
                      config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
    bucket = os.environ["CASES_BUCKET"]
    return [{
        "id": a["id"], "name": a["name"], "method": "PUT",
        "url": s3.generate_presigned_url("put_object", ExpiresIn=900, Params={
            "Bucket": bucket, "Key": a["key"], "ContentType": a["content_type"]}),
        "headers": {"Content-Type": a["content_type"]},
    } for a in atts]


def response(status: int, body: dict) -> dict:
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps(body)}


def body_of(event: dict) -> dict:
    raw = event.get("body") or "{}"
    if len(raw) > 200_000:
        raise ValueError("body is too large")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("body is not valid JSON") from None
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    return data


def caller(event: dict) -> str:
    """Who is acting, from the verified sign-in token. Never taken from the request body."""
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    return claims.get("name") or claims.get("email") or claims.get("cognito:username") or "unknown"


def secret_json(arn_env: str) -> dict:
    """Read a JSON secret; an unset or placeholder secret returns {} and the feature stays off."""
    arn = os.environ.get(arn_env)
    if not arn:
        return {}
    try:
        value = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
        data = json.loads(value)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - an unconfigured secret must not break the workflow
        log.warning("secret %s not usable; feature disabled", arn_env)
        return {}


def portal_url() -> str:
    """The portal address for links in notifications (from the stack's SSM parameter)."""
    global _portal_url
    if _portal_url is None:
        _portal_url = os.environ.get("PORTAL_URL", "")
        name = os.environ.get("PORTAL_URL_PARAMETER")
        if not _portal_url and name:
            try:
                _portal_url = boto3.client("ssm").get_parameter(Name=name)["Parameter"]["Value"]
            except Exception:  # noqa: BLE001 - a missing link must not stop the case
                log.warning("portal URL parameter %s not readable", name)
                _portal_url = ""
    return _portal_url.rstrip("/")


def case_link(case_id: str) -> str:
    base = portal_url()
    return f"{base}/#/cases/{case_id}" if base else ""


def post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URL comes from our own secret
        text = resp.read().decode("utf-8") or "{}"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def notify(text: str) -> None:
    """Post a message to the Teams incoming webhook, if one is configured; otherwise just log it."""
    log.info("notify: %s", text)
    url = secret_json("TEAMS_WEBHOOK_SECRET_ARN").get("url")
    if not url:
        return
    try:
        post_json(url, {"text": text})
    except Exception:  # noqa: BLE001 - a failed notification must not stop the case
        log.exception("Teams notification failed")
