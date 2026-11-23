"""POST /cases  {"title": ..., "request": "<the request text>", "requester": ...}

Creates the case and starts the state machine. Returns the case id.
"""

from __future__ import annotations

import json
import os

import boto3

from .common import body_of, caller, response, store


def handler(event, context):
    try:
        data = body_of(event)
    except ValueError as exc:
        return response(400, {"error": str(exc)})
    title, request = (data.get("title") or "").strip(), (data.get("request") or "").strip()
    if not title or not request:
        return response(400, {"error": "title and request are required"})

    case = store().create_case(
        title=title[:200],
        request_text=request,
        requester=data.get("requester") or caller(event, data),
        first_stage=os.environ.get("FIRST_STAGE", "intake"),
        round_limit=int(os.environ.get("MAX_REVIEW_ROUNDS", "3")),
    )
    boto3.client("stepfunctions").start_execution(
        stateMachineArn=os.environ["STATE_MACHINE_ARN"],
        name=case["id"],
        input=json.dumps({"case_id": case["id"]}),
    )
    return response(201, {"case_id": case["id"], "status": case["status"]})
