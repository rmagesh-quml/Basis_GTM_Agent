from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from core.coordinator import ScoredLead

_KNOWLEDGE_PATH = Path(__file__).parent.parent / "knowledge" / "atuin.json"
_LEADS_PATH = Path(__file__).parent.parent / "leads.json"
_APOLLO_URL = "https://api.apollo.io/api/v1/people/match"
_TIMEOUT = httpx.Timeout(10.0)

# Strip role descriptions in parentheses so Apollo receives clean title strings:
# "DevOps / Infrastructure engineer (primary buyer…)" → "DevOps / Infrastructure engineer"
_PAREN_RE = re.compile(r"\s*\([^)]*\)")


@dataclass
class EnrichedLead(ScoredLead):
    dm_first: str = ""
    dm_last: str = ""
    dm_email: str | None = None
    dm_title: str = ""
    dm_linkedin: str = ""
    needs_manual_review: bool = False
    apollo_person_id: str = ""


class Enricher:
    def __init__(self) -> None:
        self.logger = logging.getLogger(__name__)
        with _KNOWLEDGE_PATH.open() as fh:
            self.knowledge: dict = json.load(fh)
        self._titles: list[str] = self._clean_role_targets()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, leads: list[ScoredLead]) -> list[EnrichedLead]:
        api_key = os.environ.get("APOLLO_API_KEY")
        if not api_key:
            self.logger.error("APOLLO_API_KEY not set — marking all for manual review")
            return self._write_and_return(
                [self._no_match(lead) for lead in self._sort(leads)]
            )

        sorted_leads = self._sort(leads)
        enriched: list[EnrichedLead] = []

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for lead in sorted_leads:
                el = await self._enrich_one(client, lead, api_key)
                enriched.append(el)

        return self._write_and_return(enriched)

    # ------------------------------------------------------------------
    # Per-lead enrichment
    # ------------------------------------------------------------------

    async def _enrich_one(
        self, client: httpx.AsyncClient, lead: ScoredLead, api_key: str
    ) -> EnrichedLead:
        if not lead.company_domain:
            return self._no_match(lead)
        try:
            person = await self._call_apollo(client, lead.company_domain, api_key)
        except Exception as exc:
            self.logger.error("Apollo error for %s: %s", lead.company_domain, exc)
            return self._no_match(lead)

        if person:
            return self._from_person(lead, person)
        return self._no_match(lead)

    async def _call_apollo(
        self, client: httpx.AsyncClient, domain: str, api_key: str
    ) -> dict[str, Any] | None:
        payload = {"domain": domain, "titles": self._titles}
        headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}

        r = await client.post(_APOLLO_URL, json=payload, headers=headers)

        if r.status_code == 429:
            self.logger.warning("Apollo 429 on %s — sleeping 60s and retrying", domain)
            await asyncio.sleep(60)
            r = await client.post(_APOLLO_URL, json=payload, headers=headers)

        r.raise_for_status()
        return r.json().get("person")

    # ------------------------------------------------------------------
    # EnrichedLead constructors
    # ------------------------------------------------------------------

    def _from_person(self, lead: ScoredLead, person: dict[str, Any]) -> EnrichedLead:
        return EnrichedLead(
            **lead.__dict__,
            dm_first=person.get("first_name") or "",
            dm_last=person.get("last_name") or "",
            dm_email=person.get("email") or None,
            dm_title=person.get("title") or "",
            dm_linkedin=person.get("linkedin_url") or "",
            needs_manual_review=False,
            apollo_person_id=person.get("id") or "",
        )

    def _no_match(self, lead: ScoredLead) -> EnrichedLead:
        return EnrichedLead(
            **lead.__dict__,
            dm_first="",
            dm_last="",
            dm_email=None,
            dm_title="",
            dm_linkedin="",
            needs_manual_review=True,
            apollo_person_id="",
        )

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _write_and_return(self, leads: list[EnrichedLead]) -> list[EnrichedLead]:
        payload = [dataclasses.asdict(el) for el in leads]
        _LEADS_PATH.write_text(json.dumps(payload, indent=2, default=str))
        self.logger.info(
            "Wrote %d enriched leads to %s (%d manual review)",
            len(leads),
            _LEADS_PATH,
            sum(1 for l in leads if l.needs_manual_review),
        )
        return leads

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sort(self, leads: list[ScoredLead]) -> list[ScoredLead]:
        return sorted(leads, key=lambda l: (0 if l.tier == "A" else 1, -l.icp_score))

    def _clean_role_targets(self) -> list[str]:
        raw: list[str] = self.knowledge["icp"]["role_targets"]
        return [_PAREN_RE.sub("", t).strip() for t in raw]
