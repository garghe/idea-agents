"""Builds the real Agent SDK options and synthesises and lints the CDK stack, without calling Claude or AWS."""

import asyncio
import json
import shutil
import subprocess

import pytest

from conftest import REPO


def test_agent_sdk_options_are_locked_down(catalogue, tmp_path):
    pytest.importorskip("claude_agent_sdk")
    from idea_agents.agent_client import ClaudeAgentClient

    ws = tmp_path / "ws"
    (ws / "output").mkdir(parents=True)
    client = ClaudeAgentClient(use_bedrock=True)
    role = catalogue.role("analysis-lead")
    opts = client._options(role, "prompt", ws, catalogue.envelope_schema, None)
    assert opts.tools == opts.allowed_tools == list(role.tools)
    assert opts.permission_mode == "dontAsk" and opts.setting_sources == []
    assert opts.max_budget_usd == role.budget_usd and opts.cwd == str(ws)
    assert opts.env == {"CLAUDE_CODE_USE_BEDROCK": "1"}
    assert opts.output_format["type"] == "json_schema"
    assert set(opts.agents) == {"data", "security", "operations", "commercial"}
    assert all(a.background is False for a in opts.agents.values())

    guard = opts.hooks["PreToolUse"][0].hooks[0]
    denied = asyncio.run(guard({"tool_name": "Write", "tool_input": {"file_path": "/tmp/x"}}, "id", None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    allowed = asyncio.run(guard({"tool_name": "Write", "tool_input": {"file_path": "output/a.md"}}, "id", None))
    assert allowed == {}


@pytest.fixture(scope="module")
def synth(tmp_path_factory):
    cdk = pytest.importorskip("aws_cdk")
    import sys
    sys.path.insert(0, str(REPO / "infra"))
    from idea_agents.catalogue import Catalogue
    from pipeline_stack import PipelineStack

    out = tmp_path_factory.mktemp("cdk")
    app = cdk.App(outdir=str(out))
    PipelineStack(app, "Test", pipeline=Catalogue(REPO / "catalogue").pipeline,
                  model_overrides={"ANTHROPIC_DEFAULT_OPUS_MODEL": "eu.anthropic.example"},
                  alarm_email="ops@example.com", monthly_budget_usd=500, cost_centre="ARCH",
                  env=cdk.Environment(account="123456789012", region="eu-west-2"))
    app.synth()
    return out / "Test.template.json"


def resources(template_path, kind):
    t = json.loads(template_path.read_text())
    return [r for r in t["Resources"].values() if r["Type"] == kind]


def test_stack_shape(synth):
    assert len(resources(synth, "AWS::StepFunctions::StateMachine")) == 1
    assert len(resources(synth, "AWS::CloudFront::Distribution")) == 1
    assert len(resources(synth, "AWS::Cognito::UserPool")) == 1
    assert not resources(synth, "AWS::EC2::NatGateway")  # the runner has no route to the internet
    assert not resources(synth, "AWS::EC2::InternetGateway")
    task = resources(synth, "AWS::ECS::TaskDefinition")[0]
    env = {e["Name"]: e.get("Value") for e in task["Properties"]["ContainerDefinitions"][0]["Environment"]}
    assert env["IDEA_USE_BEDROCK"] == "1" and env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "eu.anthropic.example"


def test_every_api_method_needs_sign_in(synth):
    methods = resources(synth, "AWS::ApiGateway::Method")
    assert len(methods) == 6  # roles, list cases, start case, get case, respond, attach files
    assert all(m["Properties"]["AuthorizationType"] == "COGNITO_USER_POOLS" for m in methods)


def test_buckets_are_private(synth):
    for b in resources(synth, "AWS::S3::Bucket"):
        block = b["Properties"]["PublicAccessBlockConfiguration"]
        assert all(block[k] for k in ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets"))


@pytest.mark.skipif(not shutil.which("cfn-lint"), reason="cfn-lint not installed")
def test_template_passes_cfn_lint(synth):
    result = subprocess.run(["cfn-lint", "--format", "parseable", str(synth)], capture_output=True, text=True)
    errors = [line for line in result.stdout.splitlines() if ":E" in line]
    assert not errors, "\n".join(errors)


@pytest.mark.skipif(not shutil.which("cfn-lint"), reason="cfn-lint not installed")
def test_state_machine_passes_cfn_lint_schema(tmp_path):
    definition = json.loads((REPO / "statemachine" / "pipeline.asl.json").read_text())
    template = {"AWSTemplateFormatVersion": "2010-09-09", "Resources": {"SM": {
        "Type": "AWS::StepFunctions::StateMachine",
        "Properties": {"RoleArn": "arn:aws:iam::123456789012:role/x", "Definition": definition}}}}
    path = tmp_path / "sm.json"
    path.write_text(json.dumps(template))
    result = subprocess.run(["cfn-lint", "--format", "parseable", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


@pytest.mark.skipif(not shutil.which("cfn-lint"), reason="cfn-lint not installed")
def test_github_deploy_role_template_passes_cfn_lint():
    result = subprocess.run(["cfn-lint", "--format", "parseable", str(REPO / "infra" / "github-deploy-role.yaml")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


def test_github_deploy_role_is_narrow():
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    for tag in ("!Ref", "!Sub", "!If", "!Equals", "!GetAtt"):
        Loader.add_constructor(tag, lambda loader, node: loader.construct_sequence(node)
                               if isinstance(node, yaml.SequenceNode) else loader.construct_scalar(node))
    t = yaml.load((REPO / "infra" / "github-deploy-role.yaml").read_text(), Loader=Loader)
    role = t["Resources"]["DeployRole"]["Properties"]
    cond = role["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]["StringEquals"]
    assert cond["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    assert ":environment:" in cond["token.actions.githubusercontent.com:sub"]  # only the approved environment
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    assert [s["Action"] for s in statements] == ["sts:AssumeRole"]  # nothing but the CDK bootstrap roles
    assert "role/cdk-" in statements[0]["Resource"]


def test_deploy_workflow_is_gated_and_keyless():
    import yaml
    wf = yaml.safe_load((REPO / ".github" / "workflows" / "deploy.yml").read_text())
    assert wf["permissions"]["id-token"] == "write"
    deploy = wf["jobs"]["deploy"]
    assert deploy["needs"] == "test" and deploy["environment"] == "sandbox"
    text = (REPO / ".github" / "workflows" / "deploy.yml").read_text()
    assert "aws-access-key-id" not in text and "secrets.AWS" not in text
    options = wf[True]["workflow_dispatch"]["inputs"]["method"]["options"]  # YAML reads "on" as True
    assert options == ["prepare-change-set", "execute-change-set", "change-set"]


# ------------------------------------------------------------------ Well-Architected controls
def template(path):
    return json.loads(path.read_text())


def test_security_controls(synth):
    t = template(synth)
    sg = next(r for k, r in t["Resources"].items()
              if r["Type"] == "AWS::EC2::SecurityGroup" and k.startswith("RunnerSg"))
    egress = sg["Properties"]["SecurityGroupEgress"]
    assert [(e["IpProtocol"], e["FromPort"], e["ToPort"]) for e in egress] == [("tcp", 443, 443)]
    assert resources(synth, "AWS::EC2::FlowLog")
    pool = resources(synth, "AWS::Cognito::UserPool")[0]["Properties"]
    assert pool["MfaConfiguration"] == "OPTIONAL" and pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"]
    for b in resources(synth, "AWS::S3::Bucket"):
        assert b["Properties"]["BucketEncryption"]
    case_bucket = next(r for k, r in t["Resources"].items() if k.startswith("CaseDocuments")
                       and r["Type"] == "AWS::S3::Bucket")
    assert case_bucket["Properties"]["LoggingConfiguration"]["LogFilePrefix"] == "case-documents/"
    dist = resources(synth, "AWS::CloudFront::Distribution")[0]["Properties"]["DistributionConfig"]
    assert dist["Logging"]["Prefix"] == "cloudfront/"


def test_read_only_functions_cannot_write(synth):
    t = template(synth)
    for name in ("GetCase", "ListCases"):
        role = next(k for k in t["Resources"] if k.startswith(f"{name}ServiceRole") and
                    t["Resources"][k]["Type"] == "AWS::IAM::Role")
        actions = set()
        for r in t["Resources"].values():
            if r["Type"] == "AWS::IAM::Policy" and {"Ref": role} in r["Properties"]["Roles"]:
                for st in r["Properties"]["PolicyDocument"]["Statement"]:
                    acts = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
                    actions.update(acts)
        assert actions and not any(a.startswith(("s3:Put", "s3:Delete", "dynamodb:Put", "dynamodb:Update",
                                                 "dynamodb:Delete", "secretsmanager:")) for a in actions), actions


def test_reliability_controls(synth):
    table = resources(synth, "AWS::DynamoDB::Table")[0]["Properties"]
    assert table["DeletionProtectionEnabled"] is True
    assert table["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True
    assert len({s["Properties"]["AvailabilityZone"] for s in resources(synth, "AWS::EC2::Subnet")}) == 2


def test_operations_controls(synth):
    alarms = resources(synth, "AWS::CloudWatch::Alarm")
    assert len(alarms) >= 10 and all(a["Properties"]["AlarmActions"] for a in alarms)
    subscription = resources(synth, "AWS::SNS::Subscription")[0]["Properties"]
    assert subscription["Protocol"] == "email" and subscription["Endpoint"] == "ops@example.com"
    for f in resources(synth, "AWS::Lambda::Function"):
        if f["Properties"].get("Handler", "").startswith("idea_agents."):
            assert f["Properties"]["TracingConfig"]["Mode"] == "Active"
            assert f["Properties"]["Architectures"] == ["arm64"]


def test_cost_controls(synth):
    budget = resources(synth, "AWS::Budgets::Budget")[0]["Properties"]
    assert budget["Budget"]["BudgetLimit"] == {"Amount": 500, "Unit": "USD"}
    kinds = sorted(n["Notification"]["NotificationType"] for n in budget["NotificationsWithSubscribers"])
    assert kinds == ["ACTUAL", "FORECASTED"]
    tagged = resources(synth, "AWS::DynamoDB::Table")[0]["Properties"]["Tags"]
    assert {"Key": "Project", "Value": "idea-agents"} in tagged and {"Key": "CostCentre", "Value": "ARCH"} in tagged


def test_roles_summary_is_published_without_touching_case_files(synth):
    deploys = [r["Properties"] for r in resources(synth, "Custom::CDKBucketDeployment")
               if r["Properties"].get("DestinationBucketKeyPrefix") == "catalogue/"]
    assert len(deploys) == 1 and deploys[0]["Prune"] is False


# ------------------------------------------------------------------ attachments
def test_uploads_are_scanned_before_use(synth):
    plan = resources(synth, "AWS::GuardDuty::MalwareProtectionPlan")
    assert len(plan) == 1
    props = plan[0]["Properties"]
    assert props["ProtectedResource"]["S3Bucket"]["ObjectPrefixes"] == ["uploads/"]
    assert props["Actions"]["Tagging"]["Status"] == "ENABLED"
    roles = [r for r in resources(synth, "AWS::IAM::Role")
             if "malware-protection-plan.guardduty.amazonaws.com" in json.dumps(r["Properties"]["AssumeRolePolicyDocument"])]
    assert len(roles) == 1

    rules = [r["Properties"] for r in resources(synth, "AWS::Events::Rule")
             if r["Properties"].get("EventPattern", {}).get("source") == ["aws.guardduty"]]
    assert len(rules) == 1
    pattern = rules[0]["EventPattern"]
    assert pattern["detail-type"] == ["GuardDuty Malware Protection Object Scan Result"]
    assert pattern["detail"]["s3ObjectDetails"]["objectKey"] == [{"prefix": "uploads/"}]
    target = rules[0]["Targets"][0]
    assert "DeadLetterConfig" in target and target["RetryPolicy"]["MaximumRetryAttempts"] == 4
    alarms = [a["Properties"] for a in resources(synth, "AWS::CloudWatch::Alarm")
              if "scan result could not be processed" in a["Properties"].get("AlarmDescription", "")]
    assert len(alarms) == 1 and alarms[0]["AlarmActions"]


def test_ingest_lambda_is_bundled_with_pypdf(synth):
    fns = [r["Properties"] for r in resources(synth, "AWS::Lambda::Function")
           if r["Properties"].get("Handler") == "idea_agents.aws.lambdas.ingest.handler"]
    assert len(fns) == 1 and fns[0]["MemorySize"] == 1024
    asset = fns[0]["Code"]["S3Key"].split(".")[0]
    folder = synth.parent / f"asset.{asset}"
    assert (folder / "pypdf").is_dir() and (folder / "idea_agents" / "attachments.py").is_file()


def test_case_bucket_allows_browser_uploads_only_by_put(synth):
    buckets = [r["Properties"] for r in resources(synth, "AWS::S3::Bucket") if "CorsConfiguration" in r["Properties"]]
    assert len(buckets) == 1
    rule = buckets[0]["CorsConfiguration"]["CorsRules"][0]
    assert rule["AllowedMethods"] == ["PUT"] and rule["AllowedOrigins"] == ["https://*.cloudfront.net"]
