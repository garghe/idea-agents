"""Attachments: checks, text extraction, what agents get, waiting for scans, and the AWS path."""

import io
import json
import threading
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from idea_agents import api
from idea_agents import attachments as att
from idea_agents.config import pipeline_summary

LIMITS = {"max_files": 3, "max_file_mb": 1, "max_image_mb": 0.5, "max_total_mb": 2,
          "types": ["pdf", "docx", "html", "txt", "png"]}
OPEN = {"status": "running", "stage": "intake"}


# ------------------------------------------------------------------ sample files
def make_pdf(pages: list[str]) -> bytes:
    """A small valid PDF with one line of text per page."""
    n = len(pages)
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [" + " ".join(f"{4 + 2 * i} 0 R" for i in range(n)) + f"] /Count {n} >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET" if text else ""
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {5 + 2 * i} 0 R >>")
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def make_docx() -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Current workflow</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Editors cut clips </w:t></w:r><w:r><w:t>by hand.</w:t></w:r></w:p>'
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Step</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Minutes</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:p><w:r><w:t>Cut</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>12</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {w}><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


HTML = (b"<html><head><title>x</title><style>p{}</style></head><body><h2>Rights</h2>"
        b"<p>Premier League clips only.</p><script>steal()</script>"
        b"<table><tr><th>Region</th><th>Allowed</th></tr><tr><td>UK</td><td>Yes</td></tr></table></body></html>")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# ------------------------------------------------------------------ extraction
def test_html_keeps_structure_and_drops_scripts():
    text, pages, note = att.extract_text(HTML, "html")
    assert "## Rights" in text and "Premier League clips only." in text
    assert "| Region | Allowed |\n| --- | --- |\n| UK | Yes |" in text
    assert "steal" not in text and "p{}" not in text and pages is None and note == ""


def test_word_keeps_headings_and_tables():
    text, _, _ = att.extract_text(make_docx(), "docx")
    assert text.startswith("# Current workflow")
    assert "Editors cut clips by hand." in text and "| Cut | 12 |" in text and "| --- | --- |" in text


def test_pdf_text_is_split_by_page():
    text, pages, note = att.extract_text(make_pdf(["Budget is 200k", "Launch in March"]), "pdf")
    assert pages == 2 and note == ""
    assert text.index("## Page 1") < text.index("Budget is 200k") < text.index("## Page 2") < text.index("Launch in March")


def test_scanned_pdf_is_flagged():
    _, pages, note = att.extract_text(make_pdf([""]), "pdf")
    assert pages == 1 and note.startswith("No text layer")


def test_renamed_files_are_turned_away():
    for data, ext in ((b"MZ\x90\x00 not a pdf", "pdf"), (b"GIF89a...", "png"), (b"plain", "docx")):
        with pytest.raises(att.AttachmentError):
            att.extract_text(data, ext)


def test_file_checks():
    ok = [{"name": "brief.pdf", "size": 1000}]
    assert att.check_new_files(OPEN, [], ok, LIMITS) is None
    assert "not accepted" in att.check_new_files(OPEN, [], [{"name": "run.exe", "size": 5}], LIMITS)
    assert "larger than 0.5 MB" in att.check_new_files(OPEN, [], [{"name": "a.png", "size": 600_000}], LIMITS)
    assert "larger than 1 MB" in att.check_new_files(OPEN, [], [{"name": "a.pdf", "size": 2_000_000}], LIMITS)
    assert "at most 3 files" in att.check_new_files(OPEN, [{"status": "ready", "size": 1}] * 3, ok, LIMITS)
    many = [{"name": f"{i}.pdf", "size": 900_000} for i in range(3)]
    assert "add up to 2 MB" in att.check_new_files(OPEN, [], many, LIMITS)
    assert "finished" in att.check_new_files({"status": "done", "stage": "stories"}, [], ok, LIMITS)
    assert att.check_new_files({"status": "rejected", "stage": "value"}, [], ok, LIMITS) is None
    assert "size" in att.check_new_files(OPEN, [], [{"name": "a.pdf", "size": "big"}], LIMITS)
    # rejected files do not count towards the limits
    assert att.check_new_files(OPEN, [{"status": "rejected", "size": 10**9}] * 3, ok, LIMITS) is None


def test_safe_file_names():
    assert att.safe_filename("../../etc/My Report (v2).PDF") == "My-Report-v2.pdf"
    assert att.safe_filename("C:\\docs\\ünïcode.docx") == "n-code.docx"
    assert att.safe_filename("....") == "file"


# ------------------------------------------------------------------ store + ingest + agents
def attach(store, case, name, data, by="Priya"):
    a = att.plan_uploads(store, case, [{"name": name, "size": len(data)}], by)[0]
    store.put_attachment_bytes(case["id"], a, data)
    return att.ingest(store, case["id"], a["id"], data)


def test_ingest_writes_text_and_cites_the_source(store):
    case = store.create_case("Clips", "Make clips faster")
    a = attach(store, case, "rights.html", HTML)
    assert (a["id"], a["status"], a["text_doc"], a["has_text"]) == ("A1", "ready", "attachments/A1.md", True)
    doc = store.read_doc(case["id"], "attachments/A1.md")
    assert doc.startswith("# A1: rights.html") and "not instructions" in doc and "Premier League" in doc
    img = attach(store, case, "flow.png", PNG)
    assert (img["id"], img["kind"], img["text_doc"]) == ("A2", "image", None)
    bad = attach(store, case, "fake.pdf", b"not a pdf")
    assert bad["status"] == "rejected" and "not really a .pdf" in bad["reason"]
    assert att.ingest(store, case["id"], "A1", HTML)["status"] == "ready"  # a second upload changes nothing


def test_agents_get_ready_files_and_runs_record_them(pipeline_factory, store, tmp_path):
    pipeline, _ = pipeline_factory()
    case = pipeline.create("Clips", "Make clips faster", "Priya")
    attach(store, case, "rights.html", HTML)
    attach(store, case, "flow.png", PNG)
    attach(store, case, "scan.pdf", make_pdf([""]))
    attach(store, case, "broken.pdf", b"%PDF-1.4 truncated")  # rejected: never given to agents
    pipeline.runner.keep_workspaces = True
    pipeline.run(case["id"])

    brief = store.read_doc(case["id"], "01-request-brief.md")
    assert "[A1] rights.html" in brief and "[A2] flow.png" in brief
    intake = [r for r in store.list_runs(case["id"]) if r["role"] == "intake"][0]
    assert intake["attachments_seen"] == ["A1", "A2", "A3"]
    ws = next(p for p in tmp_path.iterdir() if p.name.startswith(case["id"] + "-intake-"))
    files = sorted(p.name for p in (ws / "inputs" / "attachments").iterdir())
    # text for documents; the file itself for images and scans
    assert files == ["A1.md", "A2-flow.png", "A3-scan.pdf", "A3.md", "index.md"]

    view = api.detail_view(store, case["id"], pipeline_summary(pipeline.cfg))
    rows = {r["id"]: r for r in view["attachments"]}
    assert rows["A1"]["seen_by"] == ["Intake agent"] and rows["A1"]["added_during"] == "Intake"
    assert rows["A4"]["status"] == "rejected" and rows["A4"]["seen_by"] == []
    assert not any(d.startswith("attachments/") for d in view["documents"])
    assert any("attached A1 rights.html" in a["text"] for a in view["activity"])
    assert any("A4 broken.pdf was turned away" in a["text"] for a in view["activity"])


def test_a_stage_waits_for_a_file_being_scanned(pipeline_factory, store):
    pipeline, _ = pipeline_factory()
    case = pipeline.create("Clips", "Make clips faster")
    pending = att.plan_uploads(store, case, [{"name": "rights.html", "size": len(HTML)}], "Priya")[0]
    waited = []

    def scan_finishes(seconds):  # the scan result arrives while the stage waits
        waited.append(seconds)
        store.put_attachment_bytes(case["id"], pending, HTML)
        att.ingest(store, case["id"], pending["id"], HTML, scan="no_threats_found")

    pipeline.runner.sleep = scan_finishes
    pipeline.run(case["id"])
    assert waited == [5]
    assert store.list_runs(case["id"])[0]["attachments_seen"] == ["A1"]


def test_a_stage_stops_waiting_after_the_limit(pipeline_factory, store, tmp_path):
    pipeline, _ = pipeline_factory()
    case = pipeline.create("Clips", "Make clips faster")
    pending = att.plan_uploads(store, case, [{"name": "lost.pdf", "size": 10}], "Priya")[0]
    pending["added_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    store.put_attachment(case["id"], pending)
    pipeline.runner.sleep = lambda s: pytest.fail("should not wait for an upload older than the limit")
    pipeline.runner.keep_workspaces = True
    pipeline.run(case["id"])
    ws = next(p for p in tmp_path.iterdir() if p.name.startswith(case["id"] + "-intake-"))
    assert "not available to you: A1 lost.pdf" in (ws / "inputs" / "attachments" / "index.md").read_text()


def test_a_file_added_at_a_gate_is_flagged_until_the_stage_reruns(pipeline_factory, store):
    pipeline, _ = pipeline_factory(intake_blocks=False)
    case = pipeline.start("Clips", "Make clips faster")
    summary = pipeline_summary(pipeline.cfg)
    assert store.get_case(case["id"])["status"] == "waiting_gate"  # scope check, after analysis
    attach(store, case, "rights.html", HTML)
    view = api.detail_view(store, case["id"], summary)
    assert view["unseen_at_gate"] == ["A1 rights.html"]
    pipeline.decide(case["id"], "send_back", by="PO", comment="Use the rights file")
    assert api.detail_view(store, case["id"], summary)["unseen_at_gate"] == []


# ------------------------------------------------------------------ local portal over HTTP
@pytest.fixture
def server(tmp_path):
    from idea_agents.portal_server import make_server
    srv = make_server(tmp_path / "cases", port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def http(base, path, body=None, method=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/octet-stream" if raw else "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def test_local_portal_starts_a_case_once_its_files_are_in(server):
    pdf = make_pdf(["Budget is 200k"])
    status, res = http(server, "/api/cases", {"title": "Clips", "request": "Faster clips", "attachments": [
        {"name": "rights.html", "size": len(HTML)}, {"name": "budget.pdf", "size": len(pdf)}]})
    assert status == 201 and res["status"] == "new" and len(res["uploads"]) == 2
    cid = res["case_id"]
    up = res["uploads"][0]
    assert up["method"] == "PUT" and up["headers"] == {"Content-Type": "text/html"}
    assert http(server, up["url"], raw=HTML, method="PUT")[1]["status"] == "ready"
    assert http(server, f"/api/cases/{cid}")[1]["case"]["status"] == "new"  # still waiting for budget.pdf
    assert http(server, res["uploads"][1]["url"], raw=pdf, method="PUT")[0] == 200
    view = http(server, f"/api/cases/{cid}")[1]
    assert view["case"]["status"] == "waiting_answers"
    assert [a["seen_by"] for a in view["attachments"]] == [["Intake agent"], ["Intake agent"]]
    assert view["attachments"][1]["pages"] == 1
    doc = http(server, f"/api/cases/{cid}?doc=attachments/A2.md")[1]["content"]
    assert "Budget is 200k" in doc

    # more files at any time; a second upload of the same file and oversize uploads are refused
    status, more = http(server, f"/api/cases/{cid}/attachments", {"files": [{"name": "note.txt", "size": 5}]})
    assert status == 201 and more["uploads"][0]["id"] == "A3"
    assert http(server, up["url"], raw=HTML, method="PUT")[0] == 409
    assert http(server, f"/api/cases/{cid}/attachments", {"files": [{"name": "x.exe", "size": 5}]})[0] == 400
    assert http(server, "/api/cases", {"title": "t", "request": "r", "attachments": [{"name": "x.exe", "size": 1}]})[0] == 400


# ------------------------------------------------------------------ AWS: store, upload links, ingest
@pytest.fixture
def aws(monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3
    for k, v in {"AWS_DEFAULT_REGION": "eu-west-2", "AWS_REGION": "eu-west-2", "AWS_ACCESS_KEY_ID": "test",
                 "AWS_SECRET_ACCESS_KEY": "test", "CASES_BUCKET": "cases", "CASES_TABLE": "records"}.items():
        monkeypatch.setenv(k, v)
    with moto.mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="cases", CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
        s3.put_bucket_versioning(Bucket="cases", VersioningConfiguration={"Status": "Enabled"})
        ddb = boto3.resource("dynamodb")
        ddb.create_table(TableName="records", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                                    {"AttributeName": "sk", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                               {"AttributeName": "sk", "AttributeType": "S"}])
        from idea_agents.aws.lambdas import common
        from idea_agents.aws.store import AwsCaseStore
        store = AwsCaseStore("cases", "records", s3=s3, dynamodb=ddb)
        monkeypatch.setattr(common, "_store", store)
        from conftest import REPO
        from idea_agents.catalogue import Catalogue
        monkeypatch.setenv("PIPELINE_JSON", json.dumps(pipeline_summary(Catalogue(REPO / "catalogue").pipeline)))
        yield store, s3


def scan_event(key, result="NO_THREATS_FOUND", bucket="cases", version=None):
    obj = {"bucketName": bucket, "objectKey": key}
    if version:
        obj["versionId"] = version
    return {"source": "aws.guardduty", "detail-type": "GuardDuty Malware Protection Object Scan Result",
            "detail": {"s3ObjectDetails": obj, "scanStatus": "COMPLETED",
                       "scanResultDetails": {"scanResultStatus": result}}}


def test_aws_ids_never_clash(aws):
    store, _ = aws
    case = store.create_case("Clips", "Faster clips")
    # another upload took A1 between our count and our write: we move on to A2
    store.table.put_item(Item={"pk": f"CASE#{case['id']}", "sk": "ATTACH#0001",
                               "data": json.dumps({"id": "A1", "name": "x", "status": "pending"})})
    store.list_attachments = lambda cid: []  # stale view, as in a race
    a = store.add_attachment(case["id"], {"name": "b.pdf", "file": "b.pdf"})
    assert a["id"] == "A2" and a["key"] == f"uploads/{case['id']}/A2/b.pdf"


def test_aws_attach_scan_and_ingest(aws):
    from idea_agents.aws.lambdas import attach, ingest
    store, s3 = aws
    case = store.create_case("Clips", "Faster clips")
    cid = case["id"]
    event = {"pathParameters": {"case_id": cid}, "requestContext": {"authorizer": {"claims": {"name": "Priya"}}},
             "body": json.dumps({"files": [{"name": "rights.html", "size": len(HTML)},
                                           {"name": "bad.pdf", "size": 20}, {"name": "odd.pdf", "size": 20}]})}
    resp = attach.handler(event, None)
    assert resp["statusCode"] == 201
    uploads = json.loads(resp["body"])["uploads"]
    assert uploads[0]["url"].startswith("https://cases.s3.eu-west-2.amazonaws.com/uploads/")
    assert "X-Amz-Signature" in uploads[0]["url"] and uploads[0]["headers"] == {"Content-Type": "text/html"}
    a1, a2, a3 = store.list_attachments(cid)
    assert (a1["status"], a1["added_by"]) == ("pending", "Priya")

    s3.put_object(Bucket="cases", Key=a1["key"], Body=HTML)
    assert ingest.handler(scan_event(a1["key"]), None) == {"status": "ready"}
    assert "Premier League" in store.read_doc(cid, "attachments/A1.md")
    assert store.get_attachment(cid, "A1")["scan"] == "no_threats_found"

    v = s3.put_object(Bucket="cases", Key=a2["key"], Body=b"%PDF evil")["VersionId"]
    assert ingest.handler(scan_event(a2["key"], "THREATS_FOUND", version=v), None) == {"status": "rejected"}
    assert "malware" in store.get_attachment(cid, "A2")["reason"]
    assert s3.list_object_versions(Bucket="cases", Prefix=a2["key"]).get("Versions") is None  # that version is gone

    s3.put_object(Bucket="cases", Key=a3["key"], Body=b"%PDF")
    assert ingest.handler(scan_event(a3["key"], "UNSUPPORTED"), None) == {"status": "rejected"}
    assert ingest.handler(scan_event("cases/other/key"), None) == {"ignored": True}
    assert ingest.handler(scan_event(a1["key"], bucket="someone-else"), None) == {"ignored": True}

    case = store.get_case(cid)
    case["status"] = "done"
    store.put_case(case)
    assert attach.handler(event, None)["statusCode"] == 400


def test_aws_start_case_with_files(aws, monkeypatch):
    from idea_agents.aws.lambdas import start_case
    store, _ = aws
    started = []

    class FakeSfn:
        def client(self, name, *a, **k):
            return self

        def start_execution(self, **kw):
            started.append(kw)

    monkeypatch.setattr(start_case, "boto3", FakeSfn())
    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:sm")
    body = {"title": "Clips", "request": "Faster clips", "attachments": [{"name": "rights.html", "size": 300}]}
    resp = start_case.handler({"body": json.dumps(body), "requestContext": {}}, None)
    data = json.loads(resp["body"])
    assert resp["statusCode"] == 201 and len(data["uploads"]) == 1 and len(started) == 1
    assert store.list_attachments(data["case_id"])[0]["status"] == "pending"  # recorded before the run starts
    bad = {**body, "attachments": [{"name": "x.exe", "size": 1}]}
    assert start_case.handler({"body": json.dumps(bad), "requestContext": {}}, None)["statusCode"] == 400
    assert len(started) == 1
