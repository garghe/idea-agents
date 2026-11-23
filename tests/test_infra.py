"""Builds the real Agent SDK options and synthesises the CDK stack, without calling Claude or AWS."""

import asyncio

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


def test_cdk_stack_synthesises(catalogue, tmp_path):
    cdk = pytest.importorskip("aws_cdk")
    import sys
    sys.path.insert(0, str(REPO / "infra"))
    from pipeline_stack import PipelineStack

    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    PipelineStack(app, "Test", pipeline=catalogue.pipeline,
                  model_overrides={"ANTHROPIC_DEFAULT_OPUS_MODEL": "eu.anthropic.example"},
                  env=cdk.Environment(account="123456789012", region="eu-west-2"))
    template = app.synth().get_stack_by_name("Test").template
    kinds = [r["Type"] for r in template["Resources"].values()]
    assert kinds.count("AWS::StepFunctions::StateMachine") == 1
    assert kinds.count("AWS::Lambda::Function") >= 6
    assert "AWS::EC2::NatGateway" not in kinds  # the runner has no route to the internet
    task = next(r for r in template["Resources"].values() if r["Type"] == "AWS::ECS::TaskDefinition")
    env = {e["Name"]: e.get("Value") for e in task["Properties"]["ContainerDefinitions"][0]["Environment"]}
    assert env["AGENTPIPE_USE_BEDROCK"] == "1"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "eu.anthropic.example"
