from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from agents.base import RawLead

_KNOWLEDGE_PATH = Path(__file__).parent.parent / "knowledge" / "atuin.json"

_TIER_A = 22
_TIER_B = 15


@dataclass
class ScoredLead(RawLead):
    icp_score: int = 0
    tier: str = ""
    score_breakdown: dict = field(default_factory=dict)
    feature_match: str = ""


class Coordinator:
    def __init__(self) -> None:
        self.logger = logging.getLogger(__name__)
        with _KNOWLEDGE_PATH.open() as fh:
            self.knowledge: dict = json.load(fh)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, leads: list[RawLead]) -> list[ScoredLead]:
        deduped = self._deduplicate(leads)
        self.logger.info("After dedup: %d leads (from %d raw)", len(deduped), len(leads))

        result: list[ScoredLead] = []
        for lead in deduped:
            scored = self._score_lead(lead)
            tiered = self._assign_tier(scored)
            if tiered is not None:
                result.append(tiered)

        self.logger.info(
            "Tiered: %d leads (%d A, %d B)",
            len(result),
            sum(1 for l in result if l.tier == "A"),
            sum(1 for l in result if l.tier == "B"),
        )
        return result

    # ------------------------------------------------------------------
    # Step 1 — deduplication
    # ------------------------------------------------------------------

    def _deduplicate(self, leads: list[RawLead]) -> list[RawLead]:
        by_domain: dict[str, RawLead] = {}
        unnamed: list[RawLead] = []

        for lead in leads:
            if not lead.company_domain:
                unnamed.append(lead)
                continue
            existing = by_domain.get(lead.company_domain)
            if existing is None or lead.raw_score > existing.raw_score:
                by_domain[lead.company_domain] = lead

        return list(by_domain.values()) + unnamed

    # ------------------------------------------------------------------
    # Step 2 — ICP scoring
    # ------------------------------------------------------------------

    def _score_lead(self, lead: RawLead) -> ScoredLead:
        breakdown = {
            "team_size_fit":    self._team_size_fit(lead.team_size_signal),
            "pain_specificity": self._pain_specificity(lead),
            "stack_density":    self._stack_density(lead.stack_signals),
            "dm_proximity":     self._dm_proximity(lead),
            "cli_signal":       self._cli_signal(lead.source),
            "urgency":          self._urgency(lead.raw_score),
        }
        total = sum(breakdown.values())
        feature = self._feature_match(lead.pain_type)

        return ScoredLead(
            **lead.__dict__,
            icp_score=total,
            tier="",
            score_breakdown=breakdown,
            feature_match=feature,
        )

    def _team_size_fit(self, signal: str) -> int:
        n = _parse_int(signal)
        if n is None:
            return 1
        if 15 <= n <= 50:
            return 5
        if 51 <= n <= 75:
            return 4
        if 10 <= n <= 14:
            return 2
        return 1

    def _pain_specificity(self, lead: RawLead) -> int:
        length = len(lead.pain_snippet)
        if length > 150 and lead.pain_type != "unknown":
            return 5
        if length > 50:
            return 3
        return 1

    def _stack_density(self, stack_signals: list[str]) -> int:
        count = len(stack_signals)
        if count >= 3:
            return 5
        if count == 2:
            return 3
        if count == 1:
            return 2
        return 1

    def _dm_proximity(self, lead: RawLead) -> int:
        has_dm = bool(lead.dm_hints)
        has_domain = bool(lead.company_domain)
        if has_dm and has_domain:
            return 5
        if has_dm or has_domain:
            return 3
        return 1

    def _cli_signal(self, source: str) -> int:
        return {"github_hunter": 5, "community_scanner": 3, "job_scanner": 2}.get(source, 1)

    def _urgency(self, raw_score: int) -> int:
        return max(1, min(5, raw_score // 2))

    # ------------------------------------------------------------------
    # Step 3 — tiering
    # ------------------------------------------------------------------

    def _assign_tier(self, lead: ScoredLead) -> ScoredLead | None:
        if lead.icp_score >= _TIER_A:
            lead.tier = "A"
            return lead
        if lead.icp_score >= _TIER_B:
            lead.tier = "B"
            return lead
        self.logger.debug(
            "Dropped %s (score %d): %s",
            lead.company_domain or lead.evidence_url,
            lead.icp_score,
            lead.score_breakdown,
        )
        return None

    # ------------------------------------------------------------------
    # Step 4 — feature match
    # ------------------------------------------------------------------

    def _feature_match(self, pain_type: str) -> str:
        return self.knowledge["product"]["pain_to_feature_map"].get(pain_type, "")


# ------------------------------------------------------------------
# Module-level utility
# ------------------------------------------------------------------

def _parse_int(text: str) -> int | None:
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None
