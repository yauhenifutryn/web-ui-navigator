import base64
import asyncio
from types import SimpleNamespace

from marketplace_bot.llm.gemini_provider import GeminiLLMClient
from marketplace_bot.navigator_models import GoalSpec, ObservationPacket


def test_build_multimodal_contents_includes_image_when_screenshot_present():
    screenshot_b64 = base64.b64encode(b"fake-png-bytes").decode("utf-8")

    contents = GeminiLLMClient._build_multimodal_contents("Plan this page", screenshot_b64)

    assert len(contents) == 2
    assert contents[0] == "Plan this page"


def test_build_multimodal_contents_uses_text_only_without_screenshot():
    contents = GeminiLLMClient._build_multimodal_contents("Plan this page", "")

    assert contents == ["Plan this page"]


def test_build_multimodal_contents_appends_supplementary_images():
    screenshot_b64 = base64.b64encode(b"fake-png-bytes").decode("utf-8")
    extra_b64 = base64.b64encode(b"extra-png-bytes").decode("utf-8")

    contents = GeminiLLMClient._build_multimodal_contents(
        "Plan this page",
        screenshot_b64,
        supplementary_screenshots=[{"label": "table_slice_1", "screenshot_b64": extra_b64}],
    )

    assert len(contents) == 3
    assert contents[0] == "Plan this page"


def test_index_context_prompt_includes_ax_summary_after_screenshot_grounding(monkeypatch):
    client = GeminiLLMClient.__new__(GeminiLLMClient)
    client.analysis_model_name = "gemini-test"

    captured = {}

    def fake_generate(model_name, prompt, screenshot_b64, schema, supplementary_screenshots=None):
        captured["model_name"] = model_name
        captured["prompt"] = prompt
        captured["screenshot_b64"] = screenshot_b64
        captured["supplementary_screenshots"] = supplementary_screenshots
        return '{"strategic_summary":"Indexed","workflow_stage":"live","next_focus":[],"ui_map":[],"signals":[]}'

    monkeypatch.setattr(client, "_generate_multimodal_json", fake_generate)

    goal = GoalSpec(
        raw_goal="Continue the flow",
        objective="Continue the flow",
        constraints=["Use screenshots first."],
        success_criteria=["Finish the flow."],
        created_at="2026-03-08T00:00:00Z",
    )
    observation = ObservationPacket(
        session_id="sess_ax",
        screenshot_b64=base64.b64encode(b"fake-png-bytes").decode("utf-8"),
        page_url="https://example.com",
        page_title="Example",
        visible_text_summary="Continue on screen",
        dom_summary="button Continue",
        active_goal="Continue the flow",
        domain_pack="generic_web",
        safety_mode="confirm_before_act",
        browser_metadata={
            "ax_summary": "AX: 1 interactive node, 0 blocked, 0 likely occluded",
            "ax_targets": [{"name": "Continue", "role": "button"}],
        },
        supplementary_screenshots=[
            {"label": "table_slice_1", "screenshot_b64": base64.b64encode(b"table-slice").decode("utf-8")}
        ],
        captured_at="2026-03-08T00:00:00Z",
    )
    domain_pack = SimpleNamespace(description="Generic web navigation", goal_guidance=[], strategy_hints=[])

    asyncio.run(client.index_context(goal, observation, domain_pack))

    assert captured["model_name"] == "gemini-test"
    assert captured["screenshot_b64"] == observation.screenshot_b64
    assert captured["supplementary_screenshots"] == observation.supplementary_screenshots
    assert "Ground your reasoning primarily in the screenshot" in captured["prompt"]
    assert "AX SUMMARY" in captured["prompt"]
    assert "AX TARGETS" in captured["prompt"]
