"""Entrypoint of the runner container (ECS Fargate).

Step Functions starts the task with these environment variables:
  TASK_TOKEN   token to report back with (the state machine waits for it)
  CASE_ID, STAGE, ROLE, ROUND, ROUND_LIMIT, IS_CHAIR ("1" for the review chair)
and the stack sets CASES_BUCKET, CASES_TABLE and AGENTPIPE_USE_BEDROCK=1.

The task runs one role, saves its output to the case file, and sends a small result back to the
state machine. It always reports, success or failure, so the workflow never hangs on a crash.
"""

from __future__ import annotations

import json
import logging
import os
import sys

import boto3

from ..agent_client import ClaudeAgentClient
from ..catalogue import Catalogue
from ..runner import AgentOutputError, BudgetExceeded, StageRunner
from ..store import RUNNING
from .store import AwsCaseStore

log = logging.getLogger("agentpipe.task")


def compact(envelope: dict) -> dict:
    """What the state machine needs to route; the full output stays in the case file."""
    return {
        "status": envelope["status"],
        "summary": envelope.get("summary", "")[:500],
        "route": envelope.get("route"),
        "role": envelope.get("role"),
        "round": envelope.get("round"),
        "blockers": len(envelope.get("blockers") or []),
        "findings": len(envelope.get("findings") or []),
    }


def run_from_env(env: dict, store=None, client=None, sfn=None) -> int:
    token = env["TASK_TOKEN"]
    sfn = sfn or boto3.client("stepfunctions")
    try:
        case_id, stage, role = env["CASE_ID"], env["STAGE"], env["ROLE"]
        round_no = int(env.get("ROUND", "1"))
        store = store or AwsCaseStore()
        case = store.get_case(case_id)
        case.update(status=RUNNING, stage=stage, round=round_no, pending=None,
                    round_limit=int(env.get("ROUND_LIMIT") or case.get("round_limit") or 3))
        store.put_case(case)

        runner = StageRunner(Catalogue(), store, client or ClaudeAgentClient())
        envelope = runner.run_role(case_id, stage, role, round_no, is_chair=env.get("IS_CHAIR") == "1")
        sfn.send_task_success(taskToken=token, output=json.dumps(compact(envelope)))
        log.info("%s %s done: %s", case_id, role, envelope["status"])
        return 0
    except BudgetExceeded as exc:
        sfn.send_task_failure(taskToken=token, error="BudgetExceeded", cause=str(exc)[:32000])
    except AgentOutputError as exc:
        sfn.send_task_failure(taskToken=token, error="AgentOutputError", cause=str(exc)[:32000])
    except Exception as exc:  # noqa: BLE001 - report every failure to the workflow
        log.exception("runner failed")
        sfn.send_task_failure(taskToken=token, error="RunnerError", cause=f"{type(exc).__name__}: {exc}"[:32000])
    return 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run_from_env(dict(os.environ))


if __name__ == "__main__":
    sys.exit(main())
