"""A scripted stand-in for Claude, so the whole pipeline can be run and tested without API calls.

It plays every role with canned but valid outputs, including one blocker at intake and one
review finding that gets fixed in round 2, so a demo run exercises every path.
Run it with `agentpipe --fake ...`.
"""

from __future__ import annotations

import json
from pathlib import Path

from .agent_client import AgentResult
from .catalogue import Role


class ScriptedAgentClient:
    def __init__(self, intake_blocks: bool = True, security_finding: bool = True,
                 conflict: bool = False, chair_question: bool = False):
        self.intake_blocks = intake_blocks
        self.security_finding = security_finding
        self.conflict = conflict
        self.chair_question = chair_question
        self.calls: list[str] = []

    def run(self, role: Role, system_prompt: str, task: str, workspace: Path, schema: dict,
            resume: str | None = None) -> AgentResult:
        self.calls.append(role.name)
        ws = Path(workspace)
        handler = getattr(self, "_" + role.name.replace("-", "_"), None)
        if handler is None:
            raise ValueError(f"no script for role {role.name}")
        envelope = handler(ws)
        return AgentResult(output=envelope, text=json.dumps(envelope), cost_usd=0.0, turns=1,
                           session_id=f"fake-{role.name}", model="scripted")

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _write(ws: Path, name: str, text: str) -> None:
        (ws / "output" / name).write_text(text, encoding="utf-8")

    @staticmethod
    def _has_answers(ws: Path) -> bool:
        return (ws / "inputs" / "answers").is_dir()

    @staticmethod
    def _open_findings(ws: Path) -> list[dict]:
        path = ws / "inputs" / "open-findings.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    # --------------------------------------------------------------- roles
    def _intake(self, ws: Path) -> dict:
        self._write(ws, "01-request-brief.md", "# Request brief (scripted)\n\nThe ask, users, scope.\n")
        if self.intake_blocks and not self._has_answers(ws):
            return {
                "status": "blocked",
                "summary": "Brief drafted; one question for the requester.",
                "document": "01-request-brief.md",
                "blockers": [{
                    "question": "Which competitions allow social clips?",
                    "for": "requester",
                    "why_it_matters": "Rights decide what may be published.",
                }],
            }
        return {"status": "complete", "summary": "Request brief ready.", "document": "01-request-brief.md"}

    def _value(self, ws: Path) -> dict:
        self._write(ws, "02-business-case.md", "# Business case (scripted)\n\nProceed. Benefit range given.\n")
        return {"status": "complete", "summary": "Proceed; payback 9 to 18 months.",
                "document": "02-business-case.md"}

    def _analysis_lead(self, ws: Path) -> dict:
        self._write(ws, "03-requirements.md", "# Requirements (scripted)\n\nFR-1 Detect key moments.\n")
        return {"status": "complete", "summary": "12 requirements, 5 NFRs.", "document": "03-requirements.md"}

    def _designer(self, ws: Path) -> dict:
        self._write(ws, "04-design.md", "# Solution design (scripted)\n\nComponents, flows, ADRs.\n")
        responses = [
            {"finding_id": f["id"], "response": "fixed", "note": "Addressed in the security section."}
            for f in self._open_findings(ws)
        ]
        return {"status": "complete", "summary": "Design written.", "document": "04-design.md",
                "responses": responses}

    def _reviewer(self, ws: Path, finding: dict | None) -> dict:
        own_open = self._open_findings(ws)
        if own_open:  # round 2+: everything the designer fixed is resolved
            return {"status": "complete", "summary": "Rechecked.", "document": None,
                    "verdict": "approve", "findings": [],
                    "resolved_finding_ids": [f["id"] for f in own_open]}
        findings = [finding] if finding else []
        return {"status": "complete", "summary": f"{len(findings)} finding(s).", "document": None,
                "verdict": "approve_with_changes" if findings else "approve", "findings": findings}

    def _reviewer_architecture(self, ws: Path) -> dict:
        return self._reviewer(ws, None)

    def _reviewer_security(self, ws: Path) -> dict:
        finding = None
        if self.security_finding:
            finding = {"severity": "major", "section": "Security",
                       "issue": "Editor approval UI has no single sign-on.",
                       "evidence": "SEC-STD-01", "suggested_fix": "Use the corporate identity provider."}
        return self._reviewer(ws, finding)

    def _reviewer_operability(self, ws: Path) -> dict:
        finding = None
        if self.conflict:
            finding = {"severity": "major", "section": "Cost",
                       "issue": "Dedicated SSO proxy doubles running cost.",
                       "evidence": "OPS-STD-05", "suggested_fix": "Share the existing proxy."}
        return self._reviewer(ws, finding)

    def _chair(self, ws: Path) -> dict:
        open_ids = [f["id"] for f in self._open_findings(ws)]
        conflicts = []
        if self.conflict and len(open_ids) >= 2:
            conflicts = [{"finding_ids": open_ids[:2], "description": "Security control versus running cost."}]
        envelope = {"status": "complete", "summary": f"{len(open_ids)} open finding(s).", "document": None,
                    "chair": {"duplicates": [], "conflicts": conflicts,
                              "change_list": [f"Resolve {i}" for i in open_ids]}}
        if self.chair_question and open_ids:
            envelope.update(status="blocked", blockers=[{
                "question": "Is single sign-on mandatory for freelance editors?",
                "for": "security", "why_it_matters": "Decides the identity design."}])
        return envelope

    def _story_writer(self, ws: Path) -> dict:
        backlog = {"epics": [{
            "id": "E1", "title": "Moment detection", "description": "Spot key moments in live matches.",
            "stories": [
                {"id": "E1-S1", "title": "Ingest match events", "as_a": "clip editor",
                 "i_want": "goal events within 5 seconds", "so_that": "clips start fast",
                 "acceptance_criteria": ["Events arrive within 5 seconds at p95"], "design_refs": ["ADR-1"]},
                {"id": "E1-S2", "title": "Approval queue", "as_a": "clip editor",
                 "i_want": "one-click approval", "so_that": "only cleared clips go out",
                 "acceptance_criteria": ["Editors sign in with SSO"], "depends_on": ["E1-S1"]},
            ]}]}
        self._write(ws, "05-backlog.json", json.dumps(backlog, indent=2))
        return {"status": "complete", "summary": "1 epic, 2 stories.", "document": "05-backlog.json"}
