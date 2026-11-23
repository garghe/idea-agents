"""Called by the state machine with a task token when a person must act.

Event: {"case_id", "kind": "answers" | "gate" | "escalation", "stage", "gate"?, "approver"?, "task_token"}

Stores the token on the case (the respond API uses it to resume the workflow) and notifies people.
Nothing runs while the case waits.
"""

from __future__ import annotations

import os

from ...store import WAITING_ANSWERS, WAITING_ESCALATION, WAITING_GATE, now_iso
from .common import notify, store

STATUS = {"answers": WAITING_ANSWERS, "gate": WAITING_GATE, "escalation": WAITING_ESCALATION}


def message(case: dict, pending: dict, questions: list[dict], findings: list[dict]) -> str:
    link = os.environ.get("PORTAL_URL", "").rstrip("/")
    where = f"\n\nRespond here: {link}/cases/{case['id']}" if link else ""
    head = f"Case {case['id']} ({case['title']})"
    if pending["kind"] == "answers":
        lines = [f"- {q['id']} for {q['for']}: {q['question']}" for q in questions]
        return f"{head} needs answers before it can continue:\n" + "\n".join(lines) + where
    if pending["kind"] == "gate":
        return f"{head} is ready for the {pending['gate']} decision ({pending.get('approver', 'approver')}).{where}"
    lines = [f"- {f['id']} [{f['severity']}] {f['issue']}" for f in findings]
    return (f"{head}: the design review could not settle these findings; "
            f"a decision is needed from the lead architect:\n" + "\n".join(lines) + where)


def handler(event, context):
    s = store()
    case = s.get_case(event["case_id"])
    pending = {k: event[k] for k in ("kind", "stage", "gate", "approver") if event.get(k)}
    pending.update(task_token=event["task_token"], since=now_iso())
    case.update(status=STATUS[event["kind"]], pending=pending)
    s.put_case(case)
    notify(message(case, pending, s.open_blockers(case["id"]), s.open_findings(case["id"])))
    return {"ok": True}
