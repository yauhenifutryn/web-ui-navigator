import asyncio

from marketplace_bot.navigator_models import GoalSpec, ObservationPacket, SessionMemory
from marketplace_bot.planner import PlannerService


class FakeSplitLLM:
    def __init__(self) -> None:
        self.index_calls = 0
        self.plan_calls = 0
        self.last_plan_context = None

    async def index_context(self, goal, observation, domain_pack):
        self.index_calls += 1
        return {
            "strategic_summary": "This site is a multi-step flow. Stay on the primary path.",
            "workflow_stage": "entry",
            "next_focus": ["Continue the main flow"],
            "ui_map": [{"label": "Continue", "kind": "button"}],
            "signals": ["Primary CTA visible"],
        }

    async def plan_actions(self, goal, observation, domain_pack, indexed_context=None, strategic_summary=""):
        self.plan_calls += 1
        self.last_plan_context = {
            "indexed_context": indexed_context,
            "strategic_summary": strategic_summary,
        }
        return {
            "memory_summary": "Fast live plan generated.",
            "live_advice": ["Click Continue."],
            "actions": [
                {
                    "action_id": "act_continue",
                    "action": "click",
                    "reasoning": "Primary CTA from indexed context.",
                    "confidence": 0.9,
                    "target_text": "Continue",
                }
            ],
        }


def test_planner_indexes_once_then_reuses_cached_context():
    async def _run():
        llm = FakeSplitLLM()
        planner = PlannerService(llm)
        session = SessionMemory(
            session_id="sess_split",
            project_name="Demo",
            goal=GoalSpec(
                raw_goal="Finish the current web flow",
                objective="Finish the current web flow",
                safety_mode="confirm_before_act",
                created_at="2026-03-08T00:00:00Z",
            ),
            domain_pack="generic_web",
            created_at="2026-03-08T00:00:00Z",
            updated_at="2026-03-08T00:00:00Z",
        )
        observation = ObservationPacket(
            session_id="sess_split",
            screenshot_b64="ZmFrZQ==",
            page_url="https://example.com/step-1",
            page_title="Step 1",
            visible_text_summary="Continue to next step",
            dom_summary="button Continue",
            active_goal=session.goal.raw_goal,
            domain_pack=session.domain_pack,
            safety_mode=session.goal.safety_mode,
            captured_at="2026-03-08T00:00:00Z",
        )

        first = await planner.plan(session, observation)
        second = await planner.plan(session, observation)

        assert first.actions
        assert second.actions
        assert llm.index_calls == 1
        assert llm.plan_calls == 2
        assert session.strategic_summary == "This site is a multi-step flow. Stay on the primary path."
        assert session.indexed_context["workflow_stage"] == "entry"
        assert llm.last_plan_context["strategic_summary"] == session.strategic_summary
        assert llm.last_plan_context["indexed_context"]["signals"] == ["Primary CTA visible"]

    asyncio.run(_run())
