from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from typing import Any

from marketplace_bot.llm.base import BaseLLMClient

try:
    from google import genai
    from google.genai import types
except Exception:  # pragma: no cover
    genai = None
    types = None


STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["meta", "quarter", "data", "errors"],
    "properties": {
        "meta": {"type": "object"},
        "quarter": {"type": "object"},
        "data": {"type": "object"},
        "errors": {"type": "array", "items": {"type": "string"}},
    },
}

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["decisions"],
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["action"],
                "properties": {
                    "action": {"type": "string"},
                    "quarter": {"type": "string"},
                    "apply_allowed": {"type": "boolean"},
                    "target": {"type": "string"},
                    "city": {"type": "string"},
                    "field": {"type": "string"},
                    "value": {"type": "string"},
                    "current_value": {"type": "string"},
                    "recommended_value": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "implementation_steps": {"type": "string"},
                    "kpi": {"type": "string"},
                    "evidence": {"type": "string"},
                    "risk_level": {"type": "string"},
                    "priority": {"type": "string"},
                    "expected_revenue_delta": {"type": "number"},
                    "confidence": {"type": "number"},
                },
            },
        }
    },
}

ACTION_PROPERTIES: dict[str, Any] = {
    "action_id": {"type": "string"},
    "action": {"type": "string"},
    "reasoning": {"type": "string"},
    "confidence": {"type": "number"},
    "target_text": {"type": "string"},
    "role": {"type": "string"},
    "input_text": {"type": "string"},
    "value": {"type": "string"},
    "url": {"type": "string"},
    "validation_text": {"type": "string"},
    "metadata": {"type": "object"},
}

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["memory_summary", "live_advice", "actions"],
    "properties": {
        "memory_summary": {"type": "string"},
        "live_advice": {"type": "array", "items": {"type": "string"}},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["action", "reasoning"],
                "properties": ACTION_PROPERTIES,
            },
        },
    },
}

INDEX_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["strategic_summary", "workflow_stage", "next_focus", "ui_map", "signals"],
    "properties": {
        "strategic_summary": {"type": "string"},
        "workflow_stage": {"type": "string"},
        "next_focus": {"type": "array", "items": {"type": "string"}},
        "ui_map": {
            "type": "array",
            "items": {"type": "object", "properties": {"label": {"type": "string"}, "kind": {"type": "string"}, "reason": {"type": "string"}}},
        },
        "signals": {"type": "array", "items": {"type": "string"}},
    },
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "current_focus", "previous_period_summary", "rationale", "items", "actions", "apply_ready", "beta_warning"],
    "properties": {
        "summary": {"type": "string"},
        "current_focus": {"type": "string"},
        "previous_period_summary": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "array", "items": {"type": "string"}},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["item_id", "title", "why_it_matters", "evidence"],
                "properties": {
                    "item_id": {"type": "string"},
                    "title": {"type": "string"},
                    "page_hint": {"type": "string"},
                    "anchor_text": {"type": "string"},
                    "field_type": {"type": "string"},
                    "current_value": {"type": "string"},
                    "recommended_value": {"type": "string"},
                    "recommended_range": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "evidence": {"type": "string"},
                    "priority": {"type": "string"},
                    "confidence": {"type": "number"},
                    "actionability": {"type": "string"},
                    "dependencies": {"type": "array", "items": {"type": "string"}},
                    "requires_followup_check": {"type": "boolean"},
                },
            },
        },
        "actions": {
            "type": "array",
            "items": {"type": "object", "required": ["action", "reasoning"], "properties": ACTION_PROPERTIES},
        },
        "apply_ready": {"type": "boolean"},
        "insufficiently_grounded": {"type": "boolean"},
        "comparison_payload": {"type": "object"},
        "beta_warning": {"type": "string"},
    },
}


class GeminiLLMClient(BaseLLMClient):
    def __init__(
        self,
        api_key: str | None = None,
        model_name: str = "gemini-3-flash",
        analysis_model_name: str | None = None,
        live_model_name: str | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.model_name = model_name
        self.analysis_model_name = analysis_model_name or model_name
        self.live_model_name = live_model_name or model_name
        if genai is None or types is None:
            raise RuntimeError("google-genai is not installed")
        if os.getenv("GOOGLE_CLOUD_PROJECT"):
            self.client = genai.Client(vertexai=True, project=os.getenv("GOOGLE_CLOUD_PROJECT"), location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"))
        else:
            if not self.api_key:
                raise RuntimeError("GEMINI_API_KEY/GOOGLE_API_KEY is not set")
            self.client = genai.Client(api_key=self.api_key)

    async def extract_state(self, semantic_text: str) -> dict[str, Any]:
        clipped_text = self._clip_text(semantic_text, max_chars=90000)
        prompt = (
            "You are a strict JSON extraction engine for business simulation data. "
            "Extract current quarter state, financial summary, staffing values, and any actionable metrics. "
            "Return JSON only with keys meta, quarter, data, errors. "
            "No markdown, no explanation.\n\n"
            f"SOURCE TEXT:\n{clipped_text}"
        )
        raw = await asyncio.to_thread(self._generate_json, prompt, STATE_SCHEMA)
        parsed = self._load_json(raw)
        if not isinstance(parsed, dict):
            raise RuntimeError("Gemini state response was not a JSON object")
        parsed.setdefault("meta", {})
        parsed.setdefault("quarter", {})
        parsed.setdefault("data", {})
        parsed.setdefault("errors", [])
        return parsed

    async def generate_decisions(self, state: dict[str, Any], mode: str) -> tuple[str, list[dict[str, Any]]]:
        if mode.startswith("audit"):
            mode_instructions = (
                "Mode is AUDIT. You must evaluate all completed quarters in state.data.completed_quarters and suggest edits. "
                "Only the latest quarter in state.data.editable_quarter is editable. "
                "Return recommendations only, no direct execution clicks. "
                "For editable quarter recommendations use action='audit_edit' and apply_allowed=true with exact changes. "
                "For confirmed best choices use action='audit_keep' and apply_allowed=false. "
                "For historical-quarter learning notes use action='audit_insight' and apply_allowed=false. "
                "Editable-quarter `audit_edit` items must be specific, for example exact store locations, product/channel changes, staffing levels, and budget shifts. "
                "Each item must include quarter, target, field, current_value, recommended_value, recommendation, reasoning, implementation_steps, kpi, evidence, risk_level, priority, expected_revenue_delta, confidence. "
                "Prioritize recommendations by profit impact and actionability. "
                "If data is partially insufficient, do not return empty decisions; return at least audit_insight items with missing data, confidence reduction, and concrete next checks."
            )
            if mode == "audit_retry":
                mode_instructions += (
                    " This is a retry because previous output had no recommendations. "
                    "Produce at least 3 items with at least 1 audit_edit for editable quarter whenever completed quarter data exists."
                )
        else:
            mode_instructions = (
                "Mode is EXECUTE. Propose executable decisions only from this set: "
                "click, click_role, set_value, fill_label, navigate, submit_quarter, noop. "
                "Do not invent unsupported actions."
            )

        clipped_state = self._clip_text(json.dumps(state, ensure_ascii=False), max_chars=90000)
        prompt = (
            "You are a strict JSON decision engine for Marketplace Simulation. "
            "Objective: maximize profit. "
            "Return JSON only as {\"decisions\": [...]} with no surrounding text. "
            "Each decision must include action and may include quarter/target/city/field/value/reasoning/expected_revenue_delta/confidence. "
            f"{mode_instructions}\n\n"
            f"STATE JSON:\n{clipped_state}"
        )
        raw = await asyncio.to_thread(self._generate_json, prompt, DECISION_SCHEMA)
        parsed = self._load_json(raw)
        decisions: list[dict[str, Any]] = []
        if isinstance(parsed, dict):
            payload = parsed.get("decisions", [])
            if isinstance(payload, list):
                decisions = [item for item in payload if isinstance(item, dict)]
        return raw, decisions

    def _build_context_sections(
        self,
        goal: Any,
        observation: Any,
        domain_pack: Any,
        *,
        indexed_context: dict[str, Any] | None = None,
        strategic_summary: str = "",
        metadata_limit: int = 12000,
        visible_text_limit: int = 18000,
        dom_limit: int = 12000,
        indexed_context_limit: int = 0,
        include_prior_actions: bool = False,
    ) -> str:
        sections = [
            f"GOAL OBJECTIVE:\n{goal.objective}\n",
            f"GOAL CONSTRAINTS:\n{json.dumps(goal.constraints, ensure_ascii=False)}\n",
            f"SUCCESS CRITERIA:\n{json.dumps(goal.success_criteria, ensure_ascii=False)}\n",
            f"DOMAIN PACK:\n{getattr(domain_pack, 'description', '')}\n",
            f"DOMAIN GUIDANCE:\n{json.dumps(getattr(domain_pack, 'goal_guidance', []), ensure_ascii=False)}\n",
            f"STRATEGY HINTS:\n{json.dumps(getattr(domain_pack, 'strategy_hints', []), ensure_ascii=False)}\n",
        ]
        if strategic_summary:
            sections.append(f"STRATEGIC SUMMARY:\n{strategic_summary}\n")
        if indexed_context and indexed_context_limit:
            sections.append(f"INDEXED CONTEXT:\n{self._clip_text(json.dumps(indexed_context, ensure_ascii=False), indexed_context_limit)}\n")
        sections.extend([
            f"PAGE URL:\n{observation.page_url}\n",
            f"PAGE TITLE:\n{observation.page_title}\n",
            self._ax_prompt_sections(observation),
            f"BROWSER METADATA:\n{self._clip_text(json.dumps(observation.browser_metadata, ensure_ascii=False), metadata_limit)}\n",
            f"VISIBLE TEXT SUMMARY:\n{self._clip_text(observation.visible_text_summary, visible_text_limit)}\n",
            f"DOM SUMMARY:\n{self._clip_text(observation.dom_summary, dom_limit)}\n",
        ])
        if include_prior_actions:
            sections.append(f"PRIOR ACTIONS:\n{json.dumps(observation.prior_actions[-10:], ensure_ascii=False)}\n")
        return "\n".join(sections)

    async def _multimodal_request(
        self,
        model_name: str,
        prompt: str,
        observation: Any,
        schema: dict[str, Any],
        defaults: dict[str, Any],
    ) -> dict[str, Any]:
        raw = await asyncio.to_thread(
            self._generate_multimodal_json,
            model_name,
            prompt,
            observation.screenshot_b64 or "",
            schema,
            getattr(observation, "supplementary_screenshots", []) or [],
        )
        parsed = self._load_json(raw)
        if not isinstance(parsed, dict):
            return dict(defaults)
        for key, value in defaults.items():
            parsed.setdefault(key, value)
        return parsed

    async def index_context(self, goal: Any, observation: Any, domain_pack: Any) -> dict[str, Any]:
        system_instruction = (
            "You are the strategic indexing brain for a live UI navigator. "
            "Build a durable, concise understanding of the current website or app so that a faster live advisor can react immediately on later screens. "
            "Ground your reasoning primarily in the screenshot, secondarily in AX summaries, and thirdly in visible text and DOM summary. "
            "Return JSON only with keys strategic_summary, workflow_stage, next_focus, ui_map, signals.\n\n"
        )
        context = self._build_context_sections(goal, observation, domain_pack)
        return await self._multimodal_request(
            self.analysis_model_name,
            system_instruction + context,
            observation,
            INDEX_SCHEMA,
            {"strategic_summary": "", "workflow_stage": "", "next_focus": [], "ui_map": [], "signals": []},
        )

    async def review_workflow(
        self,
        goal: Any,
        observation: Any,
        domain_pack: Any,
        indexed_context: dict[str, Any] | None = None,
        strategic_summary: str = "",
    ) -> dict[str, Any]:
        system_instruction = (
            "You are the deep review brain for a UI navigator. "
            "Use the screenshot, the indexed site context, and the user objective to prepare one coherent review of the current workflow. "
            "Use AX summaries to understand concrete controls, regions, and why something might be visible but not actionable. "
            "If the domain is a business simulation, summarize previous periods briefly and focus recommendations on the latest editable quarter only. "
            "Produce a table-by-table review of the editable workflow areas found during indexing, not just a couple of generic suggestions. "
            "Whenever exact values or costs are visible in the screenshot or indexed context, include those numbers explicitly in the recommendation and reasoning. "
            "If exact values are not visible, say that they remain unconfirmed and name the page or table that must be checked next. "
            "Return JSON only with keys summary, current_focus, previous_period_summary, rationale, items, actions, apply_ready, insufficiently_grounded, comparison_payload, beta_warning. "
            "Items should use the typed review shape with title, page_hint, anchor_text, field_type, current_value, recommended_value or recommended_range, why_it_matters, evidence, priority, confidence, actionability, dependencies, and requires_followup_check. "
            "Actions should contain only low-risk, directly executable UI steps. If execution would be risky or under-specified, leave actions empty and set apply_ready to false. "
            "Always set beta_warning to a sentence that says manual application is safer.\n\n"
        )
        context = self._build_context_sections(
            goal, observation, domain_pack,
            indexed_context=indexed_context or {},
            strategic_summary=strategic_summary,
            metadata_limit=16000, visible_text_limit=20000, dom_limit=16000,
            indexed_context_limit=24000,
        )
        return await self._multimodal_request(
            self.analysis_model_name,
            system_instruction + context,
            observation,
            REVIEW_SCHEMA,
            {
                "summary": "", "current_focus": "", "previous_period_summary": [],
                "rationale": [], "items": [], "actions": [], "apply_ready": False,
                "insufficiently_grounded": False, "comparison_payload": {},
                "beta_warning": "Apply is beta. Manual application is safer.",
            },
        )

    async def plan_actions(
        self,
        goal: Any,
        observation: Any,
        domain_pack: Any,
        indexed_context: dict[str, Any] | None = None,
        strategic_summary: str = "",
    ) -> dict[str, Any]:
        system_instruction = (
            "You are the fast live advice planner for a UI navigator. "
            "Respond quickly using the current screenshot as the primary grounding source, AX summaries as the secondary grounding source, and use the cached strategic context to avoid rethinking the whole site from scratch. "
            "Return JSON only with keys memory_summary, live_advice, actions. "
            "Use actions only from this set: navigate, click, type, select, scroll, wait_for, extract, suggest_only, stop. "
            "Prefer suggest_only when confidence is weak. "
            "If AX data suggests a control is hidden, disabled, pointer-blocked, offscreen, or covered, avoid executable actions unless revalidated. "
            "Any submit, purchase, delete, account, or save-like step should be treated as sensitive.\n\n"
        )
        context = self._build_context_sections(
            goal, observation, domain_pack,
            indexed_context=indexed_context or {},
            strategic_summary=strategic_summary,
            indexed_context_limit=10000,
            include_prior_actions=True,
        )
        return await self._multimodal_request(
            self.live_model_name,
            system_instruction + context,
            observation,
            PLAN_SCHEMA,
            {"memory_summary": "", "live_advice": [], "actions": []},
        )

    def _call_with_retry(self, model_name: str, contents: Any, schema: dict[str, Any]) -> str:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                response = self.client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema),
                )
                text = getattr(response, "text", "")
                if text:
                    return text
                candidates = getattr(response, "candidates", None)
                if candidates:
                    try:
                        parts = candidates[0].content.parts
                        return "".join(getattr(part, "text", "") for part in parts)
                    except Exception:
                        return ""
                return ""
            except Exception as exc:
                last_exc = exc
                msg = str(exc).upper()
                if "503" in msg or "UNAVAILABLE" in msg:
                    time.sleep(2**attempt)
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        return ""

    def _generate_json(self, prompt: str, schema: dict[str, Any]) -> str:
        return self._call_with_retry(self.analysis_model_name, prompt, schema)

    def _generate_multimodal_json(
        self,
        model_name: str,
        prompt: str,
        screenshot_b64: str,
        schema: dict[str, Any],
        supplementary_screenshots: list[dict[str, Any]] | None = None,
    ) -> str:
        contents = self._build_multimodal_contents(
            prompt,
            screenshot_b64,
            supplementary_screenshots=supplementary_screenshots,
        )
        return self._call_with_retry(model_name, contents, schema)

    @staticmethod
    def _build_multimodal_contents(
        prompt: str,
        screenshot_b64: str,
        supplementary_screenshots: list[Any] | None = None,
    ) -> list[Any]:
        contents: list[Any] = [prompt]
        if not screenshot_b64:
            supplementary_screenshots = supplementary_screenshots or []
        else:
            image_bytes = base64.b64decode(screenshot_b64)
            try:
                contents.append(types.Part.from_bytes(data=image_bytes, mime_type="image/png"))
            except Exception:
                contents.append({"inline_data": {"mime_type": "image/png", "data": screenshot_b64}})
        for item in supplementary_screenshots or []:
            raw = item.get("screenshot_b64", "") if isinstance(item, dict) else getattr(item, "screenshot_b64", "")
            screenshot_data = str(raw or "").strip()
            if not screenshot_data:
                continue
            image_bytes = base64.b64decode(screenshot_data)
            try:
                contents.append(types.Part.from_bytes(data=image_bytes, mime_type="image/png"))
            except Exception:
                contents.append({"inline_data": {"mime_type": "image/png", "data": screenshot_data}})
        return contents

    @staticmethod
    def _load_json(raw_text: str) -> Any:
        cleaned = raw_text.strip()
        if not cleaned:
            return {}
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start == -1 or end == -1 or end <= start:
                return {}
            return json.loads(cleaned[start : end + 1])

    @staticmethod
    def _clip_text(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        half = max_chars // 2
        return f"{text[:half]}\n\n[TRUNCATED FOR SIZE]\n\n{text[-half:]}"

    def _ax_prompt_sections(self, observation: Any) -> str:
        metadata = getattr(observation, "browser_metadata", {}) or {}
        ax_summary = str(metadata.get("ax_summary", "") or "").strip()
        ax_targets = metadata.get("ax_targets", []) or []
        ax_diagnostics = metadata.get("ax_diagnostics", {}) or {}
        if not ax_summary and not ax_targets and not ax_diagnostics:
            return ""
        targets_text = self._clip_text(json.dumps(ax_targets, ensure_ascii=False), 4000) if ax_targets else "[]"
        diagnostics_text = self._clip_text(json.dumps(ax_diagnostics, ensure_ascii=False), 2000) if ax_diagnostics else "{}"
        return (
            f"AX SUMMARY:\n{self._clip_text(ax_summary, 4000)}\n\n"
            f"AX TARGETS:\n{targets_text}\n\n"
            f"AX DIAGNOSTICS:\n{diagnostics_text}\n\n"
        )
