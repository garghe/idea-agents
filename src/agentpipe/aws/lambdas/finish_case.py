"""Records how a case ended when it did not finish normally: rejected, timed out or failed."""

from __future__ import annotations

import json

from ...store import FAILED, REJECTED, now_iso
from .common import notify, store

STATUS = {"rejected": REJECTED, "timed_out": FAILED, "failed": FAILED}


def handler(event, context):
    s = store()
    case = s.get_case(event["case_id"])
    outcome = event["outcome"]
    error = (event.get("error") or {}).get("error") if isinstance(event.get("error"), dict) else None
    case.update(status=STATUS[outcome], outcome=outcome, finished_at=now_iso(), pending=None)
    if error:
        case["error"] = json.dumps(error)[:2000]
    s.put_case(case)
    notify(f"Case {case['id']} ({case['title']}) ended: {outcome}.")
    return {"status": case["status"]}
