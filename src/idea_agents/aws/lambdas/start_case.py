"""POST /api/cases  {"title": ..., "request": "<the request text>", "requester": ..., "attachments": [...]}

Creates the case and starts the state machine. Returns the case id and, for any attachments
({name, size} each), a signed upload link per file. The first stage waits for files still being
uploaded or scanned (up to `attachments.wait_seconds`), so it does not start without them.
"""

from __future__ import annotations

import json
import os

import boto3

from ... import api
from ... import attachments as att_mod
from ...store import NEW
from .common import body_of, caller, pipeline_summary, response, store, upload_targets


def handler(event, context):
    try:
        data = body_of(event)
    except ValueError as exc:
        return response(400, {"error": str(exc)})
    problem = api.check_new_case(data)
    if problem:
        return response(400, {"error": problem})

    first_stage = os.environ.get("FIRST_STAGE", "intake")
    files = data.get("attachments") or []
    if files:
        problem = att_mod.check_new_files({"status": NEW, "stage": first_stage}, [], files,
                                          pipeline_summary()["attachments"])
        if problem:
            return response(400, {"error": problem})

    case = store().create_case(
        title=data["title"].strip()[:200],
        request_text=data["request"].strip(),
        requester=(data.get("requester") or "").strip()[:100] or caller(event),
        first_stage=first_stage,
        round_limit=int(os.environ.get("MAX_REVIEW_ROUNDS", "3")),
    )
    # Records first, then the execution: the first stage then knows to wait for these files.
    uploads = upload_targets(att_mod.plan_uploads(store(), case, files, caller(event))) if files else []
    boto3.client("stepfunctions").start_execution(
        stateMachineArn=os.environ["STATE_MACHINE_ARN"],
        name=case["id"],
        input=json.dumps({"case_id": case["id"]}),
    )
    return response(201, {"case_id": case["id"], "status": case["status"], "uploads": uploads})
