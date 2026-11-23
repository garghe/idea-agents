"""End-to-end runs of the local pipeline with scripted agents."""

import json

import pytest

from idea_agents.agent_client import AgentResult
from idea_agents.local_pipeline import PipelineError
from idea_agents.runner import AgentOutputError, StageRunner
from conftest import REQUEST


def test_happy_path_with_blocker_review_round_and_gates(pipeline_factory, store):
    pipe, client = pipeline_factory()
    case = pipe.start("Highlights", REQUEST, "Head of Digital Sport")
    cid = case["id"]

    # Intake asks a question; nothing else runs.
    assert case["status"] == "waiting_answers" and case["stage"] == "intake"
    assert [b["question"] for b in store.open_blockers(cid)] == \
        ["Which competitions, channels or regions are in scope at launch?"]

    # Answers: intake reruns, value runs with no gate (go/no-go is off), analysis stops at its gate.
    case = pipe.answer(cid, "Premier League only", by="requester")
    assert case["status"] == "waiting_gate" and case["pending"]["gate"] == "scope_check"
    assert store.read_doc(cid, "answers/01-intake.md").count("Premier League only") == 1

    # Scope approved: design round 1 raises SEC-1, round 2 closes it, sign-off gate.
    case = pipe.decide(cid, "approve", by="PO")
    assert case["status"] == "waiting_gate" and case["pending"]["gate"] == "design_signoff"
    sec = {x["id"]: x for x in store.list_findings(cid)}["SEC-1"]
    assert sec["status"] == "closed" and sec["designer_response"] == "fixed" and sec["closed_round"] == 2

    case = pipe.decide(cid, "approve", by="Lead architect")
    assert case["pending"]["gate"] == "backlog_release"
    case = pipe.decide(cid, "approve", by="PO")
    assert case["status"] == "done"

    backlog = json.loads(store.read_doc(cid, "05-backlog.json"))
    assert backlog["epics"][0]["stories"][0]["id"] == "E1-S1"
    for doc in ("01-request-brief.md", "02-business-case.md", "03-requirements.md", "04-design.md"):
        assert "MOCK DATA" in store.read_doc(cid, doc)
    assert "Premier League only" in store.read_doc(cid, "01-request-brief.md")
    assert client.calls.count("designer") == 2
    assert client.calls.count("chair") == 2
    assert [d["gate"] for d in store.list_decisions(cid)] == ["scope_check", "design_signoff", "backlog_release"]


def test_reviewers_cannot_see_each_others_findings(pipeline_factory, store, catalogue, tmp_path):
    """In round 2 each reviewer gets only its own open findings."""
    pipe, _ = pipeline_factory(intake_blocks=False, conflict=False)
    seen = {}
    original = pipe.runner._prepare_workspace

    def spy(ws, case_id, role, round_no):
        result = original(ws, case_id, role, round_no)
        path = ws / "inputs" / "open-findings.json"
        if role.is_reviewer and round_no == 2:
            seen[role.name] = json.loads(path.read_text()) if path.exists() else []
        return result

    pipe.runner._prepare_workspace = spy
    case = pipe.start("Highlights", REQUEST)
    pipe.decide(case["id"], "approve")
    assert [x["id"] for x in seen["reviewer-security"]] == ["SEC-1"]
    assert seen["reviewer-architecture"] == [] and seen["reviewer-operability"] == []


def test_conflict_escalates_and_send_back_reopens_review(pipeline_factory, store):
    pipe, client = pipeline_factory(intake_blocks=False, conflict=True)
    case = pipe.start("Highlights", REQUEST)
    case = pipe.decide(case["id"], "approve")  # scope check
    assert case["status"] == "waiting_escalation"
    assert set(case["pending"]["open_findings"]) == {"SEC-1", "OPS-1"}

    case = pipe.decide(case["id"], "send_back", by="Lead architect", comment="Share the existing proxy.")
    assert case["round_limit"] == 2 + 3 - 1
    assert case["status"] == "waiting_gate" and case["pending"]["gate"] == "design_signoff"
    assert "Share the existing proxy." in store.read_doc(case["id"], "answers/01-design-feedback.md")


def test_escalation_approval_skips_signoff_gate(pipeline_factory):
    pipe, _ = pipeline_factory(intake_blocks=False, conflict=True)
    case = pipe.start("Highlights", REQUEST)
    case = pipe.decide(case["id"], "approve")
    assert case["status"] == "waiting_escalation"
    case = pipe.decide(case["id"], "approve", by="Lead architect")
    assert case["stage"] == "stories" and case["pending"]["gate"] == "backlog_release"


def test_chair_business_question_waits_then_designer_revises(pipeline_factory, store):
    pipe, client = pipeline_factory(intake_blocks=False, chair_question=True)
    case = pipe.start("Highlights", REQUEST)
    case = pipe.decide(case["id"], "approve")
    assert case["status"] == "waiting_answers" and case["stage"] == "design" and case["round"] == 2
    assert store.open_blockers(case["id"])[0]["for"] == "security"
    case = pipe.answer(case["id"], "Yes, mandatory for everyone.")
    assert case["pending"]["gate"] == "design_signoff"


def test_reject_ends_the_case(pipeline_factory):
    pipe, client = pipeline_factory(intake_blocks=False)
    case = pipe.start("Highlights", REQUEST)
    case = pipe.decide(case["id"], "reject", by="PO", comment="Not this season.")
    assert case["status"] == "rejected"
    assert "designer" not in client.calls


def test_wrong_actions_are_refused(pipeline_factory):
    pipe, _ = pipeline_factory()
    case = pipe.start("Highlights", REQUEST)
    with pytest.raises(PipelineError):
        pipe.decide(case["id"], "approve")
    with pytest.raises(PipelineError):
        pipe.decide(case["id"], "maybe")


def test_gate_enabled_in_config_stops_after_value(catalogue, store, tmp_path):
    from dataclasses import replace
    from conftest import make_pipeline
    gates = dict(catalogue.pipeline.gates)
    gates["go_no_go"] = replace(gates["go_no_go"], enabled=True)
    catalogue.pipeline = replace(catalogue.pipeline, gates=gates)
    pipe, _ = make_pipeline(catalogue, store, tmp_path, intake_blocks=False)
    case = pipe.start("Highlights", REQUEST)
    assert case["pending"]["gate"] == "go_no_go"


class BadThenGoodClient:
    """Returns an invalid envelope first, then a valid one: the runner must retry once."""

    def __init__(self):
        self.prompts = []

    def run(self, role, system_prompt, task, workspace, schema, resume=None, budget_usd=None):
        self.prompts.append((task, resume))
        if len(self.prompts) == 1:
            return AgentResult({"status": "complete"}, "", 0.5, 3, "sess-1", "sonnet")
        (workspace / "output" / "01-request-brief.md").write_text("# Brief\n")
        return AgentResult({"status": "complete", "summary": "ok", "document": "01-request-brief.md"},
                           "", 0.25, 2, "sess-1", "sonnet")


def test_runner_retries_with_the_validation_errors(catalogue, store, tmp_path):
    client = BadThenGoodClient()
    runner = StageRunner(catalogue, store, client, work_root=tmp_path)
    case = store.create_case("t", "request")
    env = runner.run_role(case["id"], "intake", "intake")
    assert env["status"] == "complete"
    assert client.prompts[1][1] == "sess-1" and "could not be accepted" in client.prompts[1][0]
    runs = store.list_runs(case["id"])
    assert [r["status"] for r in runs] == ["invalid", "complete"]
    assert store.total_cost(case["id"]) == 0.75


def test_runner_gives_up_after_retries(catalogue, store, tmp_path):
    class AlwaysBad:
        def run(self, *a, **k):
            return AgentResult(None, "sorry", 0.1, 1, "s", "sonnet")

    runner = StageRunner(catalogue, store, AlwaysBad(), work_root=tmp_path)
    case = store.create_case("t", "request")
    with pytest.raises(AgentOutputError):
        runner.run_role(case["id"], "intake", "intake")


def test_budget_stops_the_case(pipeline_factory, store):
    pipe, _ = pipeline_factory()
    case = store.create_case("t", "request")
    store.add_run(case["id"], {"cost_usd": 1000.0})
    case = pipe.run(case["id"])
    assert case["status"] == "failed" and "budget" in case["error"]


def test_document_versions_are_kept(store):
    case = store.create_case("t", "v1")
    store.write_doc(case["id"], "request.md", "v2")
    assert store.read_doc(case["id"], "request.md") == "v2"
    history = store.root / case["id"] / "history" / "request.md.v1"
    assert history.read_text() == "v1"


def test_document_names_are_checked(store):
    case = store.create_case("t", "x")
    for bad in ("../x.md", "/etc/passwd", "a/b/c.md", ""):
        with pytest.raises(ValueError):
            store.write_doc(case["id"], bad, "x")


def test_a_run_that_hits_its_cap_is_not_retried(catalogue, store, tmp_path):
    from idea_agents.runner import AgentLimitError

    class OverBudget:
        calls = 0

        def run(self, role, system_prompt, task, workspace, schema, resume=None, budget_usd=None):
            OverBudget.calls += 1
            return AgentResult(None, "", budget_usd, 9, "s", "opus", error="max budget", limit_hit="budget")

    runner = StageRunner(catalogue, store, OverBudget(), work_root=tmp_path)
    case = store.create_case("t", "request")
    with pytest.raises(AgentLimitError):
        runner.run_role(case["id"], "intake", "intake")
    assert OverBudget.calls == 1
    assert store.list_runs(case["id"])[0]["limit_hit"] == "budget"


def test_each_run_is_capped_at_what_is_left_of_the_case_budget(catalogue, store, tmp_path):
    seen = []

    class Recorder:
        def run(self, role, system_prompt, task, workspace, schema, resume=None, budget_usd=None):
            seen.append(budget_usd)
            (workspace / "output" / "02-business-case.md").write_text("# Case\n")
            return AgentResult({"status": "complete", "summary": "ok", "document": "02-business-case.md"},
                               "", 0.5, 1, "s", "opus")

    runner = StageRunner(catalogue, store, Recorder(), work_root=tmp_path)
    case = store.create_case("t", "request")
    limit = catalogue.pipeline.limits.case_budget_usd
    store.add_run(case["id"], {"cost_usd": limit - 1.5})  # $1.50 left; the value role allows $4
    runner.run_role(case["id"], "value", "value")
    assert seen == [1.5]


def test_reopen_a_rejected_case(pipeline_factory, store):
    pipe, client = pipeline_factory(intake_blocks=False)
    cid = pipe.start("Highlights", REQUEST)["id"]
    case = pipe.decide(cid, "reject", by="PO", comment="No budget this year")
    assert case["status"] == "rejected"

    with pytest.raises(PipelineError, match="comment is required"):
        pipe.reopen(cid, by="Sponsor", comment="  ")
    calls_before = client.calls.count("analysis-lead")
    case = pipe.reopen(cid, by="Sponsor", comment="Budget now agreed for Q2")
    # Rejected at the scope check, so analysis runs again and the scope check is asked again.
    assert client.calls.count("analysis-lead") == calls_before + 1
    assert case["status"] == "waiting_gate" and case["pending"]["gate"] == "scope_check" and case["round"] == 2
    assert "Budget now agreed" in store.read_doc(cid, "answers/01-analysis-reopen.md")
    assert [d["outcome"] for d in store.list_decisions(cid)] == ["reject", "reopen"]

    with pytest.raises(PipelineError, match="only a rejected case can be reopened"):
        pipe.reopen(cid, by="Sponsor", comment="again")
    case = pipe.decide(cid, "approve", by="PO")
    assert case["pending"]["gate"] == "design_signoff"


def test_reopen_a_rejected_design_gives_fresh_review_rounds(pipeline_factory, store):
    pipe, client = pipeline_factory(intake_blocks=False, conflict=True)
    cid = pipe.start("Highlights", REQUEST)["id"]
    pipe.decide(cid, "approve")  # scope check -> design escalates (conflict)
    case = pipe.decide(cid, "reject", by="Lead architect", comment="Too costly")
    assert case["status"] == "rejected" and case["stage"] == "design"
    case = pipe.reopen(cid, by="Sponsor", comment="Use the shared proxy")
    assert case["round_limit"] == 2 + 3 - 1
    assert case["pending"]["gate"] == "design_signoff"
