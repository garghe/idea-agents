"""The case file: documents plus records (case status, agent runs, findings, blockers, decisions).

`CaseStore` is the interface. `LocalCaseStore` keeps everything in a folder per case, for the
prototype. `idea_agents.aws.store.AwsCaseStore` keeps documents in S3 and records in DynamoDB.
"""

from __future__ import annotations

import json
import re
import secrets
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

_DOC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?$")

# Case statuses
NEW = "new"
RUNNING = "running"
WAITING_ANSWERS = "waiting_answers"
WAITING_GATE = "waiting_gate"
WAITING_ESCALATION = "waiting_escalation"
DONE = "done"
REJECTED = "rejected"
FAILED = "failed"
WAITING = {WAITING_ANSWERS, WAITING_GATE, WAITING_ESCALATION}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_case_id() -> str:
    return f"C-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(3)}"


def check_doc_name(name: str) -> str:
    """Document names are flat files or one folder deep (answers/01.md); nothing else."""
    if not _DOC_NAME.match(name) or ".." in name:
        raise ValueError(f"invalid document name: {name!r}")
    return name


class CaseStore(ABC):
    # --- case -------------------------------------------------------------
    @abstractmethod
    def put_case(self, case: dict) -> None: ...

    @abstractmethod
    def get_case(self, case_id: str) -> dict: ...

    @abstractmethod
    def list_cases(self) -> list[dict]: ...

    # --- documents -------------------------------------------------------------
    @abstractmethod
    def write_doc(self, case_id: str, name: str, content: str) -> None:
        """Write a document. Every earlier version must stay retrievable."""

    @abstractmethod
    def read_doc(self, case_id: str, name: str) -> str | None: ...

    @abstractmethod
    def list_docs(self, case_id: str, prefix: str = "") -> list[str]: ...

    # --- records ---------------------------------------------------------------
    @abstractmethod
    def add_run(self, case_id: str, run: dict) -> None: ...

    @abstractmethod
    def list_runs(self, case_id: str) -> list[dict]: ...

    @abstractmethod
    def put_finding(self, case_id: str, finding: dict) -> None: ...

    @abstractmethod
    def list_findings(self, case_id: str) -> list[dict]: ...

    @abstractmethod
    def put_blocker(self, case_id: str, blocker: dict) -> None: ...

    @abstractmethod
    def list_blockers(self, case_id: str) -> list[dict]: ...

    @abstractmethod
    def add_decision(self, case_id: str, decision: dict) -> None: ...

    @abstractmethod
    def list_decisions(self, case_id: str) -> list[dict]: ...

    # --- who wrote each document version (for the portal's document list) --------
    @abstractmethod
    def add_doc_version(self, case_id: str, version: dict) -> None: ...

    @abstractmethod
    def list_doc_versions(self, case_id: str) -> list[dict]: ...

    def save_doc(self, case_id: str, name: str, content: str, *, by: str, kind: str, stage: str | None) -> None:
        """Write a document and record who wrote this version, when, and for which stage.

        kind: "person" (request, answers, feedback, reopen comments), "agent" or "system".
        """
        self.write_doc(case_id, name, content)
        self.add_doc_version(case_id, {"name": name, "by": by, "kind": kind, "stage": stage, "at": now_iso()})

    # --- attachments (files people attach; see idea_agents.attachments) ----------
    @abstractmethod
    def put_attachment(self, case_id: str, att: dict) -> None: ...

    @abstractmethod
    def list_attachments(self, case_id: str) -> list[dict]: ...

    @abstractmethod
    def put_attachment_bytes(self, case_id: str, att: dict, data: bytes) -> None: ...

    @abstractmethod
    def get_attachment_bytes(self, case_id: str, att: dict) -> bytes | None: ...

    def add_attachment(self, case_id: str, att: dict) -> dict:
        """Give the record the next id (A1, A2, ...) and save it. Stores that can race override this."""
        n = max((int(a["id"][1:]) for a in self.list_attachments(case_id)), default=0) + 1
        att = {**att, "id": f"A{n}"}
        self.put_attachment(case_id, att)
        return att

    def get_attachment(self, case_id: str, att_id: str) -> dict:
        for a in self.list_attachments(case_id):
            if a["id"] == att_id:
                return a
        raise KeyError(f"no such attachment: {att_id}")

    def claim_pending(self, case_id: str, token: str) -> bool:
        """Take the waiting task token before acting on it. Stores that can race override this."""
        return True

    def transition_status(self, case_id: str, from_status: str, to_status: str) -> bool:
        """Move a case from one status to another only if it is still in the first one.

        Stores that can race (DynamoDB) make this atomic, so two people cannot both reopen a case.
        """
        case = self.get_case(case_id)
        if case["status"] != from_status:
            return False
        case["status"] = to_status
        self.put_case(case)
        return True

    # --- shared helpers -------------------------------------------------------
    def create_case(self, title: str, request_text: str, requester: str = "unknown",
                    first_stage: str = "intake", round_limit: int = 3) -> dict:
        case = {
            "id": new_case_id(),
            "title": title,
            "requester": requester,
            "created_at": now_iso(),
            "status": NEW,
            "stage": first_stage,
            "round": 1,
            "round_limit": round_limit,
            "pending": None,
        }
        self.put_case(case)
        self.save_doc(case["id"], "request.md", request_text, by=requester, kind="person", stage=first_stage)
        return case

    def open_findings(self, case_id: str, reviewer: str | None = None) -> list[dict]:
        return [
            f for f in self.list_findings(case_id)
            if f["status"] == "open" and (reviewer is None or f["reviewer"] == reviewer)
        ]

    def open_blockers(self, case_id: str) -> list[dict]:
        return [b for b in self.list_blockers(case_id) if b["status"] == "open"]

    def total_cost(self, case_id: str) -> float:
        return round(sum(r.get("cost_usd") or 0.0 for r in self.list_runs(case_id)), 4)


class LocalCaseStore(CaseStore):
    """One folder per case: case.json, records.json, docs/ and history/ (old versions)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, case_id: str) -> Path:
        if not re.match(r"^[A-Za-z0-9-]+$", case_id):
            raise ValueError(f"invalid case id: {case_id!r}")
        return self.root / case_id

    def _records(self, case_id: str) -> dict:
        path = self._dir(case_id) / "records.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {"runs": [], "findings": {}, "blockers": {}, "decisions": [], "attachments": {}, "doc_versions": []}

    def _save_records(self, case_id: str, records: dict) -> None:
        path = self._dir(case_id) / "records.json"
        path.write_text(json.dumps(records, indent=2), encoding="utf-8")

    # case
    def put_case(self, case: dict) -> None:
        d = self._dir(case["id"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "case.json").write_text(json.dumps(case, indent=2), encoding="utf-8")

    def get_case(self, case_id: str) -> dict:
        path = self._dir(case_id) / "case.json"
        if not path.exists():
            raise KeyError(f"no such case: {case_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list_cases(self) -> list[dict]:
        cases = [json.loads(p.read_text(encoding="utf-8")) for p in self.root.glob("*/case.json")]
        return sorted(cases, key=lambda c: c["created_at"])

    # documents
    def write_doc(self, case_id: str, name: str, content: str) -> None:
        check_doc_name(name)
        path = self._dir(case_id) / "docs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            hist = self._dir(case_id) / "history"
            hist.mkdir(exist_ok=True)
            flat = name.replace("/", "__")
            n = len(list(hist.glob(f"{flat}.v*"))) + 1
            (hist / f"{flat}.v{n}").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(content, encoding="utf-8")

    def read_doc(self, case_id: str, name: str) -> str | None:
        check_doc_name(name)
        path = self._dir(case_id) / "docs" / name
        return path.read_text(encoding="utf-8") if path.exists() else None

    def list_docs(self, case_id: str, prefix: str = "") -> list[str]:
        base = self._dir(case_id) / "docs"
        if not base.exists():
            return []
        names = [p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file()]
        return sorted(n for n in names if n.startswith(prefix))

    # records
    def add_run(self, case_id: str, run: dict) -> None:
        rec = self._records(case_id)
        rec["runs"].append(run)
        self._save_records(case_id, rec)

    def list_runs(self, case_id: str) -> list[dict]:
        return self._records(case_id)["runs"]

    def put_finding(self, case_id: str, finding: dict) -> None:
        rec = self._records(case_id)
        rec["findings"][finding["id"]] = finding
        self._save_records(case_id, rec)

    def list_findings(self, case_id: str) -> list[dict]:
        return list(self._records(case_id)["findings"].values())

    def put_blocker(self, case_id: str, blocker: dict) -> None:
        rec = self._records(case_id)
        rec["blockers"][blocker["id"]] = blocker
        self._save_records(case_id, rec)

    def list_blockers(self, case_id: str) -> list[dict]:
        return list(self._records(case_id)["blockers"].values())

    def add_decision(self, case_id: str, decision: dict) -> None:
        rec = self._records(case_id)
        rec["decisions"].append(decision)
        self._save_records(case_id, rec)

    def list_decisions(self, case_id: str) -> list[dict]:
        return self._records(case_id)["decisions"]

    def add_doc_version(self, case_id: str, version: dict) -> None:
        rec = self._records(case_id)
        rec.setdefault("doc_versions", []).append(version)
        self._save_records(case_id, rec)

    def list_doc_versions(self, case_id: str) -> list[dict]:
        return self._records(case_id).get("doc_versions", [])

    # attachments: records in records.json, files in uploads/<id>-<file>
    def put_attachment(self, case_id: str, att: dict) -> None:
        rec = self._records(case_id)
        rec.setdefault("attachments", {})[att["id"]] = att
        self._save_records(case_id, rec)

    def list_attachments(self, case_id: str) -> list[dict]:
        atts = list(self._records(case_id).get("attachments", {}).values())
        return sorted(atts, key=lambda a: int(a["id"][1:]))

    def _upload_path(self, case_id: str, att: dict) -> Path:
        if not re.match(r"^A\d+$", att["id"]) or not re.match(r"^[A-Za-z0-9._-]+$", att["file"]):
            raise ValueError("invalid attachment")
        return self._dir(case_id) / "uploads" / f"{att['id']}-{att['file']}"

    def put_attachment_bytes(self, case_id: str, att: dict, data: bytes) -> None:
        path = self._upload_path(case_id, att)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get_attachment_bytes(self, case_id: str, att: dict) -> bytes | None:
        path = self._upload_path(case_id, att)
        return path.read_bytes() if path.exists() else None
