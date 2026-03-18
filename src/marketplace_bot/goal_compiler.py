from __future__ import annotations

from marketplace_bot.domain_packs import get_domain_pack
from marketplace_bot.navigator_models import GoalSpec, IndexMode, RuntimeMode, SafetyMode
from marketplace_bot.state_store import utc_now_iso


class GoalCompiler:
    def compile(
        self,
        raw_goal: str,
        domain_hint: str | None = None,
        mode: RuntimeMode | None = None,
        safety_mode: SafetyMode = "confirm_before_act",
        index_mode: IndexMode | None = None,
    ) -> GoalSpec:
        domain_pack = self._detect_domain_pack(raw_goal, domain_hint)
        pack = get_domain_pack(domain_pack)
        resolved_mode = self._resolve_mode(domain_pack, mode)
        resolved_index_mode = self._resolve_index_mode(domain_pack, index_mode)

        if domain_pack == "marketplace_simulation":
            objective = (
                "Act as a specialized operating mode within a universal UI navigator for complex web workspaces: "
                "audit prior Marketplace decisions, propose grounded edits for the editable quarter, and preserve an inspectable quarter-by-quarter history."
            )
            constraints = [
                "Prefer recommendation-first behavior unless the user approves execution.",
                "Optimize for profit, score quality, and bankruptcy avoidance.",
                "Treat screenshots as primary grounding, AX summaries as secondary grounding, and visible table text as tertiary grounding.",
            ]
            success_criteria = [
                "Produce quarter-aware recommendations tied to visible game data.",
                "Preserve history and rationale for later review.",
                "Require explicit confirmation before any irreversible action.",
            ]
            domain_hints = {
                "track": "simulation",
                "quarter_aware": True,
                "history_inspectable": True,
                "runtime_mode": resolved_mode,
                "recommended_index_mode": resolved_index_mode,
                "domain_pack_description": pack.description,
            }
        else:
            objective = (
                "Act as a universal UI navigator for complex web applications that grounds on the current screen, "
                "preserves workflow context, proposes precise safe actions, and supports simpler sites as a lighter subset."
            )
            constraints = [
                "Use screenshots as the primary source of UI understanding.",
                "Use AX summaries as the secondary source of UI structure and actionability.",
                "Prefer suggest_only fallback when confidence is weak.",
                "Require confirmation before sensitive actions unless autonomous mode is explicitly enabled.",
            ]
            success_criteria = [
                "Translate the user's goal into clear next actions.",
                "Stay grounded in the current browser state.",
                "Resume work from memory without restarting from scratch.",
            ]
            domain_hints = {
                "track": "ui_navigator",
                "runtime_mode": resolved_mode,
                "recommended_index_mode": resolved_index_mode,
                "domain_pack_description": pack.description,
            }

        return GoalSpec(
            raw_goal=raw_goal.strip(),
            objective=objective,
            constraints=constraints,
            success_criteria=success_criteria,
            domain_pack=domain_pack,
            mode=resolved_mode,
            domain_hints=domain_hints,
            safety_mode=safety_mode,
            index_mode=resolved_index_mode,
            created_at=utc_now_iso(),
        )

    @staticmethod
    def _detect_domain_pack(raw_goal: str, domain_hint: str | None) -> str:
        hint = (domain_hint or "").strip().lower()
        goal = raw_goal.lower()
        marketplace_tokens = ("marketplace", "quarter", "simulation", "pro forma", "sales channel")
        if hint in ("marketplace_simulation", "marketplace", "simulation"):
            return "marketplace_simulation"
        if any(token in goal for token in marketplace_tokens):
            return "marketplace_simulation"
        return "generic_web"

    @staticmethod
    def _resolve_index_mode(domain_pack: str, index_mode: IndexMode | None) -> IndexMode:
        if index_mode is not None:
            return index_mode
        if domain_pack == "marketplace_simulation":
            return "advanced"
        return "advanced"

    @staticmethod
    def _resolve_mode(domain_pack: str, mode: RuntimeMode | None) -> RuntimeMode:
        if mode is not None:
            return mode
        if domain_pack == "marketplace_simulation":
            return "complex_workspace"
        return "review_only"
