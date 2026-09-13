from __future__ import annotations

import json
from collections.abc import Sequence

from .models import Decision
from .providers import DecisionProvider
from .tools import TOOL_DESCRIPTIONS


def build_decision_prompt(context: str, tool_names: Sequence[str]) -> str:
    tools = "\n".join(
        f"- {name}: {TOOL_DESCRIPTIONS.get(name, 'no description')}" for name in tool_names
    )
    schema = json.dumps(Decision.model_json_schema(), separators=(",", ":"))
    return (
        "You are the controller of GCAE, a reversible coding runtime. "
        "Reply with exactly one JSON object and no other text.\n"
        "Actions: execute_tool (set tool.name/tool.arguments), complete_semantic_step (declare "
        "the current semantic step finished), replan, finish_candidate, ask_user.\n"
        "Call complete_semantic_step once the current semantic step has produced its change or "
        "evidence; the runtime then validates and evaluates it.\n"
        f"Allowed tools:\n{tools}\n"
        f"Decision JSON schema: {schema}\n"
        f"Context:\n{context}"
    )


class Controller:
    def __init__(self, provider: DecisionProvider, tool_names: Sequence[str]) -> None:
        self.provider = provider
        self.tool_names = list(tool_names)

    def decide(self, context: str) -> Decision:
        return self.provider.decide(build_decision_prompt(context, self.tool_names))
