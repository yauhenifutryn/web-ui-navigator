import asyncio

import pytest

from marketplace_bot.navigator_models import ActionProposal, ObservationPacket


class FakeBridge:
    def __init__(self) -> None:
        self.captured = []
        self.executed = []
        self.overlay_updates = []

    async def capture_observation(self, **kwargs):
        self.captured.append(kwargs)
        return ObservationPacket(
            session_id=kwargs["session_id"],
            screenshot_b64="ZmFrZQ==",
            page_url="https://example.com",
            page_title="Example",
            visible_text_summary="Continue on screen",
            dom_summary="button Continue",
            active_goal=kwargs["active_goal"],
            domain_pack=kwargs["domain_pack"],
            safety_mode=kwargs["safety_mode"],
            captured_at="2026-03-08T00:00:00Z",
        )

    async def execute_actions(self, actions):
        self.executed.append(actions)
        return [{"action_id": action.action_id, "status": "executed"} for action in actions]

    async def sync_agent_overlay(self, panel):
        self.overlay_updates.append(panel)


class FakeRemoteClient:
    def __init__(self) -> None:
        self.created = None
        self.observations = []
        self.plan_calls = []
        self.approvals = []
        self.results = []
        self.pending_actions = [
            {
                "action_id": "auto_1",
                "action": "click",
                "reasoning": "Safe continue",
                "requires_confirmation": False,
                "status": "approved",
                "target_text": "Continue",
            },
            {
                "action_id": "confirm_1",
                "action": "click",
                "reasoning": "Sensitive submit",
                "requires_confirmation": True,
                "status": "approved",
                "target_text": "Submit",
            },
        ]

    async def health(self):
        return {"ok": True}

    async def create_session(self, request):
        self.created = request
        return {
            "session_id": "sess_cloud",
            "project_name": request.project_name,
            "domain_pack": "generic_web",
            "pending_approvals": [],
        }

    async def observe(self, payload):
        self.observations.append(payload)
        return {"session_id": payload.session_id}

    async def plan(self, session_id, observation=None):
        self.plan_calls.append((session_id, observation))
        return {
            "session_id": session_id,
            "memory_summary": "summary",
            "live_advice": ["Keep moving."],
            "actions": [
                {
                    "action_id": "auto_1",
                    "action": "click",
                    "reasoning": "Safe continue",
                    "requires_confirmation": False,
                    "confidence": 0.9,
                    "target_text": "Continue",
                },
                {
                    "action_id": "confirm_1",
                    "action": "click",
                    "reasoning": "Sensitive submit",
                    "requires_confirmation": True,
                    "confidence": 0.8,
                    "target_text": "Submit",
                },
            ],
        }

    async def approve(self, session_id, payload):
        self.approvals.append((session_id, payload.action_ids))
        return {"session_id": session_id, "approved_actions": payload.action_ids}

    async def get_session(self, session_id):
        return {
            "session_id": session_id,
            "project_name": "Demo",
            "domain_pack": "generic_web",
            "pending_approvals": self.pending_actions,
            "action_history": [],
            "goal": {
                "raw_goal": "Help me navigate",
                "objective": "Finish the flow",
                "constraints": [],
                "success_criteria": [],
                "domain_pack": "generic_web",
                "domain_hints": {},
                "safety_mode": "confirm_before_act",
                "created_at": "2026-03-08T00:00:00Z",
            },
            "created_at": "2026-03-08T00:00:00Z",
            "updated_at": "2026-03-08T00:00:00Z",
        }

    async def execute_result(self, payload):
        self.results.append(payload)
        return {"session_id": payload.session_id, "status": "ready"}


def test_run_connect_loop_executes_auto_and_confirmed_actions():
    async def _run():
        from marketplace_bot.cli.connect_cloud import run_connect_loop

        bridge = FakeBridge()
        client = FakeRemoteClient()

        outcome = await run_connect_loop(
            bridge=bridge,
            remote_client=client,
            goal="Help me navigate",
            project_name="Demo",
            domain_hint="generic_web",
            safety_mode="confirm_before_act",
            poll_interval=0.0,
            max_loops=1,
            approval_callback=lambda actions: ["confirm_1"],
            sleep_fn=lambda _: asyncio.sleep(0),
        )

        assert outcome["session_id"] == "sess_cloud"
        assert bridge.captured
        assert client.observations
        assert client.plan_calls
        assert client.approvals == [("sess_cloud", ["auto_1"]), ("sess_cloud", ["confirm_1"])]
        assert bridge.executed
        executed_ids = [action.action_id for action in bridge.executed[0]]
        assert executed_ids == ["auto_1", "confirm_1"]
        assert client.results
        stages = [item["stage"] for item in bridge.overlay_updates]
        assert "indexing" in stages
        assert "planning" in stages
        assert "executing" in stages
        assert stages[-1] == "ready"

    asyncio.run(_run())


def test_check_cdp_available_reports_missing_endpoint(monkeypatch):
    from marketplace_bot.cli.connect_cloud import check_cdp_available

    class FakeResponse:
        status_code = 404

        def json(self):
            return {}

    async def fake_get(*args, **kwargs):
        return FakeResponse()

    monkeypatch.setattr("marketplace_bot.cli.connect_cloud._get_cdp_version", fake_get)
    ok, message = asyncio.run(check_cdp_available("http://localhost:9222"))
    assert ok is False
    assert "--remote-debugging-port=9222" in message
