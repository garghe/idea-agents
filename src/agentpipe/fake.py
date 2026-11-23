"""Scripted agents: the whole pipeline runs on mock data, with no LLM calls and no cost.

This is the default for local runs (CLI and portal). Every document it writes starts with a
MOCK DATA banner, and the outputs follow the same rules as real agents, so every path of the flow
can be exercised: a question at intake, a review finding fixed in round 2, the gates, the backlog.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .agent_client import AgentResult
from .catalogue import Role

MOCK_BANNER = "> **MOCK DATA.** Written by a scripted agent for local testing. No LLM was called.\n"


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
                           session_id=f"mock-{role.name}", model="mock")

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _write(ws: Path, name: str, text: str) -> None:
        (ws / "output" / name).write_text(text, encoding="utf-8")

    @staticmethod
    def _has_answers(ws: Path) -> bool:
        return (ws / "inputs" / "answers").is_dir()

    @staticmethod
    def _answers_text(ws: Path) -> str:
        folder = ws / "inputs" / "answers"
        if not folder.is_dir():
            return ""
        texts = []
        for p in sorted(folder.glob("*.md")):
            body = p.read_text(encoding="utf-8").split("## Answer", 1)[-1]
            texts.append(" ".join(body.split()))
        return " / ".join(t for t in texts if t)

    @staticmethod
    def _open_findings(ws: Path) -> list[dict]:
        path = ws / "inputs" / "open-findings.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    @staticmethod
    def _title(ws: Path) -> str:
        """The request's title, taken from the first heading of the request or the brief."""
        for name in ("request.md", "01-request-brief.md", "03-requirements.md", "04-design.md"):
            path = ws / "inputs" / name
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("# "):
                        title = re.sub(r"^#\s*(Request( brief)?|Requirements|Solution design)\s*:\s*", "", line)
                        title = title.lstrip("# ").strip()
                        return title[:1].upper() + title[1:]
        return "the request"

    # --------------------------------------------------------------- roles
    def _intake(self, ws: Path) -> dict:
        title = self._title(ws)
        answers = self._answers_text(ws)
        scope = f"As answered: {answers}" if answers else "Not confirmed yet (see open questions)."
        self._write(ws, "01-request-brief.md", f"""# Request brief: {title}

{MOCK_BANNER}
## The ask
The requester wants to deliver **{title}**, faster and with less manual effort than today.

## Outcome wanted
- Time from request to result cut from tens of minutes to a few minutes.
- No change to the editorial or compliance approval that exists today.

## Scope
- In scope: {scope}
- Out of scope: changes to existing contracts or third-party platforms.

## Assumptions
- The data the request needs already exists in current systems.
- Existing single sign-on is used for any new screens.

## Open questions
{"- None." if answers or not self.intake_blocks else "- Which competitions, channels or regions are in scope at launch?"}
""")
        if self.intake_blocks and not self._has_answers(ws):
            return {
                "status": "blocked",
                "summary": "Brief drafted; one question for the requester about scope.",
                "document": "01-request-brief.md",
                "blockers": [{
                    "question": "Which competitions, channels or regions are in scope at launch?",
                    "for": "requester",
                    "why_it_matters": "Rights and volumes depend on it, so it sets the size of the solution.",
                }],
            }
        return {"status": "complete", "summary": "Request brief ready for the value stage.",
                "document": "01-request-brief.md"}

    def _value(self, ws: Path) -> dict:
        title = self._title(ws)
        self._write(ws, "02-business-case.md", f"""# Business case: {title}

{MOCK_BANNER}
## Recommendation
**Proceed.** The likely case pays back within the first year; the result is most sensitive to adoption.

## Benefit and cost (ranges, mock figures)
| Item | Low | Likely | High |
| --- | --- | --- | --- |
| Annual benefit | £150k | £400k | £750k |
| One-off cost | £350k | £250k | £180k |
| Annual running cost | £90k | £60k | £40k |
| Payback (months) | 35 | 9 | 3 |

## Assumptions
| # | Assumption | Effect if wrong |
| --- | --- | --- |
| A1 | Teams adopt the new process within 3 months | Benefit falls towards the low case |
| A2 | Existing platforms can be reused | One-off cost rises |

## Most sensitive assumption
A1, adoption. Each month of delay moves payback by roughly one month.
""")
        return {"status": "complete", "summary": "Proceed; likely payback about 9 months (mock figures).",
                "document": "02-business-case.md"}

    def _analysis_lead(self, ws: Path) -> dict:
        title = self._title(ws)
        self._write(ws, "03-requirements.md", f"""# Requirements: {title}

{MOCK_BANNER}
## Functional requirements
| Id | Requirement | Priority |
| --- | --- | --- |
| FR-1 | Detect the events that trigger the workflow automatically | Must |
| FR-2 | Produce the output in the agreed format and branding | Must |
| FR-3 | Let an editor approve or reject each item in one click | Must |
| FR-4 | Keep a record of every approval | Should |

## Non-functional requirements
| Id | Area | Target |
| --- | --- | --- |
| NFR-1 | Latency | Item ready for approval within 2 minutes at p95 |
| NFR-2 | Availability | 99.9% during live events |
| NFR-3 | Security | Editors sign in with corporate single sign-on |

## Constraints
| Id | Constraint |
| --- | --- |
| C-1 | Nothing is published that the rights do not allow |

## Specialist input
Data, security, operations and commercial specialists reviewed the draft (mock).
""")
        return {"status": "complete", "summary": "4 functional, 3 non-functional requirements, 1 constraint.",
                "document": "03-requirements.md"}

    def _designer(self, ws: Path) -> dict:
        title = self._title(ws)
        found = self._open_findings(ws)
        changes = ""
        if found:
            changes = "\n## Changes in this round\n" + "\n".join(
                f"- {f['id']}: {f['suggested_fix']}" for f in found) + "\n"
        self._write(ws, "04-design.md", f"""# Solution design: {title}

{MOCK_BANNER}
## Summary
An event-driven service detects trigger events, produces the output, and places it in an approval
queue. A rights check blocks anything that may not be published.

## Components
| Component | Responsibility | Requirements served |
| --- | --- | --- |
| Event detector | Spots trigger events in the live data | FR-1, NFR-1 |
| Producer | Builds the output in the agreed format | FR-2 |
| Rights check | Blocks items the rights do not allow | C-1 |
| Approval queue | One-click approval for editors{", behind single sign-on" if found else ""} | FR-3, FR-4 |

## Decisions
### ADR-1: Event-driven processing
- Decision: process events from a queue so peaks do not slow approvals.
- Consequences: needs monitoring of queue depth.
{changes}""")
        responses = [{"finding_id": f["id"], "response": "fixed",
                      "note": f"Done: {f['suggested_fix']}"} for f in found]
        return {"status": "complete",
                "summary": "Design revised for open findings." if found else "Design written.",
                "document": "04-design.md", "responses": responses}

    def _reviewer(self, ws: Path, finding: dict | None) -> dict:
        own_open = self._open_findings(ws)
        if own_open:  # round 2+: everything the designer fixed is resolved
            return {"status": "complete", "summary": "Rechecked my findings: all fixed.", "document": None,
                    "verdict": "approve", "findings": [],
                    "resolved_finding_ids": [f["id"] for f in own_open]}
        findings = [finding] if finding else []
        return {"status": "complete",
                "summary": f"{len(findings)} finding(s)." if findings else "No findings.",
                "document": None,
                "verdict": "approve_with_changes" if findings else "approve", "findings": findings}

    def _reviewer_architecture(self, ws: Path) -> dict:
        return self._reviewer(ws, None)

    def _reviewer_security(self, ws: Path) -> dict:
        finding = None
        if self.security_finding:
            finding = {"severity": "major", "section": "Approval queue",
                       "issue": "The approval screen has no single sign-on.",
                       "evidence": "SEC-STD-01, NFR-3",
                       "suggested_fix": "Put the approval screen behind corporate single sign-on."}
        return self._reviewer(ws, finding)

    def _reviewer_operability(self, ws: Path) -> dict:
        finding = None
        if self.conflict:
            finding = {"severity": "major", "section": "Approval queue",
                       "issue": "A dedicated sign-on proxy doubles the running cost.",
                       "evidence": "OPS-STD-05", "suggested_fix": "Share the existing sign-on proxy."}
        return self._reviewer(ws, finding)

    def _chair(self, ws: Path) -> dict:
        open_findings = self._open_findings(ws)
        open_ids = [f["id"] for f in open_findings]
        conflicts = []
        if self.conflict and len(open_ids) >= 2:
            conflicts = [{"finding_ids": open_ids[:2],
                          "description": "The security control conflicts with the running-cost target."}]
        summary = (f"{len(open_ids)} open finding(s): " + ", ".join(open_ids)) if open_ids \
            else "No open findings. No conflicts. No business questions."
        envelope = {"status": "complete", "summary": summary, "document": None,
                    "chair": {"duplicates": [], "conflicts": conflicts,
                              "change_list": [f"{f['id']}: {f['suggested_fix']}" for f in open_findings]}}
        if self.chair_question and open_ids:
            envelope.update(status="blocked", blockers=[{
                "question": "Is single sign-on mandatory for freelance editors?",
                "for": "security", "why_it_matters": "Decides the identity design."}])
        return envelope

    def _story_writer(self, ws: Path) -> dict:
        backlog = {"epics": [
            {"id": "E1", "title": "Detection and production",
             "description": "MOCK DATA. Detect trigger events and produce the output.",
             "stories": [
                 {"id": "E1-S1", "title": "Ingest live events", "as_a": "editor",
                  "i_want": "trigger events detected automatically", "so_that": "I do not watch for them by hand",
                  "acceptance_criteria": ["Events are picked up within 5 seconds at p95",
                                          "Missed events raise an alert"],
                  "design_refs": ["ADR-1"]},
                 {"id": "E1-S2", "title": "Produce branded output", "as_a": "editor",
                  "i_want": "the output in our format", "so_that": "it is ready to publish",
                  "acceptance_criteria": ["Output matches the brand template"], "depends_on": ["E1-S1"]},
             ]},
            {"id": "E2", "title": "Approval and rights",
             "description": "MOCK DATA. Approve items and enforce rights.",
             "stories": [
                 {"id": "E2-S1", "title": "One-click approval queue", "as_a": "editor",
                  "i_want": "to approve or reject in one click", "so_that": "only cleared items go out",
                  "acceptance_criteria": ["Editors sign in with single sign-on",
                                          "Every decision is recorded"], "depends_on": ["E1-S2"]},
                 {"id": "E2-S2", "title": "Rights check", "as_a": "rights manager",
                  "i_want": "blocked items never published", "so_that": "we stay within our rights",
                  "acceptance_criteria": ["Items without rights cannot be approved"]},
             ]},
        ]}
        self._write(ws, "05-backlog.json", json.dumps(backlog, indent=2))
        return {"status": "complete", "summary": "2 epics, 4 stories (mock).", "document": "05-backlog.json"}
