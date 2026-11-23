"""Design review rules. The chair agent describes the situation; this code decides the route."""

from __future__ import annotations

APPROVED = "approved"  # no open blocker or major findings: go to design sign-off
REVISE = "revise"  # open findings, rounds left: designer revises
BLOCKED = "blocked"  # business questions: wait for answers, then designer revises
ESCALATE = "escalate"  # conflicts, or out of rounds: a person decides

BLOCKING_SEVERITIES = {"blocker", "major"}


def route(open_findings: list[dict], chair_envelope: dict, round_no: int, round_limit: int) -> str:
    """Decide what happens after a review round.

    Order matters: business questions first (nobody can fix them but the business), then
    approval, then conflicts and the round limit, which both need a person.
    """
    if chair_envelope.get("status") == "blocked":
        return BLOCKED
    blocking = [f for f in open_findings if f["severity"] in BLOCKING_SEVERITIES]
    if not blocking:
        return APPROVED
    chair = chair_envelope.get("chair") or {}
    if chair.get("conflicts"):
        return ESCALATE
    if round_no >= round_limit:
        return ESCALATE
    return REVISE


def next_finding_id(prefix: str, existing_ids: list[str]) -> str:
    numbers = [
        int(i.rsplit("-", 1)[1])
        for i in existing_ids
        if i.startswith(prefix + "-") and i.rsplit("-", 1)[1].isdigit()
    ]
    return f"{prefix}-{max(numbers, default=0) + 1}"
