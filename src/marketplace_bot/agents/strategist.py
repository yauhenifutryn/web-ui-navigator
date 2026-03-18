from __future__ import annotations

import json
from typing import Any

from marketplace_bot.llm.base import BaseLLMClient
from marketplace_bot.logging_json import log_event
from marketplace_bot.state_store import StateStore, utc_now_iso


class StrategistAgent:
    def __init__(self, llm_client: BaseLLMClient, state_store: StateStore) -> None:
        self.llm_client = llm_client
        self.state_store = state_store

    async def generate_and_persist(self, state: dict[str, Any], mode: str) -> list[dict[str, Any]]:
        raw_output, decisions = await self.llm_client.generate_decisions(state, mode)
        if mode == "audit" and not decisions:
            retry_raw, retry_decisions = await self.llm_client.generate_decisions(state, "audit_retry")
            if retry_decisions:
                raw_output = f"{raw_output}\n\n--- RETRY ---\n\n{retry_raw}"
                decisions = retry_decisions
            else:
                fallback = self._build_audit_fallback(state)
                raw_output = (
                    f"{raw_output}\n\n--- RETRY ---\n\n{retry_raw}\n\n--- FALLBACK ---\n\n"
                    f"{json.dumps({'decisions': fallback}, ensure_ascii=False)}"
                )
                decisions = fallback

        normalized = [self._normalize_decision(item) for item in decisions]

        latest_payload = {
            "captured_at": utc_now_iso(),
            "mode": mode,
            "raw_output": raw_output,
            "decisions": normalized,
        }
        if mode == "audit" and not normalized:
            latest_payload["warning"] = (
                "No actionable recommendations returned. "
                "Run AUDIT MODE once more or use AUDIT FROM CACHE after opening additional decision tabs."
            )
        self.state_store.write_latest_decision(latest_payload)

        history_entry = {
            "captured_at": utc_now_iso(),
            "mode": mode,
            "quarter": state.get("quarter", {}).get("label"),
            "decision_count": len(normalized),
            "raw_output": raw_output,
            "decisions": normalized,
        }
        self.state_store.append_history(history_entry)

        log_event("strategist", "decisions_persisted", mode=mode, count=len(normalized))
        return normalized

    def _build_audit_fallback(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        data = state.get("data", {})
        completed = data.get("completed_quarters", [])
        editable_quarter = data.get("editable_quarter")
        fallback: list[dict[str, Any]] = []

        if isinstance(completed, list):
            for item in completed:
                quarter_number = item.get("quarter_number")
                if quarter_number is None:
                    continue
                if quarter_number == editable_quarter:
                    fallback.append(
                        {
                            "action": "audit_edit",
                            "quarter": str(quarter_number),
                            "apply_allowed": True,
                            "target": "Sales Channel",
                            "field": "Open Stores and Staffing",
                            "current_value": "Current values require manual review in Workspace.",
                            "recommended_value": "Increase presence only in top Market Potential cities and align Hire Sales People counts with store footprint.",
                            "recommendation": "Review Market Potential and Open Stores in Quarter workspace, then update city allocations before submit.",
                            "reasoning": "Current run captured enough structure for an edit pass but not stable numeric deltas for deterministic city-level automation.",
                            "implementation_steps": "Open Sales Channel > Market Potential, rank cities by demand, update Open Stores, then adjust Hire Sales People to match.",
                            "kpi": "Revenue per store, stockout rate, service coverage by city.",
                            "evidence": "Quarter navigation and workspace pages were successfully crawled, but structured numeric extraction was incomplete.",
                            "risk_level": "medium",
                            "priority": "high",
                            "expected_revenue_delta": 25000,
                            "confidence": 0.6,
                        }
                    )
                else:
                    fallback.append(
                        {
                            "action": "audit_insight",
                            "quarter": str(quarter_number),
                            "apply_allowed": False,
                            "reasoning": "Quarter is historical and not editable; use it as a baseline when validating current-quarter changes.",
                            "implementation_steps": "Compare quarter trend in Summary of Decisions and Finance against proposed Quarter edits.",
                            "kpi": "Quarter-over-quarter revenue and ending cash trend.",
                            "evidence": "Quarter data was included in completed_quarters crawl.",
                            "risk_level": "low",
                            "priority": "medium",
                            "expected_revenue_delta": 0,
                            "confidence": 0.7,
                        }
                    )

        if not fallback:
            fallback.append(
                {
                    "action": "audit_insight",
                    "apply_allowed": False,
                    "reasoning": "No completed-quarter data was available in parsed state for this audit run.",
                    "implementation_steps": "Run AUDIT MODE once from a workspace decision page, then use AUDIT FROM CACHE for fast iterations.",
                    "risk_level": "low",
                    "priority": "high",
                    "expected_revenue_delta": 0,
                    "confidence": 0.8,
                }
            )
        return fallback

    def _normalize_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": str(payload.get("action", "noop")),
            "quarter": payload.get("quarter"),
            "apply_allowed": payload.get("apply_allowed", payload.get("action") != "audit_insight"),
            "target": payload.get("target"),
            "city": payload.get("city"),
            "field": payload.get("field"),
            "value": payload.get("value"),
            "current_value": payload.get("current_value"),
            "recommended_value": payload.get("recommended_value"),
            "recommendation": payload.get("recommendation"),
            "reasoning": payload.get("reasoning", ""),
            "implementation_steps": payload.get("implementation_steps"),
            "kpi": payload.get("kpi"),
            "evidence": payload.get("evidence"),
            "risk_level": payload.get("risk_level"),
            "priority": payload.get("priority"),
            "expected_revenue_delta": payload.get("expected_revenue_delta", 0.0),
            "confidence": payload.get("confidence", 0.0),
        }
