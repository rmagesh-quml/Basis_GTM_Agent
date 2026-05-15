from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from agents.base import BaseAgent, RawLead

# Companies selected for terminal-heavy stacks: infra tooling, cloud-native,
# developer platforms, and OSS infrastructure shops where DevOps/SRE roles are
# structurally common. Slugs are the board identifiers used by Greenhouse and
# Lever — 404s on either board are silently skipped, so false slugs are cheap.
_SEED_COMPANIES: list[str] = [
    "stripe",
    "cloudflare",
    "datadog",
    "hashicorp",
    "pagerduty",
    "snyk",
    "circleci",
    "sourcegraph",
    "weaveworks",
    "pulumi",
    "teleport",
    "tailscale",
    "render",
    "supabase",
    "planetscale",
    "temporal",
    "buf",
    "dagger",
    "earthly",
    "depot",
    "modal",
    "encore",
    "grafana",
    "netlify",
    "vercel",
    "neon",
    "fly",
    "railway",
    "charm",
    "codecrafters",
]

_ROLE_FILTERS: list[str] = [
    "platform engineer",
    "devops",
    "site reliability",
    "infrastructure",
    "developer experience",
    "internal tooling",
]

_STACK_TOKENS: list[tuple[str, str]] = [
    ("kubernetes", "kubernetes"),
    ("k8s", "kubernetes"),
    ("terraform", "terraform"),
    ("pulumi", "pulumi"),
    ("postgres", "postgres"),
    ("postgresql", "postgres"),
    ("mysql", "mysql"),
    ("github actions", "github-actions"),
    ("github-actions", "github-actions"),
    ("circleci", "circleci"),
    ("prometheus", "prometheus"),
    ("grafana", "grafana"),
    ("ansible", "ansible"),
    ("helm", "helm"),
    ("docker", "docker"),
    ("aws", "aws"),
    ("gcp", "gcp"),
    ("azure", "azure"),
    ("ecs", "ecs"),
    ("bash", "bash"),
    ("python", "python"),
    ("argocd", "argocd"),
    ("flux", "flux"),
]

_GH_BASE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
_LV_BASE = "https://api.lever.co/v0/postings/{slug}?mode=json"

_TAG_RE = re.compile(r"<[^>]+>")
_TIMEOUT = httpx.Timeout(5.0)


def _strip_html(html: str) -> str:
    return _TAG_RE.sub(" ", html or "")


def _matches_role(text: str) -> bool:
    lowered = text.lower()
    return any(role in lowered for role in _ROLE_FILTERS)


class JobScanner(BaseAgent):
    def __init__(self) -> None:
        super().__init__()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> list[RawLead]:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            tasks = [self._fetch_company(client, slug) for slug in _SEED_COMPANIES]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        leads: list[RawLead] = []
        for result in results:
            if isinstance(result, list):
                leads.extend(result)
            elif isinstance(result, Exception):
                self.logger.debug("Company gather error: %s", result)

        self.logger.info("JobScanner produced %d leads", len(leads))
        return leads

    # ------------------------------------------------------------------
    # Per-company fan-out
    # ------------------------------------------------------------------

    async def _fetch_company(self, client: httpx.AsyncClient, slug: str) -> list[RawLead]:
        gh, lv = await asyncio.gather(
            self._fetch_greenhouse(client, slug),
            self._fetch_lever(client, slug),
            return_exceptions=True,
        )
        leads: list[RawLead] = []
        if isinstance(gh, list):
            leads.extend(gh)
        if isinstance(lv, list):
            leads.extend(lv)
        return leads

    # ------------------------------------------------------------------
    # Board fetchers
    # ------------------------------------------------------------------

    async def _fetch_greenhouse(self, client: httpx.AsyncClient, slug: str) -> list[RawLead]:
        url = _GH_BASE.format(slug=slug)
        try:
            r = await client.get(url)
        except httpx.TimeoutException:
            return []
        except httpx.RequestError as exc:
            self.logger.debug("Greenhouse request error %s: %s", slug, exc)
            return []

        if r.status_code == 404:
            return []
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError:
            return []

        jobs: list[dict[str, Any]] = r.json().get("jobs", [])
        return self._process_greenhouse(jobs, slug)

    async def _fetch_lever(self, client: httpx.AsyncClient, slug: str) -> list[RawLead]:
        url = _LV_BASE.format(slug=slug)
        try:
            r = await client.get(url)
        except httpx.TimeoutException:
            return []
        except httpx.RequestError as exc:
            self.logger.debug("Lever request error %s: %s", slug, exc)
            return []

        if r.status_code == 404:
            return []
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError:
            return []

        postings: list[dict[str, Any]] = r.json()
        if not isinstance(postings, list):
            return []
        return self._process_lever(postings, slug)

    # ------------------------------------------------------------------
    # Job processors
    # ------------------------------------------------------------------

    def _process_greenhouse(self, jobs: list[dict], slug: str) -> list[RawLead]:
        leads: list[RawLead] = []
        for job in jobs:
            title = job.get("title", "")
            raw_content = _strip_html(job.get("content", ""))
            url = job.get("absolute_url", "")
            if not _matches_role(title) and not _matches_role(raw_content[:500]):
                continue
            if not self.matches_keywords(f"{title} {raw_content}"):
                continue
            leads.append(self._build_lead(title, raw_content, url, slug))
        return leads

    def _process_lever(self, postings: list[dict], slug: str) -> list[RawLead]:
        leads: list[RawLead] = []
        for posting in postings:
            title = posting.get("text", "")
            description = _strip_html(posting.get("description", ""))
            lists_text = " ".join(
                _strip_html(lst.get("content", ""))
                for lst in posting.get("lists", [])
            )
            full_text = f"{description} {lists_text}"
            url = posting.get("hostedUrl", "")
            if not _matches_role(title) and not _matches_role(full_text[:500]):
                continue
            if not self.matches_keywords(f"{title} {full_text}"):
                continue
            leads.append(self._build_lead(title, full_text, url, slug))
        return leads

    # ------------------------------------------------------------------
    # Lead construction
    # ------------------------------------------------------------------

    def _build_lead(
        self, title: str, description: str, url: str, slug: str
    ) -> RawLead:
        combined = f"{title} {description}"
        return RawLead(
            company_domain=f"{slug}.com",
            source="job_scanner",
            pain_snippet=f"{title} — {description[:200]}",
            evidence_url=url,
            pain_type=self.classify_pain(combined),
            team_size_signal="",
            stack_signals=self._extract_stack_signals(description),
            dm_hints=[],
            raw_score=self._score(combined),
        )

    def _score(self, text: str) -> int:
        lowered = text.lower()
        score = 3
        if "runbook" in lowered:
            score += 1
        if "internal tooling" in lowered:
            score += 1
        return score

    def _extract_stack_signals(self, text: str) -> list[str]:
        lowered = text.lower()
        found: set[str] = set()
        for token, canonical in _STACK_TOKENS:
            if token in lowered:
                found.add(canonical)
        return sorted(found)
