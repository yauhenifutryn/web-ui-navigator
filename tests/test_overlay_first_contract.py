from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marketplace_bot.api.main import create_app
from marketplace_bot.navigator_models import GoalSpec, IndexSummary, ReviewBatch, SessionMemory
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
        return {"mode": "idle", "kill_switch_active": False, "pending_submit_confirmation": False}


class FakeCompanion:
    def __init__(self) -> None:
        self.session = SessionMemory(
            session_id="sess_overlay",
            project_name="Overlay Demo",
            goal=GoalSpec(
                raw_goal="Test overlay-first workflow",
                objective="Test overlay-first workflow",
                safety_mode="confirm_before_act",
                created_at="2026-03-09T00:00:00Z",
            ),
            domain_pack="marketplace_simulation",
            current_site="",
            memory_summary="Index the site first.",
            live_advice=["Run the index first."],
            pending_approvals=[],
            status="session_ready_to_index",
            site_check_required=True,
            created_at="2026-03-09T00:00:00Z",
            updated_at="2026-03-09T00:00:00Z",
        )
        self.signature_updates: list[str] = []

    async def create_session(self, request):
        return self.session

    def list_sessions(self):
        return [self.session]

    def get_session(self, session_id):
        return self.session

    def resume_session(self, session_id):
        self.session.status = "session_ready_to_index"
        self.session.site_check_required = True
        return self.session

    def enter_live_advice_mode(self, session_id):
        self.session.status = "live_advice"
        return self.session

    def update_page_signature(self, session_id, signature):
        changed = getattr(self.session, "last_page_signature", "") != signature
        self.session.last_page_signature = signature
        self.signature_updates.append(signature)
        return changed

    def mark_indexing_progress(self, session_id, step: str, site_check_details=None):
        self.session.status = "indexing"
        self.session.updated_at = "2026-03-09T00:00:00Z"
        return self.session

    async def index_site(self, session_id, observation):
        self.session.status = "index_summary_ready"
        self.session.site_check_required = False
        self.session.last_indexed_at = observation.captured_at
        self.session.strategic_summary = "Indexed Marketplace workspace."
        self.session.index_summary = IndexSummary(
            strategic_summary="Indexed Marketplace workspace.",
            site_check_summary="Structure fingerprint matched local memory.",
            previous_period_summary=["Quarter 1 setup done.", "Quarter 2 entry done."],
            current_focus="Quarter 3 is editable.",
            top_recommendations=["Buy missing research.", "Review pricing for the editable quarter."],
            detected_changes=["Quarter 3 workspace is active."],
        )
        self.session.live_advice = ["Index complete."]
        return self.session

    async def plan(self, session_id, observation=None):
        self.session.status = "live_advice"
        self.session.live_advice = ["Live advice ready."]
        return None

    async def prepare_review_batch(self, session_id, observation=None):
        self.session.status = "review_batch_ready"
        self.session.review_batch = ReviewBatch(
            session_id=session_id,
            summary="Prepared one coherent change set for the editable quarter.",
            rationale=["Current quarter is editable.", "Recommendations are grounded in the indexed context."],
            current_focus="Quarter 3 decisions",
            previous_period_summary=["Quarter 1 setup done.", "Quarter 2 entry done."],
            actions=[],
            items=[
                {
                    "item_id": "item_open_stores",
                    "page_hint": "Open Stores",
                    "anchor_text": "Open Stores",
                    "field_label": "Store mix",
                    "recommendation": "Keep existing stores unchanged until demand evidence improves.",
                    "reasoning": "Indexed context does not show enough support for an expansion move yet.",
                    "priority": "medium",
                }
            ],
            apply_ready=False,
            beta_warning="Apply is beta. Manual application is safer.",
        )
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

    def record_execution(self, payload):
        self.session.status = "review_batch_ready"
        return self.session

    def finalize_review_batch(self, session_id):
        self.session.status = "review_batch_ready"
        return self.session


class FakeBridge:
    def __init__(self) -> None:
        self.overlay_panels: list[dict] = []
        self.bootstrap_calls = 0
        self.command_handler = None
        self.focus_calls = 0
        self.closed_ui_tabs: list[str] = []

    def register_command_handler(self, handler):
        self.command_handler = handler

    async def bootstrap_overlay(self):
        self.bootstrap_calls += 1

    async def sync_agent_overlay(self, panel):
        self.overlay_panels.append(panel)

    async def clear_agent_overlay(self):
        return None

    async def focus_active_page(self):
        self.focus_calls += 1

    async def close_local_ui_tabs(self, ui_url: str):
        self.closed_ui_tabs.append(ui_url)

    async def capture_site_index(self, **kwargs):
        return {
            "session_id": kwargs["session_id"],
            "page_url": "https://play.marketplace-simulation.com/current",
            "page_title": "Quarter 3",
            "visible_text_summary": "Quarter 3 current page",
            "dom_summary": "Quarter 3 current page",
            "active_goal": kwargs["active_goal"],
            "domain_pack": kwargs["domain_pack"],
            "safety_mode": kwargs["safety_mode"],
            "browser_metadata": {"site_index": {"navigation_items": ["Marketing", "Sales Channel"]}},
            "captured_at": "2026-03-09T00:00:00Z",
        }

    async def capture_observation(self, **kwargs):
        return {
            "session_id": kwargs["session_id"],
            "page_url": "https://play.marketplace-simulation.com/current",
            "page_title": "Quarter 3",
            "visible_text_summary": "Quarter 3 current page",
            "dom_summary": "Quarter 3 current page",
            "active_goal": kwargs["active_goal"],
            "domain_pack": kwargs["domain_pack"],
            "safety_mode": kwargs["safety_mode"],
            "browser_metadata": {"page_signature": "sig-1"},
            "captured_at": "2026-03-09T00:00:00Z",
        }

    async def execute_actions(self, actions):
        return []


class FakeNavigatorRuntime:
    def __init__(self) -> None:
        self.companion = FakeCompanion()
        self.bridge = FakeBridge()


def test_bootstrap_page_is_not_the_main_dashboard() -> None:
    html = Path("src/marketplace_bot/api/static/index.html").read_text(encoding="utf-8")
    js = Path("src/marketplace_bot/api/static/app.js").read_text(encoding="utf-8")

    assert "Controller Active" in html
    assert "session-tray" not in html
    assert "setInterval(" not in js
    assert "runLiveFollowCycle" not in js
    assert "/api/bootstrap-overlay" in js


def test_overlay_first_api_contract(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "runtime")
    store.bootstrap()
    runtime = FakeNavigatorRuntime()
    app = create_app(orchestrator=FakeOrchestrator(), state_store=store, navigator_runtime=runtime)
    client = TestClient(app)

    bootstrap = client.post("/api/bootstrap-overlay")
    assert bootstrap.status_code == 200
    assert runtime.bridge.bootstrap_calls == 1
    assert runtime.bridge.overlay_panels[-1]["view"] == "setup"
    assert runtime.bridge.overlay_panels[-1]["domain_pack"] == "generic_web"
    assert runtime.bridge.overlay_panels[-1]["index_mode"] == "advanced"

    started = client.post(
        "/api/overlay/command",
        json={
            "command": "start_session",
            "payload": {
                "project_name": "Overlay Demo",
                "goal": "Index then guide me.",
                "domain_hint": "marketplace_simulation",
                "index_mode": "advanced",
            },
        },
    )
    assert started.status_code == 200
    assert started.json()["ok"] is True
    assert runtime.bridge.overlay_panels[-1]["stage"] == "session_ready_to_index"
    assert runtime.bridge.overlay_panels[-1]["domain_pack"] == "marketplace_simulation"

    indexed = client.post("/api/overlay/command", json={"command": "start_index", "payload": {"session_id": "sess_overlay"}})
    assert indexed.status_code == 200
    assert runtime.bridge.overlay_panels[-1]["stage"] == "review_batch_ready"
    assert runtime.bridge.overlay_panels[-1]["review_batch"]["summary"] == "Prepared one coherent change set for the editable quarter."
    assert "Quarter 3" in str(runtime.bridge.overlay_panels[-1])

    advice = client.post(
        "/api/overlay/command",
        json={"command": "enter_live_advice", "payload": {"session_id": "sess_overlay"}},
    )
    assert advice.status_code == 200
    assert runtime.bridge.overlay_panels[-1]["stage"] == "live_advice"

    unchanged = client.post(
        "/api/overlay/command",
        json={"command": "page_changed", "payload": {"session_id": "sess_overlay", "page_signature": "sig-1"}},
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["ignored"] is False

    repeated = client.post(
        "/api/overlay/command",
        json={"command": "page_changed", "payload": {"session_id": "sess_overlay", "page_signature": "sig-1"}},
    )
    assert repeated.status_code == 200
    assert repeated.json()["ignored"] is True

    batch = client.post(
        "/api/overlay/command",
        json={"command": "prepare_review_batch", "payload": {"session_id": "sess_overlay"}},
    )
    assert batch.status_code == 200
    assert runtime.bridge.overlay_panels[-1]["stage"] == "review_batch_ready"
    assert runtime.bridge.overlay_panels[-1]["review_batch"]["beta_warning"] == "Apply is beta. Manual application is safer."

    apply_batch = client.post(
        "/api/overlay/command",
        json={"command": "apply_review_batch", "payload": {"session_id": "sess_overlay"}},
    )
    assert apply_batch.status_code == 409


def test_launch_script_opens_bootstrap_ui_inside_the_debug_chrome_session() -> None:
    launch = Path("scripts/launch_local.sh").read_text(encoding="utf-8")

    assert "/api/bootstrap-overlay" in launch
    assert "/json/new?${ENCODED_UI_URL}" in launch
    assert "open -a \"${CHROME_APP}\" \"${UI_URL}\"" not in launch


def test_launch_script_fails_fast_when_ui_server_never_becomes_healthy() -> None:
    launch = Path("scripts/launch_local.sh").read_text(encoding="utf-8")

    assert 'if ! curl -fsS "${UI_URL}/api/health" >/dev/null 2>&1; then' in launch
    assert 'echo "Live Navigator local server failed to become healthy on ${UI_URL}. Check ${RUNTIME_DIR}/launch.log." >&2' in launch
    assert "exit 1" in launch


def test_launch_script_detaches_ui_server_from_the_parent_terminal() -> None:
    launch = Path("scripts/launch_local.sh").read_text(encoding="utf-8")

    assert "subprocess.Popen(" in launch
    assert "start_new_session=True" in launch
    assert "stdin=subprocess.DEVNULL" in launch
    assert 'Path(sys.argv[1]).write_text(str(process.pid), encoding="utf-8")' in launch
