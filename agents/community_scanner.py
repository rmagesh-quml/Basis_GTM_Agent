from __future__ import annotations

import asyncio
import os
import re

import praw

from agents.base import BaseAgent, RawLead

_SUBREDDITS = ["devops", "sysadmin", "platform_engineering", "commandline"]
_POSTS_PER_BUCKET = 25
_COMMENTS_PER_POST = 10

# Matches bare domains mentioned in post text (e.g. "acme.io", "corp.com")
_DOMAIN_RE = re.compile(r"\b([a-z0-9][a-z0-9-]*\.[a-z]{2,6})\b")

# "at Acme", "at ACME Inc", "at acme.io" — capture the word immediately after "at"
_AT_COMPANY_RE = re.compile(r"\bat\s+([A-Z][a-zA-Z0-9&.]+(?:\s+[A-Z][a-zA-Z0-9&.]+)?)")

# "we use X", "our team uses X", "our stack is X"
_WE_USE_RE = re.compile(
    r"\b(?:we use|we're using|we are using|our (?:team|stack|org) (?:uses?|is|has))\s+([A-Za-z0-9][A-Za-z0-9 .-]{1,30}?)(?=[,.\s]|$)",
    re.IGNORECASE,
)

# "team of 20", "20 engineers", "50-person team", "~30 devs"
_TEAM_SIZE_RE = re.compile(
    r"\b(?:team\s+of\s+|~)?(\d{1,4})\s*[-\s]?"
    r"(?:engineer|developer|dev|person|people|member|employee|eng)s?\b",
    re.IGNORECASE,
)

# Tech tokens drawn from icp.stack_signals vocabulary
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
    ("piper", "piper"),
]


class CommunityScanner(BaseAgent):
    def __init__(self) -> None:
        super().__init__()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> list[RawLead]:
        creds = {
            "client_id": os.environ.get("REDDIT_CLIENT_ID"),
            "client_secret": os.environ.get("REDDIT_CLIENT_SECRET"),
            "user_agent": os.environ.get("REDDIT_USER_AGENT"),
        }
        missing = [k for k, v in creds.items() if not v]
        if missing:
            self.logger.error("Missing Reddit env vars: %s", missing)
            return []

        return await asyncio.to_thread(self._run_sync, **creds)

    # ------------------------------------------------------------------
    # Sync core
    # ------------------------------------------------------------------

    def _run_sync(
        self,
        *,
        client_id: str,
        client_secret: str,
        user_agent: str,
    ) -> list[RawLead]:
        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=user_agent,
            read_only=True,
        )

        # evidence_url → RawLead; first match wins, higher score may upgrade
        seen: dict[str, RawLead] = {}

        for sub_name in _SUBREDDITS:
            try:
                sub = reddit.subreddit(sub_name)
                self._scan_listing(sub.top(time_filter="week", limit=_POSTS_PER_BUCKET), seen)
                self._scan_listing(sub.hot(limit=_POSTS_PER_BUCKET), seen)
            except Exception as exc:
                self.logger.error("Failed scanning r/%s: %s", sub_name, exc)

        leads = list(seen.values())
        self.logger.info("CommunityScanner produced %d leads", len(leads))
        return leads

    # ------------------------------------------------------------------
    # Listing → leads
    # ------------------------------------------------------------------

    def _scan_listing(self, listing, seen: dict[str, RawLead]) -> None:
        for post in listing:
            try:
                self._process_post(post, seen)
            except Exception as exc:
                self.logger.debug("Skipping post %s: %s", getattr(post, "id", "?"), exc)

    def _process_post(self, post, seen: dict[str, RawLead]) -> None:
        post_text = f"{post.title} {post.selftext or ''}"
        evidence_url = f"https://reddit.com{post.permalink}"

        if self.matches_keywords(post_text):
            lead = self._build_lead(post_text, post_text, evidence_url, post)
            self._upsert(seen, lead)

        # Fetch top comments regardless; a keyword-free post may still host a
        # high-signal comment that surfaces a different author's pain.
        try:
            post.comments.replace_more(limit=0)
            for comment in list(post.comments)[:_COMMENTS_PER_POST]:
                body = getattr(comment, "body", "") or ""
                if self.matches_keywords(body):
                    lead = self._build_lead(body, post_text, evidence_url, post)
                    self._upsert(seen, lead)
        except Exception as exc:
            self.logger.debug("Comment fetch failed for %s: %s", evidence_url, exc)

    def _build_lead(
        self,
        match_text: str,
        full_post_text: str,
        evidence_url: str,
        post,
    ) -> RawLead:
        domain = self._extract_company_domain(full_post_text)
        author_name = None
        try:
            if post.author:
                author_name = post.author.name
        except Exception:
            pass

        return RawLead(
            company_domain=domain,
            source="community_scanner",
            pain_snippet=match_text[:300],
            evidence_url=evidence_url,
            pain_type=self.classify_pain(full_post_text),
            team_size_signal=self._extract_team_size(full_post_text),
            stack_signals=self._extract_stack_signals(full_post_text),
            dm_hints=[author_name] if author_name else [],
            raw_score=4 if domain else 2,
        )

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def _upsert(self, seen: dict[str, RawLead], lead: RawLead) -> None:
        existing = seen.get(lead.evidence_url)
        if existing is None or lead.raw_score > existing.raw_score:
            seen[lead.evidence_url] = lead

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_company_domain(self, text: str) -> str:
        # 1. Explicit domain token in text (most reliable)
        for m in _DOMAIN_RE.finditer(text.lower()):
            candidate = m.group(1)
            # Skip common false positives
            if candidate not in {
                "com", "org", "io", "dev", "net", "co.uk",
                "e.g", "i.e", "etc", "vs", "re.g",
            } and "." in candidate:
                return candidate

        # 2. "at CompanyName" → slugified + .com
        m = _AT_COMPANY_RE.search(text)
        if m:
            name = m.group(1).strip().split()[0]  # first word only
            slug = re.sub(r"[^a-z0-9]", "", name.lower())
            if slug:
                return f"{slug}.com"

        # 3. "we use / our team uses X" → first word as slug
        m = _WE_USE_RE.search(text)
        if m:
            name = m.group(1).strip().split()[0]
            slug = re.sub(r"[^a-z0-9]", "", name.lower())
            if len(slug) > 2:
                return f"{slug}.com"

        return ""

    def _extract_team_size(self, text: str) -> str:
        m = _TEAM_SIZE_RE.search(text)
        if m:
            return f"{m.group(1)} engineers"
        return ""

    def _extract_stack_signals(self, text: str) -> list[str]:
        lowered = text.lower()
        found: set[str] = set()
        for token, canonical in _STACK_TOKENS:
            if token in lowered:
                found.add(canonical)
        return sorted(found)
