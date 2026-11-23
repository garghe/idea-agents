"""The portal: the shared API views, the local server (mock only) and mock-by-default local runs."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from idea_agents import api
from idea_agents.config import pipeline_summary
from conftest import REPO, REQUEST


# ------------------------------------------------------------------ API views
def states(view):
    return [(p["label"], p["state"]) for p in view["progress"]]


def test_progress_follows_the_case(pipeline_factory, store, catalogue):
    summary = pipeline_summary(catalogue.pipeline)
    assert [s["gate"]["id"] if s["gate"] else None for s in summary["stages"]] == \
        [None, None, "scope_check", "design_signoff", "backlog_release"]  # go/no-go is off
    pipe, _ = pipeline_factory()
    cid = pipe.start("Highlights", REQUEST)["id"]

    view = api.detail_view(store, cid, summary)
    assert states(view)[0] == ("Intake", "waiting") and view["waiting_for"] == "Requester: 1 question"

    pipe.answer(cid, "Premier League only", by="Requester")
    view = api.detail_view(store, cid, summary)
    assert states(view)[:4] == [("Intake", "done"), ("Value", "done"), ("Analysis", "done"), ("Scope check", "waiting")]
    assert view["waiting_for"] == "Product owner: Scope check"
    assert view["stage_summary"].startswith("4 functional")

    pipe.decide(cid, "approve", by="PO")
    view = api.detail_view(store, cid, summary)
    assert ("Design sign-off", "waiting") in states(view)
    assert view["stage_summary"].startswith("No open findings")  # the chair's summary
    assert [f["id"] for f in view["findings"]] == ["SEC-1"]
    assert view["activity"][0]["text"].startswith("Review chair")

    row = api.list_view(store, summary)["cases"][0]
    assert row["stage_note"] == "Design sign-off: Waiting for decision"
    assert row["progress"] == ["done", "done", "done", "done", "todo"]

    pipe.decide(cid, "reject", by="Lead architect", comment="Not this season")
    view = api.detail_view(store, cid, summary)
    assert ("Design sign-off", "stopped") in states(view) and view["waiting_for"] == "Rejected"


def test_task_token_never_leaves_the_backend(store, catalogue):
    case = store.create_case("t", "x")
    case.update(status="waiting_gate", pending={"kind": "gate", "gate": "scope_check", "task_token": "SECRET"})
    store.put_case(case)
    view = api.detail_view(store, case["id"], pipeline_summary(catalogue.pipeline))
    assert "SECRET" not in json.dumps(view)


def test_response_checks():
    assert api.check_response({"status": "waiting_answers"}, {"answers": " "}) == "answers are required"
    assert api.check_response({"status": "waiting_gate"}, {"outcome": "send_back"}) == \
        "a comment is required when sending back"
    assert api.check_response({"status": "waiting_gate"}, {"outcome": "approve"}) is None
    assert "not waiting" in api.check_response({"status": "running"}, {"outcome": "approve"})
    assert api.check_new_case({"title": "x"}) == "title and request are required"


# ------------------------------------------------------------------ local server
@pytest.fixture
def server(tmp_path):
    from idea_agents.portal_server import make_server
    srv = make_server(tmp_path / "cases", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def call(base, path, body=None):
    req = urllib.request.Request(base + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if resp.headers.get_content_type() == "application/json" else raw)
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def test_local_portal_runs_the_whole_flow_on_mock_data(server):
    status, cfg = call(server, "/config.json")
    assert status == 200 and cfg["mode"] == "mock" and cfg["sampleRequest"]["title"].startswith("Automatic")

    status, created = call(server, "/api/cases", {"title": "Highlights", "request": REQUEST, "requester": "Marco"})
    assert status == 201 and created["status"] == "waiting_answers"
    cid = created["case_id"]

    assert call(server, f"/api/cases/{cid}/respond", {"answers": ""})[0] == 400
    assert call(server, f"/api/cases/{cid}/respond", {"answers": "Q-1: Premier League", "by": "Marco"})[0] == 200
    assert call(server, f"/api/cases/{cid}/respond", {"outcome": "send_back"})[0] == 400
    for _ in range(3):  # scope check, design sign-off, backlog release
        assert call(server, f"/api/cases/{cid}/respond", {"outcome": "approve", "by": "Marco"})[0] == 200
    status, view = call(server, f"/api/cases/{cid}")
    assert view["case"]["status"] == "done" and view["cost_usd"] == 0.0
    assert call(server, f"/api/cases/{cid}/respond", {"outcome": "approve"})[0] == 409

    status, doc = call(server, f"/api/cases/{cid}?doc=04-design.md")
    assert "MOCK DATA" in doc["content"]
    assert call(server, f"/api/cases/{cid}?doc=../secret")[0] == 400
    assert call(server, "/api/cases/C-nope")[0] == 404
    status, listing = call(server, "/api/cases")
    assert listing["counts"] == {"all": 1, "waiting": 0, "working": 0, "done": 1, "closed": 1}


def test_local_portal_reopens_a_rejected_case(server):
    _, created = call(server, "/api/cases", {"title": "Rail", "request": REQUEST})
    cid = created["case_id"]
    call(server, f"/api/cases/{cid}/respond", {"answers": "Q-1: UK only"})
    call(server, f"/api/cases/{cid}/respond", {"outcome": "reject", "comment": "Not now"})
    _, view = call(server, f"/api/cases/{cid}")
    assert view["can_reopen"] and view["reopen_stage"] == "analysis"
    assert call(server, f"/api/cases/{cid}/respond", {"outcome": "reopen"})[0] == 400
    status, res = call(server, f"/api/cases/{cid}/respond", {"outcome": "reopen", "comment": "Funded now", "by": "Sponsor"})
    assert status == 200 and res["stage"] == "analysis"
    _, view = call(server, f"/api/cases/{cid}")
    assert view["case"]["status"] == "waiting_gate" and not view["can_reopen"]
    assert view["activity"][0]["text"].startswith("Analysis lead") or any(
        "reopened the case" in a["text"] for a in view["activity"])


def test_local_portal_serves_files_safely(server):
    status, html = call(server, "/")
    assert status == 200 and b"Mock mode." in html and b'id="mock-banner"' in html
    assert call(server, "/app.js")[0] == 200
    req = urllib.request.Request(server + "/%2e%2e/pyproject.toml")
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req)
    assert err.value.code == 404


def test_portal_files_escape_everything():
    """Every value the portal shows from the API goes through esc() or textContent."""
    js = (REPO / "portal" / "app.js").read_text()
    assert "function esc(" in js and "innerHTML = '<p class=\"muted\">Loading" in js
    assert "eval(" not in js and "document.write" not in js


# ------------------------------------------------------------------ mock by default
def test_cli_is_mock_by_default(tmp_path, monkeypatch, capsys):
    from idea_agents import cli

    class NoLLM:
        def __init__(self, *a, **k):
            raise AssertionError("a local run must not create an LLM client")

    monkeypatch.setattr(cli, "ClaudeAgentClient", NoLLM)
    request = REPO / "examples" / "requests" / "highlights-clips.md"
    assert cli.main(["--cases-dir", str(tmp_path / "cases"), "new", str(request)]) == 0
    out = capsys.readouterr().out
    assert "MOCK MODE" in out and "waiting_answers" in out


def test_cli_live_needs_explicit_flag(tmp_path, monkeypatch):
    from idea_agents import cli
    made = []
    monkeypatch.setattr(cli, "ClaudeAgentClient", lambda: made.append(1) or object())
    cli.build_pipeline(type("A", (), {"catalogue": None, "cases_dir": str(tmp_path), "live": True,
                                      "keep_workspaces": False})())
    assert made == [1]


# ------------------------------------------------------------------ roles (read-only)
def test_roles_summary_matches_the_catalogue(catalogue):
    from idea_agents.roles import roles_summary
    data = roles_summary(catalogue)
    names = [r["name"] for r in data["roles"]]
    assert names == ["intake", "value", "analysis-lead", "designer", "reviewer-architecture",
                     "reviewer-security", "reviewer-operability", "chair", "story-writer"]
    for r in data["roles"]:
        role = catalogue.role(r["name"])
        assert (r["budget_usd"], r["max_turns"], r["model"], r["description"]) == \
            (role.budget_usd, role.max_turns, role.model, role.description)
        assert r["instructions"] == role.prompt
    lead = next(r for r in data["roles"] if r["name"] == "analysis-lead")
    assert [s["name"] for s in lead["specialists"]] == ["data", "security", "operations", "commercial"]
    assert data["limits"]["case_budget_usd"] == catalogue.pipeline.limits.case_budget_usd
    assert "House rules" in data["house_rules"]


def test_local_portal_serves_roles(server):
    status, data = call(server, "/api/roles")
    assert status == 200 and len(data["roles"]) == 9 and data["roles"][0]["label"] == "Intake agent"


def test_spend_by_role(store, catalogue):
    case = store.create_case("t", "x")
    for role, cost, budget, hit in (("designer", 2.5, 8.0, False), ("designer", 3.0, 8.0, True),
                                    ("chair", 0.4, 3.0, False)):
        store.add_run(case["id"], {"role": role, "cost_usd": cost, "budget_usd": budget, "limit_hit": hit,
                                   "started_at": "2026-11-23T10:00:00.000+00:00"})
    view = api.detail_view(store, case["id"], pipeline_summary(catalogue.pipeline))
    assert view["spend_by_role"] == [
        {"role": "designer", "label": "Designer", "runs": 2, "cost_usd": 5.5, "budget_usd": 8.0, "limit_hit": True},
        {"role": "chair", "label": "Review chair", "runs": 1, "cost_usd": 0.4, "budget_usd": 3.0, "limit_hit": False},
    ]
