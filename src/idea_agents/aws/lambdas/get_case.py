"""GET /api/cases/{case_id}             The case page: progress, open questions, findings, activity, cost.
GET /api/cases/{case_id}?doc=<name>  One document's text.
"""

from __future__ import annotations

from ... import api
from .common import pipeline_summary, response, store


def handler(event, context):
    case_id = (event.get("pathParameters") or {}).get("case_id")
    s = store()
    doc = (event.get("queryStringParameters") or {}).get("doc")
    try:
        if doc:
            text = s.read_doc(case_id, doc)
            if text is None:
                return response(404, {"error": f"no such document: {doc}"})
            return response(200, {"name": doc, "content": text})
        return response(200, api.detail_view(s, case_id, pipeline_summary()))
    except KeyError:
        return response(404, {"error": f"no such case: {case_id}"})
    except ValueError as exc:
        return response(400, {"error": str(exc)})
