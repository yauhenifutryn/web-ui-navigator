from __future__ import annotations

from typing import Any

from marketplace_bot.llm.base import BaseLLMClient


class NullLLMClient(BaseLLMClient):
    def __init__(self, reason: str = "No LLM provider configured. Set GEMINI_API_KEY.") -> None:
        self.reason = reason

    async def extract_state(self, semantic_text: str) -> dict[str, Any]:
        raise RuntimeError(self.reason)

    async def generate_decisions(self, state: dict[str, Any], mode: str) -> tuple[str, list[dict[str, Any]]]:
        raise RuntimeError(self.reason)

    async def index_context(self, goal: Any, observation: Any, domain_pack: Any) -> dict[str, Any]:
        raise RuntimeError(self.reason)

    async def review_workflow(
        self,
        goal: Any,
        observation: Any,
        domain_pack: Any,
        indexed_context: dict[str, Any] | None = None,
        strategic_summary: str = "",
    ) -> dict[str, Any]:
        raise RuntimeError(self.reason)

    async def plan_actions(
        self,
        goal: Any,
        observation: Any,
        domain_pack: Any,
        indexed_context: dict[str, Any] | None = None,
        strategic_summary: str = "",
    ) -> dict[str, Any]:
        raise RuntimeError(self.reason)
