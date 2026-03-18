import asyncio

import httpx

from marketplace_bot.navigator_models import ObservationPacket
from marketplace_bot.remote_client import RemoteNavigatorClient


def test_remote_client_plan_sends_observation_payload(monkeypatch):
    captured = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            captured["url"] = url
            captured["json"] = json
            return httpx.Response(
                200,
                json={"session_id": "sess_1", "actions": []},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("marketplace_bot.remote_client.httpx.AsyncClient", FakeAsyncClient)
    client = RemoteNavigatorClient("https://example.com")
    observation = ObservationPacket(
        session_id="sess_1",
        screenshot_b64="ZmFrZQ==",
        page_url="https://example.com",
        page_title="Example",
        visible_text_summary="Summary",
        dom_summary="DOM",
        active_goal="Goal",
        domain_pack="generic_web",
        safety_mode="confirm_before_act",
        captured_at="2026-03-08T00:00:00Z",
    )

    asyncio.run(client.plan("sess_1", observation))

    assert captured["url"] == "https://example.com/plan"
    assert captured["json"]["session_id"] == "sess_1"
    assert captured["json"]["observation"]["screenshot_b64"] == "ZmFrZQ=="


def test_remote_client_index_site_sends_observation_payload(monkeypatch):
    captured = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            captured["url"] = url
            captured["json"] = json
            return httpx.Response(
                200,
                json={"session_id": "sess_1", "status": "ready"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("marketplace_bot.remote_client.httpx.AsyncClient", FakeAsyncClient)
    client = RemoteNavigatorClient("https://example.com")
    observation = ObservationPacket(
        session_id="sess_1",
        page_url="https://example.com",
        page_title="Example",
        visible_text_summary="Summary",
        dom_summary="DOM",
        active_goal="Goal",
        domain_pack="generic_web",
        safety_mode="confirm_before_act",
        browser_metadata={"site_index": {"navigation_items": ["Home"]}},
        captured_at="2026-03-08T00:00:00Z",
    )

    asyncio.run(client.index_site("sess_1", observation))

    assert captured["url"] == "https://example.com/index-site"
    assert captured["json"]["session_id"] == "sess_1"
    assert captured["json"]["observation"]["browser_metadata"]["site_index"]["navigation_items"] == ["Home"]
