"""Generate the Step Functions state machine (Amazon States Language) from catalogue/pipeline.yaml.

The generated flow matches agentpipe.local_pipeline step for step:
- each agent run is an ECS task that reports back through a task token (`.waitForTaskToken`)
- waiting for people is a Lambda that stores a task token; the respond API resumes the workflow
- reviewers run in parallel; code (the chair's `route`) decides what happens next
- disabled gates are left out of the state machine entirely

Run `python -m agentpipe.aws.asl` to print the definition with placeholders, as committed in
statemachine/pipeline.asl.json. The CDK stack calls `build_definition` with real values.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass

from ..config import PipelineConfig, Stage

AGENT_TIMEOUT_SECONDS = 60 * 60  # one agent run
PEOPLE_TIMEOUT_SECONDS = 30 * 24 * 60 * 60  # how long a case may wait for a person

# Retries for temporary failures only. BudgetExceeded, AgentLimitReached and AgentOutputError are
# never retried: running the agent again would spend the money again for the same result.
AGENT_RETRY = [
    {"ErrorEquals": ["ECS.AmazonECSException"], "IntervalSeconds": 30, "MaxAttempts": 3, "BackoffRate": 2.0},
    {"ErrorEquals": ["RunnerError"], "IntervalSeconds": 60, "MaxAttempts": 1, "BackoffRate": 2.0},
]
LAMBDA_RETRY = [{
    "ErrorEquals": ["Lambda.ServiceException", "Lambda.AWSLambdaException",
                    "Lambda.SdkClientException", "Lambda.TooManyRequestsException"],
    "IntervalSeconds": 2, "MaxAttempts": 3, "BackoffRate": 2.0,
}]


@dataclass(frozen=True)
class Refs:
    """Resource identifiers the definition points at (CDK tokens or placeholders)."""
    cluster_arn: str = "${ClusterArn}"
    task_definition_arn: str = "${TaskDefinitionArn}"
    subnet_ids: tuple[str, ...] = ("${SubnetId1}", "${SubnetId2}")
    security_group_ids: tuple[str, ...] = ("${SecurityGroupId}",)
    container_name: str = "runner"
    wait_function_arn: str = "${WaitForPeopleFunctionArn}"
    publish_function_arn: str = "${PublishBacklogFunctionArn}"
    finish_function_arn: str = "${FinishCaseFunctionArn}"


def _name(stage: Stage, step: str) -> str:
    return f"{stage.id}.{step}"


class _Builder:
    def __init__(self, cfg: PipelineConfig, refs: Refs):
        self.cfg = cfg
        self.refs = refs
        self.states: dict[str, dict] = {}

    # --------------------------------------------------------------- pieces
    def catch(self) -> list[dict]:
        return [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "Failed"}]

    def run_agent(self, role: str, stage: Stage, nxt: str, *, chair: bool = False,
                  result_path: str | None = "$.last", end: bool = False, catch: bool = True) -> dict:
        env = [
            {"Name": "TASK_TOKEN", "Value.$": "$$.Task.Token"},
            {"Name": "CASE_ID", "Value.$": "$.case_id"},
            {"Name": "STAGE", "Value": stage.id},
            {"Name": "ROLE", "Value": role},
            {"Name": "ROUND", "Value.$": "States.Format('{}', $.round)"},
            {"Name": "ROUND_LIMIT", "Value.$": "States.Format('{}', $.round_limit)"},
        ]
        if chair:
            env.append({"Name": "IS_CHAIR", "Value": "1"})
        state = {
            "Type": "Task",
            "Comment": f"Run the {role} agent",
            "Resource": "arn:aws:states:::ecs:runTask.waitForTaskToken",
            "Parameters": {
                "LaunchType": "FARGATE",
                "Cluster": self.refs.cluster_arn,
                "TaskDefinition": self.refs.task_definition_arn,
                "NetworkConfiguration": {
                    "AwsvpcConfiguration": {
                        "Subnets": list(self.refs.subnet_ids),
                        "SecurityGroups": list(self.refs.security_group_ids),
                        "AssignPublicIp": "DISABLED",
                    }
                },
                "Overrides": {"ContainerOverrides": [{"Name": self.refs.container_name, "Environment": env}]},
            },
            "TimeoutSeconds": AGENT_TIMEOUT_SECONDS,
            "ResultPath": result_path,
            "Retry": AGENT_RETRY,
        }
        if catch:  # inside a Parallel branch, errors go to the Parallel state's own Catch
            state["Catch"] = self.catch()
        if end:
            state["End"] = True
        else:
            state["Next"] = nxt
        return state

    def wait_for_people(self, kind: str, stage: Stage, nxt: str, gate: str | None = None,
                        approver: str | None = None, result_path: str = "$.response") -> dict:
        payload = {"case_id.$": "$.case_id", "kind": kind, "stage": stage.id,
                   "task_token.$": "$$.Task.Token"}
        if gate:
            payload["gate"] = gate
        if approver:
            payload["approver"] = approver
        return {
            "Type": "Task",
            "Comment": f"Wait for people: {kind}",
            "Resource": "arn:aws:states:::lambda:invoke.waitForTaskToken",
            "Parameters": {"FunctionName": self.refs.wait_function_arn, "Payload": payload},
            "TimeoutSeconds": PEOPLE_TIMEOUT_SECONDS,
            "ResultPath": result_path,
            "Retry": LAMBDA_RETRY,
            "Next": nxt,
            "Catch": [
                {"ErrorEquals": ["States.Timeout"], "ResultPath": "$.error", "Next": "TimedOut"},
                *self.catch(),
            ],
        }

    @staticmethod
    def set_round(nxt: str, round_expr: str, limit_expr: str | None = None, limit: int | None = None) -> dict:
        params: dict = {"case_id.$": "$.case_id"}
        if round_expr.startswith("States.") or round_expr.startswith("$"):
            params["round.$"] = round_expr
        else:
            params["round"] = int(round_expr)
        if limit_expr:
            params["round_limit.$"] = limit_expr
        else:
            params["round_limit"] = limit
        return {"Type": "Pass", "Parameters": params, "Next": nxt}

    def decision(self, outcome_path: str, approve: str, send_back: str) -> dict:
        return {
            "Type": "Choice",
            "Choices": [
                {"Variable": outcome_path, "StringEquals": "approve", "Next": approve},
                {"Variable": outcome_path, "StringEquals": "send_back", "Next": send_back},
            ],
            "Default": "Rejected",
        }

    # --------------------------------------------------------------- stages
    def entry(self, stage: Stage | None) -> str:
        return _name(stage, "Start") if stage else "PublishBacklog"

    def after_stage(self, stage: Stage, next_entry: str, send_back: str) -> str:
        """Add the gate states if the gate is enabled; return the state to go to when the stage is done."""
        gate = self.cfg.gate_for(stage)
        if not gate:
            return next_entry
        self.states[_name(stage, "Gate")] = self.wait_for_people(
            "gate", stage, _name(stage, "GateOutcome"), gate=gate.id, approver=gate.approver,
            result_path="$.decision")
        self.states[_name(stage, "GateOutcome")] = self.decision("$.decision.outcome", next_entry, send_back)
        return _name(stage, "Gate")

    def agent_stage(self, stage: Stage, next_entry: str) -> None:
        n = lambda step: _name(stage, step)  # noqa: E731
        limit = self.cfg.limits.max_review_rounds
        self.states[n("Start")] = self.set_round(n("Run"), "1", limit=limit)
        done = self.after_stage(stage, next_entry, send_back=n("NextRound"))
        self.states[n("Run")] = self.run_agent(stage.role, stage, n("Blocked?"))
        self.states[n("Blocked?")] = {
            "Type": "Choice",
            "Choices": [{"Variable": "$.last.status", "StringEquals": "blocked", "Next": n("WaitAnswers")}],
            "Default": done,
        }
        self.states[n("WaitAnswers")] = self.wait_for_people("answers", stage, n("NextRound"))
        self.states[n("NextRound")] = self.set_round(n("Run"), "States.MathAdd($.round, 1)",
                                                     limit_expr="$.round_limit")

    def review_stage(self, stage: Stage, next_entry: str) -> None:
        n = lambda step: _name(stage, step)  # noqa: E731
        limit = self.cfg.limits.max_review_rounds
        self.states[n("Start")] = self.set_round(n("Designer"), "1", limit=limit)

        self.states[n("Designer")] = self.run_agent(stage.designer, stage, n("Designer.Blocked?"))
        self.states[n("Designer.Blocked?")] = {
            "Type": "Choice",
            "Choices": [{"Variable": "$.last.status", "StringEquals": "blocked",
                         "Next": n("Designer.WaitAnswers")}],
            "Default": n("Reviewers"),
        }
        # Designer blocked: same round once answered (there is nothing to review yet).
        self.states[n("Designer.WaitAnswers")] = self.wait_for_people("answers", stage, n("Designer"))

        branches = []
        for reviewer in stage.reviewers:
            branches.append({
                "StartAt": reviewer,
                "States": {reviewer: self.run_agent(reviewer, stage, "", result_path=None, end=True,
                                                    catch=False)},
            })
        self.states[n("Reviewers")] = {
            "Type": "Parallel",
            "Comment": "Reviewers work independently and cannot see each other's findings",
            "Branches": branches,
            "ResultPath": None,
            "Next": n("Chair"),
            "Catch": self.catch(),
        }
        self.states[n("Chair")] = self.run_agent(stage.chair, stage, n("Route"), chair=True)

        approved = self.after_stage(stage, next_entry, send_back=n("NextRound"))
        self.states[n("Route")] = {
            "Type": "Choice",
            "Comment": "The route is computed by code from the open findings, never by an agent",
            "Choices": [
                {"Variable": "$.last.route", "StringEquals": "approved", "Next": approved},
                {"Variable": "$.last.route", "StringEquals": "revise", "Next": n("NextRound")},
                {"Variable": "$.last.route", "StringEquals": "blocked", "Next": n("WaitAnswers")},
                {"Variable": "$.last.route", "StringEquals": "escalate", "Next": n("Escalation")},
            ],
            "Default": "Failed",
        }
        self.states[n("WaitAnswers")] = self.wait_for_people("answers", stage, n("NextRound"))
        self.states[n("NextRound")] = self.set_round(n("Designer"), "States.MathAdd($.round, 1)",
                                                     limit_expr="$.round_limit")
        self.states[n("Escalation")] = self.wait_for_people("escalation", stage, n("EscalationOutcome"),
                                                            approver="lead_architect", result_path="$.decision")
        # Approving an escalation is the design sign-off, so it skips the gate.
        self.states[n("EscalationOutcome")] = self.decision("$.decision.outcome", next_entry, n("Reopen"))
        # Sent back: a fresh set of review rounds, with the feedback as input.
        self.states[n("Reopen")] = {
            "Type": "Pass",
            "Parameters": {
                "case_id.$": "$.case_id",
                "round.$": "States.MathAdd($.round, 1)",
                "round_limit.$": f"States.MathAdd($.round, {limit})",
            },
            "Next": n("Designer"),
        }

    def build(self) -> dict:
        stages = list(self.cfg.stages)
        for i, stage in enumerate(stages):
            nxt = stages[i + 1] if i + 1 < len(stages) else None
            if stage.kind == "agent":
                self.agent_stage(stage, self.entry(nxt))
            else:
                self.review_stage(stage, self.entry(nxt))

        self.states["PublishBacklog"] = {
            "Type": "Task",
            "Resource": "arn:aws:states:::lambda:invoke",
            "Parameters": {"FunctionName": self.refs.publish_function_arn,
                           "Payload": {"case_id.$": "$.case_id"}},
            "ResultPath": None,
            "Retry": LAMBDA_RETRY,
            "Next": "Done",
            "Catch": self.catch(),
        }
        self.states["Done"] = {"Type": "Succeed"}
        for name, outcome in (("Rejected", "rejected"), ("TimedOut", "timed_out"), ("Failed", "failed")):
            self.states[name] = {
                "Type": "Task",
                "Resource": "arn:aws:states:::lambda:invoke",
                "Parameters": {"FunctionName": self.refs.finish_function_arn,
                               "Payload": {"case_id.$": "$.case_id", "outcome": outcome,
                                           "error.$": "$"}},
                "ResultPath": None,
                "Retry": LAMBDA_RETRY,
                "Next": "Succeeded" if outcome == "rejected" else "Stopped",
            }
        self.states["Succeeded"] = {"Type": "Succeed"}
        self.states["Stopped"] = {"Type": "Fail", "Error": "CaseStopped",
                                  "Cause": "The case failed or timed out; see the case record."}
        return {
            "Comment": "Multi-agent delivery pipeline, generated from catalogue/pipeline.yaml",
            "StartAt": self.entry(stages[0]),
            "States": self.states,
        }


def build_definition(cfg: PipelineConfig, refs: Refs | None = None) -> dict:
    return _Builder(cfg, refs or Refs()).build()


def main() -> int:
    from ..catalogue import default_catalogue_dir
    from ..config import load_pipeline

    cfg = load_pipeline(default_catalogue_dir() / "pipeline.yaml")
    json.dump(build_definition(cfg), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
