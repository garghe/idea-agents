"""Command line for the local prototype.

  agentpipe new examples/requests/highlights-clips.md     start a case and run it
  agentpipe status C-20261123-ab12cd                      where the case is, what it waits for
  agentpipe answer C-... --text "Premier League only"     answer the open questions
  agentpipe decide C-... approve --by "Marco"             decide a gate or an escalation
  agentpipe list                                          all cases

Add --fake to run with scripted agents instead of Claude (no API calls, no cost).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .agent_client import ClaudeAgentClient
from .catalogue import Catalogue
from .fake import ScriptedAgentClient
from .local_pipeline import LocalPipeline
from .runner import StageRunner
from .store import LocalCaseStore


def build_pipeline(args) -> LocalPipeline:
    catalogue = Catalogue(Path(args.catalogue) if args.catalogue else None)
    store = LocalCaseStore(Path(args.cases_dir))
    client = ScriptedAgentClient() if args.fake else ClaudeAgentClient()
    runner = StageRunner(catalogue, store, client, keep_workspaces=args.keep_workspaces)
    return LocalPipeline(catalogue, store, runner, notify=lambda m: print(m, flush=True))


def title_from(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip().removeprefix("Request:").strip()
    return fallback


def print_status(pipe: LocalPipeline, case_id: str) -> None:
    store = pipe.store
    case = store.get_case(case_id)
    print(f"Case {case['id']}: {case['title']}")
    print(f"  status: {case['status']}   stage: {case['stage']}   round: {case['round']}")
    print(f"  cost so far: ${store.total_cost(case_id):.2f}")
    if case.get("error"):
        print(f"  error: {case['error']}")
    pending = case.get("pending")
    if pending:
        print(f"  waiting for: {json.dumps(pending)}")
    blockers = store.open_blockers(case_id)
    if blockers:
        print("  open questions:")
        for b in blockers:
            print(f"    {b['id']} for {b['for']}: {b['question']}")
            print(f"        why: {b['why_it_matters']}")
    findings = store.open_findings(case_id)
    if findings:
        print("  open findings:")
        for f in findings:
            print(f"    {f['id']} [{f['severity']}] {f['section']}: {f['issue']} ({f['evidence']})")
    docs = store.list_docs(case_id)
    if docs:
        print("  documents:")
        for d in docs:
            print(f"    {d}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentpipe", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases-dir", default="cases", help="where case folders live (default: ./cases)")
    parser.add_argument("--catalogue", help="catalogue folder (default: the repo's catalogue/)")
    parser.add_argument("--fake", action="store_true", help="use scripted agents instead of Claude")
    parser.add_argument("--keep-workspaces", action="store_true", help="keep agent working folders for debugging")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="start a case from a request file and run it")
    p_new.add_argument("request_file")
    p_new.add_argument("--title")
    p_new.add_argument("--requester", default="unknown")

    p_run = sub.add_parser("run", help="continue a case")
    p_run.add_argument("case_id")

    p_status = sub.add_parser("status", help="show a case")
    p_status.add_argument("case_id")

    p_answer = sub.add_parser("answer", help="answer the open questions")
    p_answer.add_argument("case_id")
    src = p_answer.add_mutually_exclusive_group(required=True)
    src.add_argument("--text")
    src.add_argument("--file")
    p_answer.add_argument("--by", default="unknown")

    p_decide = sub.add_parser("decide", help="decide a gate or an escalation")
    p_decide.add_argument("case_id")
    p_decide.add_argument("outcome", choices=["approve", "send_back", "reject"])
    p_decide.add_argument("--by", default="unknown")
    p_decide.add_argument("--comment", default="")

    sub.add_parser("list", help="list cases")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    pipe = build_pipeline(args)

    if args.command == "new":
        text = Path(args.request_file).read_text(encoding="utf-8")
        title = args.title or title_from(text, Path(args.request_file).stem)
        case = pipe.start(title, text, args.requester)
        print()
        print_status(pipe, case["id"])
    elif args.command == "run":
        pipe.run(args.case_id)
        print_status(pipe, args.case_id)
    elif args.command == "status":
        print_status(pipe, args.case_id)
    elif args.command == "answer":
        text = args.text if args.text is not None else Path(args.file).read_text(encoding="utf-8")
        pipe.answer(args.case_id, text, by=args.by)
        print()
        print_status(pipe, args.case_id)
    elif args.command == "decide":
        pipe.decide(args.case_id, args.outcome, by=args.by, comment=args.comment)
        print()
        print_status(pipe, args.case_id)
    elif args.command == "list":
        for c in pipe.store.list_cases():
            print(f"{c['id']}  {c['status']:<18} {c['stage']:<10} {c['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
