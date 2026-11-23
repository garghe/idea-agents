from pathlib import Path

from agentpipe import review
from agentpipe.agent_client import check_path, parse_envelope_text
from agentpipe.schemas import backlog_errors, envelope_errors


# ------------------------------------------------------------------ review routing
def f(severity, fid="SEC-1"):
    return {"id": fid, "severity": severity, "status": "open"}


def test_route_approves_when_only_minors_remain():
    assert review.route([f("minor")], {"status": "complete", "chair": {}}, 1, 3) == review.APPROVED
    assert review.route([], {"status": "complete", "chair": {}}, 1, 3) == review.APPROVED


def test_route_revises_while_rounds_remain():
    assert review.route([f("major")], {"status": "complete", "chair": {}}, 1, 3) == review.REVISE
    assert review.route([f("blocker")], {"status": "complete", "chair": {}}, 2, 3) == review.REVISE


def test_route_escalates_at_round_limit_or_on_conflict():
    assert review.route([f("major")], {"status": "complete", "chair": {}}, 3, 3) == review.ESCALATE
    chair = {"status": "complete", "chair": {"conflicts": [{"finding_ids": ["A", "B"], "description": "x"}]}}
    assert review.route([f("major")], chair, 1, 3) == review.ESCALATE


def test_business_questions_come_first():
    assert review.route([], {"status": "blocked", "chair": None}, 3, 3) == review.BLOCKED


def test_finding_ids():
    assert review.next_finding_id("SEC", []) == "SEC-1"
    assert review.next_finding_id("SEC", ["SEC-1", "SEC-7", "ARC-9"]) == "SEC-8"


# ------------------------------------------------------------------ envelope rules
def env(**kw):
    return {"status": "complete", "summary": "ok", **kw}


def test_writer_must_name_its_document(catalogue):
    role = catalogue.role("intake")
    schema = catalogue.envelope_schema
    assert envelope_errors(env(document="01-request-brief.md"), schema, role) == []
    assert envelope_errors(env(document="other.md"), schema, role)
    assert envelope_errors(env(), schema, role)


def test_blocked_needs_questions(catalogue):
    role = catalogue.role("intake")
    errs = envelope_errors(env(status="blocked", document="01-request-brief.md"), catalogue.envelope_schema, role)
    assert any("no blockers" in e for e in errs)


def test_only_reviewers_raise_findings(catalogue):
    finding = {"severity": "major", "section": "s", "issue": "i", "evidence": "e", "suggested_fix": "x"}
    designer = catalogue.role("designer")
    errs = envelope_errors(env(document="04-design.md", findings=[finding]), catalogue.envelope_schema, designer)
    assert any("only reviewers" in e for e in errs)
    reviewer = catalogue.role("reviewer-security")
    assert envelope_errors(env(document=None, findings=[finding]), catalogue.envelope_schema, reviewer) \
        == ["reviewers must give a verdict"]


def test_finding_needs_evidence_field(catalogue):
    reviewer = catalogue.role("reviewer-security")
    bad = {"severity": "major", "section": "s", "issue": "i", "suggested_fix": "x"}
    errs = envelope_errors(env(verdict="approve", findings=[bad]), catalogue.envelope_schema, reviewer)
    assert any("evidence" in e for e in errs)


def test_designer_must_answer_every_open_finding(catalogue):
    designer = catalogue.role("designer")
    e = env(document="04-design.md",
            responses=[{"finding_id": "SEC-1", "response": "fixed", "note": "done"}])
    assert envelope_errors(e, catalogue.envelope_schema, designer, designer_open_ids=["SEC-1"]) == []
    errs = envelope_errors(e, catalogue.envelope_schema, designer, designer_open_ids=["SEC-1", "OPS-1"])
    assert any("OPS-1" in x for x in errs)


def test_chair_must_return_chair_object(catalogue):
    chair = catalogue.role("chair")
    assert envelope_errors(env(), catalogue.envelope_schema, chair, chair=True)
    assert envelope_errors(env(chair={"duplicates": [], "conflicts": []}), catalogue.envelope_schema,
                           chair, chair=True) == []


def test_backlog_rules(catalogue):
    good = {"epics": [{"id": "E1", "title": "t", "description": "d", "stories": [
        {"id": "E1-S1", "title": "t", "as_a": "a", "i_want": "b", "so_that": "c",
         "acceptance_criteria": ["x"]}]}]}
    assert backlog_errors(good, catalogue.backlog_schema) == []
    bad = {"epics": [{"id": "E1", "title": "t", "description": "d", "stories": [
        {"id": "E2-S1", "title": "t", "as_a": "a", "i_want": "b", "so_that": "c",
         "acceptance_criteria": ["x"], "depends_on": ["E9-S9"]}]}]}
    errs = backlog_errors(bad, catalogue.backlog_schema)
    assert any("not numbered" in e for e in errs) and any("unknown story" in e for e in errs)


# ------------------------------------------------------------------ path guard
def test_path_guard(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "output").mkdir(parents=True)
    assert check_path(ws, "Read", {"file_path": "inputs/request.md"}) is None
    assert check_path(ws, "Read", {"file_path": str(ws / "standards" / "a.md")}) is None
    assert check_path(ws, "Read", {"file_path": "/etc/passwd"})
    assert check_path(ws, "Read", {"file_path": "../secrets.txt"})
    assert check_path(ws, "Write", {"file_path": "output/04-design.md"}) is None
    assert check_path(ws, "Write", {"file_path": "inputs/request.md"})
    assert check_path(ws, "Edit", {"file_path": "output/../inputs/x.md"})
    assert check_path(ws, "Grep", {"pattern": "x"}) is None
    assert check_path(ws, "Glob", {"pattern": "*", "path": "/"})


def test_envelope_text_fallback():
    assert parse_envelope_text('Done.\n```json\n{"status": "complete", "summary": "x"}\n```') == \
        {"status": "complete", "summary": "x"}
    assert parse_envelope_text('{"status": "blocked"}') == {"status": "blocked"}
    assert parse_envelope_text("no json here") is None
