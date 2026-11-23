"""POST /api/cases/{case_id}/attachments  {"files": [{"name": ..., "size": <bytes>}, ...]}

Attach files to an open case, at any time. Checks the files against the limits in pipeline.yaml,
creates a pending record for each (A1, A2, ...) and returns a signed upload link per file.
GuardDuty scans each upload; the ingest function then extracts its text and marks it ready.
"""

from __future__ import annotations

from ... import attachments as att_mod
from .common import body_of, caller, pipeline_summary, response, store, upload_targets


def handler(event, context):
    case_id = (event.get("pathParameters") or {}).get("case_id", "")
    try:
        data = body_of(event)
        case = store().get_case(case_id)
    except ValueError as exc:
        return response(400, {"error": str(exc)})
    except KeyError:
        return response(404, {"error": f"no such case: {case_id}"})
    files = data.get("files")
    problem = att_mod.check_new_files(case, store().list_attachments(case_id), files,
                                      pipeline_summary()["attachments"])
    if problem:
        return response(400, {"error": problem})
    atts = att_mod.plan_uploads(store(), case, files, caller(event))
    return response(201, {"uploads": upload_targets(atts)})
