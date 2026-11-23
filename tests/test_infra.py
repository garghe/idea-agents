"""Builds the real Agent SDK options and synthesises and lints the CDK stack, without calling Claude or AWS."""

import asyncio
import json
import shutil
import subprocess

import pytest

from conftest import REPO


def test_agent_sdk_options_are_locked_down(catalogue, tmp_path):
    pytest.importorskip("claude_agent_sdk")
    from agentpipe.agent_client import ClaudeAgentClient

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
    from agentpipe.catalogue import Catalogue
    from pipeline_stack import PipelineStack

    out = tmp_path_factory.mktemp("cdk")
    app = cdk.App(outdir=str(out))
    PipelineStack(app, "Test", pipeline=Catalogue(REPO / "catalogue").pipeline,
                  model_overrides={"ANTHROPIC_DEFAULT_OPUS_MODEL": "eu.anthropic.example"},
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
    assert env["AGENTPIPE_USE_BEDROCK"] == "1" and env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "eu.anthropic.example"


def test_every_api_method_needs_sign_in(synth):
    methods = resources(synth, "AWS::ApiGateway::Method")
    assert len(methods) == 4
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
