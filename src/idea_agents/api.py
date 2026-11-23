"""What the portal sees: list and detail views of cases, and checks on what people send back.

Used by the local portal server and by the AWS Lambdas, so both return exactly the same shapes.
Standard library only (it runs inside Lambda without extra packages). `summary` is the pipeline
summary from `idea_agents.config.pipeline_summary`, passed in as a plain dict.
"""

from __future__ import annotations

import re

from .attachments import PENDING, READY, TYPE_LABELS
from .store import (DONE, FAILED, NEW, REJECTED, RUNNING, WAITING_ANSWERS, WAITING_ESCALATION,
                    WAITING_GATE, CaseStore)

ROLE_LABELS = {
    "intake": "Intake agent", "value": "Value agent", "analysis-lead": "Analysis lead",
    "designer": "Designer", "reviewer-architecture": "Architecture reviewer",
    "reviewer-security": "Security reviewer", "reviewer-operability": "Operability reviewer",
    "chair": "Review chair", "story-writer": "Story writer",
}
APPROVER_LABELS = {"sponsor": "Sponsor", "product_owner": "Product owner", "lead_architect": "Lead architect",
                   "requester": "Requester", "security": "Security", "other": "Other"}
OUTCOMES = {"approve", "send_back", "reject"}
REOPEN = "reopen"


def _gate_label(summary: dict, gate_id: str | None) -> str:
    for s in summary["stages"]:
        if s["gate"] and s["gate"]["id"] == gate_id:
            return s["gate"]["label"]
    return (gate_id or "decision").replace("_", " ").capitalize()


def public_case(case: dict) -> dict:
    """The case without the workflow's resume token, which must never leave the backend."""
    out = dict(case)
    pending = dict(case.get("pending") or {})
    pending.pop("task_token", None)
    out["pending"] = pending or None
    return out


def waiting_for(case: dict, open_questions: list[dict], summary: dict) -> str:
    status = case["status"]
    pending = case.get("pending") or {}
    if status == WAITING_ANSWERS:
        who = sorted({APPROVER_LABELS.get(q["for"], q["for"]) for q in open_questions}) or ["Requester"]
        n = len(open_questions)
        return f"{' and '.join(who)}: {n} question{'s' if n != 1 else ''}"
    if status == WAITING_GATE:
        approver = APPROVER_LABELS.get(pending.get("approver", ""), "Approver")
        return f"{approver}: {_gate_label(summary, pending.get('gate'))}"
    if status == WAITING_ESCALATION:
        return "Lead architect: escalated review"
    if status in (NEW, RUNNING):
        return "Agents working"
    if status == DONE:
        return "Done"
    if status == REJECTED:
        return "Rejected"
    return "Stopped"


def _last_summary(runs: list[dict], stage: str) -> str | None:
    done = [r for r in runs if r.get("stage") == stage and r.get("status") in ("complete", "blocked")]
    return done[-1].get("summary") if done else None


def progress(case: dict, runs: list[dict], decisions: list[dict], summary: dict) -> list[dict]:
    """Stages and enabled gates in order, each with a state: done, current, waiting, todo or stopped."""
    ids = [s["id"] for s in summary["stages"]]
    ci = ids.index(case["stage"]) if case["stage"] in ids else 0
    status = case["status"]
    last_decision = decisions[-1] if decisions else None
    items: list[dict] = []
    for i, s in enumerate(summary["stages"]):
        stage = {"kind": "stage", "id": s["id"], "label": s["label"], "state": "todo", "note": ""}
        gate = None
        if s["gate"]:
            gate = {"kind": "gate", "id": s["gate"]["id"], "label": s["gate"]["label"], "state": "todo",
                    "note": APPROVER_LABELS.get(s["gate"]["approver"], "")}
        if status == DONE or i < ci:
            stage.update(state="done", note=_last_summary(runs, s["id"]) or "Done")
            if gate:
                gate.update(state="done", note="Approved")
        elif i == ci:
            if status == WAITING_ANSWERS:
                stage.update(state="waiting", note="Needs answers")
            elif status in (NEW, RUNNING):
                stage.update(state="current", note="Agents working")
            elif status == WAITING_GATE:
                stage.update(state="done", note=_last_summary(runs, s["id"]) or "Done")
                if gate:
                    gate.update(state="waiting", note="Waiting for decision")
            elif status == WAITING_ESCALATION:
                stage.update(state="waiting", note="Escalated to a person")
            elif status == REJECTED and gate and last_decision and last_decision.get("gate") == gate["id"]:
                stage.update(state="done", note=_last_summary(runs, s["id"]) or "Done")
                gate.update(state="stopped", note="Rejected")
            elif status in (REJECTED, FAILED):
                stage.update(state="stopped", note="Rejected" if status == REJECTED else "Stopped")
        items.append(stage)
        if gate:
            items.append(gate)
    return items


def case_row(store: CaseStore, case: dict, summary: dict) -> dict:
    runs = store.list_runs(case["id"])
    decisions = store.list_decisions(case["id"])
    questions = store.open_blockers(case["id"])
    items = progress(case, runs, decisions, summary)
    steps = [p for p in items if p["kind"] == "stage"]
    current = next((p for p in items if p["state"] in ("current", "waiting", "stopped")), None)
    return {
        "id": case["id"], "title": case["title"], "requester": case.get("requester", ""),
        "status": case["status"], "stage": case["stage"], "round": case.get("round", 1),
        "created_at": case["created_at"], "finished_at": case.get("finished_at"),
        "waiting_for": waiting_for(case, questions, summary),
        "stage_note": f"{current['label']}: {current['note']}" if current else
                      ("Released" if case["status"] == DONE else steps[-1]["label"]),
        "progress": [p["state"] for p in steps],
        "cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in runs), 2),
    }


def list_view(store: CaseStore, summary: dict) -> dict:
    rows = [case_row(store, c, summary) for c in store.list_cases()]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    waiting = {WAITING_ANSWERS, WAITING_GATE, WAITING_ESCALATION}
    return {
        "cases": rows,
        "counts": {
            "all": len(rows),
            "waiting": sum(r["status"] in waiting for r in rows),
            "working": sum(r["status"] in (NEW, RUNNING) for r in rows),
            "done": sum(r["status"] == DONE for r in rows),
            "closed": sum(r["status"] in (DONE, REJECTED, FAILED) for r in rows),
        },
    }


def activity(runs: list[dict], decisions: list[dict], blockers: list[dict],
             attachments: list[dict] = ()) -> list[dict]:
    """Newest first. Entries in the same second keep their real order (runs, answers, decisions)."""
    items = []
    for a in attachments:
        items.append({"at": a["added_at"], "text": f"{a.get('added_by', 'Someone')} attached {a['id']} {a['name']}",
                      "kind": "person"})
        if a.get("status") == "rejected":
            items.append({"at": a.get("rejected_at") or a["added_at"],
                          "text": f"{a['id']} {a['name']} was turned away: {a.get('reason')}", "kind": "agent"})
    for r in runs:
        if r.get("status") == "invalid":
            text = f"{ROLE_LABELS.get(r.get('role'), r.get('role'))}: output rejected by checks, retrying"
        else:
            text = f"{ROLE_LABELS.get(r.get('role'), r.get('role'))}: {r.get('summary') or r.get('status')}"
        items.append({"at": r.get("ended_at") or r.get("started_at"), "text": text,
                      "round": r.get("round"), "kind": "agent"})
    for b in blockers:
        if b.get("answered_at"):
            items.append({"at": b["answered_at"], "text": f"{b.get('answered_by', 'Someone')} answered {b['id']}",
                          "kind": "person"})
    for d in decisions:
        if d["outcome"] == REOPEN:
            items.append({"at": d["at"], "text": f"{d['by']} reopened the case at the {d.get('stage')} stage",
                          "comment": d.get("comment") or None, "kind": "person"})
            continue
        verb = {"approve": "approved", "send_back": "sent back", "reject": "rejected"}.get(d["outcome"], d["outcome"])
        what = d.get("gate") or d.get("kind")
        what = "escalated review" if what == "escalation" else str(what).replace("_", " ").replace("signoff", "sign-off")
        items.append({"at": d["at"], "text": f"{d['by']} {verb} {what}",
                      "comment": d.get("comment") or None, "kind": "person"})
    for seq, item in enumerate(items):
        item["seq"] = seq
    items.sort(key=lambda x: (x["at"] or "", x["seq"]), reverse=True)
    for item in items:
        del item["seq"]
    return items


def spend_by_role(runs: list[dict]) -> list[dict]:
    """Per role: how many runs, what they cost, the cap the latest run had, and whether any hit a cap."""
    order: list[str] = []
    out: dict[str, dict] = {}
    for r in runs:
        role = r.get("role")
        if not role:
            continue
        if role not in out:
            order.append(role)
            out[role] = {"role": role, "label": ROLE_LABELS.get(role, role), "runs": 0, "cost_usd": 0.0,
                         "budget_usd": None, "limit_hit": False}
        e = out[role]
        e["runs"] += 1
        e["cost_usd"] = round(e["cost_usd"] + (r.get("cost_usd") or 0.0), 4)
        if r.get("budget_usd") is not None:
            e["budget_usd"] = r["budget_usd"]
        e["limit_hit"] = e["limit_hit"] or bool(r.get("limit_hit"))
    return [out[k] for k in order]


def attachment_rows(attachments: list[dict], runs: list[dict], summary: dict) -> list[dict]:
    """Each attachment with the stage it arrived in and the agents that have read it."""
    labels = {s["id"]: s["label"] for s in summary["stages"]}
    out = []
    for a in attachments:
        seen = []
        for r in runs:
            if a["id"] in (r.get("attachments_seen") or []):
                label = ROLE_LABELS.get(r.get("role"), r.get("role"))
                if label not in seen:
                    seen.append(label)
        out.append({
            "id": a["id"], "name": a["name"], "kind": a.get("kind"), "type": TYPE_LABELS.get(a.get("ext"), a.get("ext")),
            "size": a.get("size"), "status": a["status"], "reason": a.get("reason"), "note": a.get("note") or "",
            "pages": a.get("pages"), "text_doc": a.get("text_doc"), "scan": a.get("scan"),
            "added_by": a.get("added_by"), "added_at": a.get("added_at"),
            "added_during": labels.get(a.get("stage_at_upload"), a.get("stage_at_upload")),
            "seen_by": seen,
        })
    return out


def unseen_at_gate(case: dict, attachments: list[dict], runs: list[dict]) -> list[str]:
    """Ready files that no run of the stage now waiting for a decision has read."""
    if case["status"] not in (WAITING_GATE, WAITING_ESCALATION):
        return []
    seen = {i for r in runs if r.get("stage") == case["stage"] for i in (r.get("attachments_seen") or [])}
    return [f"{a['id']} {a['name']}" for a in attachments if a["status"] == READY and a["id"] not in seen]


DOC_LABELS = {"request.md": "Request", "01-request-brief.md": "Request brief", "02-business-case.md": "Business case",
              "03-requirements.md": "Requirements", "04-design.md": "Design", "05-backlog.json": "Backlog",
              "06-published.json": "Published to Jira"}
# Where the stock documents belong, for cases written before document versions were recorded.
_STOCK_STAGES = {"01-request-brief.md": "intake", "02-business-case.md": "value", "03-requirements.md": "analysis",
                 "04-design.md": "design", "05-backlog.json": "stories", "06-published.json": "stories"}
_ANSWER = re.compile(r"^answers/(\d+)-([a-z0-9_]+?)(-feedback|-reopen)?\.md$")
_ROUND = re.compile(r"^review/round-(\d+)-change-list\.md$")


def _doc_guess(name: str, summary: dict) -> dict:
    """Stage, kind and label from the document's name alone."""
    ids = [s["id"] for s in summary["stages"]]
    if name == "request.md":
        return {"stage": ids[0], "kind": "person"}
    m = _ANSWER.match(name)
    if m:
        return {"stage": m.group(2), "kind": "person"}
    if _ROUND.match(name):
        review = next((s["id"] for s in summary["stages"] if s.get("kind") == "review"), None)
        return {"stage": review, "kind": "agent"}
    return {"stage": _STOCK_STAGES.get(name), "kind": "agent"}


def _doc_label(name: str, counters: dict) -> str:
    if name in DOC_LABELS:
        return DOC_LABELS[name]
    m = _ANSWER.match(name)
    if m:
        what = {"-feedback": "Feedback", "-reopen": "Reopen comment"}.get(m.group(3) or "", "Answers")
        counters[what] = counters.get(what, 0) + 1
        return f"{what} {counters[what]}"
    m = _ROUND.match(name)
    if m:
        return f"Review round {m.group(1)}"
    stem = re.sub(r"^\d+-", "", name.rsplit("/", 1)[-1].rsplit(".", 1)[0])
    return stem.replace("-", " ").capitalize()


def _author(version: dict) -> str:
    if version.get("kind") == "agent":
        return ROLE_LABELS.get(version.get("by"), version.get("by") or "Agent")
    if version.get("kind") == "system":
        return {"jira": "Jira"}.get(version.get("by"), version.get("by") or "System")
    return version.get("by") or "Someone"


def document_groups(docs: list[str], versions: list[dict], attachments: list[dict], summary: dict) -> list[dict]:
    """The case's documents and attachments, grouped by stage in pipeline order, in time order within each.

    Each item says who wrote it (a person, an agent or the system), when it first appeared, when it last
    changed and how many versions exist. Documents from before versions were recorded get their stage
    and kind from their name, and no time.
    """
    by_name: dict[str, list[dict]] = {}
    for v in sorted(versions, key=lambda v: v.get("at") or ""):
        by_name.setdefault(v["name"], []).append(v)
    items = []
    for name in docs:
        vs = by_name.get(name) or []
        first, last = (vs[0], vs[-1]) if vs else ({}, {})
        guess = _doc_guess(name, summary)
        items.append({
            "type": "document", "name": name, "kind": first.get("kind") or guess["kind"],
            "stage": first.get("stage") or guess["stage"], "by": _author(first) if vs else None,
            "at": first.get("at"), "updated_at": last.get("at"), "versions": len(vs) or None,
        })
    for a in attachments:
        items.append({
            "type": "attachment", "name": a.get("text_doc"), "id": a["id"], "kind": "person",
            "stage": a.get("stage_at_upload"), "by": a.get("added_by"), "at": a.get("added_at"),
            "updated_at": a.get("added_at"), "versions": 1, "status": a["status"],
            "label": f"{a['id']} · {a['name']}", "file_type": TYPE_LABELS.get(a.get("ext"), a.get("ext")),
        })
    # Time order; a request comes before the files sent with it.
    items.sort(key=lambda i: (i["at"] is None, i["at"] or "", i["type"] == "attachment",
                              i["name"] != "request.md", i["name"] or ""))
    counters: dict = {}
    for i in items:
        if i["type"] == "document":
            i["label"] = _doc_label(i["name"], counters)
    for i in items:  # number answers, feedback and reopen comments only when there is more than one
        base = re.sub(r" \d+$", "", i["label"])
        if i["type"] == "document" and base != i["label"] and counters.get(base) == 1:
            i["label"] = base

    groups = [{"stage": s["id"], "label": s["label"], "items": []} for s in summary["stages"]]
    index = {g["stage"]: g for g in groups}
    other = {"stage": None, "label": "Other", "items": []}
    for i in items:
        index.get(i["stage"], other)["items"].append(i)
    return [g for g in groups + [other] if g["items"]]


def detail_view(store: CaseStore, case_id: str, summary: dict) -> dict:
    case = store.get_case(case_id)
    runs = store.list_runs(case_id)
    decisions = store.list_decisions(case_id)
    blockers = store.list_blockers(case_id)
    open_questions = [b for b in blockers if b["status"] == "open"]
    findings = store.list_findings(case_id)
    attachments = store.list_attachments(case_id)
    docs = store.list_docs(case_id)
    order = {"open": 0, "closed": 1, "duplicate": 2}
    findings.sort(key=lambda f: (order.get(f["status"], 3), {"blocker": 0, "major": 1, "minor": 2}.get(f["severity"], 3), f["id"]))
    stage_runs = [r for r in runs if r.get("stage") == case["stage"] and r.get("status") != "invalid"]
    chair = [r for r in stage_runs if r.get("role") == "chair"]
    return {
        "case": public_case(case),
        "progress": progress(case, runs, decisions, summary),
        "waiting_for": waiting_for(case, open_questions, summary),
        "open_questions": open_questions,
        "findings": findings,
        "decisions": decisions,
        "documents": [d for d in docs if not d.startswith("attachments/")],
        "document_groups": document_groups([d for d in docs if not d.startswith("attachments/")],
                                           store.list_doc_versions(case_id), attachments, summary),
        "attachments": attachment_rows(attachments, runs, summary),
        "attachments_pending": sum(a["status"] == PENDING for a in attachments),
        "can_attach": case["status"] != DONE,
        "unseen_at_gate": unseen_at_gate(case, attachments, runs),
        "activity": activity(runs, decisions, blockers, attachments),
        "stage_summary": (chair[-1] if chair else stage_runs[-1]).get("summary") if stage_runs else None,
        "cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in runs), 2),
        "budget_usd": summary.get("budget_usd"),
        "agent_runs": len(runs),
        "spend_by_role": spend_by_role(runs),
        "can_reopen": case["status"] == REJECTED,
        "reopen_stage": case["stage"] if case["status"] == REJECTED else None,
    }


def check_response(case: dict, data: dict) -> str | None:
    """Return why a response cannot be accepted, or None. Same rules locally and on AWS."""
    status = case["status"]
    if data.get("outcome") == REOPEN and status != REJECTED:
        return f"only a rejected case can be reopened (status: {status})"
    if status == REJECTED:
        if data.get("outcome") != REOPEN:
            return "a rejected case can only be reopened (outcome: reopen)"
        if not (data.get("comment") or "").strip():
            return "a comment is required when reopening: say what has changed"
        return None
    if status == WAITING_ANSWERS:
        if not (data.get("answers") or "").strip():
            return "answers are required"
        return None
    if status in (WAITING_GATE, WAITING_ESCALATION):
        outcome = data.get("outcome")
        if outcome not in OUTCOMES:
            return f"outcome must be one of {sorted(OUTCOMES)}"
        if outcome == "send_back" and not (data.get("comment") or "").strip():
            return "a comment is required when sending back"
        return None
    return f"case is not waiting for anyone (status: {status})"


def reopen_plan(case: dict, summary: dict) -> dict:
    """Where a reopened case restarts: the stage it was rejected at, in a new round.

    A reopened design review gets a fresh set of review rounds, as when an escalation is sent back.
    """
    stage = next(s for s in summary["stages"] if s["id"] == case["stage"])
    new_round = int(case.get("round") or 1) + 1
    limit = int(summary["max_review_rounds"])
    round_limit = new_round + limit - 1 if stage["kind"] == "review" else limit
    return {"stage": stage["id"], "round": new_round, "round_limit": round_limit}


def check_new_case(data: dict) -> str | None:
    if not (data.get("title") or "").strip() or not (data.get("request") or "").strip():
        return "title and request are required"
    if len(data["request"]) > 50_000:
        return "request is too long (50,000 characters at most)"
    return None
