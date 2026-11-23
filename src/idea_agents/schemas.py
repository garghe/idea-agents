"""Checks on agent outputs: the JSON schema plus the rules each kind of role must follow."""

from __future__ import annotations

from collections.abc import Iterable

from jsonschema import Draft202012Validator

from .catalogue import Role


def schema_errors(instance: object, schema: dict) -> list[str]:
    validator = Draft202012Validator(schema)
    errors = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in err.path) or "(root)"
        errors.append(f"{where}: {err.message}")
    return errors


def envelope_errors(
    envelope: object,
    schema: dict,
    role: Role,
    *,
    chair: bool = False,
    designer_open_ids: Iterable[str] = (),
) -> list[str]:
    """Return every reason the envelope is unacceptable (empty list when it is fine)."""
    if not isinstance(envelope, dict):
        return ["output is not a JSON object"]
    errors = schema_errors(envelope, schema)
    if errors:
        return errors

    status = envelope["status"]
    if status == "blocked" and not envelope.get("blockers"):
        errors.append("status is blocked but no blockers were given")

    if role.writes:
        if envelope.get("document") != role.writes:
            errors.append(f"document must be '{role.writes}'")
    elif envelope.get("document"):
        errors.append("this role must not write a document")

    if role.is_reviewer:
        if envelope.get("verdict") is None:
            errors.append("reviewers must give a verdict")
    elif envelope.get("findings"):
        errors.append("only reviewers may raise findings")

    if chair and status == "complete" and envelope.get("chair") is None:
        errors.append("the chair must return the 'chair' object")

    open_ids = set(designer_open_ids)
    if open_ids and status == "complete":
        answered = {r["finding_id"] for r in envelope.get("responses", [])}
        missing = sorted(open_ids - answered)
        if missing:
            errors.append(f"respond to every open finding; missing: {', '.join(missing)}")
    return errors


def backlog_errors(backlog: object, schema: dict) -> list[str]:
    errors = schema_errors(backlog, schema)
    if errors:
        return errors
    story_ids: set[str] = set()
    for epic in backlog["epics"]:
        for story in epic["stories"]:
            if not story["id"].startswith(epic["id"] + "-"):
                errors.append(f"story {story['id']} is not numbered under epic {epic['id']}")
            if story["id"] in story_ids:
                errors.append(f"duplicate story id {story['id']}")
            story_ids.add(story["id"])
    for epic in backlog["epics"]:
        for story in epic["stories"]:
            for dep in story.get("depends_on", []):
                if dep not in story_ids:
                    errors.append(f"story {story['id']} depends on unknown story {dep}")
    return errors
