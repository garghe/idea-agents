"""GET /api/roles  The agent roles: what each does, its model, budget and limits (read-only).

The stack publishes the summary to the case bucket as catalogue/roles.json on every deploy, from the
same catalogue that is built into the runner image, so the two always match.
"""

from __future__ import annotations

import json
import os

import boto3

from .common import response

ROLES_KEY = "catalogue/roles.json"
_cache: dict | None = None


def handler(event, context):
    global _cache
    if _cache is None:
        obj = boto3.client("s3").get_object(Bucket=os.environ["CASES_BUCKET"], Key=ROLES_KEY)
        _cache = json.loads(obj["Body"].read())
    return response(200, _cache)
