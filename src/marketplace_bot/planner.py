from __future__ import annotations

import logging
import re
import uuid
from typing import Any
from urllib.parse import urlparse

from marketplace_bot.domain_packs import get_domain_pack
from marketplace_bot.navigator_models import ActionProposal, ObservationPacket, PlanResponse, ReviewBatch, ReviewItem, SessionMemory
from marketplace_bot.safety import SafetyPolicy
from marketplace_bot.site_intelligence import merge_site_index


logger = logging.getLogger(__name__)


class PlannerService:
    def __init__(self, llm_client: Any, safety_policy: SafetyPolicy | None = None) -> None:
        self.llm_client = llm_client
        self.safety_policy = safety_policy or SafetyPolicy()

    async def index(self, session: SessionMemory, observation: ObservationPacket) -> dict[str, Any]:
        domain_pack = get_domain_pack(session.domain_pack)
        site_check = observation.browser_metadata.get("site_check", {})
        cached_context = observation.browser_metadata.get("site_memory_context", {})
        cached_indexed_context = dict(cached_context.get("indexed_context", {}) or {})
        cached_site_index = dict(cached_indexed_context.get("site_index", {}) or {})
        current_site_index = observation.browser_metadata.get("site_index", {})
        merged_site_index = merge_site_index(cached_site_index, current_site_index) if cached_site_index else current_site_index

        if site_check.get("change_status") == "unchanged" and cached_indexed_context:
            strategic_summary = str(cached_context.get("strategic_summary", "")).strip() or self._fallback_index_summary(session, observation)
            cached_indexed_context["site_index"] = merged_site_index
            live_advice = [
                "Site memory matched the current structure, so the agent reused cached context and refreshed only the current page.",
            ]
            if session.domain_pack == "marketplace_simulation":
                live_advice.append("Only changed or currently visible simulation pages need to be refreshed going forward.")
            return {
                "strategic_summary": strategic_summary,
                "indexed_context": cached_indexed_context,
                "memory_summary": "Loaded durable local site memory and refreshed only the current page structure.",
                "live_advice": live_advice,
            }

        try:
            indexed = await self.llm_client.index_context(session.goal, observation, domain_pack)
        except Exception:
            logger.warning("Planner index context generation failed; using fallback index summary.", exc_info=True)
            indexed = {}
        merged = {**indexed, "site_index": merged_site_index}
        strategic_summary = str(merged.get("strategic_summary", "")).strip() or self._fallback_index_summary(session, observation)
        next_focus = list(merged.get("next_focus", []))
        live_advice = self._index_advice(session, observation, next_focus)
        return {
            "strategic_summary": strategic_summary,
            "indexed_context": merged,
            "memory_summary": f"Indexed {observation.page_title or observation.page_url}. The agent can now react faster on later screens.",
            "live_advice": live_advice,
        }

    async def review(self, session: SessionMemory, observation: ObservationPacket) -> ReviewBatch:
        domain_pack = get_domain_pack(session.domain_pack)
        payload: dict[str, Any] = {}
        try:
            payload = await self.llm_client.review_workflow(
                session.goal,
                observation,
                domain_pack,
                indexed_context=session.indexed_context,
                strategic_summary=session.strategic_summary,
            )
        except Exception:
            logger.warning("Planner review generation failed; using fallback review.", exc_info=True)
            payload = {}

        batch = self._normalize_review(payload, session, observation)
        fallback = self._fallback_review(session, observation)
        if session.domain_pack == "marketplace_simulation":
            if self._marketplace_fallback_is_page_specific(fallback, observation):
                page_relevant_llm_items = self._filter_items_for_page(batch.items, observation)
                if page_relevant_llm_items:
                    filtered_batch = ReviewBatch(
                        session_id=batch.session_id,
                        summary=batch.summary,
                        rationale=batch.rationale,
                        current_focus=batch.current_focus,
                        previous_period_summary=batch.previous_period_summary,
                        items=page_relevant_llm_items,
                        actions=batch.actions,
                        apply_ready=batch.apply_ready,
                        comparison_payload=batch.comparison_payload,
                        beta_warning=batch.beta_warning,
                    )
                    batch = self._merge_review_batches(fallback, filtered_batch)
                else:
                    batch = fallback
            elif fallback.actions:
                batch = fallback if not batch.actions else self._merge_review_batches(fallback, batch)
            else:
                batch = self._merge_review_batches(batch, fallback)
        elif not batch.items and not batch.actions:
            batch = fallback
        batch.insufficiently_grounded = self._review_is_insufficiently_grounded(batch, session)
        return batch

    async def plan(self, session: SessionMemory, observation: ObservationPacket) -> PlanResponse:
        index_refreshed = False
        try:
            domain_pack = get_domain_pack(session.domain_pack)
            if self._needs_reindex(session, observation):
                indexed = await self.llm_client.index_context(session.goal, observation, domain_pack)
                session.strategic_summary = str(indexed.get("strategic_summary", "")).strip()
                session.indexed_context = indexed
                session.last_indexed_at = observation.captured_at
                index_refreshed = True

            raw = await self.llm_client.plan_actions(
                session.goal,
                observation,
                domain_pack,
                indexed_context=session.indexed_context,
                strategic_summary=session.strategic_summary,
            )
            return self._normalize_response(raw, session, observation, index_refreshed=index_refreshed)
        except Exception:
            logger.warning("Planner action generation failed; falling back to deterministic plan.", exc_info=True)
            return self._fallback_plan(session, observation)

    def build_inline_notes(self, session: SessionMemory, observation: ObservationPacket) -> list[dict[str, Any]]:
        if session.mode == "review_only":
            return []
        if session.review_batch is None:
            return []
        matched: list[dict[str, Any]] = []
        visible = observation.visible_text_summary.lower()
        title = observation.page_title.lower()
        url = observation.page_url.lower()

        def _tokens(value: str) -> set[str]:
            tokens = {token for token in re.split(r"[^a-z0-9]+", value.lower()) if token}
            expanded = set(tokens)
            for token in list(tokens):
                if token.endswith("s") and len(token) > 3:
                    expanded.add(token[:-1])
            return expanded

        page_tokens = _tokens(title) | _tokens(url)
        direct_page_matches: list[ReviewItem] = []
        for item in session.review_batch.items:
            page_hint = item.page_hint.lower() if item.page_hint else ""
            if not page_hint:
                continue
            hint_tokens = _tokens(page_hint)
            if page_hint in title or page_hint in url:
                direct_page_matches.append(item)
                continue
            if hint_tokens and page_tokens and len(hint_tokens & page_tokens) >= max(1, min(len(hint_tokens), 2)):
                direct_page_matches.append(item)

        candidate_items = direct_page_matches or list(session.review_batch.items)

        scored_items: list[tuple[int, ReviewItem]] = []
        for item in candidate_items:
            score = 0
            if item.page_hint and item.page_hint.lower() in title:
                score += 6
            if item.page_hint and item.page_hint.lower() in url:
                score += 4
            if item.field_label and item.field_label.lower() in visible:
                score += 3
            if item.anchor_text and item.anchor_text.lower() in visible:
                score += 2
            if score > 0:
                scored_items.append((score, item))

        if not scored_items:
            if direct_page_matches:
                scored_items = [(1, item) for item in direct_page_matches[:3]]
            else:
                return []

        for _, item in sorted(scored_items, key=lambda pair: pair[0], reverse=True)[:3]:
            recommendation = item.recommended_value or item.recommended_range or item.title
            verification = "Verify the visible table updates and the downstream summary still supports the change."
            if item.requires_followup_check:
                verification = "Verify the field updates cleanly, then re-check the adjacent table or summary page before moving on."
            matched.append(
                {
                    "note_id": item.item_id,
                    "title": item.title or item.page_hint or "Suggested change",
                    "body": recommendation,
                    "reasoning": f"{item.why_it_matters} Verify next: {verification}".strip(),
                    "anchor_text": item.anchor_text or item.title or item.page_hint,
                    "page_hint": item.page_hint,
                    "priority": item.priority,
                }
            )
        return matched

    def _normalize_review(self, payload: dict[str, Any], session: SessionMemory, observation: ObservationPacket) -> ReviewBatch:
        items: list[ReviewItem] = []
        for raw_item in payload.get("items", []):
            try:
                items.append(ReviewItem.model_validate(raw_item))
            except Exception:
                logger.warning("Planner dropped an invalid review item from model output.", exc_info=True)
                continue
        actions = self._normalize_actions(payload.get("actions", []), session)
        auto_executable_actions = [action for action in actions if not action.requires_confirmation]
        return ReviewBatch(
            session_id=session.session_id,
            summary=str(payload.get("summary", "")).strip(),
            current_focus=str(payload.get("current_focus", "")).strip() or self._current_focus(session),
            previous_period_summary=[str(item) for item in payload.get("previous_period_summary", [])],
            rationale=[str(item) for item in payload.get("rationale", [])],
            items=items,
            actions=actions,
            apply_ready=bool(payload.get("apply_ready", False)) and bool(auto_executable_actions),
            insufficiently_grounded=bool(payload.get("insufficiently_grounded", False)),
            comparison_payload=dict(payload.get("comparison_payload", {}) or {}),
            beta_warning=str(payload.get("beta_warning", "Apply is beta. Manual application is safer.")).strip() or "Apply is beta. Manual application is safer.",
        )

    def _normalize_actions(self, raw_actions: list[dict[str, Any]], session: SessionMemory) -> list[ActionProposal]:
        actions: list[ActionProposal] = []
        for item in raw_actions:
            try:
                proposal = ActionProposal.model_validate(
                    {
                        "action_id": item.get("action_id") or f"act_{uuid.uuid4().hex[:10]}",
                        "action": item.get("action", "suggest_only"),
                        "reasoning": item.get("reasoning", "No reasoning provided."),
                        "confidence": float(item.get("confidence", 0.0)),
                        "target_text": item.get("target_text"),
                        "role": item.get("role"),
                        "input_text": item.get("input_text"),
                        "value": item.get("value"),
                        "url": item.get("url"),
                        "validation_text": item.get("validation_text"),
                        "metadata": item.get("metadata", {}),
                    }
                )
                actions.append(self.safety_policy.apply(proposal, session.goal.safety_mode))
            except Exception:
                logger.warning("Planner dropped an invalid action proposal from model output.", exc_info=True)
                continue
        return actions

    def _normalize_response(
        self,
        payload: dict[str, Any],
        session: SessionMemory,
        observation: ObservationPacket,
        index_refreshed: bool = False,
    ) -> PlanResponse:
        live_advice = [str(item) for item in payload.get("live_advice", [])]
        memory_summary = str(payload.get("memory_summary", "")).strip()
        actions = self._normalize_actions(payload.get("actions", []), session)

        if not actions:
            return self._fallback_plan(session, observation)

        if index_refreshed and session.strategic_summary:
            live_advice = [f"Strategic index refreshed: {session.strategic_summary}"] + live_advice

        grounded_on = ["screenshot", "visible_text", "domain_pack"]
        if session.indexed_context:
            grounded_on.append("indexed_context")

        return PlanResponse(
            session_id=session.session_id,
            strategic_summary=session.strategic_summary,
            index_refreshed=index_refreshed,
            memory_summary=memory_summary or f"Goal remains active on {observation.page_title or observation.page_url}.",
            live_advice=live_advice or ["Review proposed actions before execution."],
            actions=actions,
            grounded_on=grounded_on,
        )

    def _marketplace_store_review_items(self, observation: ObservationPacket) -> tuple[list[ReviewItem], list[ActionProposal]]:
        rows = observation.browser_metadata.get("checkbox_rows", []) or []
        parsed: list[dict[str, Any]] = []
        pattern = re.compile(r"^(?P<city>.+?)\s+(?P<status>opened|closed)(?:\s+(?P<setup>[\d,]+))?(?:\s+(?P<lease>[\d,]+))?$", re.IGNORECASE)
        for item in rows:
            row_text = str(item.get("row_text", "")).strip()
            if not row_text:
                continue
            match = pattern.match(row_text)
            if not match:
                continue
            setup = match.group("setup") or "0"
            lease = match.group("lease") or "0"
            setup_value = int(setup.replace(",", "")) if setup else 0
            lease_value = int(lease.replace(",", "")) if lease else 0
            parsed.append(
                {
                    "city": match.group("city"),
                    "status": match.group("status").lower(),
                    "setup": setup,
                    "lease": lease,
                    "setup_value": setup_value,
                    "lease_value": lease_value,
                    "total_value": setup_value + lease_value,
                }
            )

        if not parsed:
            return [], []

        items: list[ReviewItem] = []
        actions: list[ActionProposal] = []
        closed = sorted([row for row in parsed if row["status"] == "closed"], key=lambda row: (row["total_value"], row["setup_value"]))
        opened = sorted([row for row in parsed if row["status"] == "opened"], key=lambda row: (row["total_value"], row["setup_value"]))

        if closed:
            cheapest = closed[0]
            items.append(
                ReviewItem(
                    item_id=f"item_{uuid.uuid4().hex[:10]}",
                    page_hint=observation.page_title or "Open Stores",
                    anchor_text=cheapest["city"],
                    field_label=f"Open {cheapest['city']}?",
                    recommendation=(
                        f"If you need one low-cost expansion candidate, {cheapest['city']} is the cheapest visible closed store at "
                        f"{cheapest['setup']} setup and {cheapest['lease']} lease. Open it only if the current demand evidence supports that region."
                    ),
                    reasoning="This recommendation is based on the visible fixed-cost table, not guesswork.",
                    priority="high",
                )
            )
            actions.append(
                self.safety_policy.apply(
                    ActionProposal(
                        action_id=f"act_{uuid.uuid4().hex[:10]}",
                        action="click",
                        reasoning=(
                            f"Open the {cheapest['city']} row only if you want the lowest-cost visible expansion candidate for the current quarter."
                        ),
                        confidence=0.64,
                        target_text=cheapest["city"],
                        metadata={"row_text": cheapest["city"], "control_type": "checkbox"},
                    ),
                    "confirm_before_act",
                )
            )
            if len(closed) > 1:
                priciest = closed[-1]
                items.append(
                    ReviewItem(
                        item_id=f"item_{uuid.uuid4().hex[:10]}",
                        page_hint=observation.page_title or "Open Stores",
                        anchor_text=priciest["city"],
                        field_label=f"Avoid opening {priciest['city']} early",
                        recommendation=(
                            f"Do not open {priciest['city']} without strong demand proof. It is the most expensive visible closed store at "
                            f"{priciest['setup']} setup and {priciest['lease']} lease."
                        ),
                        reasoning="High fixed-cost stores should only follow clear demand or willingness-to-pay evidence.",
                        priority="high",
                    )
                )

        if opened:
            keep = opened[0]
            items.append(
                ReviewItem(
                    item_id=f"item_{uuid.uuid4().hex[:10]}",
                    page_hint=observation.page_title or "Open Stores",
                    anchor_text=keep["city"],
                    field_label=f"Keep {keep['city']} open?",
                    recommendation=(
                        f"{keep['city']} is already open. Keep it unless current-quarter test-market results clearly underperform, because reopening later would repeat fixed costs."
                    ),
                    reasoning="This recommendation preserves momentum on already-funded stores until fresh evidence says otherwise.",
                    priority="medium",
                )
            )

        return items[:3], actions[:1]

    def _marketplace_navigation_review_items(self, site_index: dict[str, Any], observation: ObservationPacket) -> list[ReviewItem]:
        sources: list[str] = []
        sources.extend(str(item) for item in site_index.get("navigation_items", []) or [])
        for item in site_index.get("site_map", []) or []:
            sources.append(str(item.get("title", "")))
        section_data: dict[str, list[str]] = {}
        editable_quarter = site_index.get("editable_quarter")
        for detail in site_index.get("completed_quarters_detail", []) or []:
            if detail.get("quarter_number") != editable_quarter:
                continue
            sources.append(str(detail.get("title", "")))
            for preview in detail.get("section_previews", []) or []:
                menu_item = str(preview.get("menu_item", ""))
                sources.append(menu_item)
                values = [str(v) for v in preview.get("visible_values", []) or []]
                if menu_item and values:
                    section_data[menu_item.lower()] = values[:6]
        sources.append(observation.page_title)
        sources.append(observation.visible_text_summary[:1600])
        haystack = " ".join(source.lower() for source in sources if source)

        def _find_section_values(*keywords: str) -> str:
            for key, values in section_data.items():
                if any(kw in key for kw in keywords):
                    return ", ".join(values[:4])
            return ""

        templates = [
            (("market research", "buy market research"), "Buy Market Research", "Market Research", "Research coverage", "Indexed quarter includes Buy Market Research.", "Buy the unchecked reports first, then rebuild pricing and channel choices with the fuller quarter data.", "Current-quarter research gaps weaken every later pricing, staffing, and store decision.", "high"),
            (("price and priority", "pricing"), "Price and Priority", "Price", "Pricing table", "Indexed quarter includes Price and Priority.", "Rework the visible price ladder next. Keep one deliberate premium offer and one clear entry offer instead of leaving brands bunched together.", "Pricing drives both margin and demand, so this page should contain explicit numeric changes before submission.", "high"),
            (("advertising",), "Advertising", "Advertising", "Advertising plan", "Indexed quarter includes Advertising.", "Change the spend mix only behind the brands and segments you actually want to push this quarter. Do not leave support flat across every message.", "Advertising only works when it reinforces the same segment strategy as pricing and stores.", "medium"),
            (("brand management", "brand"), "Brand Management", "Brand", "Brand line-up", "Indexed quarter includes Brand Management.", "Review visible brand rows and cut any offer that no longer supports the current-quarter segment plan.", "Brand sprawl makes pricing and operations harder to manage.", "medium"),
            (("open stores", "stores"), "Open Stores", "Open Stores", "Store plan", "Indexed quarter includes Open Stores.", "Open only the lowest-cost city that the current quarter evidence supports, and delay expensive openings until demand is clearer.", "Store fixed costs are one of the biggest irreversible quarter commitments.", "high"),
            (("sales channel", "hire sales people", "sales and service"), "Sales Channel", "Sales and Service", "Channel and staffing plan", "Indexed quarter includes Sales Channel.", "Change the visible channel or staffing rows deliberately instead of leaving coverage flat across every region.", "Sales and staffing only help if they reinforce the same regional and segment strategy as stores and pricing.", "high"),
            (("demand projection",), "Demand Projection", "Demand Projection", "Demand forecast", "Indexed quarter includes Demand Projection.", "Rework the forecast before finalizing production or pricing. Treat the demand view as a hard planning input, not a formality.", "Forecast errors cascade into stockouts, idle capacity, and cash stress.", "high"),
            (("manufacturing", "brand production", "operating capacity"), "Manufacturing", "Manufacturing", "Production plan", "Indexed quarter includes Manufacturing.", "Match production and capacity to the updated quarter forecast before committing anything else.", "Production is where forecast mistakes become cash and service failures.", "high"),
            (("pro forma accounting", "cash flow", "ending cash"), "Pro Forma Accounting", "Ending Cash", "Cash check", "Ending cash is visible in the current quarter.", "Keep a comfortably positive projected ending cash buffer after every major edit and before final submission.", "Positive ending cash is a hard gating constraint in the simulation.", "high"),
            (("finance",), "Finance", "Finance", "Finance check", "Indexed quarter includes Finance.", "Use finance decisions to protect runway. Avoid optional outflows unless the indexed quarter plan clearly supports them.", "Finance should support the quarter plan, not create extra fragility.", "medium"),
        ]

        items: list[ReviewItem] = []
        for keywords, page_hint, anchor_text, field_label, current_value, recommended_value, why_it_matters, priority in templates:
            if any(keyword in haystack for keyword in keywords):
                indexed_values = _find_section_values(*keywords)
                grounded_current = f"{current_value} Visible values: {indexed_values}." if indexed_values else current_value
                grounded_evidence = f"Indexed navigation and current page context both reference {page_hint}."
                if indexed_values:
                    grounded_evidence += f" Indexed values: {indexed_values}."
                items.append(
                    ReviewItem(
                        item_id=f"item_{uuid.uuid4().hex[:10]}",
                        title=field_label,
                        page_hint=page_hint,
                        anchor_text=anchor_text,
                        field_type="summary",
                        current_value=grounded_current,
                        recommended_value=recommended_value,
                        why_it_matters=why_it_matters,
                        evidence=grounded_evidence,
                        priority=priority,
                        confidence=0.73,
                        actionability="manual_only",
                        requires_followup_check=True,
                    )
                )
        return items[:4]

    @staticmethod
    def _extract_amount(text: str, label: str) -> str:
        pattern = re.compile(rf"{re.escape(label)}\s*[:\t ]+\$?(?P<amount>[\d,]+)", re.IGNORECASE)
        match = pattern.search(text)
        return match.group("amount") if match else ""

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        cleaned = re.sub(r"[^\d-]", "", str(value or ""))
        if not cleaned:
            return None
        try:
            return int(cleaned)
        except ValueError:
            return None

    def _marketplace_price_priority_items(self, observation: ObservationPacket) -> list[ReviewItem]:
        editable_rows = observation.browser_metadata.get("editable_rows", []) or []
        parsed_rows: list[dict[str, Any]] = []
        for row in editable_rows[:6]:
            brand = str(row.get("row_text", "")).strip()
            values = list(row.get("current_values", []) or [])
            if not brand or len(values) < 3:
                continue
            parsed_values = [self._coerce_int(value) for value in values]
            tail_values = [value for value in parsed_values[1:] if value is not None]
            price = max(tail_values) if tail_values else None
            rebate_candidates = [value for value in tail_values if price is not None and value != price and value > 1]
            parsed_rows.append(
                {
                    "brand": brand,
                    "priority": parsed_values[0] if parsed_values else None,
                    "available": tail_values[0] if len(tail_values) > 2 and tail_values[0] in {0, 1} else None,
                    "price": price,
                    "rebate": max(rebate_candidates) if rebate_candidates else None,
                    "display": tail_values[-1] if tail_values else None,
                }
            )

        priced_rows = [row for row in parsed_rows if row.get("price") is not None]
        if not priced_rows:
            return []

        items: list[ReviewItem] = []
        highest = max(priced_rows, key=lambda row: int(row["price"]))
        lowest = min(priced_rows, key=lambda row: int(row["price"]))

        if highest["brand"] != lowest["brand"]:
            items.append(
                ReviewItem(
                    item_id=f"item_{uuid.uuid4().hex[:10]}",
                    page_hint="Price and Priority",
                    anchor_text=str(highest["brand"]),
                    field_label=f"{highest['brand']} premium position",
                    recommendation=(
                        f"Keep {highest['brand']} as the premium offer unless competitor-price or willingness-to-pay evidence clearly rejects it. "
                        f"It already sits at the top of the visible price ladder at {int(highest['price']):,}."
                    ),
                    reasoning="A deliberate premium anchor is stronger than flattening every brand toward the same price point.",
                    priority="high",
                )
            )
            items.append(
                ReviewItem(
                    item_id=f"item_{uuid.uuid4().hex[:10]}",
                    page_hint="Price and Priority",
                    anchor_text=str(lowest["brand"]),
                    field_label=f"{lowest['brand']} entry position",
                    recommendation=(
                        f"Use {lowest['brand']} as the entry-price offer unless the current-quarter data says this segment should be deprioritized. "
                        f"It is the cheapest visible brand at {int(lowest['price']):,}, so it can absorb value-sensitive demand."
                    ),
                    reasoning="Entry coverage is usually more useful than bunching every brand into the middle of the price ladder.",
                    priority="medium",
                )
            )

        displays = [row for row in parsed_rows if row.get("display") is not None]
        if displays and all(int(row["display"]) in {0, 1} for row in displays) and len({int(row["display"]) for row in displays}) == 1 and int(displays[0]["display"]) > 0:
            items.append(
                ReviewItem(
                    item_id=f"item_{uuid.uuid4().hex[:10]}",
                    page_hint="Price and Priority",
                    anchor_text=str(displays[0]["brand"]),
                    field_label="Display concentration",
                    recommendation=(
                        "Do not fund point-of-purchase display for every brand equally. "
                        "The visible table shows display support on every brand, so concentrate that support on the one or two brands you actually want to push this quarter."
                    ),
                    reasoning="Spreading support across every brand weakens the signal and spends money without a clear winner.",
                    priority="high",
                )
            )

        return items[:3]

    def _marketplace_editable_row_items(self, observation: ObservationPacket) -> list[ReviewItem]:
        page_title = (observation.page_title or "").strip().lower()
        if "price and priority" in page_title:
            specialized = self._marketplace_price_priority_items(observation)
            if specialized:
                return specialized

        editable_rows = observation.browser_metadata.get("editable_rows", []) or []
        items: list[ReviewItem] = []
        for row in editable_rows[:4]:
            row_text = str(row.get("row_text", "")).strip()
            if not row_text:
                continue
            values = [str(item).strip() for item in row.get("current_values", [])[:4] if str(item).strip()]
            value_summary = ", ".join(values)
            row_lower = row_text.lower()
            if "sales channel" in page_title or "coverage" in row_lower:
                recommended_value = (
                    f"Current values: {value_summary or 'visible values'}. Increase coverage only in regions with strong demand evidence and reduce in underperforming regions."
                )
            elif "advertising" in page_title or "budget" in row_lower or "spend" in row_lower:
                recommended_value = (
                    f"Current values: {value_summary or 'visible values'}. Cut spend on low-priority segments first, then reallocate to the segment you are pushing hardest this quarter."
                )
            else:
                recommended_value = (
                    f"Current values: {value_summary or 'visible values'}. Review whether these carry forward from a previous quarter and adjust to match the current-quarter strategy."
                )
            items.append(
                ReviewItem(
                    item_id=f"item_{uuid.uuid4().hex[:10]}",
                    title=row_text,
                    page_hint=observation.page_title,
                    anchor_text=row_text,
                    field_type="numeric_row",
                    current_value=value_summary,
                    recommended_value=recommended_value,
                    why_it_matters="This row is directly editable on the current quarter page and should reflect a deliberate choice, not inherited defaults.",
                    evidence=f'Visible row "{row_text}" currently shows {value_summary or "editable values"}.',
                    priority="high",
                    confidence=0.7,
                    actionability="manual_only",
                    requires_followup_check=True,
                )
            )
        return items

    @staticmethod
    def _filter_items_for_page(items: list[ReviewItem], observation: ObservationPacket) -> list[ReviewItem]:
        page_title = (observation.page_title or "").strip().lower()
        if not page_title:
            return items
        result: list[ReviewItem] = []
        for item in items:
            hint = (item.page_hint or "").strip().lower()
            if not hint or hint in page_title or page_title in hint:
                result.append(item)
        return result

    @staticmethod
    def _marketplace_items_need_navigation_support(items: list[ReviewItem]) -> bool:
        if not items:
            return True
        actionable = [
            item
            for item in items
            if item.actionability != "guardrail_only"
            and (item.recommended_value or item.recommended_range)
        ]
        return len(actionable) < 1

    def _marketplace_fallback_is_page_specific(self, batch: ReviewBatch, observation: ObservationPacket) -> bool:
        if batch.actions:
            return True
        page_title = (observation.page_title or "").strip().lower()
        if not page_title:
            return False
        for item in batch.items:
            hint = (item.page_hint or "").strip().lower()
            if hint and hint == page_title:
                return True
        visible = (observation.visible_text_summary or "").lower()
        if "market research" in page_title and "buy" in visible:
            return True
        return False

    def _merge_review_batches(self, primary: ReviewBatch, fallback: ReviewBatch) -> ReviewBatch:
        merged_items: list[ReviewItem] = []
        seen_items: set[tuple[str, str, str]] = set()
        for item in [*(primary.items or []), *(fallback.items or [])]:
            key = (item.page_hint.strip().lower(), item.field_label.strip().lower(), item.anchor_text.strip().lower())
            if key in seen_items:
                continue
            seen_items.add(key)
            merged_items.append(item)

        merged_actions: list[ActionProposal] = []
        seen_actions: set[tuple[str, str]] = set()
        for action in [*(primary.actions or []), *(fallback.actions or [])]:
            key = (action.action, str(action.target_text or action.metadata.get("row_text") or action.url or action.value or ""))
            if key in seen_actions:
                continue
            seen_actions.add(key)
            merged_actions.append(action)

        summary = primary.summary.strip() or fallback.summary
        if len(merged_items) > len(primary.items):
            summary = (summary + " Added deterministic page-level checks from indexed workflow memory.").strip()

        rationale: list[str] = []
        for item in [*(primary.rationale or []), *(fallback.rationale or [])]:
            cleaned = str(item).strip()
            if cleaned and cleaned not in rationale:
                rationale.append(cleaned)

        previous_period_summary = primary.previous_period_summary or fallback.previous_period_summary
        current_focus = primary.current_focus or fallback.current_focus

        return ReviewBatch(
            session_id=primary.session_id or fallback.session_id,
            summary=summary,
            rationale=rationale,
            current_focus=current_focus,
            previous_period_summary=previous_period_summary,
            items=merged_items,
            actions=merged_actions,
            apply_ready=bool([action for action in merged_actions if not action.requires_confirmation]) and (primary.apply_ready or fallback.apply_ready or bool([action for action in fallback.actions if not action.requires_confirmation])),
            insufficiently_grounded=primary.insufficiently_grounded and fallback.insufficiently_grounded,
            comparison_payload=primary.comparison_payload or fallback.comparison_payload,
            beta_warning=primary.beta_warning or fallback.beta_warning,
        )

    def _fallback_review(self, session: SessionMemory, observation: ObservationPacket) -> ReviewBatch:
        site_index = dict(session.indexed_context.get("site_index", {}) or {})
        editable_quarter = site_index.get("editable_quarter")
        current_focus = self._current_focus(session)
        previous_period_summary = self._previous_period_summary(site_index)
        items: list[ReviewItem] = []
        actions: list[ActionProposal] = []
        current_page_items: list[ReviewItem] = []
        current_page_actions: list[ActionProposal] = []
        rationale = [
            session.strategic_summary or self._fallback_index_summary(session, observation),
            session.site_check_summary or "The structure fingerprint was checked before this review.",
        ]

        if session.mode == "review_only":
            return self._generic_comparison_review(session, observation)

        if session.domain_pack == "marketplace_simulation":
            page_title = observation.page_title.lower()
            visible_summary = observation.visible_text_summary.lower()
            if "market research" in page_title or "market research" in visible_summary:
                unchecked_rows = [
                    item.get("row_text", "")
                    for item in observation.browser_metadata.get("checkbox_rows", [])
                    if not item.get("checked")
                ]
                if unchecked_rows:
                    current_page_items.append(
                        ReviewItem(
                            item_id=f"item_{uuid.uuid4().hex[:10]}",
                            title="Buy missing reports now",
                            page_hint="Buy Market Research",
                            anchor_text=unchecked_rows[0],
                            field_type="table",
                            current_value="Missing visible reports",
                            recommended_value=f"Buy now: {', '.join(str(row) for row in unchecked_rows[:4])}",
                            why_it_matters="The current quarter is editable, and the missing research directly improves pricing, staffing, and store decisions.",
                            evidence=f"Unchecked visible reports: {', '.join(str(row) for row in unchecked_rows[:4])}.",
                            priority="high",
                            confidence=0.9,
                            actionability="executable_action" if unchecked_rows else "manual_only",
                            requires_followup_check=True,
                        ),
                    )
                for row_text in unchecked_rows[:5]:
                    current_page_actions.append(
                        self.safety_policy.apply(
                            ActionProposal(
                                action_id=f"act_{uuid.uuid4().hex[:10]}",
                                action="click",
                                reasoning=f"Buy the {row_text} report while the quarter is still editable.",
                                confidence=0.82,
                                target_text=row_text,
                                metadata={"row_text": row_text, "control_type": "checkbox"},
                            ),
                            session.goal.safety_mode,
                        )
                    )
            if "store" in page_title or "open store" in page_title or "store" in visible_summary:
                store_items, store_actions = self._marketplace_store_review_items(observation)
                current_page_items.extend(store_items)
                current_page_actions.extend(store_actions)
            if observation.browser_metadata.get("editable_rows"):
                current_page_items.extend(self._marketplace_editable_row_items(observation))
            ending_cash = self._extract_amount(observation.visible_text_summary, "Ending Cash")
            total_revenue = self._extract_amount(observation.visible_text_summary, "Total Revenue")
            net_income = self._extract_amount(observation.visible_text_summary, "Net Income")
            if ending_cash:
                cash_value = self._coerce_int(ending_cash)
                if cash_value is not None and cash_value < 0:
                    cash_assessment = f"Ending cash is negative at {ending_cash}. This risks bankruptcy and must be corrected before submission."
                    cash_priority = "high"
                elif cash_value is not None and cash_value < 50000:
                    cash_assessment = f"Ending cash is low at {ending_cash}. Consider reducing discretionary spend to build a safer buffer before submission."
                    cash_priority = "high"
                else:
                    cash_assessment = f"Ending cash of {ending_cash} appears healthy. Verify it stays positive after any remaining edits this quarter."
                    cash_priority = "medium"
                evidence_parts = [f"Visible ending cash is {ending_cash}"]
                if total_revenue:
                    evidence_parts.append(f"total revenue is {total_revenue}")
                if net_income:
                    evidence_parts.append(f"net income is {net_income}")
                current_page_items.append(
                    ReviewItem(
                        item_id=f"item_{uuid.uuid4().hex[:10]}",
                        title="Ending cash check",
                        page_hint="Pro Forma Accounting",
                        anchor_text="Ending Cash",
                        field_type="guardrail",
                        current_value=ending_cash,
                        recommended_range=cash_assessment,
                        why_it_matters="Ending cash is a hard constraint. Negative cash means bankruptcy in the simulation.",
                        evidence=". ".join(evidence_parts) + ".",
                        priority=cash_priority,
                        confidence=0.88,
                        actionability="guardrail_only",
                        requires_followup_check=True,
                    )
                )

            if current_page_items:
                items.extend(current_page_items)
                actions.extend(current_page_actions)
                if self._marketplace_items_need_navigation_support(current_page_items):
                    for suggestion in self._marketplace_navigation_review_items(site_index, observation):
                        if all(
                            existing.page_hint != suggestion.page_hint or existing.anchor_text != suggestion.anchor_text
                            for existing in items
                        ):
                            items.append(suggestion)
            else:
                items.extend(self._marketplace_navigation_review_items(site_index, observation))
            summary = f"Prepared a review for the current editable quarter{f' {editable_quarter}' if editable_quarter else ''} using indexed simulation context."
        else:
            return self._generic_comparison_review(session, observation)

        return ReviewBatch(
            session_id=session.session_id,
            summary=summary,
            rationale=[item for item in rationale if item],
            current_focus=current_focus,
            previous_period_summary=previous_period_summary,
            items=items[:12],
            actions=actions,
            apply_ready=bool([action for action in actions if not action.requires_confirmation]),
            insufficiently_grounded=not bool(items),
            beta_warning="Apply is beta. Manual application is safer.",
        )

    def _generic_comparison_review(self, session: SessionMemory, observation: ObservationPacket) -> ReviewBatch:
        entities = self._extract_apple_entities(observation.visible_text_summary)
        if not entities:
            return ReviewBatch(
                session_id=session.session_id,
                summary="Prepared a review, but the visible page did not expose a stable comparison table yet.",
                rationale=[
                    session.strategic_summary or self._fallback_index_summary(session, observation),
                    "Visible comparable entities remain unconfirmed on the current page.",
                ],
                current_focus=self._current_focus(session),
                previous_period_summary=[],
                items=[],
                actions=[],
                apply_ready=False,
                insufficiently_grounded=True,
                comparison_payload={},
                beta_warning="Apply is beta. Manual application is safer.",
            )

        scoped_entities = self._filter_entities_for_goal(entities, session.goal.raw_goal)
        cheapest = min(scoped_entities, key=lambda item: item["price_value"])
        comparison_payload = {
            "entities": scoped_entities,
            "best_match": cheapest,
            "page_hint": observation.page_title or observation.page_url,
        }
        items = [
            ReviewItem(
                item_id=f"item_{uuid.uuid4().hex[:10]}",
                title="Cheapest option",
                page_hint=observation.page_title or observation.page_url,
                anchor_text=str(cheapest["name"]),
                field_type="comparison_row",
                current_value=str(cheapest["price"]),
                recommended_value=str(cheapest["price"]),
                why_it_matters="This is the lowest visible price that matches the current comparison goal.",
                evidence=f"{cheapest['name']} is shown at {cheapest['price']} on the current page.",
                priority="high",
                confidence=0.93,
                actionability="manual_only",
                dependencies=[],
                requires_followup_check=False,
            )
        ]
        for entity in scoped_entities[1:4]:
            items.append(
                ReviewItem(
                    item_id=f"item_{uuid.uuid4().hex[:10]}",
                    title=str(entity["name"]),
                    page_hint=observation.page_title or observation.page_url,
                    anchor_text=str(entity["name"]),
                    field_type="comparison_row",
                    current_value=str(entity["price"]),
                    recommended_range=f"+{entity['price_value'] - cheapest['price_value']:,} vs cheapest",
                    why_it_matters="This row is useful for side-by-side price comparison against the cheapest visible option.",
                    evidence=f"{entity['name']} is shown at {entity['price']} on the current page.",
                    priority="medium",
                    confidence=0.86,
                    actionability="manual_only",
                    dependencies=[],
                    requires_followup_check=False,
                )
            )
        return ReviewBatch(
            session_id=session.session_id,
            summary=f"Cheapest visible option: {cheapest['name']} at {cheapest['price']}.",
            rationale=[
                session.strategic_summary or self._fallback_index_summary(session, observation),
                "Structured comparison rows were extracted from the current visible pricing content.",
            ],
            current_focus=self._current_focus(session),
            previous_period_summary=[],
            items=items,
            actions=[],
            apply_ready=False,
            insufficiently_grounded=False,
            comparison_payload=comparison_payload,
            beta_warning="Apply is beta. Manual application is safer.",
        )

    @classmethod
    def _filter_entities_for_goal(cls, entities: list[dict[str, Any]], goal_text: str) -> list[dict[str, Any]]:
        normalized_goal = cls._normalize_phrase(goal_text)
        if not normalized_goal:
            return entities

        best_phrase = ""
        for entity in entities:
            for phrase in cls._goal_match_candidates(str(entity.get("name", ""))):
                if phrase in normalized_goal and len(phrase) > len(best_phrase):
                    best_phrase = phrase
        if not best_phrase:
            return entities

        filtered = [
            entity for entity in entities if best_phrase in cls._normalize_phrase(str(entity.get("name", "")))
        ]
        return filtered or entities

    @staticmethod
    def _goal_match_candidates(name: str) -> list[str]:
        tokens = re.findall(r"[a-z0-9]+", (name or "").lower())
        if not tokens:
            return []

        candidates: list[str] = []
        max_length = min(4, len(tokens))
        for length in range(max_length, 0, -1):
            phrase_tokens = tokens[:length]
            if not phrase_tokens:
                continue
            if re.fullmatch(r"\d+(?:\.\d+)?", phrase_tokens[-1]):
                continue
            if phrase_tokens[-1] in {"inch", "in", "mm", "cm", "gb", "tb"}:
                continue
            candidates.append(" ".join(phrase_tokens))
        return candidates

    @staticmethod
    def _normalize_phrase(value: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()

    @staticmethod
    def _extract_apple_entities(visible_text: str) -> list[dict[str, Any]]:
        text = re.sub(r"\s+", " ", visible_text or "").strip()
        if not text:
            return []

        product_pattern = r"(?:MacBook(?:\s+Air|\s+Pro)?|Mac\s+mini|Mac\s+Studio|Mac\s+Pro|iMac)"
        name_pattern = re.compile(
            rf"{product_pattern}(?:\s+\d+(?:\.\d+)?(?:-inch|-in\.))?(?:\s+(?:M\d+(?:\s+(?:Pro|Max))?|A\d+\s+Pro))?(?:\s+\([^)]+\))?",
            re.IGNORECASE,
        )
        price_pattern = re.compile(r"\$[\d,]+")
        variant_pattern = re.compile(r"(?P<size>\d+(?:\.\d+)?-inch)\s+(?:Footnote\s+\d+\s+)?From\s+(?P<price>\$[\d,]+)", re.IGNORECASE)
        base_name_pattern = re.compile(product_pattern, re.IGNORECASE)
        name_matches = list(name_pattern.finditer(text))
        price_matches = list(price_pattern.finditer(text))
        candidates: list[tuple[int, str, str]] = []

        def _base_name(name: str) -> str:
            match = base_name_pattern.search(name)
            return re.sub(r"\s+", " ", match.group(0)).strip() if match else name

        for index, match in enumerate(name_matches):
            name = re.sub(r"\s+", " ", match.group(0)).strip(" .")
            name = re.sub(r"\s+from$", "", name, flags=re.IGNORECASE)
            next_name_start = name_matches[index + 1].start() if index + 1 < len(name_matches) else len(text)
            price = ""
            for price_match in price_matches:
                if price_match.start() < match.end():
                    continue
                if price_match.start() >= next_name_start:
                    break
                price = price_match.group(0).strip()
                break
            if not name:
                continue
            if not price:
                continue
            candidates.append((match.start(), name, price))

        for match in variant_pattern.finditer(text):
            prior_bases = list(base_name_pattern.finditer(text[: match.start()]))
            if not prior_bases:
                continue
            base_name = re.sub(r"\s+", " ", prior_bases[-1].group(0)).strip()
            variant_name = f"{base_name} {match.group('size').strip()}"
            candidates.append((match.start(), variant_name, match.group("price").strip()))

        variant_bases = {
            _base_name(name)
            for _, name, _ in candidates
            if re.search(r"\d+(?:\.\d+)?-inch", name, re.IGNORECASE)
        }
        entities: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _, name, price in sorted(candidates, key=lambda item: item[0]):
            normalized_name = re.sub(r"\s+", " ", name).strip()
            if _base_name(normalized_name) in variant_bases and normalized_name == _base_name(normalized_name):
                continue
            if normalized_name.lower() in seen:
                continue
            seen.add(normalized_name.lower())
            entities.append(
                {
                    "name": normalized_name,
                    "price": price,
                    "price_value": int(price.replace("$", "").replace(",", "")),
                }
            )
        return entities

    @staticmethod
    def _review_is_insufficiently_grounded(batch: ReviewBatch, session: SessionMemory) -> bool:
        if batch.insufficiently_grounded:
            return True
        if session.mode == "review_only":
            return not bool(batch.comparison_payload)
        if not batch.items:
            return True
        concrete_items = [
            item
            for item in batch.items
            if item.current_value or item.recommended_value or item.recommended_range
        ]
        return not bool(concrete_items)

    def _fallback_plan(self, session: SessionMemory, observation: ObservationPacket) -> PlanResponse:
        advice: list[str] = []
        actions: list[ActionProposal] = []
        visible = observation.visible_text_summary.lower()
        title = observation.page_title.lower()
        site_index = session.indexed_context.get("site_index", {})

        if session.domain_pack == "marketplace_simulation":
            advice.append("Simulation domain pack is active. Focus edits on the latest editable quarter.")
            if "ending cash" in visible:
                advice.append("Current screen includes cash context. Preserve a positive cash buffer before submission.")
            if "market research" in title or "market research" in visible:
                unchecked_rows = [item.get("row_text", "") for item in observation.browser_metadata.get("checkbox_rows", []) if not item.get("checked")]
                advice.append("Market research is visible. Buy the missing reports now so later pricing and channel choices use fuller demand context.")
                for row_text in unchecked_rows[:4]:
                    actions.append(
                        self.safety_policy.apply(
                            ActionProposal(
                                action_id=f"act_{uuid.uuid4().hex[:10]}",
                                action="click",
                                reasoning=f"Buy the {row_text} market research report while the quarter is still editable.",
                                confidence=0.82,
                                target_text=row_text,
                                metadata={"row_text": row_text, "control_type": "checkbox"},
                            ),
                            session.goal.safety_mode,
                        )
                    )
            if not advice and site_index.get("editable_quarter"):
                advice.append(f"Indexed context says quarter {site_index.get('editable_quarter')} is the current editable quarter.")
        else:
            advice.append("Universal UI navigator mode is active for the current workspace.")
            advice.append("The system is grounded in the current screenshot, visible interface state, and page text.")
            if session.strategic_summary:
                advice.append(session.strategic_summary)
            actions.append(
                self.safety_policy.apply(
                    ActionProposal(
                        action_id=f"act_{uuid.uuid4().hex[:10]}",
                        action="suggest_only",
                        reasoning="No structured plan was returned by the model, so the system is staying in recommendation-only mode.",
                        confidence=0.55,
                        target_text=observation.page_title or observation.page_url,
                        validation_text=observation.page_title or None,
                    ),
                    session.goal.safety_mode,
                )
            )

        return PlanResponse(
            session_id=session.session_id,
            strategic_summary=session.strategic_summary,
            index_refreshed=False,
            memory_summary=f"Observed {observation.page_title or observation.page_url}. Goal: {session.goal.raw_goal}",
            live_advice=advice,
            actions=actions,
            grounded_on=["visible_text", "fallback_heuristics"],
        )

    @staticmethod
    def _needs_reindex(session: SessionMemory, observation: ObservationPacket) -> bool:
        if not session.strategic_summary or not session.indexed_context or not session.last_indexed_at:
            return True
        previous_host = urlparse(session.current_site).netloc if session.current_site else ""
        current_host = urlparse(observation.page_url).netloc
        if previous_host and current_host and previous_host != current_host:
            return True
        return False

    @staticmethod
    def _fallback_index_summary(session: SessionMemory, observation: ObservationPacket) -> str:
        site_check = observation.browser_metadata.get("site_check", {})
        current_node_count = int(site_check.get("current_node_count", 0) or 0)
        if session.domain_pack == "marketplace_simulation":
            if current_node_count:
                return (
                    "Indexed the simulation workspace and cached a structure checklist "
                    f"covering {current_node_count} nodes so advice can react to the current editable screen."
                )
            return "Indexed the simulation workspace and cached the quarter navigation so advice can react to the current editable screen."
        if current_node_count:
            return (
                f"Indexed the current site around {observation.page_title or observation.page_url} "
                f"and built a {current_node_count}-node structure checklist for faster live guidance."
            )
        return f"Indexed the current site around {observation.page_title or observation.page_url} for faster live guidance."

    @staticmethod
    def _index_advice(session: SessionMemory, observation: ObservationPacket, next_focus: list[str]) -> list[str]:
        site_check = observation.browser_metadata.get("site_check", {})
        advice = ["Indexing completed. The agent now has cached site context and can react faster on later screens."]
        matched_nodes = int(site_check.get("matched_nodes", 0) or 0)
        current_node_count = int(site_check.get("current_node_count", 0) or 0)
        if site_check.get("change_summary"):
            advice.append(str(site_check.get("change_summary")))
        if current_node_count:
            advice.append(
                f"Structure checklist coverage: {matched_nodes} reused nodes, {current_node_count} nodes visible in the latest pass."
            )
        if session.domain_pack == "marketplace_simulation":
            advice.append("Move through editable tables. The overlay will reuse the indexed quarter map instead of starting from zero.")
        if next_focus:
            advice.append(f"Next likely focus: {', '.join(next_focus[:3])}.")
        return advice

    def _previous_period_summary(self, site_index: dict[str, Any]) -> list[str]:
        previous: list[str] = []
        editable_quarter = site_index.get("editable_quarter")
        for item in site_index.get("completed_quarters_detail", []) or []:
            quarter_number = item.get("quarter_number")
            if editable_quarter and quarter_number == editable_quarter:
                continue
            title = item.get("title") or item.get("page_title_excerpt") or "Indexed quarter"
            excerpt = str(item.get("page_text_excerpt", "")).strip().replace("\n", " ")
            excerpt = re.sub(r"\s+", " ", excerpt)
            excerpt = excerpt[:180].strip()
            if excerpt:
                previous.append(f"Quarter {quarter_number}: {title}. Snapshot: {excerpt}.")
            else:
                previous.append(f"Quarter {quarter_number}: {title}.")
        if not previous:
            for item in site_index.get("completed_quarters", []) or []:
                quarter_number = item.get("quarter_number")
                if editable_quarter and quarter_number == editable_quarter:
                    continue
                previous.append(f"Quarter {quarter_number}: {item.get('title', 'Indexed quarter')}.")
        return previous[:3]

    def _current_focus(self, session: SessionMemory) -> str:
        site_index = session.indexed_context.get("site_index", {})
        editable_quarter = site_index.get("editable_quarter")
        if editable_quarter:
            return f"Quarter {editable_quarter} is the current editable quarter."
        return session.index_summary.current_focus if session.index_summary else "Focus on the current editable page."
