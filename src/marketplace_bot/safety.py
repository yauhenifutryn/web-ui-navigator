from __future__ import annotations

import re

from marketplace_bot.navigator_models import ActionProposal, SafetyLevel, SafetyMode

HIGH_RISK_KEYWORDS = (
    "submit", "delete", "remove", "purchase", "pay", "account",
    "save", "confirm", "checkout", "transfer", "authorize", "cancel",
)

_HIGH_RISK_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in HIGH_RISK_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


class SafetyPolicy:
    def classify(self, proposal: ActionProposal) -> SafetyLevel:
        joined = " ".join(
            filter(
                None,
                [
                    proposal.action,
                    proposal.target_text,
                    proposal.url,
                    proposal.validation_text,
                ],
            )
        )

        if _HIGH_RISK_PATTERN.search(joined):
            return "high"
        if proposal.action in ("type", "select", "click", "navigate"):
            return "medium"
        return "low"

    def apply(self, proposal: ActionProposal, safety_mode: SafetyMode) -> ActionProposal:
        proposal.safety_level = self.classify(proposal)
        if safety_mode == "guided":
            proposal.requires_confirmation = True
        elif safety_mode == "autonomous":
            proposal.requires_confirmation = proposal.safety_level == "high"
        else:
            proposal.requires_confirmation = proposal.safety_level != "low"
        return proposal
