from pathlib import Path

import pytest

from agentpipe.catalogue import Catalogue
from agentpipe.fake import ScriptedAgentClient
from agentpipe.local_pipeline import LocalPipeline
from agentpipe.runner import StageRunner
from agentpipe.store import LocalCaseStore

REPO = Path(__file__).resolve().parents[1]
REQUEST = (REPO / "examples" / "requests" / "highlights-clips.md").read_text(encoding="utf-8")


@pytest.fixture
def catalogue():
    return Catalogue(REPO / "catalogue")


@pytest.fixture
def store(tmp_path):
    return LocalCaseStore(tmp_path / "cases")


def make_pipeline(catalogue, store, tmp_path, **fake_options):
    client = ScriptedAgentClient(**fake_options)
    runner = StageRunner(catalogue, store, client, work_root=tmp_path)
    return LocalPipeline(catalogue, store, runner), client


@pytest.fixture
def pipeline_factory(catalogue, store, tmp_path):
    return lambda **opts: make_pipeline(catalogue, store, tmp_path, **opts)
