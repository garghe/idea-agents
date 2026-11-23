"""POST /api/cases  {"title": ..., "request": "<the request text>", "requester": ...}

Creates the case and starts the state machine. Returns the case id.
"""

from __future__ import annotations

import json
import os

import boto3

from ... import api
from .common import body_of, caller, response, store


def handler(event, context):
    try:
        data = body_of(event)
    except ValueError as exc:
        return response(400, {"error": str(exc)})
    problem = api.check_new_case(data)
    if problem:
        return response(400, {"error": problem})

    case = store().create_case(
        title=data["title"].strip()[:200],
        request_text=data["request"].strip(),
        requester=(data.get("requester") or "").strip()[:100] or caller(event),
        first_stage=os.environ.get("FIRST_STAGE", "intake"),
        round_limit=int(os.environ.get("MAX_REVIEW_ROUNDS", "3")),
    )
    boto3.client("stepfunctions").start_execution(
        stateMachineArn=os.environ["STATE_MACHINE_ARN"],
        name=case["id"],
        input=json.dumps({"case_id": case["id"]}),
    )
    return response(201, {"case_id": case["id"], "status": case["status"]})
