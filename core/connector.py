from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from core.enricher import EnrichedLead

_KNOWLEDGE_PATH = Path(__file__).parent.parent / "knowledge" / "atuin.json"
_LEADS_PATH = Path(__file__).parent.parent / "leads.json"

_CONTACTS_URL = "https://api.apollo.io/api/v1/contacts"
_NOTES_URL = "https://api.apollo.io/api/v1/notes"
_SEQUENCE_URL = "https://api.apollo.io/api/v1/emailer_campaigns/{seq_id}/add_contact_ids"
_TIMEOUT = httpx.Timeout(10.0)


def _field(lead: Any, key: str, default: Any = "") -> Any:
    """Access a field from either a dataclass instance or a plain dict."""
    if isinstance(lead, dict):
        return lead.get(key, default)
    return getattr(lead, key, default)


class Connector:
    def __init__(self) -> None:
        self.logger = logging.getLogger(__name__)
        with _KNOWLEDGE_PATH.open() as fh:
            self.knowledge: dict = json.load(fh)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, leads: list[Any]) -> dict[str, Any]:
        api_key = os.environ.get("APOLLO_API_KEY")
        if not api_key:
            self.logger.error("APOLLO_API_KEY not set — cannot push to Apollo")
            return {"pushed": 0, "failed": len(leads), "errors": ["APOLLO_API_KEY not set"]}

        approved = [l for l in leads if _field(l, "status") == "approved"]
        self.logger.info("Pushing %d approved leads to Apollo", len(approved))

        pushed, failed = 0, 0
        errors: list[str] = []

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for lead in approved:
                try:
                    await self._push_lead(client, lead, api_key)
                    pushed += 1
                    self.logger.info("Pushed: %s", _field(lead, "company_domain"))
                except Exception as exc:
                    failed += 1
                    msg = f"{_field(lead, 'company_domain')}: {exc}"
                    self.logger.error("Failed: %s", msg)
                    errors.append(msg)

        result = {"pushed": pushed, "failed": failed, "errors": errors}
        self.logger.info("Connector done — %s", result)
        return result

    # ------------------------------------------------------------------
    # Three-step push per lead
    # ------------------------------------------------------------------

    async def _push_lead(
        self, client: httpx.AsyncClient, lead: Any, api_key: str
    ) -> None:
        headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}

        contact_id = await self._create_contact(client, lead, headers)

        # Note failure is non-fatal; sequence enrollment is the critical step.
        try:
            await self._add_note(client, contact_id, lead, headers)
        except Exception as exc:
            self.logger.warning(
                "Note failed for %s (continuing): %s",
                _field(lead, "company_domain"),
                exc,
            )

        await self._enroll_in_sequence(client, contact_id, lead, headers)

    async def _create_contact(
        self, client: httpx.AsyncClient, lead: Any, headers: dict
    ) -> str:
        payload = self._contact_payload(lead)
        r = await client.post(_CONTACTS_URL, json=payload, headers=headers)
        r.raise_for_status()
        contact = r.json().get("contact", {})
        contact_id: str = contact.get("id", "")
        if not contact_id:
            raise ValueError(f"Apollo returned no contact id for {_field(lead, 'company_domain')}")
        return contact_id

    async def _add_note(
        self,
        client: httpx.AsyncClient,
        contact_id: str,
        lead: Any,
        headers: dict,
    ) -> None:
        payload = {
            "contact_id": contact_id,
            "body": _field(lead, "pain_snippet"),
            "type": "note",
        }
        r = await client.post(_NOTES_URL, json=payload, headers=headers)
        r.raise_for_status()

    async def _enroll_in_sequence(
        self,
        client: httpx.AsyncClient,
        contact_id: str,
        lead: Any,
        headers: dict,
    ) -> None:
        seq_id = self._sequence_id(_field(lead, "tier"))
        url = _SEQUENCE_URL.format(seq_id=seq_id)
        payload = {"contact_ids": [contact_id]}
        r = await client.post(url, json=payload, headers=headers)
        r.raise_for_status()

    # ------------------------------------------------------------------
    # Payload builders
    # ------------------------------------------------------------------

    def _contact_payload(self, lead: Any) -> dict[str, Any]:
        domain = _field(lead, "company_domain")
        stack = _field(lead, "stack_signals", [])
        return {
            "first_name": _field(lead, "dm_first"),
            "last_name": _field(lead, "dm_last"),
            "email": _field(lead, "dm_email") or None,
            "title": _field(lead, "dm_title"),
            "organization_name": domain,
            "website_url": f"https://{domain}" if domain else "",
            "custom_fields": {
                "icp_tier": _field(lead, "tier"),
                "icp_score": _field(lead, "icp_score"),
                "pain_snippet": _field(lead, "pain_snippet"),
                "pain_type": _field(lead, "pain_type"),
                "feature_match": _field(lead, "feature_match"),
                "evidence_url": _field(lead, "evidence_url"),
                "source": _field(lead, "source"),
                "stack_signals": ", ".join(stack) if isinstance(stack, list) else stack,
                "custom_note": _field(lead, "custom_note"),
            },
        }

    def _sequence_id(self, tier: str) -> str:
        seqs = self.knowledge["sequences"]
        return seqs["tier_a_id"] if tier == "A" else seqs["tier_b_id"]


# ------------------------------------------------------------------
# CLI entry point — called by ui/review.py via subprocess
# ------------------------------------------------------------------

def _load_approved() -> list[dict]:
    if not _LEADS_PATH.exists():
        return []
    leads: list[dict] = json.loads(_LEADS_PATH.read_text())
    return [l for l in leads if l.get("status") == "approved"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    approved = _load_approved()
    if not approved:
        print("No approved leads in leads.json.")
    else:
        connector = Connector()
        result = asyncio.run(connector.run(approved))
        print(json.dumps(result, indent=2))
