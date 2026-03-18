from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseLLMClient(ABC):
    @abstractmethod
    async def extract_state(self, semantic_text: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def generate_decisions(self, state: dict[str, Any], mode: str) -> tuple[str, list[dict[str, Any]]]:
        raise NotImplementedError

    @abstractmethod
    async def index_context(
        self,
        goal: Any,
        observation: Any,
        domain_pack: Any,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def review_workflow(
        self,
        goal: Any,
        observation: Any,
        domain_pack: Any,
        indexed_context: dict[str, Any] | None = None,
        strategic_summary: str = "",
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def plan_actions(
        self,
        goal: Any,
        observation: Any,
        domain_pack: Any,
        indexed_context: dict[str, Any] | None = None,
        strategic_summary: str = "",
    ) -> dict[str, Any]:
        raise NotImplementedError
