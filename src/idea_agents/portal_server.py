"""The portal, run locally: `idea-agents portal` serves it on http://127.0.0.1:8080.

Locally the portal always uses the scripted agents: every document is mock data, no LLM is called,
and the page says so in a banner. The API it serves has the same routes and shapes as the AWS API:

  GET  /config.json                        mode, pipeline stages, sample request
  GET  /api/roles                          the agent roles: description, model, budget, limits
  GET  /api/cases                          list of cases with counts
  POST /api/cases                          start a case {title, request, requester, attachments?}
  GET  /api/cases/<id>                     case detail
  GET  /api/cases/<id>?doc=<name>          one document
  POST /api/cases/<id>/respond             {answers} or {outcome, comment}
  POST /api/cases/<id>/attachments         {files: [{name, size}]}: returns where to upload each
  PUT  /api/cases/<id>/attachments/<A1>/content   the file itself (on AWS: a signed S3 link instead)

A new case with files starts once they have all arrived. Locally files are not scanned for
malware (on AWS GuardDuty scans each one before it is used).
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import api
from . import attachments as att_mod
from .catalogue import Catalogue
from .config import pipeline_summary
from .fake import ScriptedAgentClient
from .local_pipeline import LocalPipeline
from .roles import roles_summary
from .runner import StageRunner
from .store import NEW, REJECTED, WAITING_ANSWERS, LocalCaseStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CASE_PATH = re.compile(r"^/api/cases/([A-Za-z0-9-]+)(/respond|/attachments)?$")
UPLOAD_PATH = re.compile(r"^/api/cases/([A-Za-z0-9-]+)/attachments/(A\d+)/content$")
MAX_BODY = 200_000


def default_portal_dir() -> Path:
    return REPO_ROOT / "portal"


class PortalApp:
    """The local backend: a LocalPipeline on scripted agents, behind one lock."""

    def __init__(self, cases_dir: Path, catalogue_dir: Path | None = None, portal_dir: Path | None = None):
        self.catalogue = Catalogue(catalogue_dir)
        self.store = LocalCaseStore(cases_dir)
        runner = StageRunner(self.catalogue, self.store, ScriptedAgentClient())
        self.pipeline = LocalPipeline(self.catalogue, self.store, runner)
        self.summary = pipeline_summary(self.catalogue.pipeline)
        self.portal_dir = (portal_dir or default_portal_dir()).resolve()
        self.lock = threading.Lock()

    def config(self) -> dict:
        sample = REPO_ROOT / "examples" / "requests" / "highlights-clips.md"
        sample_request = None
        if sample.exists():
            text = sample.read_text(encoding="utf-8")
            first = text.splitlines()[0].lstrip("# ").removeprefix("Request:").strip()
            sample_request = {"title": first[:1].upper() + first[1:], "request": text,
                              "requester": "Head of Digital Sport"}
        return {"mode": "mock", "apiBase": "/api", "pipeline": self.summary, "sampleRequest": sample_request}

    # Each handler returns (status, body)
    def roles(self):
        return 200, roles_summary(self.catalogue)

    def list_cases(self):
        return 200, api.list_view(self.store, self.summary)

    def create_case(self, data: dict):
        problem = api.check_new_case(data)
        if problem:
            return 400, {"error": problem}
        title, text = data["title"].strip()[:200], data["request"].strip()
        requester = (data.get("requester") or "Local user").strip()[:100]
        files = data.get("attachments") or []
        with self.lock:
            if not files:
                case = self.pipeline.start(title, text, requester)
                return 201, {"case_id": case["id"], "status": case["status"], "uploads": []}
            pseudo = {"status": NEW, "stage": self.catalogue.pipeline.first_stage().id}
            problem = att_mod.check_new_files(pseudo, [], files, self.summary["attachments"])
            if problem:
                return 400, {"error": problem}
            case = self.pipeline.create(title, text, requester)  # starts once the files are in
            uploads = self._uploads(case, att_mod.plan_uploads(self.store, case, files, requester))
        return 201, {"case_id": case["id"], "status": case["status"], "uploads": uploads}

    @staticmethod
    def _uploads(case: dict, atts: list[dict]) -> list[dict]:
        return [{"id": a["id"], "name": a["name"], "method": "PUT",
                 "url": f"/api/cases/{case['id']}/attachments/{a['id']}/content",
                 "headers": {"Content-Type": a["content_type"]}} for a in atts]

    def add_attachments(self, case_id: str, data: dict):
        with self.lock:
            try:
                case = self.store.get_case(case_id)
            except KeyError:
                return 404, {"error": f"no such case: {case_id}"}
            files = data.get("files")
            problem = att_mod.check_new_files(case, self.store.list_attachments(case_id), files,
                                              self.summary["attachments"])
            if problem:
                return 400, {"error": problem}
            by = (data.get("by") or "Local user").strip()[:100]
            return 201, {"uploads": self._uploads(case, att_mod.plan_uploads(self.store, case, files, by))}

    def upload(self, case_id: str, att_id: str, data: bytes):
        """Store and ingest one file (no malware scan locally). A new case starts when its last file is in."""
        with self.lock:
            try:
                case = self.store.get_case(case_id)
                att = self.store.get_attachment(case_id, att_id)
            except KeyError as exc:
                return 404, {"error": str(exc).strip("'")}
            if att["status"] != att_mod.PENDING:
                return 409, {"error": f"{att_id} has already been uploaded"}
            self.store.put_attachment_bytes(case_id, att, data)
            att = att_mod.ingest(self.store, case_id, att_id, data, scan="not_scanned (local)")
            if case["status"] == NEW and not any(a["status"] == att_mod.PENDING
                                                 for a in self.store.list_attachments(case_id)):
                self.pipeline.run(case_id)
        return 200, {"id": att["id"], "status": att["status"], "reason": att.get("reason")}

    def get_case(self, case_id: str, doc: str | None):
        try:
            if doc:
                text = self.store.read_doc(case_id, doc)
                if text is None:
                    return 404, {"error": f"no such document: {doc}"}
                return 200, {"name": doc, "content": text}
            return 200, api.detail_view(self.store, case_id, self.summary)
        except KeyError:
            return 404, {"error": f"no such case: {case_id}"}
        except ValueError as exc:
            return 400, {"error": str(exc)}

    def respond(self, case_id: str, data: dict):
        with self.lock:
            try:
                case = self.store.get_case(case_id)
            except KeyError:
                return 404, {"error": f"no such case: {case_id}"}
            problem = api.check_response(case, data)
            if problem:
                return (409 if "not waiting" in problem else 400), {"error": problem}
            by = (data.get("by") or "Local user").strip()[:100]
            if case["status"] == REJECTED:
                case = self.pipeline.reopen(case_id, by=by, comment=data["comment"])
                return 200, {"case_id": case_id, "outcome": "reopened", "stage": case["stage"]}
            if case["status"] == WAITING_ANSWERS:
                self.pipeline.answer(case_id, data["answers"], by=by)
                return 200, {"case_id": case_id, "outcome": "answered"}
            self.pipeline.decide(case_id, data["outcome"], by=by, comment=(data.get("comment") or "").strip())
            return 200, {"case_id": case_id, "outcome": data["outcome"]}


def make_handler(app: PortalApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "idea_agents-portal"

        def log_message(self, fmt, *args):  # quieter console
            pass

        def _send(self, status: int, body: dict | bytes, content_type: str = "application/json") -> None:
            data = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return None
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return None
            return data if isinstance(data, dict) else None

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/config.json":
                return self._send(200, app.config())
            if url.path == "/api/cases":
                return self._send(*app.list_cases())
            if url.path == "/api/roles":
                return self._send(*app.roles())
            m = CASE_PATH.match(url.path)
            if m and not m.group(2):
                doc = (parse_qs(url.query).get("doc") or [None])[0]
                return self._send(*app.get_case(m.group(1), doc))
            if url.path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            return self._static(url.path)

        def do_POST(self):
            url = urlparse(self.path)
            data = self._body()
            if data is None:
                return self._send(400, {"error": "body must be a JSON object"})
            if url.path == "/api/cases":
                return self._send(*app.create_case(data))
            m = CASE_PATH.match(url.path)
            if m and m.group(2) == "/respond":
                return self._send(*app.respond(m.group(1), data))
            if m and m.group(2) == "/attachments":
                return self._send(*app.add_attachments(m.group(1), data))
            return self._send(404, {"error": "not found"})

        def do_PUT(self):
            m = UPLOAD_PATH.match(urlparse(self.path).path)
            if not m:
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            cap = int(app.summary["attachments"]["max_file_mb"] * 1024 * 1024)
            if length <= 0 or length > cap:
                return self._send(413, {"error": "the file is empty or too large"})
            return self._send(*app.upload(m.group(1), m.group(2), self.rfile.read(length)))

        def _static(self, path: str):
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (app.portal_dir / rel).resolve()
            if app.portal_dir not in target.parents or not target.is_file():
                return self._send(404, {"error": "not found"})
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            return self._send(200, target.read_bytes(), ctype)

    return Handler


def make_server(cases_dir: Path, host: str = "127.0.0.1", port: int = 8080,
                catalogue_dir: Path | None = None) -> ThreadingHTTPServer:
    app = PortalApp(cases_dir, catalogue_dir)
    return ThreadingHTTPServer((host, port), make_handler(app))


def serve(cases_dir: Path, host: str = "127.0.0.1", port: int = 8080, catalogue_dir: Path | None = None) -> None:
    server = make_server(cases_dir, host, port, catalogue_dir)
    print(f"Portal running on http://{host}:{server.server_port}  (MOCK DATA: scripted agents, no LLM calls)")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
