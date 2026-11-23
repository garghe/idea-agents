"""POST /api/cases/{case_id}/respond

Answers:   {"answers": "<text>"}                                   when the case waits for answers
Decision:  {"outcome": "approve" | "send_back" | "reject", "comment": "..."}
                                                                   at a gate or an escalation

The person acting is taken from their sign-in token. The response is saved to the case file and the
workflow resumes with the stored task token.
"""

from __future__ import annotations

import json

import boto3

from ... import api
from ...store import RUNNING, WAITING_ANSWERS, now_iso
from .common import body_of, caller, response, store


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
    pending = case.get("pending") or {}
    token = pending.get("task_token")
    problem = api.check_response(case, data)
    if problem or not token:
        status = 409 if (not token or "not waiting" in (problem or "")) else 400
        return response(status, {"error": problem or f"case is not waiting for anyone (status: {case['status']})"})
    if not s.claim_pending(case_id, token):
        return response(409, {"error": "someone else has just responded to this case; refresh the page"})
    by = caller(event)

    if case["status"] == WAITING_ANSWERS:
        questions = s.open_blockers(case_id)
        n = len(s.list_docs(case_id, "answers/")) + 1
        lines = [f"# Answers from {by} ({now_iso()})", ""]
        lines += [f"- {q['id']} ({q['for']}): {q['question']}" for q in questions]
        lines += ["", "## Answer", "", data["answers"].strip(), ""]
        s.write_doc(case_id, f"answers/{n:02d}-{pending.get('stage', case['stage'])}.md", "\n".join(lines))
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
            s.write_doc(case_id, f"answers/{n:02d}-{pending.get('stage')}-feedback.md",
                        f"# Feedback from {by} on {pending.get('stage')}\n\n{comment}\n")
        result = {"outcome": outcome}

    case.update(status=RUNNING, pending=None)
    s.put_case(case)
    boto3.client("stepfunctions").send_task_success(taskToken=token, output=json.dumps(result))
    return response(200, {"case_id": case_id, **result})
