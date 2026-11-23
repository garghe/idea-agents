"""Running one agent with the Claude Agent SDK.

`AgentClient` is the small interface the runner depends on, so tests and the demo can swap in
`idea_agents.fake.ScriptedAgentClient` without calling Claude.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .catalogue import Role

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
PATH_KEYS = ("file_path", "path", "notebook_path")


@dataclass
class AgentResult:
    output: dict | None  # the parsed envelope, or None when the agent gave none
    text: str  # the final text answer, kept for debugging
    cost_usd: float
    turns: int
    session_id: str | None
    model: str
    error: str | None = None
    limit_hit: str | None = None  # "budget" or "turns" when the run was stopped by its cap


class AgentClient(Protocol):
    def run(
        self,
        role: Role,
        system_prompt: str,
        task: str,
        workspace: Path,
        schema: dict,
        resume: str | None = None,
        budget_usd: float | None = None,
    ) -> AgentResult: ...


def check_path(workspace: Path, tool_name: str, tool_input: dict) -> str | None:
    """Return a reason to deny the tool call, or None to allow it.

    Agents may read anything inside their workspace and write only inside workspace/output.
    """
    raw = next((tool_input[k] for k in PATH_KEYS if tool_input.get(k)), None)
    if raw is None:
        return None  # e.g. Glob or Grep without a path: they default to the workspace
    ws = workspace.resolve()
    p = Path(raw)
    target = (p if p.is_absolute() else ws / p).resolve()
    if tool_name in WRITE_TOOLS:
        out = (ws / "output").resolve()
        if target != out and out not in target.parents:
            return f"{tool_name} is only allowed inside {out}"
    elif target != ws and ws not in target.parents:
        return f"{tool_name} is only allowed inside {ws}"
    return None


def parse_envelope_text(text: str) -> dict | None:
    """Fallback when structured output is unavailable: take the last JSON object in the text."""
    if not text:
        return None
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    candidates = fenced or [text[text.find("{"): text.rfind("}") + 1]]
    for chunk in reversed(candidates):
        try:
            obj = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


class ClaudeAgentClient:
    """Runs a role with the Claude Agent SDK, in a workspace folder, with tool and path limits."""

    def __init__(self, use_bedrock: bool | None = None):
        if use_bedrock is None:
            use_bedrock = os.environ.get("IDEA_USE_BEDROCK") == "1"
        self.use_bedrock = use_bedrock

    def run(self, role, system_prompt, task, workspace, schema, resume=None, budget_usd=None) -> AgentResult:
        return asyncio.run(self._run(role, system_prompt, task, Path(workspace), schema, resume, budget_usd))

    def _options(self, role: Role, system_prompt: str, workspace: Path, schema: dict, resume,
                 budget_usd: float | None = None):
        from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, HookMatcher

        async def guard(input_data, tool_use_id, context):
            reason = check_path(workspace, input_data.get("tool_name", ""), input_data.get("tool_input") or {})
            if reason is None:
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

        agents = {
            s.name: AgentDefinition(
                description=s.description,
                prompt=s.prompt,
                tools=list(s.tools),
                model=s.model,
                maxTurns=s.max_turns,
                background=False,
            )
            for s in role.specialists
        }
        env = {"CLAUDE_CODE_USE_BEDROCK": "1"} if self.use_bedrock else {}
        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            model=role.model,
            tools=list(role.tools),
            allowed_tools=list(role.tools),
            permission_mode="dontAsk",  # anything not allowed is refused, never prompted
            setting_sources=[],  # ignore user/project settings on the host
            cwd=str(workspace),
            max_turns=role.max_turns,
            max_budget_usd=budget_usd if budget_usd is not None else role.budget_usd,
            agents=agents or None,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[guard])]},
            output_format={"type": "json_schema", "schema": schema},
            effort=role.effort,
            resume=resume,
            env=env,
        )

    async def _run(self, role, system_prompt, task, workspace, schema, resume, budget_usd=None) -> AgentResult:
        from claude_agent_sdk import ResultMessage, query

        options = self._options(role, system_prompt, workspace, schema, resume, budget_usd)
        result: ResultMessage | None = None
        async for message in query(prompt=task, options=options):
            if isinstance(message, ResultMessage):
                result = message
        if result is None:
            return AgentResult(None, "", 0.0, 0, None, role.model, error="agent returned no result")

        output = result.structured_output if isinstance(result.structured_output, dict) else None
        if output is None:
            output = parse_envelope_text(result.result or "")
        error = None
        limit_hit = {"error_max_budget_usd": "budget", "error_max_turns": "turns"}.get(result.subtype)
        if result.is_error or limit_hit:
            error = f"agent ended with {result.subtype}: {result.errors or result.result}"
        return AgentResult(
            output=output,
            text=result.result or "",
            cost_usd=float(result.total_cost_usd or 0.0),
            turns=int(result.num_turns or 0),
            session_id=result.session_id,
            model=role.model,
            error=error,
            limit_hit=limit_hit,
        )
