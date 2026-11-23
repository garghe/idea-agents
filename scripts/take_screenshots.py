"""Seed a few mock cases in a throwaway folder and capture the portal for the README.

    pip install playwright && playwright install chromium     (or pass --chrome /path/to/chrome)
    python scripts/take_screenshots.py

Everything runs on the scripted agents: no LLM calls.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from idea_agents.portal_server import make_server  # noqa: E402

REQUESTS = {
    "ads": ("Ad break markers for FAST channels", "Head of Ad Ops",
            "# Request: Ad break markers for FAST channels\n\nInsert ad break markers automatically in our free "
            "ad-supported channels so ad sales can sell the breaks."),
    "rail": ("Personalised home rail test", "Head of Product",
             "# Request: Personalised home rail test\n\nTest a personalised rail on the home screen for 5% of users."),
    "clips": ("Automatic highlight clips for social media", "Head of Digital Sport",
              (REPO / "examples" / "requests" / "highlights-clips.md").read_text(encoding="utf-8")),
    "news": ("Regional news opt-outs on streaming", "Head of News Distribution",
             "# Request: Regional news opt-outs on streaming\n\nLet streaming viewers see their own region's news "
             "bulletins instead of the national one, as they do on broadcast."),
}


def call(base: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(base + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def seed(base: str) -> dict[str, str]:
    ids = {}
    for key, (title, requester, text) in REQUESTS.items():
        ids[key] = call(base, "/api/cases", {"title": title, "request": text, "requester": requester})["case_id"]
        time.sleep(0.05)
    # Released: answer, then approve the three gates.
    call(base, f"/api/cases/{ids['ads']}/respond", {"answers": "Q-1: All FAST channels in the UK.", "by": "Head of Ad Ops"})
    for who in ("Product owner", "Lead architect", "Product owner"):
        call(base, f"/api/cases/{ids['ads']}/respond", {"outcome": "approve", "by": who})
    # Rejected at the scope check.
    call(base, f"/api/cases/{ids['rail']}/respond", {"answers": "Q-1: Home screen only, UK.", "by": "Head of Product"})
    call(base, f"/api/cases/{ids['rail']}/respond", {"outcome": "reject", "by": "Product owner",
                                                      "comment": "Covered by the personalisation roadmap already."})
    # Waiting for design sign-off.
    call(base, f"/api/cases/{ids['clips']}/respond", {"answers": "Q-1: Premier League and Champions League only.",
                                                       "by": "Head of Digital Sport"})
    call(base, f"/api/cases/{ids['clips']}/respond", {"outcome": "approve", "by": "Product owner"})
    # "news" stays waiting for answers at intake.
    return ids


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", help="path to a Chrome or Chromium binary")
    parser.add_argument("--out", default=str(REPO / "docs" / "screenshots"))
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        server = make_server(Path(tmp) / "cases", port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        ids = seed(base)
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=args.chrome, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
            page.route("**/fonts.googleapis.com/**", lambda route: route.abort())

            def shot(url: str, name: str, ready: str, click: str | None = None, full: bool = True) -> None:
                page.goto(base + url)
                page.wait_for_selector(ready)
                if click:
                    page.click(click)
                    page.wait_for_timeout(300)
                page.wait_for_timeout(300)
                page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(path=str(out / name), full_page=full)
                print("saved", out / name)

            shot("/#/cases", "case-list.png", "table.cases")
            shot(f"/#/cases/{ids['clips']}", "case-design-signoff.png", "#decide-h")
            shot(f"/#/cases/{ids['news']}", "case-questions.png", "#answer-form")
            shot(f"/#/cases/{ids['ads']}", "case-backlog.png", ".tab", click='.tab[data-doc="05-backlog.json"]')
            shot(f"/#/cases/{ids['rail']}", "case-rejected.png", "#reopen-h")
            shot("/#/new", "new-request.png", "#new-form", click="#fill-sample")
            shot("/#/roles", "roles.png", ".role-card", full=False)
            browser.close()
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
