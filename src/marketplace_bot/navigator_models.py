from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


ActionKind = Literal[
    "navigate",
    "click",
    "type",
    "select",
    "scroll",
    "wait_for",
    "extract",
    "suggest_only",
    "stop",
]
SafetyMode = Literal["guided", "confirm_before_act", "autonomous"]
SafetyLevel = Literal["low", "medium", "high"]
PriorityLevel = Literal["low", "medium", "high"]
DomainPackId = Literal["generic_web", "marketplace_simulation"]
IndexMode = Literal["lightweight", "adaptive", "advanced"]
SiteCheckStatus = Literal["unchecked", "new", "unchanged", "changed"]
RuntimeMode = Literal["complex_workspace", "review_only"]
ReviewFieldType = Literal["field", "numeric_row", "comparison_row", "table", "guardrail", "summary"]
ReviewActionability = Literal["manual_only", "suggested_action", "executable_action", "guardrail_only"]
SessionStatus = Literal[
    "idle",
    "session_ready_to_index",
    "indexing",
    "index_summary_ready",
    "live_advice",
    "review_batch_ready",
    "applying_batch",
    "stopped",
]


class GoalSpec(BaseModel):
    raw_goal: str
    objective: str
    constraints: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    domain_pack: DomainPackId = "generic_web"
    mode: RuntimeMode = "review_only"
    domain_hints: dict[str, Any] = Field(default_factory=dict)
    safety_mode: SafetyMode = "confirm_before_act"
    index_mode: IndexMode = "adaptive"
    created_at: str


class ActionProposal(BaseModel):
    action_id: str
    action: ActionKind
    reasoning: str
    confidence: float = 0.0
    safety_level: SafetyLevel = "medium"
    requires_confirmation: bool = True
    status: Literal["proposed", "approved", "executed", "failed", "skipped"] = "proposed"
    target_text: str | None = None
    role: str | None = None
    input_text: str | None = None
    value: str | None = None
    url: str | None = None
    validation_text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewItem(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_review_item(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        title = str(data.get("title") or data.get("field_label") or data.get("page_hint") or data.get("anchor_text") or "Recommendation").strip()
        recommendation = str(data.get("recommended_value") or data.get("recommendation") or "").strip()
        why_it_matters = str(data.get("why_it_matters") or data.get("reasoning") or "").strip()
        evidence = str(data.get("evidence") or data.get("reasoning") or "").strip()
        data.setdefault("title", title)
        data.setdefault("field_type", "field")
        data.setdefault("current_value", str(data.get("current_value") or "").strip())
        if not data.get("recommended_value") and recommendation:
            data["recommended_value"] = recommendation
        data.setdefault("recommended_range", str(data.get("recommended_range") or "").strip())
        data.setdefault("why_it_matters", why_it_matters)
        data.setdefault("evidence", evidence)
        data.setdefault("confidence", float(data.get("confidence", 0.0) or 0.0))
        data.setdefault("actionability", "manual_only")
        data.setdefault("dependencies", list(data.get("dependencies", []) or []))
        data.setdefault("requires_followup_check", bool(data.get("requires_followup_check", False)))
        return data

    item_id: str
    title: str = ""
    page_hint: str = ""
    anchor_text: str = ""
    field_type: ReviewFieldType = "field"
    current_value: str = ""
    recommended_value: str = ""
    recommended_range: str = ""
    why_it_matters: str = ""
    evidence: str = ""
    priority: PriorityLevel = "medium"
    confidence: float = 0.0
    actionability: ReviewActionability = "manual_only"
    dependencies: list[str] = Field(default_factory=list)
    requires_followup_check: bool = False
    apply_actions: list[ActionProposal] = Field(default_factory=list)

    @property
    def field_label(self) -> str:
        return self.title

    @property
    def recommendation(self) -> str:
        if self.recommended_value and self.recommended_range:
            return f"{self.recommended_value} ({self.recommended_range})"
        return self.recommended_value or self.recommended_range

    @property
    def reasoning(self) -> str:
        return self.why_it_matters


class SupplementaryScreenshot(BaseModel):
    label: str = "capture"
    screenshot_b64: str = ""
    screenshot_path: str | None = None


class ObservationPacket(BaseModel):
    session_id: str
    screenshot_b64: str | None = None
    screenshot_path: str | None = None
    supplementary_screenshots: list[SupplementaryScreenshot] = Field(default_factory=list)
    page_url: str = ""
    page_title: str = ""
    dom_summary: str = ""
    visible_text_summary: str = ""
    prior_actions: list[dict[str, Any]] = Field(default_factory=list)
    active_goal: str = ""
    domain_pack: DomainPackId = "generic_web"
    safety_mode: SafetyMode = "confirm_before_act"
    browser_metadata: dict[str, Any] = Field(default_factory=dict)
    captured_at: str


class DomainPackConfig(BaseModel):
    id: DomainPackId
    name: str
    description: str
    goal_guidance: list[str] = Field(default_factory=list)
    strategy_hints: list[str] = Field(default_factory=list)
    risk_rules: list[str] = Field(default_factory=list)


class IndexSummary(BaseModel):
    strategic_summary: str = ""
    site_check_summary: str = ""
    previous_period_summary: list[str] = Field(default_factory=list)
    current_focus: str = ""
    top_recommendations: list[str] = Field(default_factory=list)
    detected_changes: list[str] = Field(default_factory=list)


class ReviewBatch(BaseModel):
    session_id: str
    summary: str = ""
    rationale: list[str] = Field(default_factory=list)
    current_focus: str = ""
    previous_period_summary: list[str] = Field(default_factory=list)
    items: list[ReviewItem] = Field(default_factory=list)
    actions: list[ActionProposal] = Field(default_factory=list)
    apply_ready: bool = False
    insufficiently_grounded: bool = False
    comparison_payload: dict[str, Any] = Field(default_factory=dict)
    beta_warning: str = "Apply is beta. Manual application is safer."


class SessionMemory(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        legacy_status = str(data.get("status", "")).strip()
        if legacy_status:
            if legacy_status == "awaiting_index":
                data["status"] = "session_ready_to_index"
            elif legacy_status in {"ready", "planned", "planning", "observed"}:
                if data.get("review_batch") or data.get("pending_approvals"):
                    data["status"] = "review_batch_ready"
                elif data.get("last_indexed_at"):
                    data["status"] = "index_summary_ready"
                else:
                    data["status"] = "session_ready_to_index"
        goal = data.get("goal")
        if isinstance(goal, dict) and not data.get("mode"):
            data["mode"] = goal.get("mode", "review_only")
        elif not data.get("mode") and data.get("domain_pack") == "marketplace_simulation":
            data["mode"] = "complex_workspace"
        data.setdefault("review_ready", bool(data.get("review_batch")))
        data.setdefault("activity_log_tail", list(data.get("event_log", [])[:5]))
        data.setdefault("structure_map_summary", dict(data.get("structure_map_summary", {}) or {}))
        data.setdefault("coverage_summary", dict(data.get("coverage_summary", {}) or {}))
        data.setdefault("degraded_reason", str(data.get("degraded_reason", "") or ""))
        data.setdefault("insufficiently_grounded", bool(data.get("insufficiently_grounded", False)))
        return data

    session_id: str
    project_name: str
    goal: GoalSpec
    mode: RuntimeMode = "review_only"
    current_site: str = ""
    domain_pack: DomainPackId = "generic_web"
    index_mode: IndexMode = "adaptive"
    site_memory_key: str = ""
    site_origin: str = ""
    site_fingerprint: str = ""
    site_check_status: SiteCheckStatus = "unchecked"
    site_check_summary: str = "Site changes have not been checked yet."
    last_site_check_at: str = ""
    site_check_required: bool = True
    strategic_summary: str = ""
    indexed_context: dict[str, Any] = Field(default_factory=dict)
    last_indexed_at: str = ""
    memory_summary: str = ""
    checkpoints: list[dict[str, Any]] = Field(default_factory=list)
    pending_approvals: list[ActionProposal] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    action_history: list[dict[str, Any]] = Field(default_factory=list)
    event_log: list[str] = Field(default_factory=list)
    activity_log_tail: list[str] = Field(default_factory=list)
    last_observation: ObservationPacket | None = None
    live_advice: list[str] = Field(default_factory=list)
    status: SessionStatus = "idle"
    last_page_signature: str = ""
    index_summary: IndexSummary | None = None
    review_batch: ReviewBatch | None = None
    review_ready: bool = False
    structure_map_summary: dict[str, Any] = Field(default_factory=dict)
    coverage_summary: dict[str, Any] = Field(default_factory=dict)
    degraded_reason: str = ""
    insufficiently_grounded: bool = False
    created_at: str
    updated_at: str


class CreateSessionRequest(BaseModel):
    goal: str
    project_name: str = "Default Project"
    domain_hint: str | None = None
    mode: RuntimeMode | None = None
    safety_mode: SafetyMode = "confirm_before_act"
    index_mode: IndexMode | None = None


class PlanResponse(BaseModel):
    session_id: str
    strategic_summary: str = ""
    index_refreshed: bool = False
    memory_summary: str = ""
    live_advice: list[str] = Field(default_factory=list)
    actions: list[ActionProposal] = Field(default_factory=list)
    grounded_on: list[str] = Field(default_factory=list)


class ExecuteResultPayload(BaseModel):
    session_id: str
    results: list[dict[str, Any]] = Field(default_factory=list)


class ApprovalRequest(BaseModel):
    action_ids: list[str] = Field(default_factory=list)


class SiteMemory(BaseModel):
    memory_key: str
    site_origin: str
    domain_pack: DomainPackId = "generic_web"
    index_mode: IndexMode = "adaptive"
    site_fingerprint: str = ""
    structure_digest: str = ""
    strategic_summary: str = ""
    indexed_context: dict[str, Any] = Field(default_factory=dict)
    last_checked_at: str = ""
    last_indexed_at: str = ""
    change_status: SiteCheckStatus = "unchecked"
    change_summary: str = ""


class OverlayCommandRequest(BaseModel):
    command: Literal[
        "start_session",
        "resume_session",
        "show_setup",
        "start_index",
        "enter_live_advice",
        "page_changed",
        "prepare_review_batch",
        "apply_review_batch",
        "open_logs",
        "open_sessions",
        "open_map",
        "open_review",
        "stop_session",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
