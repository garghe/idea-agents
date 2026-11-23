"""A read-only summary of the agent roles: what each does, its model, budget and limits.

Built from the catalogue. The local portal serves it directly; on AWS the stack publishes it as
`catalogue/roles.json` in the case bucket at deploy time and the roles Lambda reads it from there.
"""

from __future__ import annotations

from .api import ROLE_LABELS
from .catalogue import Catalogue, Role
from .runner import HOUSE_RULES

STAGE_PART = {"designer": "Designer", "reviewers": "Reviewer", "chair": "Chair"}


def _role_entry(role: Role, stage_id: str, part: str) -> dict:
    return {
        "name": role.name,
        "label": ROLE_LABELS.get(role.name, role.name),
        "stage": stage_id,
        "part": part,
        "description": role.description,
        "model": role.model,
        "budget_usd": role.budget_usd,
        "max_turns": role.max_turns,
        "tools": list(role.tools),
        "reads": list(role.reads),
        "writes": role.writes,
        "standards": list(role.standards),
        "finding_prefix": role.finding_prefix,
        "specialists": [
            {"name": s.name, "description": s.description, "model": s.model, "max_turns": s.max_turns,
             "tools": list(s.tools)}
            for s in role.specialists
        ],
        "instructions": role.prompt,
    }


def roles_summary(catalogue: Catalogue) -> dict:
    """Every role in pipeline order, with the limits that apply to each case."""
    roles = []
    for stage in catalogue.pipeline.stages:
        if stage.kind == "agent":
            roles.append(_role_entry(catalogue.role(stage.role), stage.id, "Agent"))
        else:
            roles.append(_role_entry(catalogue.role(stage.designer), stage.id, "Designer"))
            for reviewer in stage.reviewers:
                roles.append(_role_entry(catalogue.role(reviewer), stage.id, "Reviewer"))
            roles.append(_role_entry(catalogue.role(stage.chair), stage.id, "Chair"))
    limits = catalogue.pipeline.limits
    return {
        "roles": roles,
        "limits": {
            "case_budget_usd": limits.case_budget_usd,
            "max_review_rounds": limits.max_review_rounds,
            "output_retries": limits.output_retries,
        },
        "house_rules": HOUSE_RULES.strip(),
    }
