import json

import pytest

from idea_agents.catalogue import Catalogue, CatalogueError, parse_frontmatter
from idea_agents.config import ConfigError, parse_pipeline


def test_catalogue_loads_and_is_consistent(catalogue):
    assert {s.id for s in catalogue.pipeline.stages} == {"intake", "value", "analysis", "design", "stories"}
    for stage in catalogue.pipeline.stages:
        for role in stage.roles():
            assert role in catalogue.roles


def test_reviewers_are_read_only_and_have_prefixes(catalogue):
    design = catalogue.pipeline.stage("design")
    prefixes = set()
    for name in design.reviewers:
        role = catalogue.role(name)
        assert role.finding_prefix and role.finding_prefix not in prefixes
        prefixes.add(role.finding_prefix)
        assert set(role.tools) <= {"Read", "Glob", "Grep"}
        assert role.writes is None
    assert set(catalogue.role("chair").tools) <= {"Read", "Glob", "Grep"}


def test_designer_uses_a_different_model_from_reviewers(catalogue):
    designer = catalogue.role("designer").model
    for name in catalogue.pipeline.stage("design").reviewers:
        assert catalogue.role(name).model != designer


def test_analysis_lead_has_specialists(catalogue):
    lead = catalogue.role("analysis-lead")
    assert {s.name for s in lead.specialists} == {"data", "security", "operations", "commercial"}
    assert "Agent" in lead.tools


def test_go_no_go_gate_is_off_by_default(catalogue):
    cfg = catalogue.pipeline
    assert cfg.gate_for(cfg.stage("value")) is None
    assert cfg.gate_for(cfg.stage("design")).id == "design_signoff"


def test_schemas_are_valid_json_schema(catalogue):
    from jsonschema import Draft202012Validator
    Draft202012Validator.check_schema(catalogue.envelope_schema)
    Draft202012Validator.check_schema(catalogue.backlog_schema)


def test_frontmatter_parsing():
    meta, body = parse_frontmatter("---\nname: x\ntools: [Read]\n---\n\nHello\n")
    assert meta == {"name": "x", "tools": ["Read"]}
    assert body == "Hello\n"
    with pytest.raises(CatalogueError):
        parse_frontmatter("no frontmatter")


def test_pipeline_validation_errors():
    with pytest.raises(ConfigError):
        parse_pipeline({"stages": [{"id": "a", "kind": "agent"}]})
    with pytest.raises(ConfigError):
        parse_pipeline({"stages": [{"id": "a", "role": "r", "gate": "missing"}]})
    with pytest.raises(ConfigError):
        parse_pipeline({"stages": [{"id": "a", "role": "r"}, {"id": "a", "role": "r"}]})


def test_catalogue_rejects_unknown_role(tmp_path, catalogue):
    import shutil
    root = tmp_path / "cat"
    shutil.copytree(catalogue.root, root)
    text = (root / "pipeline.yaml").read_text().replace("role: intake", "role: nobody")
    (root / "pipeline.yaml").write_text(text)
    with pytest.raises(CatalogueError, match="unknown role 'nobody'"):
        Catalogue(root)


def test_committed_state_machine_matches_pipeline(catalogue):
    """statemachine/pipeline.asl.json must be regenerated whenever pipeline.yaml changes."""
    from idea_agents.aws.asl import build_definition
    from conftest import REPO
    committed = json.loads((REPO / "statemachine" / "pipeline.asl.json").read_text())
    assert committed == build_definition(catalogue.pipeline), \
        "run: python -m idea_agents.aws.asl > statemachine/pipeline.asl.json"
