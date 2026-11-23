"""POST /api/cases/{case_id}/respond

Answers:   {"answers": "<text>"}                                   when the case waits for answers
Decision:  {"outcome": "approve" | "send_back" | "reject", "comment": "..."}
                                                                   at a gate or an escalation
Reopen:    {"outcome": "reopen", "comment": "<what has changed>"}  when the case was rejected

The person acting is taken from their sign-in token. The response is saved to the case file and the
workflow resumes with the stored task token.
"""

from __future__ import annotations

import json
import logging
import os

import boto3

from ... import api
from ...store import REJECTED, RUNNING, WAITING_ANSWERS, now_iso
from .common import body_of, caller, pipeline_summary, response, store

log = logging.getLogger()


def reopen(s, case: dict, comment: str, by: str) -> dict:
    """Reopen a rejected case: a new execution resumes at the stage where it was rejected."""
    case_id = case["id"]
    if not s.transition_status(case_id, REJECTED, RUNNING):
        return response(409, {"error": "someone else has just reopened this case; refresh the page"})
    plan = api.reopen_plan(case, pipeline_summary())
    s.add_decision(case_id, {"kind": "reopen", "gate": None, "stage": plan["stage"], "outcome": api.REOPEN,
                             "by": by, "comment": comment, "at": now_iso()})
    n = len(s.list_docs(case_id, "answers/")) + 1
    s.save_doc(case_id, f"answers/{n:02d}-{plan['stage']}-reopen.md",
               f"# Reopened by {by}: what has changed\n\n{comment}\n", by=by, kind="person", stage=plan["stage"])
    case.update(status=RUNNING, pending=None, round=plan["round"], round_limit=plan["round_limit"])
    case.pop("finished_at", None)
    case.pop("outcome", None)
    s.put_case(case)
    reopens = sum(d["outcome"] == api.REOPEN for d in s.list_decisions(case_id))
    try:
        boto3.client("stepfunctions").start_execution(
            stateMachineArn=os.environ["STATE_MACHINE_ARN"],
            name=f"{case_id}-reopen-{reopens}",
            input=json.dumps({"case_id": case_id, "resume_stage": plan["stage"],
                              "round": plan["round"], "round_limit": plan["round_limit"]}),
        )
    except Exception:  # noqa: BLE001 - put the case back so it can be reopened again
        log.exception("could not start the reopened execution")
        case.update(status=REJECTED)
        s.put_case(case)
        return response(500, {"error": "the case could not be restarted; it is still rejected, try again"})
    return response(200, {"case_id": case_id, "outcome": "reopened", "stage": plan["stage"]})


def handler(event, context):
    case_id = (event.get("pathParameters") or {}).get("case_id")
    try:
        data = body_of(event)
    except ValueError as exc:
        return response(400, {"error": str(exc)})
    s = store()
    try:
        case = s.get_case(case_id)
    except KeyError:
        return response(404, {"error": f"no such case: {case_id}"})
    problem = api.check_response(case, data)
    if problem:
        return response(409 if "not waiting" in problem else 400, {"error": problem})
    if case["status"] == REJECTED:
        return reopen(s, case, data["comment"].strip(), caller(event))

    pending = case.get("pending") or {}
    token = pending.get("task_token")
    if not token:
        return response(409, {"error": f"case is not waiting for anyone (status: {case['status']})"})
    if not s.claim_pending(case_id, token):
        return response(409, {"error": "someone else has just responded to this case; refresh the page"})
    by = caller(event)

    if case["status"] == WAITING_ANSWERS:
        questions = s.open_blockers(case_id)
        n = len(s.list_docs(case_id, "answers/")) + 1
        lines = [f"# Answers from {by} ({now_iso()})", ""]
        lines += [f"- {q['id']} ({q['for']}): {q['question']}" for q in questions]
        lines += ["", "## Answer", "", data["answers"].strip(), ""]
        stage = pending.get("stage", case["stage"])
        s.save_doc(case_id, f"answers/{n:02d}-{stage}.md", "\n".join(lines), by=by, kind="person", stage=stage)
        for q in questions:
            q.update(status="answered", answered_at=now_iso(), answered_by=by)
            s.put_blocker(case_id, q)
        result = {"outcome": "answered"}
    else:
        outcome = data["outcome"]
        comment = (data.get("comment") or "").strip()
        s.add_decision(case_id, {"kind": pending["kind"], "gate": pending.get("gate"),
                                 "stage": pending.get("stage"), "outcome": outcome, "by": by,
                                 "comment": comment, "at": now_iso()})
        if outcome == "send_back":
            n = len(s.list_docs(case_id, "answers/")) + 1
            s.save_doc(case_id, f"answers/{n:02d}-{pending.get('stage')}-feedback.md",
                       f"# Feedback from {by} on {pending.get('stage')}\n\n{comment}\n",
                       by=by, kind="person", stage=pending.get("stage"))
        result = {"outcome": outcome}

    case.update(status=RUNNING, pending=None)
    s.put_case(case)
    boto3.client("stepfunctions").send_task_success(taskToken=token, output=json.dumps(result))
    return response(200, {"case_id": case_id, **result})
