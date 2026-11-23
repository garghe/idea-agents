"""The agent catalogue: role prompts, tool lists, models, schemas, templates and standards.

Everything that shapes what the agents produce lives in catalogue/ as Markdown, YAML and JSON,
so it can be reviewed in pull requests like code.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import PipelineConfig, load_pipeline

# Input tokens a role may list under `reads:` besides plain document names.
SPECIAL_INPUTS = {"answers", "attachments", "open-findings", "own-open-findings", "all-open-findings"}


class CatalogueError(ValueError):
    """The catalogue is incomplete or inconsistent."""


@dataclass(frozen=True)
class Specialist:
    name: str
    description: str
    prompt: str
    model: str
    tools: tuple[str, ...]
    max_turns: int


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    prompt: str
    model: str
    tools: tuple[str, ...]
    max_turns: int
    budget_usd: float
    reads: tuple[str, ...]
    writes: str | None
    template: str | None = None
    standards: tuple[str, ...] = ()
    specialists: tuple[Specialist, ...] = ()
    finding_prefix: str | None = None
    effort: str | None = None

    @property
    def is_reviewer(self) -> bool:
        return self.finding_prefix is not None


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a Markdown file with a leading '---' YAML block into (metadata, body)."""
    if not text.startswith("---"):
        raise CatalogueError("missing YAML frontmatter")
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        raise CatalogueError("unterminated YAML frontmatter")
    meta = yaml.safe_load(parts[0][3:]) or {}
    body = parts[1].lstrip("-").lstrip("\n")
    return meta, body.strip() + "\n"


def default_catalogue_dir() -> Path:
    env = os.environ.get("IDEA_CATALOGUE")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "catalogue"


class Catalogue:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else default_catalogue_dir()
        if not self.root.is_dir():
            raise CatalogueError(f"catalogue folder not found: {self.root}")
        self.pipeline: PipelineConfig = load_pipeline(self.root / "pipeline.yaml")
        self.envelope_schema = self._load_json("schemas/envelope.schema.json")
        self.backlog_schema = self._load_json("schemas/backlog.schema.json")
        self._specialists = self._load_specialists()
        self.roles: dict[str, Role] = self._load_roles()
        self.validate()

    @property
    def standards_dir(self) -> Path:
        return self.root / "standards"

    @property
    def templates_dir(self) -> Path:
        return self.root / "templates"

    @property
    def schemas_dir(self) -> Path:
        return self.root / "schemas"

    def role(self, name: str) -> Role:
        try:
            return self.roles[name]
        except KeyError:
            raise CatalogueError(f"unknown role: {name}") from None

    def _load_json(self, rel: str) -> dict:
        return json.loads((self.root / rel).read_text(encoding="utf-8"))

    def _load_specialists(self) -> dict[str, Specialist]:
        out: dict[str, Specialist] = {}
        folder = self.root / "roles" / "specialists"
        if not folder.is_dir():
            return out
        for path in sorted(folder.glob("*.md")):
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            name = meta.get("name", path.stem)
            out[name] = Specialist(
                name=name,
                description=meta["description"],
                prompt=body,
                model=meta.get("model", "sonnet"),
                tools=tuple(meta.get("tools", ["Read", "Glob", "Grep"])),
                max_turns=int(meta.get("max_turns", 20)),
            )
        return out

    def _load_roles(self) -> dict[str, Role]:
        roles: dict[str, Role] = {}
        for path in sorted((self.root / "roles").glob("*.md")):
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            name = meta.get("name", path.stem)
            try:
                specialists = tuple(self._specialists[s] for s in meta.get("specialists", []))
            except KeyError as exc:
                raise CatalogueError(f"role '{name}' uses unknown specialist {exc}") from None
            roles[name] = Role(
                name=name,
                description=meta["description"],
                prompt=body,
                model=meta.get("model", "sonnet"),
                tools=tuple(meta.get("tools", ["Read", "Glob", "Grep"])),
                max_turns=int(meta.get("max_turns", 30)),
                budget_usd=float(meta.get("budget_usd", 2.0)),
                reads=tuple(meta.get("reads", [])),
                writes=meta.get("writes"),
                template=meta.get("template"),
                standards=tuple(meta.get("standards", [])),
                specialists=specialists,
                finding_prefix=meta.get("finding_prefix"),
                effort=meta.get("effort"),
            )
        return roles

    def validate(self) -> None:
        problems: list[str] = []
        for stage in self.pipeline.stages:
            for role_name in stage.roles():
                if role_name not in self.roles:
                    problems.append(f"stage '{stage.id}' uses unknown role '{role_name}'")
            if stage.kind == "review":
                for r in stage.reviewers:
                    if r in self.roles and not self.roles[r].is_reviewer:
                        problems.append(f"reviewer '{r}' needs a finding_prefix")
        prefixes: dict[str, str] = {}
        for role in self.roles.values():
            if role.writes and "Write" not in role.tools:
                problems.append(f"role '{role.name}' writes {role.writes} but has no Write tool")
            if not role.writes and {"Write", "Edit"} & set(role.tools):
                problems.append(f"role '{role.name}' writes nothing but has write tools")
            if role.specialists and "Agent" not in role.tools:
                problems.append(f"role '{role.name}' has specialists but no Agent tool")
            if role.template and not (self.templates_dir / role.template).is_file():
                problems.append(f"role '{role.name}' template missing: {role.template}")
            for s in role.standards:
                if not (self.standards_dir / s).is_file():
                    problems.append(f"role '{role.name}' standard missing: {s}")
            for token in role.reads:
                if token.endswith("findings") and token not in SPECIAL_INPUTS:
                    problems.append(f"role '{role.name}' reads unknown input '{token}'")
            if role.finding_prefix:
                other = prefixes.setdefault(role.finding_prefix, role.name)
                if other != role.name:
                    problems.append(f"finding prefix {role.finding_prefix} used by {other} and {role.name}")
        if problems:
            raise CatalogueError("; ".join(problems))
