"""GET /cases/{case_id}  The case, what it waits for, open questions and findings, documents and cost.

GET /cases/{case_id}?doc=<name>  returns one document's text.
"""

from __future__ import annotations

from .common import response, store


def handler(event, context):
    case_id = (event.get("pathParameters") or {}).get("case_id")
    s = store()
    try:
        case = s.get_case(case_id)
    except KeyError:
        return response(404, {"error": f"no such case: {case_id}"})

    doc = (event.get("queryStringParameters") or {}).get("doc")
    if doc:
        try:
            text = s.read_doc(case_id, doc)
        except ValueError as exc:
            return response(400, {"error": str(exc)})
        if text is None:
            return response(404, {"error": f"no such document: {doc}"})
        return response(200, {"name": doc, "content": text})

    pending = dict(case.get("pending") or {})
    pending.pop("task_token", None)  # never hand the resume token out
    case["pending"] = pending or None
    return response(200, {
        "case": case,
        "open_questions": s.open_blockers(case_id),
        "open_findings": s.open_findings(case_id),
        "decisions": s.list_decisions(case_id),
        "documents": s.list_docs(case_id),
        "cost_usd": s.total_cost(case_id),
    })
