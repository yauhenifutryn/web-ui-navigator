import asyncio

import marketplace_bot.kill_switch as kill_switch
from marketplace_bot.agents.executor import SemanticExecutor
from marketplace_bot.state_store import StateStore


class FailingLocator:
    def get_by_role(self, _role: str, **_kwargs):
        return self

    def get_by_label(self, _label: str):
        return self

    async def click(self):
        raise RuntimeError("click failed")

    async def fill(self, _value: str):
        raise RuntimeError("fill failed")


class FailingPage:
    def get_by_role(self, _role: str, **_kwargs):
        return FailingLocator()

    def get_by_text(self, _text: str, **_kwargs):
        return FailingLocator()

    def get_by_label(self, _label: str, **_kwargs):
        return FailingLocator()

    async def evaluate(self, _script: str):
        return "DOM SNAPSHOT"


def test_semantic_executor_writes_error_dom_and_pauses(tmp_path) -> None:
    async def _run() -> None:
        store = StateStore(tmp_path / "runtime")
        store.bootstrap()
        executor = SemanticExecutor(state_store=store)
        kill_switch.reset_kill_switch()
        page = FailingPage()

        decisions = [{"action": "click", "target": "Modify"}]

        first = await executor.execute_decisions(page, decisions)
        second = await executor.execute_decisions(page, decisions)
        third = await executor.execute_decisions(page, decisions)

        assert first["paused"] is True
        assert second["paused"] is True
        assert third["paused"] is True
        assert kill_switch.CONSECUTIVE_EXECUTOR_FAILURES == 3
        assert kill_switch.KILL_SWITCH_ACTIVE is True
        assert "DOM SNAPSHOT" in store.error_dom_path.read_text(encoding="utf-8")

    asyncio.run(_run())
