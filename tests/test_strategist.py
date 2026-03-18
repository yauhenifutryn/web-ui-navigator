import asyncio

from marketplace_bot.agents.strategist import StrategistAgent
from marketplace_bot.llm.base import BaseLLMClient
from marketplace_bot.state_store import StateStore


class FakeLLM(BaseLLMClient):
    def __init__(self) -> None:
        self.calls = 0

    async def extract_state(self, semantic_text: str) -> dict:
        return {}

    async def generate_decisions(self, state: dict, mode: str) -> tuple[str, list[dict]]:
        self.calls += 1
        if mode == "audit" and self.calls == 1:
            return ('{"decisions":[]}', [])
        return (
            '{"decisions":[{"action":"set_value","target":"Service","value":4}]}',
            [{"action": "set_value", "target": "Service", "value": 4}],
        )

    async def index_context(self, goal, observation, domain_pack) -> dict:
        return {
            "strategic_summary": "",
            "workflow_stage": "",
            "next_focus": [],
            "ui_map": [],
            "signals": [],
        }

    async def review_workflow(self, goal, observation, domain_pack, indexed_context=None, strategic_summary="") -> dict:
        return {"summary": "", "current_focus": "", "previous_period_summary": [], "rationale": [], "items": [], "actions": [], "apply_ready": False, "beta_warning": "Apply is beta. Manual application is safer."}

    async def plan_actions(self, goal, observation, domain_pack, indexed_context=None, strategic_summary="") -> dict:
        return {"memory_summary": "", "live_advice": [], "actions": []}


class FakeEmptyLLM(BaseLLMClient):
    async def extract_state(self, semantic_text: str) -> dict:
        return {}

    async def generate_decisions(self, state: dict, mode: str) -> tuple[str, list[dict]]:
        return ('{"decisions":[]}', [])

    async def index_context(self, goal, observation, domain_pack) -> dict:
        return {
            "strategic_summary": "",
            "workflow_stage": "",
            "next_focus": [],
            "ui_map": [],
            "signals": [],
        }

    async def review_workflow(self, goal, observation, domain_pack, indexed_context=None, strategic_summary="") -> dict:
        return {"summary": "", "current_focus": "", "previous_period_summary": [], "rationale": [], "items": [], "actions": [], "apply_ready": False, "beta_warning": "Apply is beta. Manual application is safer."}

    async def plan_actions(self, goal, observation, domain_pack, indexed_context=None, strategic_summary="") -> dict:
        return {"memory_summary": "", "live_advice": [], "actions": []}


def test_strategist_writes_latest_decision_and_history(tmp_path) -> None:
    async def _run() -> None:
        store = StateStore(tmp_path / "runtime")
        store.bootstrap()
        strategist = StrategistAgent(llm_client=FakeLLM(), state_store=store)

        decisions = await strategist.generate_and_persist(
            state={"quarter": {"label": "Quarter 3"}},
            mode="audit",
        )

        latest = store.read_json(store.latest_decision_path)
        history = store.read_json(store.history_path)

        assert decisions[0]["action"] == "set_value"
        assert "--- RETRY ---" in latest["raw_output"]
        assert "decisions" in latest["raw_output"]
        assert len(history["entries"]) == 1

    asyncio.run(_run())


def test_strategist_audit_fallback_when_llm_returns_empty(tmp_path) -> None:
    async def _run() -> None:
        store = StateStore(tmp_path / "runtime")
        store.bootstrap()
        strategist = StrategistAgent(llm_client=FakeEmptyLLM(), state_store=store)

        decisions = await strategist.generate_and_persist(
            state={
                "data": {
                    "editable_quarter": 3,
                    "completed_quarters": [
                        {"quarter_number": 1, "editable": False},
                        {"quarter_number": 2, "editable": False},
                        {"quarter_number": 3, "editable": True},
                    ],
                }
            },
            mode="audit",
        )

        latest = store.read_json(store.latest_decision_path)
        assert decisions
        assert any(item["action"] == "audit_edit" for item in decisions)
        assert "--- FALLBACK ---" in latest["raw_output"]

    asyncio.run(_run())
