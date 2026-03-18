import asyncio

from marketplace_bot.agents.parser import SemanticParserAgent
from marketplace_bot.llm.base import BaseLLMClient
from marketplace_bot.state_store import StateStore


class FakeLLM(BaseLLMClient):
    async def extract_state(self, semantic_text: str) -> dict:
        return {
            "meta": {"status": "WAITING", "last_updated": "2026-03-02T00:00:00Z"},
            "quarter": {"label": "Quarter 3"},
            "data": {
                "summary": {
                    "ending_cash": 1193809,
                    "budget": 110577,
                    "menu_items": ["Hire Sales People", "Submit"],
                }
            },
            "errors": [],
        }

    async def generate_decisions(self, state: dict, mode: str) -> tuple[str, list[dict]]:
        return "{}", []

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


def test_parser_persists_state_json(tmp_path) -> None:
    async def _run() -> None:
        store = StateStore(tmp_path / "runtime")
        store.bootstrap()
        agent = SemanticParserAgent(llm_client=FakeLLM(), state_store=store)

        result = await agent.parse_and_persist("Quarter 3 Ending Cash 1,193,809")

        persisted = store.read_json(store.state_path)
        assert result["quarter"]["label"] == "Quarter 3"
        assert persisted["data"]["summary"]["ending_cash"] == 1193809

    asyncio.run(_run())
