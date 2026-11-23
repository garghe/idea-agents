"""Shared helpers for the Lambda handlers."""

from __future__ import annotations

import json
import logging
import os
import urllib.request

import boto3

from ..store import AwsCaseStore

log = logging.getLogger()
log.setLevel(logging.INFO)

_store: AwsCaseStore | None = None


def store() -> AwsCaseStore:
    global _store
    if _store is None:
        _store = AwsCaseStore()
    return _store


def response(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def body_of(event: dict) -> dict:
    raw = event.get("body") or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("body is not valid JSON") from None
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    return data


def caller(event: dict, data: dict) -> str:
    """Who is acting: the IAM caller recorded by API Gateway, else the name given in the body."""
    identity = (event.get("requestContext") or {}).get("identity") or {}
    return identity.get("userArn") or data.get("by") or "unknown"


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
