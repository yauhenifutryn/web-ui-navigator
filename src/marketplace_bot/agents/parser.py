from __future__ import annotations

from typing import Any

from marketplace_bot.llm.base import BaseLLMClient
from marketplace_bot.logging_json import log_event
from marketplace_bot.state_store import StateStore, utc_now_iso


class SemanticParserAgent:
    def __init__(self, llm_client: BaseLLMClient, state_store: StateStore) -> None:
        self.llm_client = llm_client
        self.state_store = state_store

    async def parse_and_persist(self, scrape_payload: dict[str, Any] | str) -> dict[str, Any]:
        semantic_text = scrape_payload if isinstance(scrape_payload, str) else scrape_payload.get("semantic_text", "")
        navigation_items = []
        sections = []
        source_url = None
        quarter_range = {}
        completed_quarters: list[dict[str, Any]] = []
        editable_quarter = None
        if isinstance(scrape_payload, dict):
            navigation_items = scrape_payload.get("navigation_items", [])
            sections = scrape_payload.get("sections", [])
            source_url = scrape_payload.get("url")
            quarter_range = scrape_payload.get("quarter_range", {})
            completed_quarters = scrape_payload.get("completed_quarters", [])
            editable_quarter = scrape_payload.get("editable_quarter")

        parsed = await self.llm_client.extract_state(semantic_text)

        normalized = {
            "meta": {
                "status": "PARSED",
                "last_updated": utc_now_iso(),
                "source": "semantic_llm_parser",
                "source_url": source_url,
            },
            "quarter": parsed.get("quarter", {}),
            "data": parsed.get("data", {}),
            "errors": parsed.get("errors", []),
        }
        if not normalized.get("quarter") and editable_quarter:
            normalized["quarter"] = {"label": f"Quarter {editable_quarter}", "number": editable_quarter}

        normalized["data"].setdefault("navigation_items", navigation_items)
        normalized["data"].setdefault("section_count", len(sections))
        normalized["data"].setdefault("quarter_range", quarter_range)
        normalized["data"].setdefault("editable_quarter", editable_quarter)
        normalized["data"].setdefault(
            "completed_quarters",
            [
                {
                    "quarter_number": item.get("quarter_number"),
                    "editable": bool(item.get("editable", False)),
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "section_count": len(item.get("sections", [])),
                }
                for item in completed_quarters
            ],
        )
        normalized["data"].setdefault(
            "completed_quarters_detail",
            self._build_completed_quarters_detail(completed_quarters),
        )
        normalized["data"].setdefault("semantic_text_excerpt", self._clip_text(semantic_text, max_chars=12000))

        self.state_store.write_json(self.state_store.state_path, normalized)
        log_event("parser", "state_persisted", error_count=len(normalized["errors"]))
        return normalized

    def _build_completed_quarters_detail(self, completed_quarters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        detail: list[dict[str, Any]] = []
        for item in completed_quarters:
            sections = item.get("sections", [])
            section_previews = [
                {
                    "menu_item": section.get("menu_item"),
                    "semantic_text_excerpt": self._clip_text(
                        str(section.get("semantic_text", "")),
                        max_chars=3000,
                    ),
                }
                for section in sections
            ]
            detail.append(
                {
                    "quarter_number": item.get("quarter_number"),
                    "editable": bool(item.get("editable", False)),
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "page_text_excerpt": self._clip_text(str(item.get("semantic_text", "")), max_chars=6000),
                    "section_previews": section_previews,
                }
            )
        return detail

    @staticmethod
    def _clip_text(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return f"{text[:max_chars]}\n[TRUNCATED]"
