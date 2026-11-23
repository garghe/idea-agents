"""Pipeline definition: stages, gates and limits, loaded from catalogue/pipeline.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(ValueError):
    """The pipeline definition is invalid."""


@dataclass(frozen=True)
class Gate:
    id: str
    enabled: bool
    approver: str


@dataclass(frozen=True)
class Stage:
    id: str
    kind: str  # "agent" or "review"
    role: str | None = None
    designer: str | None = None
    reviewers: tuple[str, ...] = ()
    chair: str | None = None
    gate: str | None = None

    def roles(self) -> list[str]:
        if self.kind == "agent":
            return [self.role] if self.role else []
        return [r for r in (self.designer, *self.reviewers, self.chair) if r]


@dataclass(frozen=True)
class Limits:
    max_review_rounds: int = 3
    case_budget_usd: float = 40.0
    output_retries: int = 1


@dataclass(frozen=True)
class PipelineConfig:
    stages: tuple[Stage, ...]
    gates: dict[str, Gate] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)

    def stage(self, stage_id: str) -> Stage:
        for s in self.stages:
            if s.id == stage_id:
                return s
        raise KeyError(f"unknown stage: {stage_id}")

    def first_stage(self) -> Stage:
        return self.stages[0]

    def next_stage(self, stage_id: str) -> Stage | None:
        ids = [s.id for s in self.stages]
        i = ids.index(stage_id)
        return self.stages[i + 1] if i + 1 < len(self.stages) else None

    def gate_for(self, stage: Stage) -> Gate | None:
        """The stage's gate, or None when it has none or the gate is disabled."""
        if not stage.gate:
            return None
        gate = self.gates[stage.gate]
        return gate if gate.enabled else None

    def gate_flags(self) -> dict[str, bool]:
        return {g.id: g.enabled for g in self.gates.values()}


def parse_pipeline(data: dict) -> PipelineConfig:
    if not isinstance(data, dict) or "stages" not in data:
        raise ConfigError("pipeline must define 'stages'")

    gates = {
        gid: Gate(id=gid, enabled=bool(g.get("enabled", True)), approver=str(g.get("approver", "")))
        for gid, g in (data.get("gates") or {}).items()
    }

    stages: list[Stage] = []
    seen: set[str] = set()
    for raw in data["stages"]:
        sid = raw.get("id")
        kind = raw.get("kind", "agent")
        if not sid:
            raise ConfigError("every stage needs an id")
        if sid in seen:
            raise ConfigError(f"duplicate stage id: {sid}")
        seen.add(sid)
        if kind == "agent":
            if not raw.get("role"):
                raise ConfigError(f"agent stage '{sid}' needs a role")
            stage = Stage(id=sid, kind=kind, role=raw["role"], gate=raw.get("gate"))
        elif kind == "review":
            if not raw.get("designer") or not raw.get("reviewers") or not raw.get("chair"):
                raise ConfigError(f"review stage '{sid}' needs designer, reviewers and chair")
            stage = Stage(
                id=sid,
                kind=kind,
                designer=raw["designer"],
                reviewers=tuple(raw["reviewers"]),
                chair=raw["chair"],
                gate=raw.get("gate"),
            )
        else:
            raise ConfigError(f"stage '{sid}' has unknown kind '{kind}'")
        if stage.gate and stage.gate not in gates:
            raise ConfigError(f"stage '{sid}' refers to undefined gate '{stage.gate}'")
        stages.append(stage)

    if not stages:
        raise ConfigError("pipeline has no stages")

    lim = data.get("limits") or {}
    limits = Limits(
        max_review_rounds=int(lim.get("max_review_rounds", 3)),
        case_budget_usd=float(lim.get("case_budget_usd", 40.0)),
        output_retries=int(lim.get("output_retries", 1)),
    )
    if limits.max_review_rounds < 1:
        raise ConfigError("max_review_rounds must be at least 1")
    return PipelineConfig(stages=tuple(stages), gates=gates, limits=limits)


def load_pipeline(path: Path) -> PipelineConfig:
    return parse_pipeline(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
