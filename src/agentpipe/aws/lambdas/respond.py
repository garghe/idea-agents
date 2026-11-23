"""POST /cases/{case_id}/respond

Answers:   {"answers": "<text>", "by": "..."}                              when the case waits for answers
Decision:  {"outcome": "approve" | "send_back" | "reject", "comment": "...", "by": "..."}
                                                                         at a gate or an escalation

Saves the response to the case file and resumes the workflow with the stored task token.
"""

from __future__ import annotations

import json

import boto3

from ...store import RUNNING, WAITING_ANSWERS, WAITING_ESCALATION, WAITING_GATE, now_iso
from .common import body_of, caller, response, store

OUTCOMES = {"approve", "send_back", "reject"}


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
    if not token:
        return response(409, {"error": f"case is not waiting for anyone (status: {case['status']})"})
    by = caller(event, data)

    if case["status"] == WAITING_ANSWERS:
        text = (data.get("answers") or "").strip()
        if not text:
            return response(400, {"error": "answers are required"})
        questions = s.open_blockers(case_id)
        n = len(s.list_docs(case_id, "answers/")) + 1
        lines = [f"# Answers from {by} ({now_iso()})", ""]
        lines += [f"- {q['id']} ({q['for']}): {q['question']}" for q in questions]
        lines += ["", "## Answer", "", text, ""]
        s.write_doc(case_id, f"answers/{n:02d}-{pending.get('stage', case['stage'])}.md", "\n".join(lines))
        for q in questions:
            q.update(status="answered", answered_at=now_iso(), answered_by=by)
            s.put_blocker(case_id, q)
        result = {"outcome": "answered"}

    elif case["status"] in (WAITING_GATE, WAITING_ESCALATION):
        outcome = data.get("outcome")
        if outcome not in OUTCOMES:
            return response(400, {"error": f"outcome must be one of {sorted(OUTCOMES)}"})
        comment = (data.get("comment") or "").strip()
        if outcome == "send_back" and not comment:
            return response(400, {"error": "a comment is required when sending back"})
        s.add_decision(case_id, {"kind": pending["kind"], "gate": pending.get("gate"),
                                 "stage": pending.get("stage"), "outcome": outcome, "by": by,
                                 "comment": comment, "at": now_iso()})
        if outcome == "send_back":
            n = len(s.list_docs(case_id, "answers/")) + 1
            s.write_doc(case_id, f"answers/{n:02d}-{pending.get('stage')}-feedback.md",
                        f"# Feedback from {by} on {pending.get('stage')}\n\n{comment}\n")
        result = {"outcome": outcome}
    else:
        return response(409, {"error": f"unexpected status {case['status']}"})

    case.update(status=RUNNING, pending=None)
    s.put_case(case)
    boto3.client("stepfunctions").send_task_success(taskToken=token, output=json.dumps(result))
    return response(200, {"case_id": case_id, **result})
