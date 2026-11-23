"""AWS side: the generated state machine, the runner entrypoint, the Lambdas and the S3/DynamoDB store."""

import json

import pytest

from idea_agents.aws.asl import build_definition
from idea_agents.fake import ScriptedAgentClient
from idea_agents.store import LocalCaseStore


# ------------------------------------------------------------------ state machine
def targets(state: dict) -> list[str]:
    out = [state[k] for k in ("Next", "Default") if k in state]
    out += [c["Next"] for c in state.get("Choices", [])]
    out += [c["Next"] for c in state.get("Catch", [])]
    return out


def check_states(states: dict, start: str) -> None:
    assert start in states
    for name, state in states.items():
        terminal = state["Type"] in ("Succeed", "Fail") or state.get("End")
        assert terminal or "Next" in state or state["Type"] == "Choice", f"{name} goes nowhere"
        for t in targets(state):
            assert t in states, f"{name} points at missing state {t}"
        for branch in state.get("Branches", []):
            check_states(branch["States"], branch["StartAt"])
    seen, todo = set(), [start]
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo += targets(states[name])
    assert seen == set(states), f"unreachable: {set(states) - seen}"


def test_state_machine_is_well_formed(catalogue):
    d = build_definition(catalogue.pipeline)
    check_states(d["States"], d["StartAt"])
    assert d["StartAt"] == "Start" and d["States"]["Start"]["Default"] == "intake.Start"


def test_reopened_cases_resume_at_their_stage(catalogue):
    states = build_definition(catalogue.pipeline)["States"]
    targets = {c["And"][1]["StringEquals"]: c["Next"] for c in states["Start"]["Choices"]}
    assert targets == {s.id: f"{s.id}.Resume" for s in catalogue.pipeline.stages}
    assert states["analysis.Resume"]["Next"] == "analysis.Run"
    assert states["design.Resume"]["Next"] == "design.Designer"
    assert states["design.Resume"]["Parameters"]["round.$"] == "$.round"


def test_disabled_gate_is_left_out(catalogue):
    states = build_definition(catalogue.pipeline)["States"]
    assert "value.Gate" not in states
    assert states["value.Blocked?"]["Default"] == "analysis.Start"
    assert states["analysis.Blocked?"]["Default"] == "analysis.Gate"


def test_reviewers_run_in_parallel_and_route_is_code(catalogue):
    states = build_definition(catalogue.pipeline)["States"]
    par = states["design.Reviewers"]
    assert par["Type"] == "Parallel" and len(par["Branches"]) == 3
    route = states["design.Route"]
    assert {c["StringEquals"] for c in route["Choices"]} == {"approved", "revise", "blocked", "escalate"}
    chair_env = states["design.Chair"]["Parameters"]["Overrides"]["ContainerOverrides"][0]["Environment"]
    assert {"Name": "IS_CHAIR", "Value": "1"} in chair_env


def test_retries_cover_temporary_errors_only(catalogue):
    states = build_definition(catalogue.pipeline)["States"]
    retried = {e for r in states["intake.Run"]["Retry"] for e in r["ErrorEquals"]}
    assert "RunnerError" in retried and "ECS.AmazonECSException" in retried
    assert not retried & {"BudgetExceeded", "AgentLimitReached", "AgentOutputError", "States.ALL", "States.Timeout"}
    branch = states["design.Reviewers"]["Branches"][0]
    assert branch["States"][branch["StartAt"]]["Retry"]
    for name in ("intake.WaitAnswers", "PublishBacklog", "Failed"):
        assert states[name]["Retry"][0]["ErrorEquals"][0].startswith("Lambda.")


def test_agent_tasks_wait_for_a_token(catalogue):
    states = build_definition(catalogue.pipeline)["States"]
    for name in ("intake.Run", "design.Designer", "design.Chair", "stories.Run"):
        assert states[name]["Resource"].endswith("runTask.waitForTaskToken")
        assert states[name]["Parameters"]["NetworkConfiguration"]["AwsvpcConfiguration"]["AssignPublicIp"] == "DISABLED"
    for name in ("intake.WaitAnswers", "design.Gate", "design.Escalation"):
        assert states[name]["Resource"].endswith("lambda:invoke.waitForTaskToken")
        assert states[name]["Catch"][0]["ErrorEquals"] == ["States.Timeout"]


# ------------------------------------------------------------------ runner entrypoint
class FakeSfn:
    def __init__(self):
        self.success, self.failure = [], []

    def send_task_success(self, taskToken, output):
        self.success.append((taskToken, json.loads(output)))

    def send_task_failure(self, taskToken, error, cause):
        self.failure.append((taskToken, error, cause))


def test_task_entry_reports_success(tmp_path, monkeypatch, catalogue):
    from idea_agents.aws import task_entry
    monkeypatch.setenv("IDEA_CATALOGUE", str(catalogue.root))
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    store = LocalCaseStore(tmp_path / "cases")
    case = store.create_case("t", "request")
    sfn = FakeSfn()
    env = {"TASK_TOKEN": "tok", "CASE_ID": case["id"], "STAGE": "intake", "ROLE": "intake",
           "ROUND": "1", "ROUND_LIMIT": "3"}
    assert task_entry.run_from_env(env, store=store, client=ScriptedAgentClient(), sfn=sfn) == 0
    token, out = sfn.success[0]
    assert token == "tok" and out["status"] == "blocked" and out["blockers"] == 1
    assert store.get_case(case["id"])["status"] == "running"


def test_task_entry_reports_failure(tmp_path, monkeypatch, catalogue):
    from idea_agents.aws import task_entry
    monkeypatch.setenv("IDEA_CATALOGUE", str(catalogue.root))
    store = LocalCaseStore(tmp_path / "cases")
    sfn = FakeSfn()
    env = {"TASK_TOKEN": "tok", "CASE_ID": "C-missing", "STAGE": "intake", "ROLE": "intake"}
    assert task_entry.run_from_env(env, store=store, client=ScriptedAgentClient(), sfn=sfn) == 1
    assert sfn.failure[0][1] == "RunnerError"


def test_chair_output_carries_the_route(tmp_path, monkeypatch, catalogue):
    from idea_agents.aws import task_entry
    monkeypatch.setenv("IDEA_CATALOGUE", str(catalogue.root))
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    store = LocalCaseStore(tmp_path / "cases")
    case = store.create_case("t", "request")
    store.put_finding(case["id"], {"id": "SEC-1", "reviewer": "reviewer-security", "severity": "major",
                                   "status": "open", "section": "s", "issue": "i", "evidence": "e",
                                   "suggested_fix": "f"})
    sfn = FakeSfn()
    env = {"TASK_TOKEN": "tok", "CASE_ID": case["id"], "STAGE": "design", "ROLE": "chair",
           "ROUND": "3", "ROUND_LIMIT": "3", "IS_CHAIR": "1"}
    task_entry.run_from_env(env, store=store, client=ScriptedAgentClient(), sfn=sfn)
    assert sfn.success[0][1]["route"] == "escalate"


# ------------------------------------------------------------------ lambdas
@pytest.fixture
def lambda_env(tmp_path, monkeypatch):
    from idea_agents.aws.lambdas import common
    store = LocalCaseStore(tmp_path / "cases")
    monkeypatch.setattr(common, "_store", store)
    sent = FakeSfn()

    class FakeBoto:
        def client(self, name, *a, **k):
            assert name == "stepfunctions"
            return self

        def start_execution(self, **kw):
            sent.success.append(("start", kw))

        def send_task_success(self, taskToken, output):
            sent.success.append((taskToken, json.loads(output)))

    for mod in ("start_case", "respond"):
        monkeypatch.setattr(f"idea_agents.aws.lambdas.{mod}.boto3", FakeBoto())
    monkeypatch.setenv("MAX_REVIEW_ROUNDS", "3")
    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:sm")
    from idea_agents.config import pipeline_summary
    from idea_agents.catalogue import Catalogue
    from conftest import REPO
    monkeypatch.setenv("PIPELINE_JSON", json.dumps(pipeline_summary(Catalogue(REPO / "catalogue").pipeline)))
    return store, sent


def api(body=None, case_id=None, query=None):
    return {"body": json.dumps(body or {}), "pathParameters": {"case_id": case_id} if case_id else None,
            "queryStringParameters": query,
            "requestContext": {"authorizer": {"claims": {"email": "marco@example.com", "name": "Marco G"}}}}


def test_start_wait_answer_and_decide(lambda_env):
    from idea_agents.aws.lambdas import get_case, respond, start_case, wait_for_people
    store, sent = lambda_env

    resp = start_case.handler(api({"title": "Highlights", "request": "Clips please"}), None)
    assert resp["statusCode"] == 201
    cid = json.loads(resp["body"])["case_id"]
    assert sent.success[0][1]["name"] == cid
    assert start_case.handler(api({"title": "x"}), None)["statusCode"] == 400

    store.put_blocker(cid, {"id": "Q-1", "stage": "intake", "for": "requester", "question": "Which leagues?",
                            "why_it_matters": "rights", "status": "open"})
    wait_for_people.handler({"case_id": cid, "kind": "answers", "stage": "intake", "task_token": "T1"}, None)
    case = store.get_case(cid)
    assert case["status"] == "waiting_answers" and case["pending"]["task_token"] == "T1"

    shown = json.loads(get_case.handler(api(case_id=cid), None)["body"])
    assert "task_token" not in (shown["case"]["pending"] or {})
    assert shown["open_questions"][0]["id"] == "Q-1"
    assert shown["waiting_for"] == "Requester: 1 question"
    assert shown["progress"][0] == {"kind": "stage", "id": "intake", "label": "Intake", "state": "waiting",
                                    "note": "Needs answers"}
    assert json.loads(get_case.handler(api(case_id=cid, query={"doc": "request.md"}), None)["body"])["content"] \
        == "Clips please"
    assert get_case.handler(api(case_id=cid, query={"doc": "../x"}), None)["statusCode"] == 400

    from idea_agents.aws.lambdas import list_cases
    listed = json.loads(list_cases.handler({}, None)["body"])
    assert listed["counts"]["waiting"] == 1 and listed["cases"][0]["id"] == cid

    resp = respond.handler(api({"answers": "Premier League"}, cid), None)
    assert resp["statusCode"] == 200
    assert sent.success[-1] == ("T1", {"outcome": "answered"})
    assert store.open_blockers(cid) == []
    assert "Premier League" in store.read_doc(cid, "answers/01-intake.md")
    assert respond.handler(api({"answers": "again"}, cid), None)["statusCode"] == 409

    wait_for_people.handler({"case_id": cid, "kind": "gate", "stage": "design", "gate": "design_signoff",
                             "approver": "lead_architect", "task_token": "T2"}, None)
    assert respond.handler(api({"outcome": "send_back"}, cid), None)["statusCode"] == 400
    assert respond.handler(api({"outcome": "maybe"}, cid), None)["statusCode"] == 400
    resp = respond.handler(api({"outcome": "approve", "comment": "Good"}, cid), None)
    assert resp["statusCode"] == 200 and sent.success[-1] == ("T2", {"outcome": "approve"})
    assert store.list_decisions(cid)[0]["by"] == "Marco G"
    assert store.read_doc(cid, "answers/01-intake.md").startswith("# Answers from Marco G")


def test_publish_backlog_dry_run(lambda_env):
    from idea_agents.aws.lambdas import publish_backlog
    store, _ = lambda_env
    case = store.create_case("t", "x")
    backlog = {"epics": [{"id": "E1", "title": "T", "description": "D", "stories": [
        {"id": "E1-S1", "title": "S", "as_a": "a", "i_want": "b", "so_that": "c",
         "acceptance_criteria": ["x"]}]}]}
    store.write_doc(case["id"], "05-backlog.json", json.dumps(backlog))
    assert publish_backlog.handler({"case_id": case["id"]}, None) == {"issues": 2}
    published = json.loads(store.read_doc(case["id"], "06-published.json"))
    assert [i["key"] for i in published["issues"]] == ["DRY-E1", "DRY-E1-S1"]
    assert store.get_case(case["id"])["status"] == "done"


def test_jira_story_fields():
    from idea_agents.aws.lambdas.publish_backlog import issue_fields
    story = {"id": "E1-S1", "title": "S", "as_a": "editor", "i_want": "x", "so_that": "y",
             "acceptance_criteria": ["one", "two"], "design_refs": ["ADR-1"]}
    fields = issue_fields("ABC", "Story", story, parent_key="ABC-1")
    assert fields["parent"] == {"key": "ABC-1"} and "* two" in fields["description"]


def test_finish_case_records_outcome(lambda_env):
    from idea_agents.aws.lambdas import finish_case
    store, _ = lambda_env
    case = store.create_case("t", "x")
    finish_case.handler({"case_id": case["id"], "outcome": "failed",
                         "error": {"error": {"Error": "RunnerError", "Cause": "boom"}}}, None)
    saved = store.get_case(case["id"])
    assert saved["status"] == "failed" and "RunnerError" in saved["error"]


# ------------------------------------------------------------------ S3 + DynamoDB store
def test_aws_store_round_trip(monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with moto.mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="cases", CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
        s3.put_bucket_versioning(Bucket="cases", VersioningConfiguration={"Status": "Enabled"})
        ddb = boto3.resource("dynamodb")
        ddb.create_table(TableName="records", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                                    {"AttributeName": "sk", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                               {"AttributeName": "sk", "AttributeType": "S"}])
        from idea_agents.aws.store import AwsCaseStore
        store = AwsCaseStore("cases", "records", s3=s3, dynamodb=ddb)

        case = store.create_case("Highlights", "Clips please", "marco")
        cid = case["id"]
        assert store.get_case(cid)["title"] == "Highlights"
        assert store.read_doc(cid, "request.md") == "Clips please"
        assert store.read_doc(cid, "missing.md") is None
        store.write_doc(cid, "answers/01-intake.md", "PL only")
        store.write_doc(cid, "request.md", "v2")
        versions = s3.list_object_versions(Bucket="cases", Prefix=f"cases/{cid}/request.md")["Versions"]
        assert len(versions) == 2
        assert store.list_docs(cid, "answers/") == ["answers/01-intake.md"]

        store.add_run(cid, {"started_at": "2026-11-23T10:00:00+00:00", "role": "intake", "cost_usd": 0.4})
        store.add_run(cid, {"started_at": "2026-11-23T10:05:00+00:00", "role": "value", "cost_usd": 0.6})
        assert store.total_cost(cid) == 1.0
        store.put_finding(cid, {"id": "SEC-1", "reviewer": "reviewer-security", "status": "open", "severity": "major"})
        assert [f["id"] for f in store.open_findings(cid)] == ["SEC-1"]
        store.put_blocker(cid, {"id": "Q-1", "status": "open"})
        store.add_decision(cid, {"at": "2026-11-23T11:00:00+00:00", "outcome": "approve"})
        assert len(store.open_blockers(cid)) == 1 and len(store.list_decisions(cid)) == 1
        assert [c["id"] for c in store.list_cases()] == [cid]
        store.save_doc(cid, "01-request-brief.md", "brief", by="intake", kind="agent", stage="intake")
        versions = store.list_doc_versions(cid)
        assert [(v["name"], v["kind"]) for v in versions] == [("request.md", "person"), ("01-request-brief.md", "agent")]
        with pytest.raises(KeyError):
            store.get_case("C-nope")

        # Only the first of two simultaneous responses may take the task token.
        case = store.get_case(cid)
        case.update(status="waiting_gate", pending={"kind": "gate", "task_token": "TOKEN-1"})
        store.put_case(case)
        assert store.claim_pending(cid, "TOKEN-1") is True
        assert store.claim_pending(cid, "TOKEN-1") is False

        # Only one of two simultaneous reopens may win.
        case.update(status="rejected", pending=None)
        store.put_case(case)
        assert store.transition_status(cid, "rejected", "running") is True
        assert store.transition_status(cid, "rejected", "running") is False


def test_reopen_through_the_api(lambda_env):
    from idea_agents.aws.lambdas import get_case, respond
    store, sent = lambda_env
    case = store.create_case("t", "x")
    case.update(status="rejected", stage="analysis", round=1)
    store.put_case(case)
    cid = case["id"]
    shown = json.loads(get_case.handler(api(case_id=cid), None)["body"])
    assert shown["can_reopen"] and shown["reopen_stage"] == "analysis"

    assert respond.handler(api({"outcome": "approve"}, cid), None)["statusCode"] == 400
    assert respond.handler(api({"outcome": "reopen"}, cid), None)["statusCode"] == 400
    resp = respond.handler(api({"outcome": "reopen", "comment": "Budget agreed"}, cid), None)
    assert resp["statusCode"] == 200
    _, started = sent.success[-1]
    assert started["name"] == f"{cid}-reopen-1"
    assert json.loads(started["input"]) == {"case_id": cid, "resume_stage": "analysis", "round": 2, "round_limit": 3}
    saved = store.get_case(cid)
    assert saved["status"] == "running" and saved["round"] == 2
    assert store.list_decisions(cid)[-1]["by"] == "Marco G"
    again = respond.handler(api({"outcome": "reopen", "comment": "again"}, cid), None)
    assert again["statusCode"] == 400 and "only a rejected case" in again["body"]


def test_list_roles_lambda_reads_the_published_summary(monkeypatch):
    from idea_agents.aws.lambdas import list_roles

    class Body:
        def read(self):
            return b'{"roles": [{"name": "intake"}], "limits": {}}'

    class FakeS3:
        def get_object(self, Bucket, Key):
            assert (Bucket, Key) == ("cases-bucket", "catalogue/roles.json")
            return {"Body": Body()}

    monkeypatch.setattr(list_roles, "_cache", None)
    monkeypatch.setattr(list_roles.boto3, "client", lambda name: FakeS3())
    monkeypatch.setenv("CASES_BUCKET", "cases-bucket")
    resp = list_roles.handler({}, None)
    assert resp["statusCode"] == 200 and json.loads(resp["body"])["roles"][0]["name"] == "intake"
