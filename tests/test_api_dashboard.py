from pathlib import Path
import re

import pytest
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from marketplace_bot.api.main import create_app
from marketplace_bot.navigator_models import ActionProposal, GoalSpec, IndexSummary, ReviewBatch, SessionMemory
from marketplace_bot.state_store import StateStore


class FakeOrchestrator:
    async def run_audit_mode(self) -> dict:
        return {"mode": "audit", "status": "ok"}

    async def run_audit_mode_from_cache(self) -> dict:
        return {"mode": "audit", "status": "ok", "used_cached": True}

    async def run_execute_mode(self) -> dict:
        return {"mode": "execute", "status": "awaiting_submit_confirmation"}

    async def confirm_final_submit(self) -> dict:
        return {"status": "submitted"}

    async def stop(self) -> dict:
        return {"status": "stopped"}

    def get_status(self) -> dict:
        return {
            "mode": "idle",
            "kill_switch_active": False,
            "pending_submit_confirmation": False,
        }


class FakeCompanion:
    def __init__(self) -> None:
        self.session = SessionMemory(
            session_id="sess_test",
            project_name="Demo",
            goal=GoalSpec(
                raw_goal="Test goal",
                objective="Test objective",
                safety_mode="confirm_before_act",
                created_at="2026-03-08T00:00:00Z",
            ),
            domain_pack="generic_web",
            mode="review_only",
            current_site="",
            memory_summary="Index the site before live advice.",
            live_advice=["Step 1: index the site."],
            pending_approvals=[],
            status="session_ready_to_index",
            site_check_required=True,
            review_ready=False,
            activity_log_tail=["Session created."],
            structure_map_summary={},
            coverage_summary={},
            created_at="2026-03-08T00:00:00Z",
            updated_at="2026-03-08T00:00:00Z",
        )
        self.indexing_marks = []

    async def create_session(self, request):
        return self.session

    def list_sessions(self):
        return [self.session]

    def get_session(self, session_id):
        return self.session

    def resume_session(self, session_id):
        self.session.site_check_required = True
        self.session.status = "session_ready_to_index"
        self.session.site_check_summary = "Resume requested. Run the structure fingerprint check before guidance."
        self.session.memory_summary = "Index the site first so the session can verify structure changes."
        return self.session

    def store_observation(self, observation):
        self.session.last_observation = observation
        return self.session

    async def plan(self, session_id, observation=None):
        self.session.status = "live_advice"
        self.session.live_advice = ["Advice"]
        return None

    async def index_site(self, session_id, observation):
        self.session.last_observation = observation
        self.session.current_site = observation.page_url
        self.session.strategic_summary = "Indexed simulation workflow."
        self.session.indexed_context = observation.browser_metadata.get("site_index", {})
        self.session.last_indexed_at = observation.captured_at
        self.session.site_check_required = False
        self.session.site_check_summary = "Structure checklist drift detected, reused nodes: 7, new nodes: 1. The agent will run a partial refresh."
        self.session.status = "index_summary_ready"
        self.session.memory_summary = "Site indexed."
        self.session.live_advice = ["Index complete."]
        self.session.coverage_summary = {
            "discovered_nodes": 8,
            "indexed_nodes": 8,
            "skipped_nodes": 0,
            "blocked_nodes": 0,
            "alias_collapsed_nodes": 0,
            "current_node_count": 8,
        }
        self.session.structure_map_summary = {
            "mode": "review_only",
            "active_node": "Workflow",
            "node_count": 8,
        }
        self.session.activity_log_tail = ["Indexed the workflow.", "Prepared the summary."]
        self.session.artifacts["site_check_details"] = {
            "change_status": "changed",
            "strategy": "partial",
            "matched_nodes": 7,
            "changed_nodes_count": 0,
            "new_nodes_count": 1,
            "removed_nodes_count": 0,
            "current_node_count": 8,
        }
        self.session.index_summary = IndexSummary(
            strategic_summary="Indexed simulation workflow.",
            site_check_summary="Structure fingerprint matched local memory.",
            previous_period_summary=["Quarter 1 setup complete.", "Quarter 2 market entry complete."],
            current_focus="Quarter 3 is editable.",
            top_recommendations=["Review pricing.", "Review staffing."],
            detected_changes=["Quarter 3 workspace is active."],
        )
        return self.session

    def mark_indexing_progress(self, session_id, step: str, site_check_details=None):
        self.indexing_marks.append(
            {
                "session_id": session_id,
                "step": step,
                "site_check_details": dict(site_check_details or {}),
            }
        )
        self.session.status = "indexing"
        self.session.updated_at = "2026-03-08T00:00:01Z"
        return self.session

    def enter_live_advice_mode(self, session_id):
        self.session.status = "live_advice"
        return self.session

    def update_page_signature(self, session_id, signature: str) -> bool:
        changed = getattr(self.session, "last_page_signature", "") != signature
        self.session.last_page_signature = signature
        return changed

    async def prepare_review_batch(self, session_id, observation=None):
        self.session.status = "review_batch_ready"
        self.session.review_batch = ReviewBatch(
            session_id=session_id,
            summary="Prepared one grouped change set.",
            rationale=["Current quarter is editable.", "Changes are low risk and directly actionable."],
            current_focus="Quarter 3 decisions",
            previous_period_summary=["Quarter 1 setup done.", "Quarter 2 market entry complete."],
            actions=[],
            items=[
                {
                    "item_id": "item_research",
                    "page_hint": "Buy Market Research",
                    "anchor_text": "Market Research",
                    "field_label": "Missing reports",
                    "recommendation": "Buy the remaining reports before changing pricing.",
                    "reasoning": "Current quarter still lacks key market data.",
                    "priority": "high",
                }
            ],
            apply_ready=False,
            beta_warning="Apply is beta. Manual application is safer.",
        )
        self.session.review_ready = True
        return self.session.review_batch

    async def refresh_live_advice_from_review(self, session_id, observation=None):
        self.session.status = "live_advice"
        self.session.live_advice = ["Live notes ready."]
        self.session.artifacts["inline_notes"] = [
            {
                "note_id": "note_market_research",
                "title": "Missing reports",
                "body": "Buy the remaining reports before changing pricing.",
                "reasoning": "Current quarter still lacks key market data.",
                "anchor_text": "Market Research",
                "priority": "high",
            }
        ]
        return self.session

    def mark_applying_batch(self, session_id):
        self.session.status = "applying_batch"
        return self.session

    def finalize_review_batch(self, session_id):
        self.session.status = "review_batch_ready"
        return self.session

    def approve_actions(self, session_id, action_ids):
        return []

    def auto_approve_executable_actions(self, session_id):
        return []

    def record_execution(self, payload):
        self.session.status = "review_batch_ready"
        return self.session


class FakeBridge:
    def __init__(self) -> None:
        self.overlay_panels = []
        self.focus_calls = 0
        self.bootstrap_calls = 0
        self.command_handler = None
        self.closed_ui_tabs = []

    async def capture_observation(self, **kwargs):
        return {
            "session_id": kwargs["session_id"],
            "page_url": "https://example.com",
            "page_title": "Example",
            "visible_text_summary": "text",
            "dom_summary": "dom",
            "active_goal": kwargs["active_goal"],
            "domain_pack": kwargs["domain_pack"],
            "safety_mode": kwargs["safety_mode"],
            "browser_metadata": {"page_signature": "sig-1"},
            "captured_at": "2026-03-08T00:00:00Z",
        }

    async def capture_site_index(self, **kwargs):
        progress_callback = kwargs.get("progress_callback")
        if progress_callback is not None:
            await progress_callback(
                "Fingerprint check complete. Strategy selected: partial.",
                28,
                {
                    "change_status": "changed",
                    "strategy": "partial",
                    "matched_nodes": 7,
                    "changed_nodes_count": 0,
                    "new_nodes_count": 1,
                    "removed_nodes_count": 0,
                    "current_node_count": 8,
                },
            )
        return {
            "session_id": kwargs["session_id"],
            "page_url": "https://example.com/workflow",
            "page_title": "Workflow",
            "visible_text_summary": "Step one Step two",
            "dom_summary": "Step one Step two",
            "active_goal": kwargs["active_goal"],
            "domain_pack": kwargs["domain_pack"],
            "safety_mode": kwargs["safety_mode"],
            "screenshot_path": "runtime/artifacts/sess_test/2026-03-08T00-00-00Z.png",
            "browser_metadata": {
                "site_index": {"navigation_items": ["Step one", "Step two"]},
                "site_check": {
                    "change_status": "changed",
                    "strategy": "partial",
                    "matched_nodes": 7,
                    "changed_nodes": [],
                    "new_nodes": ["https://example.com/workflow/new-step"],
                    "removed_nodes": [],
                    "current_node_count": 8,
                },
            },
            "captured_at": "2026-03-08T00:00:00Z",
        }

    async def execute_actions(self, actions):
        return []

    async def sync_agent_overlay(self, panel):
        self.overlay_panels.append(panel)

    async def clear_agent_overlay(self):
        return None

    async def focus_active_page(self):
        self.focus_calls += 1

    async def bootstrap_overlay(self):
        self.bootstrap_calls += 1

    async def close_local_ui_tabs(self, ui_url: str):
        self.closed_ui_tabs.append(ui_url)

    def register_command_handler(self, handler):
        self.command_handler = handler


class FailingBootstrapBridge(FakeBridge):
    async def bootstrap_overlay(self):
        raise RuntimeError("Open a target website tab in the controlled Chrome window first, then retry the overlay.")


class GenericFailingBootstrapBridge(FakeBridge):
    async def bootstrap_overlay(self):
        raise Exception("BrowserType.connect_over_cdp: connect ECONNREFUSED 127.0.0.1:9222")


class FakeNavigatorRuntime:
    def __init__(self) -> None:
        self.companion = FakeCompanion()
        self.bridge = FakeBridge()


def test_dashboard_endpoints(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runtime")
    store.bootstrap()

    runtime = FakeNavigatorRuntime()
    app = create_app(orchestrator=FakeOrchestrator(), state_store=store, navigator_runtime=runtime)
    client = TestClient(app)

    assert client.post("/api/bootstrap-overlay").status_code == 200
    assert runtime.bridge.bootstrap_calls == 1

    create_response = client.post(
        "/api/sessions",
        json={
            "project_name": "Demo",
            "goal": "Test goal",
            "domain_hint": "generic_web",
            "safety_mode": "confirm_before_act",
            "index_mode": "adaptive",
        },
    )
    assert create_response.status_code == 200
    assert create_response.json()["session_id"] == "sess_test"
    assert create_response.json()["mode"] == "review_only"
    assert runtime.bridge.focus_calls == 2
    assert runtime.bridge.overlay_panels[-1]["stage"] == "session_ready_to_index"
    assert runtime.bridge.overlay_panels[-1]["mode"] == "review_only"
    assert runtime.bridge.overlay_panels[-1]["review_ready"] is False
    assert "activity_log_tail" in runtime.bridge.overlay_panels[-1]
    assert "coverage_summary" in runtime.bridge.overlay_panels[-1]
    assert "structure_map_summary" in runtime.bridge.overlay_panels[-1]
    assert "degraded_reason" in runtime.bridge.overlay_panels[-1]
    assert "insufficiently_grounded" in runtime.bridge.overlay_panels[-1]

    assert client.get("/api/health").status_code == 200
    assert client.get("/architecture").status_code == 200
    status_payload = client.get("/api/status").json()
    assert "mode" not in status_payload
    assert "last_error" not in status_payload
    assert status_payload["kill_switch_active"] is False
    assert status_payload["active_session_id"] == "sess_test"
    assert status_payload["session_count"] == 1
    assert client.get("/report").status_code == 404
    assert client.get("/api/state").status_code == 404
    assert client.get("/api/history").status_code == 404
    assert client.get("/api/latest-decision").status_code == 404
    assert client.get("/api/sessions").status_code == 200
    assert client.post("/api/plan", json={"session_id": "sess_test"}).status_code == 409

    resume_response = client.post("/api/sessions/sess_test/resume")
    assert resume_response.status_code == 200
    assert resume_response.json()["site_check_required"] is True
    assert runtime.bridge.focus_calls == 3

    index_response = client.post("/api/index-site", json={"session_id": "sess_test"})
    assert index_response.status_code == 200
    assert index_response.json()["strategic_summary"] == "Indexed simulation workflow."
    assert index_response.json()["site_check_required"] is False
    assert index_response.json()["status"] == "review_batch_ready"
    assert index_response.json()["mode"] == "review_only"
    assert index_response.json()["review_ready"] is True
    assert index_response.json()["review_batch"]["summary"] == "Prepared one grouped change set."
    assert runtime.bridge.overlay_panels[-1]["site_check_details"]["matched_nodes"] == 7
    assert runtime.bridge.overlay_panels[-1]["site_check_details"]["new_nodes_count"] == 1
    assert runtime.bridge.overlay_panels[-1]["coverage_summary"]["indexed_nodes"] >= 1
    assert runtime.bridge.overlay_panels[-1]["last_capture_at"] == "2026-03-08T00:00:00Z"
    assert runtime.bridge.overlay_panels[-1]["last_capture_path"] == "runtime/artifacts/sess_test/2026-03-08T00-00-00Z.png"
    assert runtime.bridge.focus_calls == 4
    assert runtime.companion.indexing_marks
    assert runtime.companion.indexing_marks[0]["step"] == "Checking the current structure fingerprint against saved local memory."
    assert runtime.companion.indexing_marks[1]["step"] == "Fingerprint check complete. Strategy selected: partial."
    assert runtime.companion.indexing_marks[1]["site_check_details"]["strategy"] == "partial"

    overlay_sessions_response = client.post("/api/overlay/command", json={"command": "open_sessions", "payload": {"session_id": "sess_test"}})
    assert overlay_sessions_response.status_code == 200
    assert runtime.bridge.overlay_panels[-1]["view"] == "session"
    assert runtime.bridge.overlay_panels[-1]["sessions_open"] is True

    overlay_map_response = client.post("/api/overlay/command", json={"command": "open_map", "payload": {"session_id": "sess_test"}})
    assert overlay_map_response.status_code == 200
    assert overlay_map_response.json()["map_url"].endswith("/map?session_id=sess_test")

    overlay_review_response = client.post("/api/overlay/command", json={"command": "open_review", "payload": {"session_id": "sess_test"}})
    assert overlay_review_response.status_code == 200
    assert overlay_review_response.json()["review_url"].endswith("/review?session_id=sess_test")

    show_setup_response = client.post("/api/overlay/command", json={"command": "show_setup", "payload": {"session_id": "sess_test"}})
    assert show_setup_response.status_code == 200
    assert runtime.bridge.overlay_panels[-1]["view"] == "setup"
    assert runtime.bridge.overlay_panels[-1]["active_session_id"] == "sess_test"

    live_response = client.post("/api/live-advice/start", json={"session_id": "sess_test"})
    assert live_response.status_code == 200
    assert live_response.json()["status"] == "live_advice"
    assert live_response.json()["artifacts"]["inline_notes"][0]["title"] == "Missing reports"

    review_response = client.post("/api/review-batch", json={"session_id": "sess_test"})
    assert review_response.status_code == 200
    assert review_response.json()["summary"] == "Prepared one grouped change set."
    apply_response = client.post("/api/review-batch/apply", json={"session_id": "sess_test"})
    assert apply_response.status_code == 409

    assert client.post("/api/start/audit").status_code == 404
    assert client.post("/api/start/audit-cached").status_code == 404
    assert client.post("/api/start/execute").status_code == 404
    assert client.post("/api/confirm-submit").status_code == 404


def test_bootstrap_overlay_returns_recovery_message_when_target_tab_is_missing(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runtime")
    store.bootstrap()
    runtime = FakeNavigatorRuntime()
    runtime.bridge = FailingBootstrapBridge()
    app = create_app(orchestrator=FakeOrchestrator(), state_store=store, navigator_runtime=runtime)
    client = TestClient(app)

    response = client.post("/api/bootstrap-overlay")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "Open a target website tab" in response.json()["message"]


def test_bootstrap_overlay_returns_recovery_message_when_cdp_is_not_ready(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runtime")
    store.bootstrap()
    runtime = FakeNavigatorRuntime()
    runtime.bridge = GenericFailingBootstrapBridge()
    app = create_app(orchestrator=FakeOrchestrator(), state_store=store, navigator_runtime=runtime)
    client = TestClient(app)

    response = client.post("/api/bootstrap-overlay")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "9222" in response.json()["message"]


def test_map_page_renders_a_graph_from_the_session_structure_manifest(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runtime")
    store.bootstrap()
    runtime = FakeNavigatorRuntime()
    runtime.companion.session.artifacts["normalized_structure_manifest"] = {
        "nodes": [
            {
                "key": "https://example.com/workflow",
                "title": "Workflow",
                "url": "https://example.com/workflow",
                "section_count": 3,
                "quarter_number": None,
                "editable": False,
            },
            {
                "key": "https://example.com/pricing",
                "title": "Pricing",
                "url": "https://example.com/pricing",
                "section_count": 2,
                "quarter_number": 4,
                "editable": True,
            },
        ]
    }
    runtime.companion.session.coverage_summary = {
        "discovered_nodes": 2,
        "indexed_nodes": 2,
        "skipped_nodes": 0,
        "blocked_nodes": 0,
        "alias_collapsed_nodes": 0,
        "current_node_count": 2,
    }
    runtime.companion.session.structure_map_summary = {
        "mode": "complex_workspace",
        "active_node": "Pricing",
        "editable_quarter": 4,
    }
    app = create_app(orchestrator=FakeOrchestrator(), state_store=store, navigator_runtime=runtime)
    client = TestClient(app)

    response = client.get("/map?session_id=sess_test")

    assert response.status_code == 200
    assert "Website Structure Graph" in response.text
    assert "Workflow" in response.text
    assert "Pricing" in response.text
    assert "coverage-badges" in response.text


def test_map_page_uses_manifest_parent_keys_to_render_a_hierarchy(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runtime")
    store.bootstrap()
    runtime = FakeNavigatorRuntime()
    runtime.companion.session.artifacts["normalized_structure_manifest"] = {
        "nodes": [
            {
                "key": "root::workflow",
                "title": "Workflow",
                "url": "https://example.com/workflow",
                "section_count": 3,
                "quarter_number": None,
                "editable": False,
                "parent_key": "",
            },
            {
                "key": "family::pricing",
                "title": "Pricing",
                "url": "https://example.com/workflow/pricing",
                "section_count": 2,
                "quarter_number": None,
                "editable": False,
                "parent_key": "root::workflow",
            },
            {
                "key": "leaf::review",
                "title": "Review",
                "url": "https://example.com/workflow/pricing/review",
                "section_count": 0,
                "quarter_number": None,
                "editable": False,
                "parent_key": "family::pricing",
            },
        ]
    }
    app = create_app(orchestrator=FakeOrchestrator(), state_store=store, navigator_runtime=runtime)
    client = TestClient(app)

    response = client.get("/map?session_id=sess_test")

    assert response.status_code == 200
    assert '"parentKey":"root::workflow"' in response.text
    assert "childrenByParent" in response.text
    assert "primaryRoots" in response.text


def test_map_page_rebuilds_hierarchy_from_indexed_context_when_artifact_manifest_is_flat(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runtime")
    store.bootstrap()
    runtime = FakeNavigatorRuntime()
    runtime.companion.session.current_site = "https://play.marketplace-simulation.com/mpl/web7/engine.php?tpl=student&resource=pricing&parentResource=pricing_&tab=workspace&quarter=4&language=en-us"
    runtime.companion.session.site_origin = "https://play.marketplace-simulation.com"
    runtime.companion.session.mode = "complex_workspace"
    runtime.companion.session.artifacts["normalized_structure_manifest"] = {
        "nodes": [
            {
                "key": runtime.companion.session.current_site,
                "title": "Price and Priority",
                "url": runtime.companion.session.current_site,
                "section_count": 4,
                "quarter_number": 4,
                "editable": True,
                "parent_key": "",
            }
        ]
    }
    runtime.companion.session.indexed_context = {
        "site_index": {
            "site_map": [
                {
                    "url": runtime.companion.session.current_site,
                    "title": "Price and Priority",
                    "section_count": 4,
                },
                {
                    "url": "https://play.marketplace-simulation.com/mpl/web7/engine.php?tpl=student&resource=performance-report&parentResource=pricing_&tab=workspace&quarter=4&language=en-us",
                    "title": "Performance Report",
                    "section_count": 3,
                },
            ]
        }
    }
    app = create_app(orchestrator=FakeOrchestrator(), state_store=store, navigator_runtime=runtime)
    client = TestClient(app)

    response = client.get("/map?session_id=sess_test")

    assert response.status_code == 200
    assert '"key":"marketplace::quarter::4"' in response.text
    assert '"parentKey":"marketplace::quarter::4::tab::workspace::parent::pricing"' in response.text


def test_map_page_groups_unattached_nodes_under_a_separate_bucket(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runtime")
    store.bootstrap()
    runtime = FakeNavigatorRuntime()
    runtime.companion.session.artifacts["normalized_structure_manifest"] = {
        "nodes": [
            {
                "key": "root::workflow",
                "title": "Workflow",
                "url": "https://example.com/workflow",
                "section_count": 3,
                "quarter_number": 4,
                "editable": False,
                "parent_key": "",
            },
            {
                "key": "leaf::pricing",
                "title": "Pricing",
                "url": "https://example.com/workflow/pricing",
                "section_count": 2,
                "quarter_number": 4,
                "editable": True,
                "parent_key": "root::workflow",
            },
            {
                "key": "orphan::legacy",
                "title": "Legacy Detached",
                "url": "https://example.com/legacy",
                "section_count": 0,
                "quarter_number": 2,
                "editable": False,
                "parent_key": "missing::parent",
            },
        ]
    }
    runtime.companion.session.structure_map_summary = {
        "mode": "complex_workspace",
        "active_node": "Pricing",
        "editable_quarter": 4,
    }
    app = create_app(orchestrator=FakeOrchestrator(), state_store=store, navigator_runtime=runtime)
    client = TestClient(app)

    response = client.get("/map?session_id=sess_test")

    assert response.status_code == 200
    assert "Unattached Nodes" in response.text
    assert "primaryRoots" in response.text
    assert "unattachedRoots" in response.text


def test_architecture_page_surfaces_system_and_session_diagrams(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runtime")
    store.bootstrap()
    runtime = FakeNavigatorRuntime()
    app = create_app(orchestrator=FakeOrchestrator(), state_store=store, navigator_runtime=runtime)
    client = TestClient(app)

    response = client.get("/architecture")

    assert response.status_code == 200
    assert "System Architecture" in response.text
    assert "Session Lifecycle" in response.text
    assert "/static/live_navigator_system_architecture.png" in response.text
    assert "/static/live_navigator_session_lifecycle.png" in response.text


def test_review_page_renders_structured_recommendations_in_a_separate_tab(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runtime")
    store.bootstrap()
    runtime = FakeNavigatorRuntime()
    runtime.companion.session.mode = "complex_workspace"
    runtime.companion.session.review_batch = ReviewBatch(
        session_id="sess_test",
        summary="Raise premium pricing and verify the low-end ladder.",
        rationale=["Visible pricing rows are editable.", "The premium slot is underpriced."],
        current_focus="Quarter 4 Pricing",
        items=[
            {
                "item_id": "item_price",
                "title": "Premium price gap",
                "page_hint": "Pricing",
                "anchor_text": "EDGETOSPEED",
                "field_type": "numeric_row",
                "current_value": "1,159",
                "recommended_value": "1,199",
                "why_it_matters": "Premium positioning is already visible.",
                "evidence": "Visible price ladder shows EDGETOSPEED below the premium band.",
                "priority": "high",
                "confidence": 0.82,
                "actionability": "manual_only",
                "dependencies": ["Verify competitor pricing."],
                "requires_followup_check": True,
            }
        ],
        actions=[],
        apply_ready=False,
    )
    app = create_app(orchestrator=FakeOrchestrator(), state_store=store, navigator_runtime=runtime)
    client = TestClient(app)

    response = client.get("/review?session_id=sess_test")

    assert response.status_code == 200
    assert "Rendered Review" in response.text
    assert "Premium price gap" in response.text
    assert "Current value" in response.text
    assert "Recommended" in response.text


def test_dashboard_js_only_binds_existing_dom_ids() -> None:
    html = Path("src/marketplace_bot/api/static/index.html").read_text(encoding="utf-8")
    js = Path("src/marketplace_bot/api/static/app.js").read_text(encoding="utf-8")

    html_ids = set(re.findall(r'id="([^"]+)"', html))
    listener_ids = re.findall(r'document\.getElementById\("([^"]+)"\)\.addEventListener', js)

    missing = sorted(set(listener_ids) - html_ids)
    assert missing == []


def test_bootstrap_ui_contract() -> None:
    html = Path("src/marketplace_bot/api/static/index.html").read_text(encoding="utf-8")
    js = Path("src/marketplace_bot/api/static/app.js").read_text(encoding="utf-8")

    assert "Controller Active" in html
    assert 'id="session-tray"' not in html
    assert 'id="retry-bootstrap-btn"' in html
    assert "Report" not in html
    assert 'class="bootstrap-list"' not in html
    assert 'href="/architecture"' not in html
    assert "/api/bootstrap-overlay" in js
    assert "setInterval(" not in js
    assert "runLiveFollowCycle" not in js


def test_bootstrap_closes_localhost_tabs_on_success(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runtime")
    store.bootstrap()
    runtime = FakeNavigatorRuntime()
    app = create_app(orchestrator=FakeOrchestrator(), state_store=store, navigator_runtime=runtime)
    client = TestClient(app)

    response = client.post("/api/bootstrap-overlay")

    assert response.status_code == 200
    assert runtime.bridge.closed_ui_tabs == ["http://testserver"]


def test_readme_documents_debug_chrome_and_one_command_launch() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "remote-debugging-port=9222" in readme
    assert "make launch" in readme
    assert "make relaunch" in readme
    assert "Quick Scan" in readme
    assert "Smart Scan" in readme
    assert "Deep Scan" in readme
    assert "runtime/site_memory" in readme
    assert "structure fingerprint" in readme
    assert "127.0.0.1:8002" in readme
    assert "single-user local development tool" in readme
    assert "no authentication" in readme
    assert "Chrome debug mode and the API bound to localhost" in readme


def test_makefile_exposes_setup_launch_and_test_targets() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    assert re.search(r"^setup:", makefile, re.MULTILINE)
    assert re.search(r"^launch:", makefile, re.MULTILINE)
    assert re.search(r"^test:", makefile, re.MULTILINE)
    assert re.search(r"^reset-cache:", makefile, re.MULTILINE)
    assert re.search(r"^relaunch:", makefile, re.MULTILINE)


def test_launch_script_does_not_use_literal_ui_url_placeholder_when_closing_tabs() -> None:
    launch_script = Path("scripts/launch_local.sh").read_text(encoding="utf-8")
    assert "ui_prefix = '${UI_URL}'" not in launch_script


def test_apply_review_batch_executes_only_auto_approved_actions(tmp_path: Path) -> None:
    class ApplyCompanion(FakeCompanion):
        def __init__(self) -> None:
            super().__init__()
            self.auto_approve_calls = 0
            self.session.mode = "complex_workspace"
            self.session.status = "review_batch_ready"
            self.session.review_batch = ReviewBatch(
                session_id=self.session.session_id,
                summary="Prepared one safe action and one confirmation-gated action.",
                rationale=["Current page contains both safe and risky actions."],
                current_focus="Quarter 4 actions",
                previous_period_summary=[],
                items=[],
                actions=[
                    ActionProposal(
                        action_id="act_wait",
                        action="wait_for",
                        reasoning="Wait for the visible totals to refresh.",
                        confidence=0.7,
                        validation_text="Updated",
                        requires_confirmation=False,
                        safety_level="low",
                    ),
                    ActionProposal(
                        action_id="act_click",
                        action="click",
                        reasoning="Click the visible change button.",
                        confidence=0.82,
                        target_text="Apply change",
                        requires_confirmation=True,
                        safety_level="medium",
                    ),
                ],
                apply_ready=True,
                beta_warning="Apply is beta. Manual application is safer.",
            )
            self.session.pending_approvals = list(self.session.review_batch.actions)

        def approve_actions(self, session_id, action_ids):
            raise AssertionError("apply_review_batch must not auto-approve confirmation-gated actions")

        def auto_approve_executable_actions(self, session_id):
            self.auto_approve_calls += 1
            selected = []
            for action in self.session.pending_approvals:
                if not action.requires_confirmation:
                    action.status = "approved"
                    selected.append(action.model_dump(mode="json"))
            return selected

        def record_execution(self, payload):
            executed_ids = {item.get("action_id") for item in payload.results}
            for action in self.session.pending_approvals:
                if action.action_id in executed_ids:
                    action.status = "executed"
            self.session.status = "review_batch_ready"
            return self.session

    class ApplyBridge(FakeBridge):
        def __init__(self) -> None:
            super().__init__()
            self.executed_action_ids = []

        async def execute_actions(self, actions):
            self.executed_action_ids = [action.action_id for action in actions]
            return [{"action_id": action.action_id, "status": "executed"} for action in actions]

    class ApplyRuntime:
        def __init__(self) -> None:
            self.companion = ApplyCompanion()
            self.bridge = ApplyBridge()

    store = StateStore(tmp_path / "runtime")
    store.bootstrap()
    runtime = ApplyRuntime()
    app = create_app(orchestrator=FakeOrchestrator(), state_store=store, navigator_runtime=runtime)
    client = TestClient(app)

    response = client.post("/api/review-batch/apply", json={"session_id": "sess_test"})

    assert response.status_code == 200
    assert runtime.companion.auto_approve_calls == 1
    assert runtime.bridge.executed_action_ids == ["act_wait"]
    statuses = {action.action_id: action.status for action in runtime.companion.session.pending_approvals}
    assert statuses["act_wait"] == "executed"
    assert statuses["act_click"] == "proposed"


def test_index_site_rejects_duplicate_active_index(tmp_path: Path) -> None:
    class IndexingCompanion(FakeCompanion):
        def __init__(self) -> None:
            super().__init__()
            self.session.status = "indexing"

    class FailingIndexBridge(FakeBridge):
        async def capture_site_index(self, **kwargs):
            raise AssertionError("duplicate index requests must be rejected before capture starts")

    class DuplicateIndexRuntime:
        def __init__(self) -> None:
            self.companion = IndexingCompanion()
            self.bridge = FailingIndexBridge()

    store = StateStore(tmp_path / "runtime")
    store.bootstrap()
    runtime = DuplicateIndexRuntime()
    app = create_app(orchestrator=FakeOrchestrator(), state_store=store, navigator_runtime=runtime)
    client = TestClient(app)

    response = client.post("/api/index-site", json={"session_id": "sess_test"})

    assert response.status_code == 409
    assert "already indexing" in response.json()["detail"].lower()
