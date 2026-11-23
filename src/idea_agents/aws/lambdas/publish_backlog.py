"""Publishes the released backlog (05-backlog.json) to Jira, then marks the case done.

Jira is configured through the JIRA_SECRET_ARN secret:
  {"base_url": "https://<site>.atlassian.net", "email": "...", "api_token": "...", "project_key": "ABC"}
With no usable secret this runs as a dry run: it records what it would create and still finishes.
Stories are linked to their epic through the `parent` field (Jira Cloud).
"""

from __future__ import annotations

import base64
import json

from ...store import DONE, now_iso
from .common import notify, post_json, secret_json, store


def issue_fields(project: str, kind: str, item: dict, parent_key: str | None = None) -> dict:
    if kind == "Epic":
        description = item["description"]
    else:
        criteria = "\n".join(f"* {c}" for c in item["acceptance_criteria"])
        description = (f"As a {item['as_a']}, I want {item['i_want']}, so that {item['so_that']}.\n\n"
                       f"Acceptance criteria:\n{criteria}")
        if item.get("design_refs"):
            description += "\n\nDesign: " + ", ".join(item["design_refs"])
    fields = {"project": {"key": project}, "summary": f"[{item['id']}] {item['title']}",
              "description": description, "issuetype": {"name": kind}}
    if parent_key:
        fields["parent"] = {"key": parent_key}
    return fields


def publish(backlog: dict, jira: dict) -> list[dict]:
    created: list[dict] = []
    dry = not all(jira.get(k) for k in ("base_url", "email", "api_token", "project_key"))
    if not dry:
        auth = base64.b64encode(f"{jira['email']}:{jira['api_token']}".encode()).decode()
        url = jira["base_url"].rstrip("/") + "/rest/api/2/issue"
        headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
    project = jira.get("project_key", "DRYRUN")
    for epic in backlog["epics"]:
        fields = issue_fields(project, "Epic", epic)
        epic_key = post_json(url, {"fields": fields}, headers)["key"] if not dry else f"DRY-{epic['id']}"
        created.append({"id": epic["id"], "key": epic_key, "type": "Epic"})
        for story in epic["stories"]:
            fields = issue_fields(project, "Story", story, parent_key=epic_key)
            key = post_json(url, {"fields": fields}, headers)["key"] if not dry else f"DRY-{story['id']}"
            created.append({"id": story["id"], "key": key, "type": "Story"})
    return created


def handler(event, context):
    s = store()
    case_id = event["case_id"]
    backlog = json.loads(s.read_doc(case_id, "05-backlog.json") or "{}")
    if not backlog.get("epics"):
        raise ValueError(f"case {case_id} has no backlog to publish")
    created = publish(backlog, secret_json("JIRA_SECRET_ARN"))
    s.write_doc(case_id, "06-published.json", json.dumps({"published_at": now_iso(), "issues": created}, indent=2))
    case = s.get_case(case_id)
    case.update(status=DONE, finished_at=now_iso(), pending=None)
    s.put_case(case)
    notify(f"Case {case_id} ({case['title']}): backlog released, {len(created)} Jira issues.")
    return {"issues": len(created)}
