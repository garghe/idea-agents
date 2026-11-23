#!/usr/bin/env python3
"""CDK app. Deploy with:  cd infra && cdk deploy  (see README for context values)."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aws_cdk as cdk  # noqa: E402

from agentpipe.config import load_pipeline  # noqa: E402
from pipeline_stack import PipelineStack  # noqa: E402

app = cdk.App()
overrides = {
    env: app.node.try_get_context(key)
    for key, env in (
        ("opusModel", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
        ("sonnetModel", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
        ("haikuModel", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
    )
    if app.node.try_get_context(key)
}
PipelineStack(
    app, "AgentPipeline",
    pipeline=load_pipeline(REPO_ROOT / "catalogue" / "pipeline.yaml"),
    portal_url=app.node.try_get_context("portalUrl") or "",
    model_overrides=overrides,
    env=cdk.Environment(
        account=app.node.try_get_context("account"),
        region=app.node.try_get_context("region") or "eu-west-2",
    ),
)
app.synth()
