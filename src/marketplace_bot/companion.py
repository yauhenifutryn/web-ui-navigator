from __future__ import annotations

import uuid

from marketplace_bot.goal_compiler import GoalCompiler
from marketplace_bot.navigator_models import CreateSessionRequest, ExecuteResultPayload, IndexSummary, ObservationPacket, PlanResponse, ReviewBatch, SessionMemory, SiteMemory
from marketplace_bot.planner import PlannerService
from marketplace_bot.session_repository import HybridSessionRepository
from marketplace_bot.site_intelligence import (
    build_site_memory_key,
    build_structure_manifest,
    compute_site_fingerprint,
    compute_structure_digest,
    normalize_site_origin,
)
from marketplace_bot.site_memory_repository import HybridSiteMemoryRepository
from marketplace_bot.state_store import utc_now_iso


class LiveNavigatorCompanion:
    def __init__(
        self,
        session_repository: HybridSessionRepository,
        goal_compiler: GoalCompiler,
        planner: PlannerService,
        site_memory_repository: HybridSiteMemoryRepository | None = None,
    ) -> None:
        self.session_repository = session_repository
        self.site_memory_repository = site_memory_repository
        self.goal_compiler = goal_compiler
        self.planner = planner

    async def create_session(self, request: CreateSessionRequest) -> SessionMemory:
        goal = self.goal_compiler.compile(
            raw_goal=request.goal,
            domain_hint=request.domain_hint,
            mode=request.mode,
            safety_mode=request.safety_mode,
            index_mode=request.index_mode,
        )
        now = utc_now_iso()
        default_memory_summary = (
            "Session created. Run the site index first so the agent can compare the current structure fingerprint against saved local memory."
            if goal.mode == "complex_workspace"
            else "Session created. Run the site index first so the agent can explore the target pages and build a grounded comparison."
        )
        default_live_advice = (
            [
                "Step 1: run the site index.",
                "That pass checks the current site fingerprint and decides whether to reuse memory, refresh changed areas, or rebuild the crawl.",
            ]
            if goal.mode == "complex_workspace"
            else [
                "Step 1: run the site index.",
                "That pass explores the target site and prepares a summary-first review without default execution.",
            ]
        )
        session = SessionMemory(
            session_id=f"sess_{uuid.uuid4().hex[:10]}",
            project_name=request.project_name,
            goal=goal,
            mode=goal.mode,
            current_site="",
            domain_pack=goal.domain_pack,
            index_mode=goal.index_mode,
            memory_summary=default_memory_summary,
            live_advice=default_live_advice,
            status="session_ready_to_index",
            created_at=now,
            updated_at=now,
        )
        self._append_event(session, "Session created. Waiting for the first index pass.")
        return self.session_repository.save(session)

    def get_session(self, session_id: str) -> SessionMemory | None:
        return self.session_repository.get(session_id)

    def list_sessions(self) -> list[SessionMemory]:
        return self.session_repository.list()

    def resume_session(self, session_id: str) -> SessionMemory:
        session = self._require_session(session_id)
        if session.domain_pack == "marketplace_simulation":
            session.mode = "complex_workspace"
        else:
            session.mode = session.goal.mode
        session = self._ensure_site_memory_backfill(session)
        session = self._upgrade_legacy_marketplace_index_mode(session)
        has_cached_summary = bool(session.last_indexed_at and session.index_summary)
        has_cached_review = bool(session.last_indexed_at and session.review_batch)

        if has_cached_review or has_cached_summary:
            session.site_check_required = False
            session.site_check_status = "unchecked"
            session.site_check_summary = "Cached session restored. Run the site index to refresh only changed workflow areas before critical edits."
            session.memory_summary = "Loaded cached review from local memory. You can inspect it now, but refresh is recommended before making critical changes."
            session.live_advice = [
                "Refresh the site index when you want a fresh structure check and partial refresh.",
                "Cached review is available now, so you can keep reading the recommendations without waiting.",
            ]
            session.artifacts["inline_notes"] = []
            session.status = "review_batch_ready" if has_cached_review else "index_summary_ready"
            self._append_event(session, "Session resumed from cached review. Refresh recommended before critical edits.")
        else:
            session.site_check_required = True
            session.site_check_status = "unchecked"
            session.site_check_summary = "Resume requested. Run the site index to compare the current structure fingerprint against saved local memory."
            session.memory_summary = "Index the site first so the session can verify structure changes before showing review notes."
            session.live_advice = [
                "This session has saved local memory.",
                "Run the site index now. The agent will reuse unchanged structure, refresh only changed areas, or rebuild fully if the site drift is large.",
            ]
            session.review_batch = None
            session.index_summary = None
            session.pending_approvals = []
            session.artifacts["inline_notes"] = []
            session.status = "session_ready_to_index"
            self._append_event(session, "Session resumed. Awaiting structure check.")
        session.updated_at = utc_now_iso()
        return self.session_repository.save(session)

    def enter_live_advice_mode(self, session_id: str) -> SessionMemory:
        session = self._require_session(session_id)
        session.status = "live_advice"
        session.updated_at = utc_now_iso()
        self._append_event(session, "Live Notes mode enabled.")
        return self.session_repository.save(session)

    def mark_indexing_progress(self, session_id: str, step: str, site_check_details: dict[str, object] | None = None) -> SessionMemory:
        session = self._require_session(session_id)
        session.status = "indexing"
        session.updated_at = utc_now_iso()
        session.artifacts["index_progress"] = {
            "step": step,
            "site_check_details": dict(site_check_details or {}),
        }
        if session.event_log[:1] != [step]:
            self._append_event(session, step)
        return self.session_repository.save(session)

    def update_page_signature(self, session_id: str, signature: str) -> bool:
        session = self._require_session(session_id)
        changed = session.last_page_signature != signature
        session.last_page_signature = signature
        session.updated_at = utc_now_iso()
        self.session_repository.save(session)
        return changed

    def store_observation(self, observation: ObservationPacket) -> SessionMemory:
        session = self._require_session(observation.session_id)
        session.last_observation = observation
        session.current_site = observation.page_url
        if observation.browser_metadata.get("ax_summary"):
            session.artifacts["ax_summary"] = str(observation.browser_metadata.get("ax_summary", ""))
        if observation.browser_metadata.get("ax_diagnostics"):
            session.artifacts["ax_diagnostics"] = dict(observation.browser_metadata.get("ax_diagnostics", {}) or {})
        if observation.browser_metadata.get("ax_targets"):
            session.artifacts["ax_targets"] = list(observation.browser_metadata.get("ax_targets", []) or [])
        session.updated_at = utc_now_iso()
        session.checkpoints.append(
            {
                "ts": observation.captured_at,
                "kind": "observation",
                "page_url": observation.page_url,
                "page_title": observation.page_title,
                "screenshot_path": observation.screenshot_path,
            }
        )
        if observation.screenshot_path:
            screenshot_file = str(observation.screenshot_path).rsplit("/", 1)[-1]
            capture_target = observation.page_title or observation.page_url
            self._append_event(session, f"Captured screenshot {screenshot_file} on {capture_target}.")
        return self.session_repository.save(session)

    async def index_site(self, session_id: str, observation: ObservationPacket) -> SessionMemory:
        session = self.store_observation(observation)
        session.mode = session.goal.mode
        session.status = "indexing"
        indexed = await self.planner.index(session, observation)
        site_index = dict(observation.browser_metadata.get("site_index", {}) or {})
        site_check = observation.browser_metadata.get("site_check", {})
        structure_manifest = build_structure_manifest(site_index)
        coverage_summary = self._build_coverage_summary(site_index, site_check)
        structure_map_summary = self._build_structure_map_summary(session, site_index, coverage_summary)
        degraded_reason = self._build_degraded_reason(session, site_index, coverage_summary)
        session.site_memory_key = str(site_check.get("memory_key", session.site_memory_key))
        session.site_origin = str(site_check.get("site_origin", session.site_origin))
        session.site_fingerprint = str(site_check.get("site_fingerprint", session.site_fingerprint))
        session.site_check_status = str(site_check.get("change_status", session.site_check_status))
        session.site_check_summary = str(site_check.get("change_summary", session.site_check_summary))
        session.last_site_check_at = observation.captured_at
        session.site_check_required = False
        session.strategic_summary = indexed["strategic_summary"]
        session.indexed_context = indexed["indexed_context"]
        session.last_indexed_at = observation.captured_at
        session.memory_summary = indexed["memory_summary"]
        session.live_advice = indexed["live_advice"]
        session.review_ready = False
        session.structure_map_summary = structure_map_summary
        session.coverage_summary = coverage_summary
        session.degraded_reason = degraded_reason
        session.insufficiently_grounded = False
        session.artifacts["site_check_details"] = {
            "change_status": str(site_check.get("change_status", "")).strip().lower(),
            "strategy": str(site_check.get("strategy", "")).strip().lower(),
            "matched_nodes": int(site_check.get("matched_nodes", 0) or 0),
            "changed_nodes_count": len(site_check.get("changed_nodes", []) or []),
            "new_nodes_count": len(site_check.get("new_nodes", []) or []),
            "removed_nodes_count": len(site_check.get("removed_nodes", []) or []),
            "previous_node_count": int(site_check.get("previous_node_count", 0) or 0),
            "current_node_count": int(site_check.get("current_node_count", 0) or 0),
        }
        session.artifacts["normalized_structure_manifest"] = structure_manifest
        session.artifacts["coverage_state"] = coverage_summary
        session.artifacts["structure_map_preview"] = self._build_structure_map_preview(site_index)
        session.artifacts["structure_map_total"] = len(build_structure_manifest(site_index).get("nodes", []) or [])
        session.index_summary = self._build_index_summary(session, observation)
        session.review_batch = None
        session.artifacts["inline_notes"] = []
        session.updated_at = utc_now_iso()
        session.status = "index_summary_ready"
        self._append_event(session, f"Index complete. {session.site_check_summary}")
        if degraded_reason:
            self._append_event(session, degraded_reason)
        session.checkpoints.append(
            {
                "ts": observation.captured_at,
                "kind": "site_index",
                "page_url": observation.page_url,
                "page_title": observation.page_title,
                "site_check_status": session.site_check_status,
                "site_check_summary": session.site_check_summary,
            }
        )
        stored = self.session_repository.save(session)
        if self.site_memory_repository is not None and session.site_memory_key and session.site_origin:
            self.site_memory_repository.save(
                SiteMemory(
                    memory_key=session.site_memory_key,
                    site_origin=session.site_origin,
                    domain_pack=session.domain_pack,
                    index_mode=session.index_mode,
                    site_fingerprint=session.site_fingerprint,
                    structure_digest=str(site_check.get("structure_digest", "")),
                    strategic_summary=session.strategic_summary,
                    indexed_context=session.indexed_context,
                    last_checked_at=observation.captured_at,
                    last_indexed_at=observation.captured_at,
                    change_status=session.site_check_status,
                    change_summary=session.site_check_summary,
                )
            )
        return stored

    async def plan(self, session_id: str, observation: ObservationPacket | None = None) -> PlanResponse:
        session = self._require_session(session_id)
        if observation is not None:
            session = self.store_observation(observation)
        if session.last_observation is None:
            raise RuntimeError("No observation is available for this session")

        response = await self.planner.plan(session, session.last_observation)
        session.strategic_summary = response.strategic_summary
        session.memory_summary = response.memory_summary
        session.live_advice = response.live_advice
        session.pending_approvals = response.actions
        session.review_batch = None
        session.artifacts["inline_notes"] = []
        session.updated_at = utc_now_iso()
        if session.status != "applying_batch":
            session.status = "live_advice"
        self._append_event(session, "Live advice refreshed from the current page.")
        self.session_repository.save(session)
        return response

    async def prepare_review_batch(self, session_id: str, observation: ObservationPacket | None = None) -> ReviewBatch:
        session = self._require_session(session_id)
        if observation is not None:
            session = self.store_observation(observation)
        if session.last_observation is None:
            raise RuntimeError("No observation is available for this session")
        batch = await self.planner.review(session, session.last_observation)
        session.review_batch = batch
        session.pending_approvals = list(batch.actions)
        session.review_ready = not bool(session.degraded_reason)
        session.insufficiently_grounded = bool(batch.insufficiently_grounded)
        session.live_advice = self._review_summary_lines(batch)
        session.artifacts["full_review_payload"] = batch.model_dump(mode="json")
        session.artifacts["generic_comparison_payload"] = dict(batch.comparison_payload or {})
        session.artifacts["inline_notes"] = [] if session.mode == "review_only" else self.planner.build_inline_notes(session, session.last_observation)
        session.status = "review_batch_ready"
        session.updated_at = utc_now_iso()
        self._append_event(session, "Prepared a review from indexed context for the current workflow.")
        self.session_repository.save(session)
        return batch

    async def refresh_live_advice_from_review(self, session_id: str, observation: ObservationPacket | None = None) -> SessionMemory:
        session = self._require_session(session_id)
        if observation is not None:
            session = self.store_observation(observation)
        if session.last_observation is None:
            raise RuntimeError("No observation is available for this session")
        if session.review_batch is None:
            await self.prepare_review_batch(session_id, session.last_observation)
            session = self._require_session(session_id)
        elif session.domain_pack == "marketplace_simulation" and observation is not None:
            await self.prepare_review_batch(session_id, session.last_observation)
            session = self._require_session(session_id)
        if session.mode == "review_only":
            session.artifacts["inline_notes"] = []
            session.live_advice = self._review_summary_lines(session.review_batch) if session.review_batch is not None else list(session.live_advice)
            session.status = "review_batch_ready"
            session.updated_at = utc_now_iso()
            self._append_event(session, "Review-only mode keeps inline notes disabled by default.")
            return self.session_repository.save(session)
        inline_notes = self.planner.build_inline_notes(session, session.last_observation)
        session.artifacts["inline_notes"] = inline_notes
        session.live_advice = [note["body"] for note in inline_notes[:3]] or self._review_summary_lines(session.review_batch)
        session.status = "live_advice"
        session.updated_at = utc_now_iso()
        self._append_event(session, "Live Notes refreshed from the review batch.")
        return self.session_repository.save(session)

    def mark_applying_batch(self, session_id: str) -> SessionMemory:
        session = self._require_session(session_id)
        session.status = "applying_batch"
        session.updated_at = utc_now_iso()
        self._append_event(session, "Applying the current review batch.")
        return self.session_repository.save(session)

    def finalize_review_batch(self, session_id: str) -> SessionMemory:
        session = self._require_session(session_id)
        session.status = "review_batch_ready"
        session.updated_at = utc_now_iso()
        self._append_event(session, "Review batch applied.")
        return self.session_repository.save(session)

    def record_execution(self, payload: ExecuteResultPayload) -> SessionMemory:
        session = self._require_session(payload.session_id)
        session.action_history.extend(payload.results)
        executed_ids = {item.get("action_id") for item in payload.results}
        session.pending_approvals = [action for action in session.pending_approvals if action.action_id not in executed_ids]
        if session.review_batch is not None:
            session.review_batch.actions = [action for action in session.review_batch.actions if action.action_id not in executed_ids]
            session.review_batch.apply_ready = bool(session.review_batch.actions)
        session.updated_at = utc_now_iso()
        if session.status == "applying_batch":
            session.status = "review_batch_ready"
        elif session.status != "live_advice":
            session.status = "review_batch_ready"
        self._append_event(session, f"Recorded {len(payload.results)} execution results.")
        return self.session_repository.save(session)

    def approve_actions(self, session_id: str, action_ids: list[str]) -> list[dict]:
        session = self._require_session(session_id)
        selected = []
        for action in session.pending_approvals:
            if action.action_id in action_ids:
                action.status = "approved"
                selected.append(action.model_dump(mode="json"))
        self.session_repository.save(session)
        return selected

    def auto_approve_executable_actions(self, session_id: str) -> list[dict]:
        session = self._require_session(session_id)
        selected = []
        for action in session.pending_approvals:
            if not action.requires_confirmation:
                action.status = "approved"
                selected.append(action.model_dump(mode="json"))
        self.session_repository.save(session)
        return selected

    def _require_session(self, session_id: str) -> SessionMemory:
        session = self.session_repository.get(session_id)
        if session is None:
            raise RuntimeError(f"Unknown session_id: {session_id}")
        return session

    def _ensure_site_memory_backfill(self, session: SessionMemory) -> SessionMemory:
        if self.site_memory_repository is None:
            return session
        if session.site_memory_key and session.site_origin:
            return session
        site_index = dict(session.indexed_context.get("site_index", {}) or {})
        if not site_index:
            return session
        origin_source = session.current_site
        if not origin_source:
            site_map = site_index.get("site_map", []) or []
            if site_map and isinstance(site_map[0], dict):
                origin_source = str(site_map[0].get("url", ""))
        if not origin_source:
            return session
        site_origin = normalize_site_origin(origin_source)
        if not site_origin:
            return session
        memory_key = build_site_memory_key(session.domain_pack, site_origin)
        resolved_index_mode = session.index_mode
        if session.domain_pack == "marketplace_simulation" and resolved_index_mode == "adaptive":
            resolved_index_mode = "advanced"
        session.site_memory_key = memory_key
        session.site_origin = site_origin
        session.site_fingerprint = compute_site_fingerprint(site_origin, site_index)
        session.site_check_status = "unchecked"
        session.site_check_summary = "Backfilled local site memory from a previous successful index. Run the site index to verify the current structure fingerprint."
        session.index_mode = resolved_index_mode
        self.site_memory_repository.save(
            SiteMemory(
                memory_key=memory_key,
                site_origin=site_origin,
                domain_pack=session.domain_pack,
                index_mode=resolved_index_mode,
                site_fingerprint=session.site_fingerprint,
                structure_digest=compute_structure_digest(site_index),
                strategic_summary=session.strategic_summary,
                indexed_context=session.indexed_context,
                last_checked_at=session.last_site_check_at or session.updated_at,
                last_indexed_at=session.last_indexed_at,
                change_status="unchecked",
                change_summary=session.site_check_summary,
            )
        )
        return self.session_repository.save(session)

    def _upgrade_legacy_marketplace_index_mode(self, session: SessionMemory) -> SessionMemory:
        if self.site_memory_repository is None:
            return session
        if session.domain_pack != "marketplace_simulation":
            return session
        if session.index_mode != "adaptive":
            return session
        session.index_mode = "advanced"
        if session.site_memory_key:
            loaded = self.site_memory_repository.get(session.site_memory_key)
            if loaded is not None:
                loaded.index_mode = "advanced"
                self.site_memory_repository.save(loaded)
        return self.session_repository.save(session)

    def _build_index_summary(self, session: SessionMemory, observation: ObservationPacket) -> IndexSummary:
        site_index = dict(session.indexed_context.get("site_index", {}) or {})
        completed_quarters = [item for item in site_index.get("completed_quarters", []) if isinstance(item, dict)]
        previous_summary: list[str] = []
        if completed_quarters and session.domain_pack == "marketplace_simulation":
            for item in completed_quarters[:-1]:
                quarter_number = item.get("quarter_number")
                title = str(item.get("title", "Current page")).strip() or "Current page"
                previous_summary.append(f"Quarter {quarter_number}: {title}.")
        current_focus = self._current_focus(session)
        top_recommendations = list(session.live_advice[:3])
        detected_changes = [session.site_check_summary] if session.site_check_summary else []
        return IndexSummary(
            strategic_summary=session.strategic_summary,
            site_check_summary=session.site_check_summary,
            previous_period_summary=previous_summary,
            current_focus=current_focus,
            top_recommendations=top_recommendations,
            detected_changes=detected_changes,
        )

    def _current_focus(self, session: SessionMemory) -> str:
        site_index = dict(session.indexed_context.get("site_index", {}) or {})
        editable_quarter = site_index.get("editable_quarter")
        if session.domain_pack == "marketplace_simulation" and editable_quarter:
            return f"Quarter {editable_quarter} is the current editable quarter."
        if session.last_observation is not None:
            return session.last_observation.page_title or session.last_observation.page_url
        return session.memory_summary or "Current site context is ready."

    @staticmethod
    def _build_structure_map_preview(site_index: dict[str, object]) -> list[str]:
        manifest = build_structure_manifest(site_index)
        preview: list[str] = []
        for node in manifest.get("nodes", [])[:12]:
            if not isinstance(node, dict):
                continue
            label = str(node.get("title") or node.get("url") or node.get("key") or "").strip()
            if not label:
                continue
            details: list[str] = []
            quarter_number = node.get("quarter_number")
            if quarter_number not in (None, "", 0):
                details.append(f"Q{quarter_number}")
            details.append(f"{int(node.get('section_count', 0) or 0)} sections")
            if bool(node.get("editable", False)):
                details.append("editable")
            preview.append(" | ".join([label, *details]))
        return preview

    @staticmethod
    def _append_event(session: SessionMemory, message: str) -> None:
        session.event_log = [message, *session.event_log[:19]]
        session.activity_log_tail = list(session.event_log[:5])

    @staticmethod
    def _build_coverage_summary(site_index: dict[str, Any], site_check: dict[str, Any]) -> dict[str, Any]:
        manifest = build_structure_manifest(site_index)
        nodes = [item for item in manifest.get("nodes", []) if isinstance(item, dict)]
        real_node_count = len([item for item in nodes if not bool(item.get("synthetic", False))])
        navigation_items = list(site_index.get("navigation_items", []) or [])
        raw_current_node_count = int(site_check.get("current_node_count", 0) or 0)
        current_node_count = max(raw_current_node_count, real_node_count)
        discovered_nodes = max(current_node_count, real_node_count, len(navigation_items))
        indexed_nodes = real_node_count
        blocked_nodes = int(site_index.get("coverage", {}).get("blocked_nodes", 0) or 0)
        alias_collapsed_nodes = int(site_index.get("coverage", {}).get("alias_collapsed_nodes", 0) or 0)
        skipped_nodes = max(discovered_nodes - indexed_nodes - blocked_nodes, 0)
        return {
            "discovered_nodes": discovered_nodes,
            "indexed_nodes": indexed_nodes,
            "skipped_nodes": skipped_nodes,
            "blocked_nodes": blocked_nodes,
            "alias_collapsed_nodes": alias_collapsed_nodes,
            "current_node_count": current_node_count,
        }

    @staticmethod
    def _build_structure_map_summary(session: SessionMemory, site_index: dict[str, Any], coverage_summary: dict[str, Any]) -> dict[str, Any]:
        manifest = build_structure_manifest(site_index)
        nodes = [item for item in manifest.get("nodes", []) if isinstance(item, dict)]
        active_node = session.last_observation.page_title if session.last_observation is not None else str(site_index.get("title", "") or "")
        return {
            "mode": session.mode,
            "active_node": active_node,
            "editable_quarter": site_index.get("editable_quarter"),
            "parent_sections": [str(item.get("title", "")) for item in nodes[:6] if str(item.get("title", "")).strip()],
            "node_count": int(manifest.get("node_count", 0) or 0),
            **coverage_summary,
        }

    @staticmethod
    def _build_degraded_reason(session: SessionMemory, site_index: dict[str, Any], coverage_summary: dict[str, Any]) -> str:
        if session.mode != "complex_workspace":
            return ""
        navigation_count = len(site_index.get("navigation_items", []) or [])
        current_node_count = int(coverage_summary.get("current_node_count", 0) or 0)
        indexed_nodes = int(coverage_summary.get("indexed_nodes", 0) or 0)
        if max(current_node_count, indexed_nodes) <= 1 and navigation_count >= 3:
            return (
                "Coverage degraded: the live crawl only indexed 1 visible node while the current workspace still exposes sibling navigation. "
                "Treat this run as not review-ready and re-index before trusting recommendations."
            )
        return ""

    @staticmethod
    def _review_summary_lines(batch: ReviewBatch | None) -> list[str]:
        if batch is None:
            return []
        if batch.comparison_payload:
            best_match = dict(batch.comparison_payload.get("best_match", {}) or {})
            lines: list[str] = []
            if best_match:
                lines.append(
                    f"Best match: {best_match.get('name', 'Unconfirmed option')} at {best_match.get('price', 'unconfirmed price')}."
                )
            if batch.summary:
                lines.append(batch.summary)
            return lines[:3]
        summary_lines = [item.recommended_value or item.recommended_range or item.title for item in batch.items[:3]]
        return [line for line in summary_lines if line] or list(batch.rationale[:3])
