from __future__ import annotations

from marketplace_bot.navigator_models import DomainPackConfig


DOMAIN_PACKS: dict[str, DomainPackConfig] = {
    "generic_web": DomainPackConfig(
        id="generic_web",
        name="General Web",
        description="Universal UI navigator support for complex web workflows, nested interfaces, legacy business applications, and stateful internal tools that reward visual precision over speed.",
        goal_guidance=[
            "Prefer grounded observations over assumptions.",
            "Preserve workflow context across dense, recurring interfaces instead of treating each page as a fresh start.",
            "Break tasks into visible, reversible steps whenever possible.",
            "If confidence is low, fall back to suggest_only.",
        ],
        strategy_hints=[
            "Use screenshots as the primary source of UI understanding.",
            "Use visible text and DOM summary as secondary grounding.",
            "Favor precise, page-anchored recommendations over broad generic browsing advice.",
            "Only execute after approval unless autonomous mode is explicitly enabled.",
        ],
        risk_rules=[
            "Never submit, delete, purchase, or change account settings without confirmation.",
        ],
    ),
    "marketplace_simulation": DomainPackConfig(
        id="marketplace_simulation",
        name="Marketplace Simulation",
        description="Domain-optimized operating mode for a dense quarter-based simulation workspace, with editable-quarter focus, prior-period context, and profit-risk heuristics layered on top of the core UI navigator runtime.",
        goal_guidance=[
            "Maximize score and profit while avoiding bankruptcy.",
            "Treat only the latest quarter as editable unless the UI clearly allows otherwise.",
            "Preserve quarter-by-quarter rationale so later review stays inspectable.",
        ],
        strategy_hints=[
            "Prefer deterministic recommendations when numeric tables are visible.",
            "Use prior-quarter data for context, not as direct execution targets.",
            "Explain recommendations in terms of revenue, cash, cost, or risk.",
        ],
        risk_rules=[
            "Final submit always requires confirmation.",
            "If data is incomplete, produce audit insights rather than pretending precision.",
        ],
    ),
}


def get_domain_pack(domain_id: str) -> DomainPackConfig:
    return DOMAIN_PACKS.get(domain_id, DOMAIN_PACKS["generic_web"])
