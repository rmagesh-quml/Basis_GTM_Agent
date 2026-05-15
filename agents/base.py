from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

PainType = Literal[
    "runbook_sprawl",
    "deploy_chaos",
    "onboarding_debt",
    "script_rot",
    "shell_history_not_shared",
]

_PAIN_HINTS: dict[str, list[str]] = {
    "runbook_sprawl": [
        "runbook", "runbooks", "wiki", "confluence", "notion", "scattered",
        "docs", "documentation", "canonical", "playbook", "playbooks",
    ],
    "deploy_chaos": [
        "deploy", "deployment", "release", "rollback", "prod", "production",
        "staging", "incident", "outage", "hotfix", "copy-paste deploy",
    ],
    "onboarding_debt": [
        "onboard", "onboarding", "new engineer", "new hire", "ramp",
        "tribal knowledge", "knowledge transfer", "slow onboarding",
    ],
    "script_rot": [
        "script", "rot", "stale", "outdated", "broken", "legacy",
        "unmaintained", "bit rot", "script_rot",
    ],
    "shell_history_not_shared": [
        "shell history", "history", "command history", "who knows",
        "five commands", "sync history", "shared commands",
    ],
}

_KNOWLEDGE_PATH = Path(__file__).parent.parent / "knowledge" / "atuin.json"


@dataclass
class RawLead:
    company_domain: str
    source: str
    pain_snippet: str
    evidence_url: str
    pain_type: PainType
    team_size_signal: str
    stack_signals: list[str] = field(default_factory=list)
    dm_hints: list[str] = field(default_factory=list)
    raw_score: int = 0


class BaseAgent(ABC):
    def __init__(self) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        with _KNOWLEDGE_PATH.open() as fh:
            self.knowledge: dict = json.load(fh)
        self._keyword_pool: list[str] = self._build_keyword_pool()

    def _build_keyword_pool(self) -> list[str]:
        icp = self.knowledge["icp"]
        # Extract individual lowercase tokens from pain_keywords phrases
        pain_terms = [
            token
            for phrase in icp["pain_keywords"]
            for token in phrase.lower().split()
            if len(token) > 3
        ]
        # Extract individual lowercase tokens from stack_signals sentences
        stack_terms = [
            token.strip("(),")
            for phrase in icp["stack_signals"]
            for token in phrase.lower().split()
            if len(token) > 3
        ]
        # Also keep multi-word pain_keywords intact for substring matching
        full_phrases = [kw.lower() for kw in icp["pain_keywords"]]
        return list(set(pain_terms + stack_terms + full_phrases))

    def matches_keywords(self, text: str) -> bool:
        lowered = text.lower()
        return any(kw in lowered for kw in self._keyword_pool)

    def classify_pain(self, text: str) -> PainType:
        lowered = text.lower()
        scores: dict[str, int] = {pain: 0 for pain in _PAIN_HINTS}
        for pain, hints in _PAIN_HINTS.items():
            for hint in hints:
                if hint in lowered:
                    scores[pain] += 1
        best = max(scores, key=lambda p: scores[p])
        # Fall back to runbook_sprawl when nothing matches — most common cold signal
        return best if scores[best] > 0 else "runbook_sprawl"  # type: ignore[return-value]

    @abstractmethod
    async def run(self) -> list[RawLead]: ...
