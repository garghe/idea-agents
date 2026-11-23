#!/usr/bin/env python3
"""CDK app. Deploy with:  cd infra && cdk deploy  (the README has the full steps and context values)."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aws_cdk as cdk  # noqa: E402

from idea_agents.config import load_pipeline  # noqa: E402
from pipeline_stack import PipelineStack  # noqa: E402

app = cdk.App()
ctx = app.node.try_get_context
overrides = {
    env: ctx(key)
    for key, env in (
        ("opusModel", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
        ("sonnetModel", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
        ("haikuModel", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
    )
    if ctx(key)
}
PipelineStack(
    app, "IdeaAgents",
    pipeline=load_pipeline(REPO_ROOT / "catalogue" / "pipeline.yaml"),
    model_overrides=overrides,
    auth_domain_prefix=ctx("authDomainPrefix") or None,
    saml_metadata_url=ctx("samlMetadataUrl") or None,
    saml_provider_name=ctx("samlProviderName") or "CorporateSSO",
    alarm_email=ctx("alarmEmail") or None,
    monthly_budget_usd=float(ctx("monthlyBudgetUsd")) if ctx("monthlyBudgetUsd") else None,
    cost_centre=ctx("costCentre") or None,
    env=cdk.Environment(account=ctx("account"), region=ctx("region") or "eu-west-2"),
)
app.synth()
