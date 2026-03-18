import json
from pathlib import Path
import asyncio

from marketplace_bot.companion import LiveNavigatorCompanion
from marketplace_bot.goal_compiler import GoalCompiler
from marketplace_bot.navigator_models import ActionProposal, CreateSessionRequest, ExecuteResultPayload, GoalSpec, ObservationPacket, ReviewBatch, SessionMemory, SiteMemory
from marketplace_bot.planner import PlannerService
from marketplace_bot.safety import SafetyPolicy
from marketplace_bot.session_repository import HybridSessionRepository, LocalJsonSessionRepository
from marketplace_bot.site_memory_repository import HybridSiteMemoryRepository, LocalJsonSiteMemoryRepository
from marketplace_bot.state_store import StateStore, utc_now_iso


class FakePlannerLLM:
    async def index_context(self, goal, observation, domain_pack):
        return {
            "strategic_summary": "Strategic context ready.",
            "workflow_stage": "step",
            "next_focus": ["Continue"],
            "ui_map": [],
            "signals": ["button visible"],
        }

    async def plan_actions(self, goal, observation, domain_pack, indexed_context=None, strategic_summary=""):
        return {
            "memory_summary": "Session is focused on the current browser tab.",
            "live_advice": ["Check the current screen before execution."],
            "actions": [
                {
                    "action_id": "act_1",
                    "action": "click",
                    "reasoning": "Continue the visible workflow.",
                    "confidence": 0.9,
                    "target_text": "Continue",
                    "role": "button",
                },
                {
                    "action_id": "act_2",
                    "action": "suggest_only",
                    "reasoning": "Low-risk advisory note.",
                    "confidence": 0.5,
                    "target_text": "Notes",
                },
            ],
        }

    async def review_workflow(self, goal, observation, domain_pack, indexed_context=None, strategic_summary=""):
        return {
            "summary": "Prepared a review.",
            "current_focus": observation.page_title,
            "previous_period_summary": [],
            "rationale": ["Grounded in the current page."],
            "items": [],
            "actions": [],
            "apply_ready": False,
            "beta_warning": "Apply is beta. Manual application is safer.",
        }


def test_local_session_repository_normalizes_legacy_status_values(tmp_path) -> None:
    store = StateStore(tmp_path / "runtime")
    store.bootstrap()
    repo = LocalJsonSessionRepository(store.runtime_dir)
    created_at = utc_now_iso()
    payload = {
        "session_id": "sess_legacy",
        "project_name": "Legacy Demo",
        "goal": {
            "raw_goal": "Resume older session",
            "objective": "Resume older session",
            "created_at": created_at,
        },
        "current_site": "https://example.com/workflow",
        "domain_pack": "generic_web",
        "index_mode": "adaptive",
        "site_check_required": False,
        "last_indexed_at": created_at,
        "memory_summary": "Legacy summary",
        "live_advice": ["Legacy advice"],
        "pending_approvals": [],
        "artifacts": {},
        "action_history": [],
        "event_log": [],
        "checkpoints": [],
        "status": "planned",
        "created_at": created_at,
        "updated_at": created_at,
    }
    path = Path(store.runtime_dir) / "sessions" / "sess_legacy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = repo.get("sess_legacy")
    listed = repo.list()

    assert loaded is not None
    assert loaded.status == "index_summary_ready"
    assert listed
    assert listed[0].session_id == "sess_legacy"


def test_goal_compiler_detects_marketplace_domain() -> None:
    compiler = GoalCompiler()
    goal = compiler.compile("Audit the current Marketplace Simulation quarter and keep history.")
    assert goal.domain_pack == "marketplace_simulation"
    assert goal.safety_mode == "confirm_before_act"
    assert goal.mode == "complex_workspace"


def test_goal_compiler_defaults_generic_sites_to_review_only_mode() -> None:
    compiler = GoalCompiler()

    goal = compiler.compile("Find the cheapest MacBook Air on Apple and summarize the price differences.")

    assert goal.domain_pack == "generic_web"
    assert goal.mode == "review_only"


def test_safety_policy_respects_autonomous_mode() -> None:
    from marketplace_bot.navigator_models import ActionProposal

    policy = SafetyPolicy()
    proposal = ActionProposal(action_id="a1", action="click", reasoning="click continue", target_text="Continue")
    policy.apply(proposal, "autonomous")
    assert proposal.requires_confirmation is False

    sensitive = ActionProposal(action_id="a2", action="click", reasoning="submit form", target_text="Submit")
    policy.apply(sensitive, "autonomous")
    assert sensitive.requires_confirmation is True


def test_safety_policy_does_not_treat_partial_word_matches_as_high_risk() -> None:
    policy = SafetyPolicy()

    display_action = ActionProposal(action_id="a1", action="click", reasoning="open display settings", target_text="Display")
    repay_action = ActionProposal(action_id="a2", action="click", reasoning="review repay schedule", target_text="Repayment")
    payment_action = ActionProposal(action_id="a3", action="click", reasoning="open payment history", target_text="Payment")
    pay_action = ActionProposal(action_id="a4", action="click", reasoning="pay the invoice now", target_text="Pay")

    assert policy.classify(display_action) == "medium"
    assert policy.classify(repay_action) == "medium"
    assert policy.classify(payment_action) == "medium"
    assert policy.classify(pay_action) == "high"


def test_prepare_review_batch_disables_apply_for_confirmation_gated_actions(tmp_path) -> None:
    class ReviewActionLLM(FakePlannerLLM):
        async def review_workflow(self, goal, observation, domain_pack, indexed_context=None, strategic_summary=""):
            return {
                "summary": "Prepared a review with one executable action.",
                "current_focus": observation.page_title,
                "previous_period_summary": [],
                "rationale": ["Grounded in the current page."],
                "items": [],
                "actions": [
                    {
                        "action_id": "act_click",
                        "action": "click",
                        "reasoning": "Apply the current-page recommendation.",
                        "confidence": 0.83,
                        "target_text": "Continue",
                    }
                ],
                "apply_ready": True,
                "beta_warning": "Apply is beta. Manual application is safer.",
            }

    async def _run() -> None:
        store = StateStore(tmp_path / "runtime")
        store.bootstrap()
        repo = HybridSessionRepository(LocalJsonSessionRepository(store.runtime_dir))
        companion = LiveNavigatorCompanion(
            session_repository=repo,
            goal_compiler=GoalCompiler(),
            planner=PlannerService(ReviewActionLLM()),
        )

        session = await companion.create_session(
            CreateSessionRequest(
                goal="Review the current workspace and prepare edits.",
                project_name="Navigator Demo",
                domain_hint="generic_web",
                safety_mode="confirm_before_act",
            )
        )
        session.last_indexed_at = utc_now_iso()
        session.site_check_required = False
        repo.save(session)

        batch = await companion.prepare_review_batch(
            session.session_id,
            ObservationPacket(
                session_id=session.session_id,
                page_url="https://example.com/workflow",
                page_title="Workflow",
                visible_text_summary="Continue to the next step",
                dom_summary="button Continue",
                active_goal=session.goal.raw_goal,
                domain_pack=session.domain_pack,
                safety_mode=session.goal.safety_mode,
                captured_at=utc_now_iso(),
            ),
        )

        assert batch.actions
        assert batch.actions[0].action == "click"
        assert batch.actions[0].requires_confirmation is True
        assert batch.apply_ready is False

    asyncio.run(_run())


def test_companion_persists_session_and_plans(tmp_path) -> None:
    async def _run() -> None:
        store = StateStore(tmp_path / "runtime")
        store.bootstrap()
        repo = HybridSessionRepository(LocalJsonSessionRepository(store.runtime_dir))
        companion = LiveNavigatorCompanion(
            session_repository=repo,
            goal_compiler=GoalCompiler(),
            planner=PlannerService(FakePlannerLLM()),
        )

        session = await companion.create_session(
            CreateSessionRequest(
                goal="Help me navigate a web workflow.",
                project_name="Navigator Demo",
                safety_mode="confirm_before_act",
            )
        )
        observation = ObservationPacket(
            session_id=session.session_id,
            page_url="https://example.com",
            page_title="Example",
            visible_text_summary="Continue to next step",
            dom_summary="button Continue",
            active_goal=session.goal.raw_goal,
            domain_pack=session.domain_pack,
            safety_mode=session.goal.safety_mode,
            captured_at=utc_now_iso(),
        )
        companion.store_observation(observation)
        plan = await companion.plan(session.session_id)
        restored = companion.get_session(session.session_id)

        assert plan.actions
        assert restored is not None
        assert restored.pending_approvals
        assert restored.memory_summary

    asyncio.run(_run())


def test_companion_persists_explicit_site_index(tmp_path) -> None:
    async def _run() -> None:
        store = StateStore(tmp_path / "runtime")
        store.bootstrap()
        repo = HybridSessionRepository(LocalJsonSessionRepository(store.runtime_dir))
        companion = LiveNavigatorCompanion(
            session_repository=repo,
            goal_compiler=GoalCompiler(),
            planner=PlannerService(FakePlannerLLM()),
        )

        session = await companion.create_session(
            CreateSessionRequest(
                goal="Help me navigate the simulation and keep strategy context.",
                project_name="Navigator Demo",
                domain_hint="marketplace_simulation",
                safety_mode="confirm_before_act",
            )
        )
        observation = ObservationPacket(
            session_id=session.session_id,
            page_url="https://play.marketplace-simulation.com/mpl/web7/engine.php?tpl=student&resource=welcome-to-marketplace&quarter=3",
            page_title="Welcome to Marketplace",
            visible_text_summary="Quarter 3 Enter the Market Marketing Sales Channel",
            dom_summary="Quarter 3 Enter the Market Marketing Sales Channel",
            active_goal=session.goal.raw_goal,
            domain_pack=session.domain_pack,
            safety_mode=session.goal.safety_mode,
            browser_metadata={"site_index": {"navigation_items": ["Marketing", "Sales Channel"]}},
            captured_at=utc_now_iso(),
        )

        indexed_session = await companion.index_site(session.session_id, observation)

        assert indexed_session.strategic_summary
        assert indexed_session.indexed_context["site_index"]["navigation_items"] == ["Marketing", "Sales Channel"]
        assert indexed_session.last_indexed_at == observation.captured_at
        assert indexed_session.status == "index_summary_ready"

    asyncio.run(_run())


def test_companion_marks_marketplace_index_as_degraded_when_coverage_is_implausibly_thin(tmp_path) -> None:
    async def _run() -> None:
        store = StateStore(tmp_path / "runtime")
        store.bootstrap()
        repo = HybridSessionRepository(LocalJsonSessionRepository(store.runtime_dir))
        companion = LiveNavigatorCompanion(
            session_repository=repo,
            goal_compiler=GoalCompiler(),
            planner=PlannerService(FakePlannerLLM()),
        )

        session = await companion.create_session(
            CreateSessionRequest(
                goal="Review the Marketplace quarter and propose grounded edits.",
                project_name="Marketplace Demo",
                domain_hint="marketplace_simulation",
                safety_mode="confirm_before_act",
            )
        )
        observation = ObservationPacket(
            session_id=session.session_id,
            page_url="https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=pricing&quarter=4",
            page_title="Pricing",
            visible_text_summary="Quarter 4 Pricing Advertising Open Stores Finance",
            dom_summary="Quarter 4 Pricing Advertising Open Stores Finance",
            active_goal=session.goal.raw_goal,
            domain_pack=session.domain_pack,
            safety_mode=session.goal.safety_mode,
            browser_metadata={
                "site_index": {
                    "navigation_items": ["Pricing", "Advertising", "Open Stores", "Finance"],
                    "site_map": [
                        {
                            "url": "https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=pricing&quarter=4",
                            "title": "Pricing",
                            "section_count": 0,
                            "quarter_number": 4,
                            "editable": True,
                        }
                    ],
                },
                "site_check": {
                    "change_status": "changed",
                    "change_summary": "Structure checklist drift detected.",
                    "matched_nodes": 0,
                    "changed_nodes": [],
                    "new_nodes": [],
                    "removed_nodes": [],
                    "current_node_count": 1,
                    "strategy": "full",
                },
            },
            captured_at=utc_now_iso(),
        )

        indexed_session = await companion.index_site(session.session_id, observation)

        assert indexed_session.mode == "complex_workspace"
        assert indexed_session.degraded_reason
        assert "coverage" in indexed_session.degraded_reason.lower()
        assert indexed_session.review_ready is False
        assert indexed_session.coverage_summary["current_node_count"] == 1

    asyncio.run(_run())


def test_resuming_indexed_session_keeps_cached_summary_usable(tmp_path) -> None:
    async def _run() -> None:
        store = StateStore(tmp_path / "runtime")
        store.bootstrap()
        repo = HybridSessionRepository(LocalJsonSessionRepository(store.runtime_dir))
        companion = LiveNavigatorCompanion(
            session_repository=repo,
            goal_compiler=GoalCompiler(),
            planner=PlannerService(FakePlannerLLM()),
        )

        session = await companion.create_session(
            CreateSessionRequest(
                goal="Reuse older site knowledge safely.",
                project_name="Navigator Demo",
                safety_mode="confirm_before_act",
            )
        )
        indexed = await companion.index_site(
            session.session_id,
            ObservationPacket(
                session_id=session.session_id,
                page_url="https://example.com",
                page_title="Example",
                visible_text_summary="Continue",
                dom_summary="Continue",
                active_goal=session.goal.raw_goal,
                domain_pack=session.domain_pack,
                safety_mode=session.goal.safety_mode,
                browser_metadata={"site_index": {"navigation_items": ["Home"]}},
                captured_at=utc_now_iso(),
            ),
        )
        assert indexed.last_indexed_at

        resumed = companion.resume_session(session.session_id)

        assert resumed.site_check_required is False
        assert resumed.status == "index_summary_ready"
        assert "cached session restored" in resumed.site_check_summary.lower()
        assert "cached review" in resumed.memory_summary.lower()
        assert "refresh" in resumed.live_advice[0].lower()

    asyncio.run(_run())


def test_marketplace_full_crawl_manifest_prevents_false_degraded_coverage_when_sections_are_indexed(tmp_path) -> None:
    async def _run() -> None:
        store = StateStore(tmp_path / "runtime")
        store.bootstrap()
        repo = HybridSessionRepository(LocalJsonSessionRepository(store.runtime_dir))
        companion = LiveNavigatorCompanion(
            session_repository=repo,
            goal_compiler=GoalCompiler(),
            planner=PlannerService(FakePlannerLLM()),
        )

        session = await companion.create_session(
            CreateSessionRequest(
                goal="Review the Marketplace quarter and propose grounded edits.",
                project_name="Marketplace Demo",
                domain_hint="marketplace_simulation",
                safety_mode="confirm_before_act",
            )
        )
        observation = ObservationPacket(
            session_id=session.session_id,
            page_url="https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=sales-channel&quarter=4",
            page_title="Sales Channel",
            visible_text_summary="Quarter 4 Performance Report Marketing Sales Channel Finance",
            dom_summary="Quarter 4 Performance Report Marketing Sales Channel Finance",
            active_goal=session.goal.raw_goal,
            domain_pack=session.domain_pack,
            safety_mode=session.goal.safety_mode,
            browser_metadata={
                "site_index": {
                    "navigation_items": ["Performance Report", "Marketing", "Sales Channel", "Finance", "Cash Flow"],
                    "site_map": [],
                    "title": "Sales Channel",
                    "url": "https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=sales-channel&quarter=4",
                    "editable_quarter": 4,
                    "completed_quarters": [
                        {
                            "title": "Sales Channel",
                            "url": "https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=sales-channel&quarter=4",
                            "quarter_number": 4,
                            "editable": True,
                            "sections": [
                                {"menu_item": "Performance Report", "navigation_items": ["Market Share", "Cash Flow"]},
                                {"menu_item": "Marketing", "navigation_items": ["Pricing", "Advertising"]},
                            ],
                        },
                        {
                            "title": "Summary of Decisions",
                            "url": "https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=summary&quarter=3",
                            "quarter_number": 3,
                            "editable": False,
                            "sections": [],
                        },
                    ],
                    "sections": [
                        {"menu_item": "Performance Report", "navigation_items": ["Market Share", "Cash Flow"]},
                        {"menu_item": "Marketing", "navigation_items": ["Pricing", "Advertising"]},
                        {"menu_item": "Finance", "navigation_items": ["Cash Flow"]},
                    ],
                },
                "site_check": {
                    "change_status": "changed",
                    "change_summary": "Structure checklist drift detected.",
                    "matched_nodes": 0,
                    "changed_nodes": [],
                    "new_nodes": [],
                    "removed_nodes": [],
                    "current_node_count": 1,
                    "strategy": "full",
                },
            },
            captured_at=utc_now_iso(),
        )

        indexed_session = await companion.index_site(session.session_id, observation)

        assert indexed_session.coverage_summary["indexed_nodes"] >= 5
        assert indexed_session.coverage_summary["current_node_count"] >= 5
        assert indexed_session.degraded_reason == ""

    asyncio.run(_run())


def test_resuming_legacy_indexed_session_backfills_durable_site_memory(tmp_path) -> None:
    async def _run() -> None:
        store = StateStore(tmp_path / "runtime")
        store.bootstrap()
        repo = HybridSessionRepository(LocalJsonSessionRepository(store.runtime_dir))
        site_memory_repo = HybridSiteMemoryRepository(LocalJsonSiteMemoryRepository(store.runtime_dir))
        companion = LiveNavigatorCompanion(
            session_repository=repo,
            goal_compiler=GoalCompiler(),
            planner=PlannerService(FakePlannerLLM()),
            site_memory_repository=site_memory_repo,
        )

        session = await companion.create_session(
            CreateSessionRequest(
                goal="Reuse older indexed context safely.",
                project_name="Navigator Demo",
                domain_hint="marketplace_simulation",
                safety_mode="confirm_before_act",
            )
        )
        session.current_site = "https://play.marketplace-simulation.com/mpl/web7/engine.php?tpl=student&resource=welcome-to-marketplace&quarter=3"
        session.index_mode = "adaptive"
        session.last_indexed_at = utc_now_iso()
        session.indexed_context = {
            "site_index": {
                "editable_quarter": 3,
                "navigation_items": ["Marketing", "Sales Channel"],
                "site_map": [
                    {
                        "url": "https://play.marketplace-simulation.com/mpl/web7/engine.php?tpl=student&resource=welcome-to-marketplace&quarter=3",
                        "title": "Enter the Market",
                        "section_count": 11,
                        "quarter_number": 3,
                        "editable": True,
                    }
                ],
            }
        }
        session.strategic_summary = "Cached simulation index."
        repo.save(session)

        resumed = companion.resume_session(session.session_id)

        assert resumed.site_memory_key
        assert resumed.site_origin == "https://play.marketplace-simulation.com"
        loaded = site_memory_repo.get(resumed.site_memory_key)
        assert loaded is not None
        assert loaded.strategic_summary == "Cached simulation index."
        assert loaded.indexed_context["site_index"]["editable_quarter"] == 3
        assert loaded.index_mode == "advanced"

    asyncio.run(_run())


def test_resuming_marketplace_session_upgrades_existing_adaptive_site_memory(tmp_path) -> None:
    async def _run() -> None:
        store = StateStore(tmp_path / "runtime")
        store.bootstrap()
        repo = HybridSessionRepository(LocalJsonSessionRepository(store.runtime_dir))
        site_memory_repo = HybridSiteMemoryRepository(LocalJsonSiteMemoryRepository(store.runtime_dir))
        companion = LiveNavigatorCompanion(
            session_repository=repo,
            goal_compiler=GoalCompiler(),
            planner=PlannerService(FakePlannerLLM()),
            site_memory_repository=site_memory_repo,
        )

        session = await companion.create_session(
            CreateSessionRequest(
                goal="Reuse a saved Marketplace cache.",
                project_name="Navigator Demo",
                domain_hint="marketplace_simulation",
                safety_mode="confirm_before_act",
            )
        )
        session.index_mode = "adaptive"
        session.site_memory_key = "mem_existing"
        session.site_origin = "https://play.marketplace-simulation.com"
        session.indexed_context = {"site_index": {"editable_quarter": 3}}
        repo.save(session)
        site_memory_repo.save(
            SiteMemory(
                memory_key="mem_existing",
                site_origin="https://play.marketplace-simulation.com",
                domain_pack="marketplace_simulation",
                index_mode="adaptive",
                site_fingerprint="fp",
                structure_digest="dg",
                strategic_summary="Cached simulation index.",
                indexed_context={"site_index": {"editable_quarter": 3}},
                last_checked_at=utc_now_iso(),
                last_indexed_at=utc_now_iso(),
                change_status="unchecked",
                change_summary="Backfilled.",
            )
        )

        resumed = companion.resume_session(session.session_id)
        loaded = site_memory_repo.get("mem_existing")

        assert resumed.index_mode == "advanced"
        assert loaded is not None
        assert loaded.index_mode == "advanced"

    asyncio.run(_run())


def test_resume_keeps_cached_review_usable(tmp_path) -> None:
    async def _run() -> None:
        store = StateStore(tmp_path / "runtime")
        store.bootstrap()
        repo = HybridSessionRepository(LocalJsonSessionRepository(store.runtime_dir))
        companion = LiveNavigatorCompanion(
            session_repository=repo,
            goal_compiler=GoalCompiler(),
            planner=PlannerService(FakePlannerLLM()),
        )

        session = await companion.create_session(
            CreateSessionRequest(
                goal="Reuse a saved Marketplace session.",
                project_name="Navigator Demo",
                domain_hint="marketplace_simulation",
                safety_mode="confirm_before_act",
            )
        )
        session.last_indexed_at = utc_now_iso()
        session.site_check_required = False
        session.site_check_summary = "Structure fingerprint matches cached memory."
        session.index_summary = {
            "strategic_summary": "Indexed Marketplace workspace.",
            "site_check_summary": "Structure fingerprint matches cached memory.",
            "previous_period_summary": ["Quarter 1 setup done."],
            "current_focus": "Quarter 4 is editable.",
            "top_recommendations": ["Review stores."],
            "detected_changes": ["No major changes."],
        }
        session.review_batch = {
            "session_id": session.session_id,
            "summary": "Prepared cached review.",
            "rationale": ["Cached review is available."],
            "current_focus": "Quarter 4",
            "previous_period_summary": ["Quarter 1 setup done."],
            "items": [
                {
                    "item_id": "item_cached",
                    "page_hint": "Open Stores",
                    "field_label": "Store plan",
                    "recommendation": "Keep current stores unless new data says otherwise.",
                    "reasoning": "Cached review already exists.",
                    "priority": "medium",
                }
            ],
            "actions": [],
            "apply_ready": False,
            "beta_warning": "Apply is beta. Manual application is safer.",
        }
        session.live_advice = ["Cached review is ready."]
        session.status = "review_batch_ready"
        session.indexed_context = {"site_index": {"editable_quarter": 4}}
        repo.save(session)

        resumed = companion.resume_session(session.session_id)

        assert resumed.status == "review_batch_ready"
        assert resumed.site_check_required is False
        assert resumed.review_batch is not None
        assert "cached review" in resumed.memory_summary.lower()
        assert "refresh" in resumed.live_advice[0].lower()

    import asyncio
    asyncio.run(_run())


def test_resume_requiring_index_clears_stale_pending_actions(tmp_path) -> None:
    async def _run() -> None:
        store = StateStore(tmp_path / "runtime")
        store.bootstrap()
        repo = HybridSessionRepository(LocalJsonSessionRepository(store.runtime_dir))
        companion = LiveNavigatorCompanion(
            session_repository=repo,
            goal_compiler=GoalCompiler(),
            planner=PlannerService(FakePlannerLLM()),
        )

        session = await companion.create_session(
            CreateSessionRequest(
                goal="Resume a saved Marketplace session safely.",
                project_name="Navigator Demo",
                domain_hint="marketplace_simulation",
                safety_mode="confirm_before_act",
            )
        )
        session.last_indexed_at = utc_now_iso()
        session.site_check_required = False
        session.status = "live_advice"
        session.pending_approvals = [
            {
                "action_id": "act_stale",
                "action": "click",
                "reasoning": "Old live-advice action.",
                "target_text": "Continue",
                "requires_confirmation": False,
            }
        ]
        session.live_advice = ["Old live advice."]
        session.index_summary = None
        session.review_batch = None
        repo.save(session)

        resumed = companion.resume_session(session.session_id)

        assert resumed.status == "session_ready_to_index"
        assert resumed.site_check_required is True
        assert resumed.pending_approvals == []
        assert "run the site index" in resumed.live_advice[1].lower()

    asyncio.run(_run())


def test_live_advice_refresh_rebuilds_review_for_the_new_visible_page(tmp_path) -> None:
    async def _run() -> None:
        store = StateStore(tmp_path / "runtime")
        store.bootstrap()
        repo = HybridSessionRepository(LocalJsonSessionRepository(store.runtime_dir))
        companion = LiveNavigatorCompanion(
            session_repository=repo,
            goal_compiler=GoalCompiler(),
            planner=PlannerService(FakePlannerLLM()),
        )

        session = await companion.create_session(
            CreateSessionRequest(
                goal="Win the Marketplace simulation and optimize the current quarter.",
                project_name="Navigator Demo",
                domain_hint="marketplace_simulation",
                safety_mode="confirm_before_act",
            )
        )
        session.last_indexed_at = utc_now_iso()
        session.site_check_required = False
        session.indexed_context = {
            "site_index": {
                "editable_quarter": 4,
                "navigation_items": [
                    "Advertising",
                    "Buy Market Research",
                    "Open Stores",
                ],
            }
        }
        session.review_batch = {
            "session_id": session.session_id,
            "summary": "Prepared cached review.",
            "rationale": ["Cached review is available."],
            "current_focus": "Quarter 4",
            "previous_period_summary": [],
            "items": [
                {
                    "item_id": "item_advertising",
                    "page_hint": "Advertising",
                    "field_label": "Advertising plan",
                    "anchor_text": "Advertising",
                    "recommendation": "Keep ads aligned.",
                    "reasoning": "Cached review from the previous page.",
                    "priority": "medium",
                }
            ],
            "actions": [],
            "apply_ready": False,
            "beta_warning": "Apply is beta. Manual application is safer.",
        }
        repo.save(session)

        refreshed = await companion.refresh_live_advice_from_review(
            session.session_id,
            ObservationPacket(
                session_id=session.session_id,
                page_url="https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=buymarketresearch",
                page_title="Buy Market Research",
                visible_text_summary="Buy Market Research Modify Market Research Region Cost Buy NORAM 20,000 LATAM 20,000 MEA 20,000 APAC 20,000 Ending Cash 1,138,235",
                dom_summary="Market Research Region Cost Buy NORAM LATAM MEA APAC",
                active_goal=session.goal.raw_goal,
                domain_pack=session.domain_pack,
                safety_mode=session.goal.safety_mode,
                browser_metadata={
                    "checkbox_rows": [
                        {"row_text": "NORAM 20,000", "checked": True},
                        {"row_text": "LATAM 20,000", "checked": False},
                        {"row_text": "MEA 20,000", "checked": False},
                    ]
                },
                captured_at=utc_now_iso(),
            ),
        )

        titles = [item["title"] for item in refreshed.artifacts["inline_notes"]]
        assert "Advertising plan" not in titles
        assert any("Buy missing reports now" == title or "Research coverage" == title for title in titles)
        assert refreshed.review_batch is not None
        assert refreshed.review_batch.items[0].page_hint == "Buy Market Research"

    asyncio.run(_run())


def test_record_execution_removes_executed_actions_from_pending_batch(tmp_path) -> None:
    store = StateStore(tmp_path / "runtime")
    store.bootstrap()
    repo = HybridSessionRepository(LocalJsonSessionRepository(store.runtime_dir))
    companion = LiveNavigatorCompanion(
        session_repository=repo,
        goal_compiler=GoalCompiler(),
        planner=PlannerService(FakePlannerLLM()),
    )

    session = SessionMemory(
        session_id="sess_exec",
        project_name="Navigator Demo",
        goal=GoalSpec(
            raw_goal="Review the current workflow.",
            objective="Keep executable actions synchronized with the pending batch.",
            domain_pack="generic_web",
            safety_mode="confirm_before_act",
            created_at=utc_now_iso(),
        ),
        domain_pack="generic_web",
        pending_approvals=[
            ActionProposal(action_id="act_safe", action="wait_for", reasoning="Wait for a safe UI update.", requires_confirmation=False, safety_level="low"),
            ActionProposal(action_id="act_confirm", action="click", reasoning="Click a confirmation-gated control.", target_text="Apply", requires_confirmation=True, safety_level="medium"),
        ],
        review_batch=ReviewBatch(
            session_id="sess_exec",
            summary="Prepared one safe action and one confirmation-gated action.",
            rationale=["Keep pending state synchronized."],
            current_focus="Workflow",
            previous_period_summary=[],
            items=[],
            actions=[
                ActionProposal(action_id="act_safe", action="wait_for", reasoning="Wait for a safe UI update.", requires_confirmation=False, safety_level="low"),
                ActionProposal(action_id="act_confirm", action="click", reasoning="Click a confirmation-gated control.", target_text="Apply", requires_confirmation=True, safety_level="medium"),
            ],
            apply_ready=True,
            beta_warning="Apply is beta. Manual application is safer.",
        ),
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
    )
    repo.save(session)

    updated = companion.record_execution(
        ExecuteResultPayload(
            session_id="sess_exec",
            results=[{"action_id": "act_safe", "status": "executed"}],
        )
    )

    assert [action.action_id for action in updated.pending_approvals] == ["act_confirm"]
    assert updated.review_batch is not None
    assert [action.action_id for action in updated.review_batch.actions] == ["act_confirm"]
