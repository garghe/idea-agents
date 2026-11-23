"""Ingest an uploaded file once GuardDuty has scanned it (EventBridge: Malware Protection scan result).

  NO_THREATS_FOUND  extract the text into the case file and mark the attachment ready
  THREATS_FOUND     delete that version of the file and mark the attachment rejected
  anything else     (unsupported, access denied, failed) mark it rejected: it could not be checked

Only keys under uploads/<case id>/<attachment id>/<file> are handled; anything else is ignored.
This function is packaged with pypdf for PDF text (see infra/pipeline_stack.py).
"""

from __future__ import annotations

import logging
import os
import re

from ... import attachments as att_mod
from .common import store

log = logging.getLogger()
KEY = re.compile(r"^uploads/([A-Za-z0-9-]+)/(A\d+)/[A-Za-z0-9._-]+$")


def handler(event, context):
    detail = event.get("detail") or {}
    obj = detail.get("s3ObjectDetails") or {}
    key, bucket = obj.get("objectKey", ""), obj.get("bucketName")
    m = KEY.match(key)
    if bucket != os.environ["CASES_BUCKET"] or not m:
        log.warning("ignoring scan result for %s/%s", bucket, key)
        return {"ignored": True}
    case_id, att_id = m.groups()
    s = store()
    try:
        att = s.get_attachment(case_id, att_id)
    except KeyError:
        log.warning("no attachment record for %s", key)
        return {"ignored": True}
    if att["status"] != att_mod.PENDING or att.get("key") != key:
        return {"status": att["status"]}

    result = (detail.get("scanResultDetails") or {}).get("scanResultStatus")
    version = {"VersionId": obj["versionId"]} if obj.get("versionId") else {}
    if result == "THREATS_FOUND":
        s.s3.delete_object(Bucket=bucket, Key=key, **version)
        return {"status": att_mod.reject(s, case_id, att, "malware was found, so the file was deleted")["status"]}
    if result != "NO_THREATS_FOUND":
        return {"status": att_mod.reject(s, case_id, att, f"the file could not be scanned ({result or 'no result'})")["status"]}

    head = s.s3.head_object(Bucket=bucket, Key=key, **version)
    if head["ContentLength"] > att["size"] * 1.01 + 1024:
        return {"status": att_mod.reject(s, case_id, att, "the uploaded file is larger than declared")["status"]}
    data = s.s3.get_object(Bucket=bucket, Key=key, **version)["Body"].read()
    return {"status": att_mod.ingest(s, case_id, att_id, data, scan="no_threats_found")["status"]}
