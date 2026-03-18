import asyncio
import logging

from marketplace_bot.navigator_models import GoalSpec, ObservationPacket, SessionMemory
from marketplace_bot.planner import PlannerService


class EmptyLLM:
    async def index_context(self, goal, observation, domain_pack):
        return {
            "strategic_summary": "Indexed the simulation workflow.",
            "workflow_stage": "test-market",
            "next_focus": ["Buy Market Research"],
            "ui_map": [],
            "signals": ["Research table visible"],
        }

    async def plan_actions(self, goal, observation, domain_pack, indexed_context=None, strategic_summary=""):
        return {"memory_summary": "", "live_advice": [], "actions": []}


def test_planner_fallback_generates_market_research_actions():
    planner = PlannerService(EmptyLLM())
    session = SessionMemory(
        session_id="sess_demo",
        project_name="Demo",
        goal=GoalSpec(
            raw_goal="Win the Marketplace simulation and optimize the current quarter.",
            objective="Act as a quarter-aware business simulation companion.",
            constraints=[],
            success_criteria=[],
            domain_pack="marketplace_simulation",
            safety_mode="confirm_before_act",
            created_at="2026-03-09T00:00:00Z",
        ),
        domain_pack="marketplace_simulation",
        strategic_summary="Indexed simulation workflow.",
        indexed_context={"site_index": {"navigation_items": ["Buy Market Research"]}},
        last_indexed_at="2026-03-09T00:00:00Z",
        created_at="2026-03-09T00:00:00Z",
        updated_at="2026-03-09T00:00:00Z",
    )
    observation = ObservationPacket(
        session_id="sess_demo",
        page_url="https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=buymarketresearch",
        page_title="Buy Market Research",
        visible_text_summary="Market Research Region Cost Buy NORAM LATAM EUROPE MEA APAC Total Expenses 20,000",
        dom_summary="Market Research Region Cost Buy NORAM LATAM EUROPE MEA APAC",
        active_goal=session.goal.raw_goal,
        domain_pack="marketplace_simulation",
        safety_mode="confirm_before_act",
        browser_metadata={
            "checkbox_rows": [
                {"row_text": "NORAM", "checked": True},
                {"row_text": "LATAM", "checked": False},
                {"row_text": "EUROPE", "checked": False},
            ]
        },
        captured_at="2026-03-09T00:00:01Z",
    )

    response = asyncio.run(planner.plan(session, observation))

    assert any("research" in item.lower() for item in response.live_advice)
    assert any(action.action == "click" for action in response.actions)
    assert any(action.metadata.get("row_text") == "LATAM" for action in response.actions)


def test_index_reuses_cached_site_memory_when_structure_is_unchanged():
    planner = PlannerService(EmptyLLM())
    session = SessionMemory(
        session_id="sess_cached",
        project_name="Demo",
        goal=GoalSpec(
            raw_goal="Keep cached context and refresh only changed pages.",
            objective="Reuse local site memory when the structure has not changed.",
            constraints=[],
            success_criteria=[],
            domain_pack="generic_web",
            safety_mode="confirm_before_act",
            created_at="2026-03-09T00:00:00Z",
        ),
        domain_pack="generic_web",
        strategic_summary="Old strategic summary.",
        created_at="2026-03-09T00:00:00Z",
        updated_at="2026-03-09T00:00:00Z",
    )
    observation = ObservationPacket(
        session_id="sess_cached",
        page_url="https://apple.com/iphone",
        page_title="iPhone",
        visible_text_summary="Compare iPhone models.",
        dom_summary="Compare iPhone models.",
        active_goal=session.goal.raw_goal,
        domain_pack="generic_web",
        safety_mode="confirm_before_act",
        browser_metadata={
            "site_index": {
                "navigation_items": ["Store", "iPhone", "Compare"],
                "site_map": [{"url": "https://apple.com/iphone", "title": "iPhone", "section_count": 2}],
            },
            "site_check": {
                "change_status": "unchanged",
                "change_summary": "Structure fingerprint matches local memory.",
            },
            "site_memory_context": {
                "strategic_summary": "Cached site summary.",
                "indexed_context": {
                    "site_index": {
                        "navigation_items": ["Store", "iPhone"],
                        "site_map": [{"url": "https://apple.com", "title": "Home", "section_count": 1}],
                    }
                },
            },
        },
        captured_at="2026-03-09T00:00:01Z",
    )

    indexed = asyncio.run(planner.index(session, observation))

    assert indexed["strategic_summary"] == "Cached site summary."
    assert "Loaded durable local site memory" in indexed["memory_summary"]
    urls = [item["url"] for item in indexed["indexed_context"]["site_index"]["site_map"]]
    assert "https://apple.com" in urls
    assert "https://apple.com/iphone" in urls


def test_review_only_review_builds_structured_generic_comparison_without_actions():
    planner = PlannerService(EmptyLLM())
    session = SessionMemory(
        session_id="sess_apple_compare",
        project_name="Apple Pricing",
        goal=GoalSpec(
            raw_goal="Find prices for all MacBook models and identify the cheapest variation.",
            objective="Extract comparable MacBook offers and summarize the cheapest option.",
            constraints=[],
            success_criteria=[],
            domain_pack="generic_web",
            safety_mode="confirm_before_act",
            created_at="2026-03-14T00:00:00Z",
            mode="review_only",
        ),
        domain_pack="generic_web",
        mode="review_only",
        strategic_summary="Apple pricing page is visible.",
        indexed_context={"site_index": {"navigation_items": ["MacBook Air", "MacBook Pro"]}},
        last_indexed_at="2026-03-14T00:00:00Z",
        created_at="2026-03-14T00:00:00Z",
        updated_at="2026-03-14T00:00:00Z",
    )
    observation = ObservationPacket(
        session_id="sess_apple_compare",
        page_url="https://www.apple.com/mac/",
        page_title="Mac",
        visible_text_summary=(
            "MacBook Air 13-inch M4 from $999. "
            "MacBook Air 15-inch M4 from $1,199. "
            "MacBook Pro 14-inch M4 from $1,599. "
            "MacBook Pro 16-inch M4 Max from $3,499."
        ),
        dom_summary="MacBook comparison cards and pricing.",
        active_goal=session.goal.raw_goal,
        domain_pack="generic_web",
        safety_mode="confirm_before_act",
        browser_metadata={},
        captured_at="2026-03-14T00:00:01Z",
    )

    batch = asyncio.run(planner.review(session, observation))

    assert batch.actions == []
    assert batch.apply_ready is False
    assert batch.comparison_payload["best_match"]["name"] == "MacBook Air 13-inch M4"
    assert batch.comparison_payload["best_match"]["price"] == "$999"
    assert len(batch.comparison_payload["entities"]) == 4
    assert "cheapest" in batch.summary.lower()
    assert batch.items[0].title == "Cheapest option"
    assert batch.items[0].current_value == "$999"
    assert batch.items[0].actionability == "manual_only"


def test_review_only_comparison_filters_entities_to_the_requested_product_family_when_possible():
    planner = PlannerService(EmptyLLM())
    session = SessionMemory(
        session_id="sess_mac_mini_compare",
        project_name="Apple Pricing",
        goal=GoalSpec(
            raw_goal="Find Mac mini pricing and configuration options on Apple.",
            objective="Extract Mac mini options and summarize the cheapest visible configuration.",
            constraints=[],
            success_criteria=[],
            domain_pack="generic_web",
            safety_mode="confirm_before_act",
            created_at="2026-03-14T00:00:00Z",
            mode="review_only",
        ),
        domain_pack="generic_web",
        mode="review_only",
        strategic_summary="Apple Mac page is visible.",
        indexed_context={"site_index": {"navigation_items": ["Mac mini", "MacBook Air"]}},
        last_indexed_at="2026-03-14T00:00:00Z",
        created_at="2026-03-14T00:00:00Z",
        updated_at="2026-03-14T00:00:00Z",
    )
    observation = ObservationPacket(
        session_id="sess_mac_mini_compare",
        page_url="https://www.apple.com/mac/",
        page_title="Mac",
        visible_text_summary=(
            "MacBook Air 13-inch from $999. "
            "Mac mini from $599. "
            "Mac mini M4 Pro from $1,399."
        ),
        dom_summary="Mac family pricing cards and buy links.",
        active_goal=session.goal.raw_goal,
        domain_pack="generic_web",
        safety_mode="confirm_before_act",
        browser_metadata={},
        captured_at="2026-03-14T00:00:01Z",
    )

    batch = asyncio.run(planner.review(session, observation))

    assert batch.comparison_payload["best_match"]["name"] == "Mac mini"
    assert batch.comparison_payload["best_match"]["price"] == "$599"
    assert [entity["name"] for entity in batch.comparison_payload["entities"]] == [
        "Mac mini",
        "Mac mini M4 Pro",
    ]


def test_extract_apple_entities_handles_compact_compare_page_model_labels():
    planner = PlannerService(EmptyLLM())

    entities = planner._extract_apple_entities(
        "MacBook Air 13-in. (M4) From $999 "
        "MacBook Pro 14-in. (M4) From $1,599 "
        "MacBook Pro 16-in. (M4 Max) From $3,499"
    )

    assert [entity["name"] for entity in entities] == [
        "MacBook Air 13-in. (M4)",
        "MacBook Pro 14-in. (M4)",
        "MacBook Pro 16-in. (M4 Max)",
    ]
    assert [entity["price"] for entity in entities] == ["$999", "$1,599", "$3,499"]


def test_extract_apple_entities_uses_size_rows_for_buy_page_variants():
    planner = PlannerService(EmptyLLM())

    entities = planner._extract_apple_entities(
        "MacBook Air From $1099 Choose your size 13-inch From $1099 15-inch From $1299"
    )

    assert [entity["name"] for entity in entities] == [
        "MacBook Air 13-inch",
        "MacBook Air 15-inch",
    ]
    assert [entity["price"] for entity in entities] == ["$1099", "$1299"]


def test_extract_apple_entities_does_not_treat_freeform_words_as_model_suffixes():
    planner = PlannerService(EmptyLLM())

    entities = planner._extract_apple_entities("MacBook Air awaits From $1099")

    assert [entity["name"] for entity in entities] == ["MacBook Air"]


class RaisingPlannerLLM(EmptyLLM):
    async def plan_actions(self, goal, observation, domain_pack, indexed_context=None, strategic_summary=""):
        raise RuntimeError("planner transport failed")


def test_planner_logs_warning_when_plan_actions_fails(caplog):
    planner = PlannerService(RaisingPlannerLLM())
    session = SessionMemory(
        session_id="sess_log_warning",
        project_name="Demo",
        goal=GoalSpec(
            raw_goal="Review the current site.",
            objective="Review the current site.",
            constraints=[],
            success_criteria=[],
            domain_pack="generic_web",
            safety_mode="confirm_before_act",
            created_at="2026-03-18T00:00:00Z",
        ),
        domain_pack="generic_web",
        strategic_summary="Known context.",
        indexed_context={},
        last_indexed_at="2026-03-18T00:00:00Z",
        created_at="2026-03-18T00:00:00Z",
        updated_at="2026-03-18T00:00:00Z",
    )
    observation = ObservationPacket(
        session_id="sess_log_warning",
        page_url="https://example.com/workflow",
        page_title="Workflow",
        visible_text_summary="Continue to the next step.",
        dom_summary="Continue to the next step.",
        active_goal=session.goal.raw_goal,
        domain_pack="generic_web",
        safety_mode="confirm_before_act",
        browser_metadata={},
        captured_at="2026-03-18T00:00:01Z",
    )

    caplog.set_level(logging.WARNING)

    response = asyncio.run(planner.plan(session, observation))

    assert response.live_advice
    assert "Planner action generation failed" in caplog.text


def test_build_inline_notes_prefers_exact_page_match():
    planner = PlannerService(EmptyLLM())
    session = SessionMemory(
        session_id="sess_notes",
        project_name="Demo",
        goal=GoalSpec(
            raw_goal="Win the Marketplace simulation.",
            objective="Act as a quarter-aware business simulation companion.",
            constraints=[],
            success_criteria=[],
            domain_pack="marketplace_simulation",
            safety_mode="confirm_before_act",
            created_at="2026-03-09T00:00:00Z",
        ),
        domain_pack="marketplace_simulation",
        review_batch={
            "session_id": "sess_notes",
            "summary": "Prepared a review.",
            "items": [
                {
                    "item_id": "item_open_stores",
                    "page_hint": "Open Stores",
                    "anchor_text": "Open Stores",
                    "field_label": "Store plan",
                    "recommendation": "Keep store openings conservative.",
                    "reasoning": "Fixed costs are high.",
                    "priority": "high",
                },
                {
                    "item_id": "item_demand",
                    "page_hint": "Demand Projection",
                    "anchor_text": "Demand Projection",
                    "field_label": "Demand forecast",
                    "recommendation": "Recheck the forecast before production.",
                    "reasoning": "Forecast quality drives production.",
                    "priority": "medium",
                },
            ],
        },
        created_at="2026-03-09T00:00:00Z",
        updated_at="2026-03-09T00:00:00Z",
    )
    observation = ObservationPacket(
        session_id="sess_notes",
        page_url="https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=opensalesoffice",
        page_title="Open Stores",
        visible_text_summary="Open Stores City Open Close Current Status",
        dom_summary="Open Stores City Open Close Current Status",
        active_goal=session.goal.raw_goal,
        domain_pack="marketplace_simulation",
        safety_mode="confirm_before_act",
        browser_metadata={},
        captured_at="2026-03-09T00:00:01Z",
    )

    notes = planner.build_inline_notes(session, observation)

    assert notes[0]["title"] == "Store plan"
    assert all(note["title"] != "Demand forecast" for note in notes[:1])


def test_build_inline_notes_prefers_abbreviated_page_title_match():
    planner = PlannerService(EmptyLLM())
    session = SessionMemory(
        session_id="sess_notes_short",
        project_name="Demo",
        goal=GoalSpec(
            raw_goal="Win the Marketplace simulation.",
            objective="Act as a quarter-aware business simulation companion.",
            constraints=[],
            success_criteria=[],
            domain_pack="marketplace_simulation",
            safety_mode="confirm_before_act",
            created_at="2026-03-09T00:00:00Z",
        ),
        domain_pack="marketplace_simulation",
        review_batch={
            "session_id": "sess_notes_short",
            "summary": "Prepared a review.",
            "items": [
                {
                    "item_id": "item_open_stores",
                    "page_hint": "Open Stores",
                    "anchor_text": "Open Stores",
                    "field_label": "Store plan",
                    "recommendation": "Keep store openings conservative.",
                    "reasoning": "Fixed costs are high.",
                    "priority": "high",
                },
                {
                    "item_id": "item_cash",
                    "page_hint": "Pro Forma Accounting",
                    "anchor_text": "Ending Cash",
                    "field_label": "Cash check",
                    "recommendation": "Keep ending cash positive.",
                    "reasoning": "Cash risk is critical.",
                    "priority": "high",
                },
            ],
        },
        created_at="2026-03-09T00:00:00Z",
        updated_at="2026-03-09T00:00:00Z",
    )
    observation = ObservationPacket(
        session_id="sess_notes_short",
        page_url="https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=opensalesoffice",
        page_title="Stores",
        visible_text_summary="Stores Open Close Current Status Pro Forma Accounting Ending Cash",
        dom_summary="Stores Open Close Current Status Pro Forma Accounting Ending Cash",
        active_goal=session.goal.raw_goal,
        domain_pack="marketplace_simulation",
        safety_mode="confirm_before_act",
        browser_metadata={},
        captured_at="2026-03-09T00:00:01Z",
    )

    notes = planner.build_inline_notes(session, observation)

    assert notes[0]["title"] == "Store plan"
    assert all(note["title"] != "Cash check" for note in notes[:1])


def test_build_inline_notes_stay_off_by_default_in_review_only_mode():
    planner = PlannerService(EmptyLLM())
    session = SessionMemory(
        session_id="sess_notes_off",
        project_name="Apple Pricing",
        goal=GoalSpec(
            raw_goal="Compare MacBook pricing.",
            objective="Find the cheapest MacBook.",
            constraints=[],
            success_criteria=[],
            domain_pack="generic_web",
            safety_mode="confirm_before_act",
            created_at="2026-03-14T00:00:00Z",
            mode="review_only",
        ),
        domain_pack="generic_web",
        mode="review_only",
        review_batch={
            "session_id": "sess_notes_off",
            "summary": "Prepared a review.",
            "items": [
                {
                    "item_id": "item_price",
                    "title": "Cheapest option",
                    "page_hint": "Mac",
                    "anchor_text": "MacBook Air",
                    "field_type": "comparison_row",
                    "current_value": "$999",
                    "recommended_value": "$999",
                    "why_it_matters": "This is the cheapest visible model.",
                    "evidence": "Visible pricing card.",
                    "priority": "high",
                    "confidence": 0.9,
                    "actionability": "manual_only",
                    "dependencies": [],
                    "requires_followup_check": False,
                }
            ],
        },
        created_at="2026-03-14T00:00:00Z",
        updated_at="2026-03-14T00:00:00Z",
    )
    observation = ObservationPacket(
        session_id="sess_notes_off",
        page_url="https://www.apple.com/mac/",
        page_title="Mac",
        visible_text_summary="MacBook Air from $999",
        dom_summary="Mac pricing page.",
        active_goal=session.goal.raw_goal,
        domain_pack="generic_web",
        safety_mode="confirm_before_act",
        browser_metadata={},
        captured_at="2026-03-14T00:00:01Z",
    )

    notes = planner.build_inline_notes(session, observation)

    assert notes == []


def test_fallback_review_generates_store_specific_recommendations():
    planner = PlannerService(EmptyLLM())
    session = SessionMemory(
        session_id="sess_store_review",
        project_name="Demo",
        goal=GoalSpec(
            raw_goal="Win the Marketplace simulation and optimize the current quarter.",
            objective="Act as a quarter-aware business simulation companion.",
            constraints=[],
            success_criteria=[],
            domain_pack="marketplace_simulation",
            safety_mode="confirm_before_act",
            created_at="2026-03-09T00:00:00Z",
        ),
        domain_pack="marketplace_simulation",
        strategic_summary="Indexed simulation workflow.",
        indexed_context={"site_index": {"editable_quarter": 4}},
        last_indexed_at="2026-03-09T00:00:00Z",
        created_at="2026-03-09T00:00:00Z",
        updated_at="2026-03-09T00:00:00Z",
    )
    observation = ObservationPacket(
        session_id="sess_store_review",
        page_url="https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=opensalesoffice",
        page_title="Stores",
        visible_text_summary="Stores city setup lease current status",
        dom_summary="Stores city setup lease current status",
        active_goal=session.goal.raw_goal,
        domain_pack="marketplace_simulation",
        safety_mode="confirm_before_act",
        browser_metadata={
            "checkbox_rows": [
                {"row_text": "Tokyo closed 292,000 65,000", "checked": False},
                {"row_text": "Hangzhou closed 68,000 10,000", "checked": False},
                {"row_text": "Toronto opened 195,000 35,000", "checked": False},
            ]
        },
        captured_at="2026-03-09T00:00:01Z",
    )

    import asyncio

    batch = asyncio.run(planner.review(session, observation))

    recs = [item.recommendation for item in batch.items]
    assert any("Hangzhou" in rec and "68,000" in rec for rec in recs)
    assert any("Tokyo" in rec and "292,000" in rec for rec in recs)
    assert any(item.page_hint == "Stores" for item in batch.items)



class SparseReviewLLM(EmptyLLM):
    async def review_workflow(self, goal, observation, domain_pack, indexed_context=None, strategic_summary=""):
        return {
            "summary": "Prepared a sparse review.",
            "current_focus": "Quarter 4 is editable.",
            "previous_period_summary": ["Quarter 3 results reviewed."],
            "rationale": ["Visible table data matters."],
            "items": [
                {
                    "item_id": "item_sparse",
                    "page_hint": "Pricing",
                    "anchor_text": "Price",
                    "field_label": "Pricing table",
                    "recommendation": "Review pricing carefully.",
                    "reasoning": "Visible prices matter.",
                    "priority": "high",
                }
            ],
            "actions": [],
            "apply_ready": False,
            "beta_warning": "Apply is beta. Manual application is safer.",
        }


def test_review_prefers_page_specific_marketplace_actions_over_sparse_cross_page_items():
    planner = PlannerService(SparseReviewLLM())
    session = SessionMemory(
        session_id="sess_review_merge",
        project_name="Demo",
        goal=GoalSpec(
            raw_goal="Win the Marketplace simulation and optimize the current quarter.",
            objective="Act as a quarter-aware business simulation companion.",
            constraints=[],
            success_criteria=[],
            domain_pack="marketplace_simulation",
            safety_mode="confirm_before_act",
            created_at="2026-03-09T00:00:00Z",
        ),
        domain_pack="marketplace_simulation",
        strategic_summary="Indexed simulation workflow.",
        indexed_context={
            "site_index": {
                "editable_quarter": 4,
                "navigation_items": [
                    "Marketing",
                    "Pricing",
                    "Advertising",
                    "Sales Channel",
                    "Open Stores",
                    "Hire Sales People",
                    "Demand Projection",
                    "Manufacturing",
                    "Pro Forma Accounting",
                    "Finance",
                ],
                "completed_quarters_detail": [
                    {
                        "quarter_number": 2,
                        "title": "Set Up Shop",
                        "page_text_excerpt": "Opened the first stores and built the initial team.",
                    },
                    {
                        "quarter_number": 3,
                        "title": "Enter the Market",
                        "page_text_excerpt": "Entered the market and started the first test-market decisions.",
                    },
                    {
                        "quarter_number": 4,
                        "title": "Test Market Results",
                        "section_previews": [
                            {"menu_item": "Open Stores"},
                            {"menu_item": "Pricing"},
                            {"menu_item": "Demand Projection"},
                        ],
                    },
                ],
            }
        },
        last_indexed_at="2026-03-09T00:00:00Z",
        created_at="2026-03-09T00:00:00Z",
        updated_at="2026-03-09T00:00:00Z",
    )
    observation = ObservationPacket(
        session_id="sess_review_merge",
        page_url="https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=opensalesoffice",
        page_title="Open Stores",
        visible_text_summary="Open Stores City Open Close Current Status Setup/Close Cost Quarterly Lease Cost Hangzhou closed 68,000 10,000 Tokyo closed 292,000 65,000 Ending Cash 1,193,809",
        dom_summary="Open Stores City Open Close Current Status Setup/Close Cost Quarterly Lease Cost",
        active_goal=session.goal.raw_goal,
        domain_pack="marketplace_simulation",
        safety_mode="confirm_before_act",
        browser_metadata={
            "checkbox_rows": [
                {"row_text": "Hangzhou closed 68,000 10,000", "checked": False},
                {"row_text": "Tokyo closed 292,000 65,000", "checked": False},
            ],
            "editable_rows": [
                {"row_text": "Hangzhou closed 68,000 10,000", "current_values": []},
            ],
        },
        captured_at="2026-03-09T00:00:01Z",
    )

    import asyncio

    batch = asyncio.run(planner.review(session, observation))

    assert len(batch.items) >= 3
    assert any("Hangzhou" in item.recommendation for item in batch.items)
    assert any("Tokyo" in item.recommendation for item in batch.items)
    assert any("Ending cash" in item.field_label or "Ending cash" in item.recommendation for item in batch.items)
    assert all(item.page_hint in {"Open Stores", "Pro Forma Accounting"} for item in batch.items)
    assert batch.actions
    assert all(action.requires_confirmation is True for action in batch.actions)
    assert batch.apply_ready is False


def test_market_research_review_stays_focused_on_current_editable_table():
    planner = PlannerService(EmptyLLM())
    session = SessionMemory(
        session_id="sess_market_research_focus",
        project_name="Demo",
        goal=GoalSpec(
            raw_goal="Win the Marketplace simulation and optimize the current quarter.",
            objective="Act as a quarter-aware business simulation companion.",
            constraints=[],
            success_criteria=[],
            domain_pack="marketplace_simulation",
            safety_mode="confirm_before_act",
            created_at="2026-03-09T00:00:00Z",
        ),
        domain_pack="marketplace_simulation",
        strategic_summary="Indexed simulation workflow.",
        indexed_context={
            "site_index": {
                "editable_quarter": 4,
                "navigation_items": [
                    "Brand Management",
                    "Pricing",
                    "Advertising",
                    "Buy Market Research",
                    "Open Stores",
                ],
            }
        },
        last_indexed_at="2026-03-09T00:00:00Z",
        created_at="2026-03-09T00:00:00Z",
        updated_at="2026-03-09T00:00:00Z",
    )
    observation = ObservationPacket(
        session_id="sess_market_research_focus",
        page_url="https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=buymarketresearch",
        page_title="Buy Market Research",
        visible_text_summary="Buy Market Research Modify Market Research Region Cost Buy NORAM 20,000 LATAM 20,000 EUROPE 20,000 MEA 20,000 APAC 20,000 Total Expenses 40,000 Ending Cash 1,138,235",
        dom_summary="Market Research Region Cost Buy NORAM LATAM EUROPE MEA APAC",
        active_goal=session.goal.raw_goal,
        domain_pack="marketplace_simulation",
        safety_mode="confirm_before_act",
        browser_metadata={
            "checkbox_rows": [
                {"row_text": "NORAM 20,000", "checked": True},
                {"row_text": "LATAM 20,000", "checked": False},
                {"row_text": "MEA 20,000", "checked": False},
                {"row_text": "APAC 20,000", "checked": False},
            ]
        },
        captured_at="2026-03-09T00:00:01Z",
    )

    import asyncio

    batch = asyncio.run(planner.review(session, observation))

    assert batch.items
    assert batch.items[0].page_hint == "Buy Market Research"
    assert "LATAM" in batch.items[0].recommendation
    assert all(item.page_hint in {"Buy Market Research", "Pro Forma Accounting"} for item in batch.items)
    assert all(item.page_hint != "Advertising" for item in batch.items)


def test_price_and_priority_review_replaces_generic_editable_row_filler():
    planner = PlannerService(EmptyLLM())
    session = SessionMemory(
        session_id="sess_price_priority",
        project_name="Demo",
        goal=GoalSpec(
            raw_goal="Win the Marketplace simulation and optimize the current quarter.",
            objective="Act as a quarter-aware business simulation companion.",
            constraints=[],
            success_criteria=[],
            domain_pack="marketplace_simulation",
            safety_mode="confirm_before_act",
            created_at="2026-03-09T00:00:00Z",
        ),
        domain_pack="marketplace_simulation",
        strategic_summary="Indexed simulation workflow.",
        indexed_context={
            "site_index": {
                "editable_quarter": 4,
                "navigation_items": ["Pricing", "Price and Priority", "Competitors' Prices", "Cost of Production"],
            }
        },
        last_indexed_at="2026-03-09T00:00:00Z",
        created_at="2026-03-09T00:00:00Z",
        updated_at="2026-03-09T00:00:00Z",
    )
    observation = ObservationPacket(
        session_id="sess_price_priority",
        page_url="https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=pricing&parentResource=pricing_",
        page_title="Price and Priority",
        visible_text_summary="Price and Priority Sales Priority Brand Available for Sale Retail Price Price Rebate Point of Purchase Display EDGETOSPEED EDGETOMOUNTAIN EDGETOWORK1 EDGEFORFUN Ending Cash 1,138,235",
        dom_summary="Price and Priority table",
        active_goal=session.goal.raw_goal,
        domain_pack="marketplace_simulation",
        safety_mode="confirm_before_act",
        browser_metadata={
            "editable_rows": [
                {"row_text": "EDGETOSPEED", "current_values": ["1", "1", "1,159", "100", "1"]},
                {"row_text": "EDGETOMOUNTAIN", "current_values": ["2", "1", "1,060", "150", "1"]},
                {"row_text": "EDGETOWORK1", "current_values": ["3", "1", "750", "50", "1"]},
                {"row_text": "EDGEFORFUN", "current_values": ["4", "1", "900", "80", "1"]},
            ]
        },
        captured_at="2026-03-09T00:00:01Z",
    )

    import asyncio

    batch = asyncio.run(planner.review(session, observation))

    price_items = [item for item in batch.items if item.page_hint == "Price and Priority"]
    assert price_items
    assert all("Review the editable row" not in item.recommendation for item in price_items)
    assert any("EDGETOSPEED" in item.recommendation and "1,159" in item.recommendation for item in price_items)
    assert any(
        ("premium" in item.recommendation.lower()) or ("price ladder" in item.recommendation.lower()) or ("display" in item.recommendation.lower())
        for item in price_items
    )


def test_generic_editable_rows_use_visible_values_and_change_direction():
    planner = PlannerService(EmptyLLM())
    session = SessionMemory(
        session_id="sess_sales_channel",
        project_name="Demo",
        goal=GoalSpec(
            raw_goal="Win the Marketplace simulation and improve quarter 4 execution.",
            objective="Act as a quarter-aware business simulation companion.",
            constraints=[],
            success_criteria=[],
            domain_pack="marketplace_simulation",
            safety_mode="confirm_before_act",
            created_at="2026-03-09T00:00:00Z",
        ),
        domain_pack="marketplace_simulation",
        strategic_summary="Indexed simulation workflow.",
        indexed_context={
            "site_index": {
                "editable_quarter": 4,
                "navigation_items": ["Sales Channel", "Pro Forma Accounting", "Pricing"],
            }
        },
        last_indexed_at="2026-03-09T00:00:00Z",
        created_at="2026-03-09T00:00:00Z",
        updated_at="2026-03-09T00:00:00Z",
    )
    observation = ObservationPacket(
        session_id="sess_sales_channel",
        page_url="https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=saleschannel",
        page_title="Sales Channel",
        visible_text_summary="Sales Channel Retail coverage NORAM 12 LATAM 18 EUROPE 9 Budget 250 Ending Cash 1,138,235",
        dom_summary="Sales Channel table",
        active_goal=session.goal.raw_goal,
        domain_pack="marketplace_simulation",
        safety_mode="confirm_before_act",
        browser_metadata={
            "editable_rows": [
                {"row_text": "Retail coverage NORAM", "current_values": ["12", "18", "9"]},
                {"row_text": "Budget mix", "current_values": ["250", "180", "90"]},
            ]
        },
        captured_at="2026-03-09T00:00:01Z",
    )

    import asyncio

    batch = asyncio.run(planner.review(session, observation))

    sales_items = [item for item in batch.items if item.page_hint == "Sales Channel"]
    assert sales_items
    assert all("Review the editable row" not in item.recommendation for item in sales_items)
    assert any(item.current_value == "12, 18, 9" for item in sales_items)
    assert any(
        ("change" in item.recommendation.lower()) or ("increase" in item.recommendation.lower()) or ("decrease" in item.recommendation.lower())
        for item in sales_items
    )


def test_cash_guardrail_review_is_supplemented_with_indexed_action_pages():
    planner = PlannerService(EmptyLLM())
    session = SessionMemory(
        session_id="sess_performance_report",
        project_name="Demo",
        goal=GoalSpec(
            raw_goal="Win the Marketplace simulation and improve quarter 4 decisions.",
            objective="Act as a quarter-aware business simulation companion.",
            constraints=[],
            success_criteria=[],
            domain_pack="marketplace_simulation",
            safety_mode="confirm_before_act",
            created_at="2026-03-09T00:00:00Z",
        ),
        domain_pack="marketplace_simulation",
        strategic_summary="Indexed simulation workflow.",
        indexed_context={
            "site_index": {
                "editable_quarter": 4,
                "navigation_items": [
                    "Performance Report",
                    "Price and Priority",
                    "Buy Market Research",
                    "Sales Channel",
                    "Pro Forma Accounting",
                ],
            }
        },
        last_indexed_at="2026-03-09T00:00:00Z",
        created_at="2026-03-09T00:00:00Z",
        updated_at="2026-03-09T00:00:00Z",
    )
    observation = ObservationPacket(
        session_id="sess_performance_report",
        page_url="https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=performance-report",
        page_title="Performance Report",
        visible_text_summary="Performance Report Quarter 4 Ending Cash 1,138,235 Market Share Sales Income Statement Cash Flow",
        dom_summary="Performance Report",
        active_goal=session.goal.raw_goal,
        domain_pack="marketplace_simulation",
        safety_mode="confirm_before_act",
        browser_metadata={},
        captured_at="2026-03-09T00:00:01Z",
    )

    import asyncio

    batch = asyncio.run(planner.review(session, observation))

    page_hints = {item.page_hint for item in batch.items}
    assert "Pro Forma Accounting" in page_hints
    assert "Buy Market Research" in page_hints
    assert "Price and Priority" in page_hints


class GenericCrossPageReviewLLM(EmptyLLM):
    async def review_workflow(self, goal, observation, domain_pack, indexed_context=None, strategic_summary=""):
        return {
            "summary": "Prepared a generic review.",
            "current_focus": "Quarter 4 is editable.",
            "previous_period_summary": ["Quarter 3 results reviewed."],
            "rationale": ["Generic guidance."],
            "items": [
                {
                    "item_id": "item_generic_advertising",
                    "page_hint": "Advertising",
                    "anchor_text": "Advertising",
                    "field_label": "Advertising plan",
                    "recommendation": "Keep the ads balanced.",
                    "reasoning": "Generic cross-page guidance.",
                    "priority": "medium",
                }
            ],
            "actions": [],
            "apply_ready": False,
            "beta_warning": "Apply is beta. Manual application is safer.",
        }


def test_editable_table_review_suppresses_generic_cross_page_items():
    planner = PlannerService(GenericCrossPageReviewLLM())
    session = SessionMemory(
        session_id="sess_editable_table_focus",
        project_name="Demo",
        goal=GoalSpec(
            raw_goal="Win the Marketplace simulation and optimize the current quarter.",
            objective="Act as a quarter-aware business simulation companion.",
            constraints=[],
            success_criteria=[],
            domain_pack="marketplace_simulation",
            safety_mode="confirm_before_act",
            created_at="2026-03-09T00:00:00Z",
        ),
        domain_pack="marketplace_simulation",
        strategic_summary="Indexed simulation workflow.",
        indexed_context={
            "site_index": {
                "editable_quarter": 4,
                "navigation_items": ["Pricing", "Advertising", "Buy Market Research"],
            }
        },
        last_indexed_at="2026-03-09T00:00:00Z",
        created_at="2026-03-09T00:00:00Z",
        updated_at="2026-03-09T00:00:00Z",
    )
    observation = ObservationPacket(
        session_id="sess_editable_table_focus",
        page_url="https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=pricing",
        page_title="Pricing",
        visible_text_summary="Pricing Brand Price Priority Ending Cash 1,138,235 Trailblazer 1399 High City Runner 999 Medium",
        dom_summary="Pricing table with editable brand rows",
        active_goal=session.goal.raw_goal,
        domain_pack="marketplace_simulation",
        safety_mode="confirm_before_act",
        browser_metadata={
            "editable_rows": [
                {
                    "row_text": "Trailblazer price priority",
                    "current_values": ["1399", "High"],
                },
                {
                    "row_text": "City Runner price priority",
                    "current_values": ["999", "Medium"],
                },
            ],
        },
        captured_at="2026-03-09T00:00:01Z",
    )

    import asyncio

    batch = asyncio.run(planner.review(session, observation))

    assert batch.items
    assert all("Review the editable row" not in item.recommendation for item in batch.items)
    assert all(item.page_hint in {"Pricing", "Pro Forma Accounting"} for item in batch.items)
    assert all(item.field_label != "Advertising plan" for item in batch.items)
