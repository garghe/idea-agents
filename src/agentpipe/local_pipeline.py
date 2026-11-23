"""The local orchestrator for the prototype.

It follows exactly the flow the AWS Step Functions state machine follows (both come from
catalogue/pipeline.yaml): stages in order, blockers pause the case until answered, gates pause it
until a person decides, and the design stage loops through the review panel.
Waiting costs nothing: the case is saved and `run` simply returns.
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from . import review
from .catalogue import Catalogue
from .config import Stage
from .runner import AgentOutputError, BudgetExceeded, StageRunner
from .store import (DONE, FAILED, NEW, REJECTED, RUNNING, WAITING_ANSWERS, WAITING_ESCALATION,
                    WAITING_GATE, CaseStore, now_iso)

log = logging.getLogger(__name__)

APPROVE, SEND_BACK, REJECT = "approve", "send_back", "reject"


class PipelineError(RuntimeError):
    pass


class LocalPipeline:
    def __init__(self, catalogue: Catalogue, store: CaseStore, runner: StageRunner,
                 notify: Callable[[str], None] | None = None):
        self.catalogue = catalogue
        self.cfg = catalogue.pipeline
        self.store = store
        self.runner = runner
        self.notify = notify or (lambda msg: log.info(msg))

    # ----------------------------------------------------------------- entry points
    def start(self, title: str, request_text: str, requester: str = "unknown") -> dict:
        case = self.store.create_case(title, request_text, requester,
                                      first_stage=self.cfg.first_stage().id,
                                      round_limit=self.cfg.limits.max_review_rounds)
        return self.run(case["id"])

    def run(self, case_id: str) -> dict:
        """Advance the case until it finishes or has to wait for a person."""
        case = self.store.get_case(case_id)
        if case["status"] not in (NEW, RUNNING):
            return case
        case["status"] = RUNNING
        self.store.put_case(case)
        try:
            while case["status"] == RUNNING:
                stage = self.cfg.stage(case["stage"])
                if stage.kind == "agent":
                    self._run_agent_stage(case, stage)
                else:
                    self._run_review_stage(case, stage)
        except (BudgetExceeded, AgentOutputError) as exc:
            case.update(status=FAILED, error=str(exc))
            self.store.put_case(case)
            self.notify(f"[{case_id}] stopped: {exc}")
        return case

    def answer(self, case_id: str, text: str, by: str = "unknown") -> dict:
        """Record answers to the open questions and carry on."""
        case = self.store.get_case(case_id)
        if case["status"] != WAITING_ANSWERS:
            raise PipelineError(f"case {case_id} is not waiting for answers (status: {case['status']})")
        questions = self.store.open_blockers(case_id)
        n = len(self.store.list_docs(case_id, "answers/")) + 1
        lines = [f"# Answers from {by} ({now_iso()})", ""]
        for q in questions:
            lines.append(f"- {q['id']} ({q['for']}): {q['question']}")
        lines += ["", "## Answer", "", text.strip(), ""]
        self.store.write_doc(case_id, f"answers/{n:02d}-{case['stage']}.md", "\n".join(lines))
        for q in questions:
            q.update(status="answered", answered_at=now_iso(), answered_by=by)
            self.store.put_blocker(case_id, q)
        if self.cfg.stage(case["stage"]).kind == "agent":
            case["round"] += 1  # the stage reruns with the answers; the review stage already moved on
        case.update(status=RUNNING, pending=None)
        self.store.put_case(case)
        return self.run(case_id)

    def decide(self, case_id: str, outcome: str, by: str = "unknown", comment: str = "") -> dict:
        """A person's decision at a gate or on an escalated design review."""
        if outcome not in (APPROVE, SEND_BACK, REJECT):
            raise PipelineError(f"outcome must be approve, send_back or reject, not {outcome!r}")
        case = self.store.get_case(case_id)
        if case["status"] not in (WAITING_GATE, WAITING_ESCALATION):
            raise PipelineError(f"case {case_id} has no decision pending (status: {case['status']})")
        pending = case["pending"]
        self.store.add_decision(case_id, {
            "kind": pending["kind"], "gate": pending.get("gate"), "stage": case["stage"],
            "outcome": outcome, "by": by, "comment": comment, "at": now_iso(),
        })
        stage = self.cfg.stage(case["stage"])
        case["pending"] = None
        if outcome == REJECT:
            case["status"] = REJECTED
            self.store.put_case(case)
            self.notify(f"[{case_id}] rejected by {by}")
            return case

        case["status"] = RUNNING
        if outcome == APPROVE:
            self._advance(case, stage)
        else:  # send back: the comment becomes input, the stage runs again
            n = len(self.store.list_docs(case_id, "answers/")) + 1
            self.store.write_doc(case_id, f"answers/{n:02d}-{stage.id}-feedback.md",
                                 f"# Feedback from {by} on {stage.id}\n\n{comment.strip()}\n")
            case["round"] += 1
            if pending["kind"] == "escalation":
                # A person reopened the review: give the panel a fresh set of rounds.
                case["round_limit"] = case["round"] + self.cfg.limits.max_review_rounds - 1
        self.store.put_case(case)
        return self.run(case_id)

    # ------------------------------------------------------------------- stages
    def _run_agent_stage(self, case: dict, stage: Stage) -> None:
        env = self.runner.run_role(case["id"], stage.id, stage.role, case["round"])
        self.notify(f"[{case['id']}] {stage.id}: {env['status']} - {env['summary']}")
        if env["status"] == "blocked":
            self._wait(case, WAITING_ANSWERS, {"kind": "answers", "stage": stage.id})
            return
        self._after_stage(case, stage)

    def _run_review_stage(self, case: dict, stage: Stage) -> None:
        cid = case["id"]
        while True:
            rnd = case["round"]
            env = self.runner.run_role(cid, stage.id, stage.designer, rnd)
            self.notify(f"[{cid}] {stage.id} round {rnd}: designer {env['status']}")
            if env["status"] == "blocked":
                self._wait(case, WAITING_ANSWERS, {"kind": "answers", "stage": stage.id})
                return
            for reviewer in stage.reviewers:  # in parallel on AWS
                r_env = self.runner.run_role(cid, stage.id, reviewer, rnd)
                self.notify(f"[{cid}] {stage.id} round {rnd}: {reviewer} {r_env.get('verdict')}")
            chair = self.runner.run_role(cid, stage.id, stage.chair, rnd, is_chair=True)
            route = chair["route"]
            self.notify(f"[{cid}] {stage.id} round {rnd}: route {route}")
            if route == review.APPROVED:
                self._after_stage(case, stage)
                return
            if route == review.BLOCKED:
                case["round"] = rnd + 1  # the designer revises once the answers are in
                self._wait(case, WAITING_ANSWERS, {"kind": "answers", "stage": stage.id})
                return
            if route == review.ESCALATE:
                self._wait(case, WAITING_ESCALATION, {"kind": "escalation", "stage": stage.id,
                                                      "open_findings": [f["id"] for f in self.store.open_findings(cid)]})
                return
            case["round"] = rnd + 1
            self.store.put_case(case)

    def _after_stage(self, case: dict, stage: Stage) -> None:
        gate = self.cfg.gate_for(stage)
        if gate:
            self._wait(case, WAITING_GATE, {"kind": "gate", "gate": gate.id, "stage": stage.id,
                                            "approver": gate.approver})
            return
        self._advance(case, stage)

    def _advance(self, case: dict, stage: Stage) -> None:
        nxt = self.cfg.next_stage(stage.id)
        if nxt is None:
            self._publish(case)
            return
        case.update(stage=nxt.id, round=1, round_limit=self.cfg.limits.max_review_rounds)
        self.store.put_case(case)

    def _wait(self, case: dict, status: str, pending: dict) -> None:
        case.update(status=status, pending=pending)
        self.store.put_case(case)
        self.notify(f"[{case['id']}] waiting: {json.dumps(pending)}")

    def _publish(self, case: dict) -> None:
        """Locally there is no Jira: the approved backlog is simply marked as released."""
        case.update(status=DONE, finished_at=now_iso())
        self.store.put_case(case)
        self.notify(f"[{case['id']}] done: backlog released")
