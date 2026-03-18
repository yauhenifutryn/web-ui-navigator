from __future__ import annotations

from typing import Any

import marketplace_bot.kill_switch as kill_switch
from marketplace_bot.logging_json import log_event
from marketplace_bot.state_store import StateStore


class SemanticExecutor:
    def __init__(self, state_store: StateStore) -> None:
        self.state_store = state_store

    async def execute_decisions(self, page: Any, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        if kill_switch.KILL_SWITCH_ACTIVE:
            return {
                "ok": False,
                "paused": True,
                "reason": "kill_switch_active",
                "executed": 0,
            }

        executed = 0
        for decision in decisions:
            if kill_switch.KILL_SWITCH_ACTIVE:
                return {
                    "ok": False,
                    "paused": True,
                    "reason": "kill_switch_active",
                    "executed": executed,
                }

            try:
                await self._execute_single(page, decision)
                kill_switch.record_executor_success()
                executed += 1
            except Exception as exc:
                kill_switch.record_executor_failure()
                await self._dump_error_dom(page, decision, exc)
                log_event("executor", "decision_failed", error=str(exc), decision=decision)
                return {
                    "ok": False,
                    "paused": True,
                    "reason": str(exc),
                    "executed": executed,
                }

        return {
            "ok": True,
            "paused": False,
            "executed": executed,
        }

    async def _execute_single(self, page: Any, decision: dict[str, Any]) -> None:
        action = str(decision.get("action", "noop"))

        if action == "noop":
            return
        if action == "click":
            await self._click_by_target(page, str(decision.get("target", "")))
            return
        if action == "click_role":
            role = str(decision.get("role", "button"))
            name = str(decision.get("name", decision.get("target", "")))
            await page.get_by_role(role, name=name).click()
            return
        if action == "set_value":
            await self._set_value(page, decision)
            return
        if action == "fill_label":
            label = str(decision.get("field", decision.get("target", "")))
            value = "" if decision.get("value") is None else str(decision.get("value"))
            await page.get_by_label(label).fill(value)
            return
        if action == "navigate":
            target = str(decision.get("target", ""))
            await page.get_by_text(target, exact=False).click()
            return
        if action == "submit_quarter":
            target = str(decision.get("target", "Submit"))
            await page.get_by_text(target, exact=False).click()
            return

        raise RuntimeError(f"Unsupported action: {action}")

    async def _click_by_target(self, page: Any, target: str) -> None:
        if not target:
            raise RuntimeError("click action missing target")
        try:
            await page.get_by_role("button", name=target).click()
            return
        except Exception:
            await page.get_by_text(target, exact=False).click()

    async def _set_value(self, page: Any, decision: dict[str, Any]) -> None:
        city = decision.get("city")
        field = decision.get("field") or decision.get("target")
        value = decision.get("value")
        if field is None:
            raise RuntimeError("set_value action missing field/target")

        text_value = "" if value is None else str(value)

        if city:
            row = page.get_by_role("row", name=str(city))
            try:
                await row.get_by_label(str(field)).fill(text_value)
                return
            except Exception:
                pass

        await page.get_by_label(str(field)).fill(text_value)

    async def _dump_error_dom(self, page: Any, decision: dict[str, Any], exc: Exception) -> None:
        dom_text = ""
        try:
            dom_text = await page.evaluate("document.body.innerText")
        except Exception:
            dom_text = "<unable_to_capture_dom_text>"

        payload = (
            f"ERROR: {exc}\n"
            f"DECISION: {decision}\n\n"
            f"DOM_TEXT:\n{dom_text}\n"
        )
        self.state_store.write_text(self.state_store.error_dom_path, payload)
