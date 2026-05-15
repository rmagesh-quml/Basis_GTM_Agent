from __future__ import annotations

import asyncio
import os
import re
from typing import TYPE_CHECKING

import github
from github import GithubException

from agents.base import BaseAgent, RawLead

if TYPE_CHECKING:
    from github.Organization import Organization
    from github.Repository import Repository

_STARGAZER_CAP = 200
_ORGS_PER_USER = 5
_REPOS_PER_ORG = 10
_ISSUES_PER_REPO = 20

_SIGNAL_DIRS = ["runbooks", "scripts", "playbooks", ".github/workflows"]

_STACK_TOKENS = {
    "kubernetes", "k8s", "terraform", "pulumi", "postgres", "postgresql",
    "mysql", "aws", "gcp", "azure", "docker", "bash", "ansible", "helm",
    "prometheus", "grafana", "github-actions", "circleci", "ecs", "python",
}

_DOMAIN_RE = re.compile(r"(?:https?://)?(?:www\.)?([^/\s]+)")


class GitHubHunter(BaseAgent):
    def __init__(self) -> None:
        super().__init__()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> list[RawLead]:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            self.logger.error("GITHUB_TOKEN not set — aborting")
            return []

        return await asyncio.to_thread(self._run_sync, token)

    # ------------------------------------------------------------------
    # Sync core (runs in a thread so the event loop stays free)
    # ------------------------------------------------------------------

    def _run_sync(self, token: str) -> list[RawLead]:
        g = github.Github(token, per_page=100)
        leads: list[RawLead] = []

        try:
            atuin_repo = g.get_repo("atuinsh/atuin")
        except GithubException as exc:
            self.logger.error("Cannot fetch atuinsh/atuin: %s", exc)
            return []

        stargazers = self._collect_stargazers(atuin_repo)
        self.logger.info("Collected %d stargazers", len(stargazers))

        seen_orgs: set[str] = set()

        for user in stargazers:
            try:
                orgs = list(user.get_orgs())[:_ORGS_PER_USER]
            except GithubException as exc:
                self.logger.debug("Cannot fetch orgs for %s: %s", user.login, exc)
                continue

            for org in orgs:
                if org.login in seen_orgs:
                    continue
                seen_orgs.add(org.login)

                try:
                    org_leads = self._process_org(org, user.login)
                except GithubException as exc:
                    self.logger.debug("Skipping org %s: %s", org.login, exc)
                    continue

                leads.extend(org_leads)

        self.logger.info("GitHubHunter produced %d leads", len(leads))
        return leads

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _collect_stargazers(self, repo) -> list:
        out = []
        try:
            for user in repo.get_stargazers():
                out.append(user)
                if len(out) >= _STARGAZER_CAP:
                    break
        except GithubException as exc:
            self.logger.error("Stargazer pagination failed: %s", exc)
        return out

    def _process_org(self, org: Organization, trigger_login: str) -> list[RawLead]:
        # --- filter ---
        if org.login.lower() == trigger_login.lower():
            return []
        if org.public_repos > 200:
            return []
        if (org.public_members_count or 0) < 2:
            return []

        leads: list[RawLead] = []

        try:
            repos = sorted(
                org.get_repos(type="public"),
                key=lambda r: r.updated_at or r.created_at,
                reverse=True,
            )[:_REPOS_PER_ORG]
        except GithubException as exc:
            self.logger.debug("Cannot list repos for %s: %s", org.login, exc)
            return []

        for repo in repos:
            try:
                repo_leads = self._process_repo(repo, org)
                leads.extend(repo_leads)
            except GithubException as exc:
                self.logger.debug("Skipping repo %s: %s", repo.full_name, exc)

        return leads

    def _process_repo(self, repo: Repository, org: Organization) -> list[RawLead]:
        found_dirs = self._check_signal_dirs(repo)
        if not found_dirs:
            return []

        has_runbooks = "runbooks" in found_dirs
        base_score = 5 if has_runbooks else 3

        try:
            topics = repo.get_topics()
        except GithubException:
            topics = []

        leads: list[RawLead] = []

        try:
            issues = repo.get_issues(state="open")
            count = 0
            for issue in issues:
                if count >= _ISSUES_PER_REPO:
                    break
                count += 1

                body = issue.body or ""
                combined = f"{issue.title} {body}"

                if not self.matches_keywords(combined):
                    continue

                pain_snippet = f"{issue.title} — {body[:200]}"
                pain_type = self.classify_pain(combined)
                stack_signals = self._extract_stack_signals(topics, combined)

                dm_hints = [issue.user.login]
                if issue.user.name:
                    dm_hints.append(issue.user.name)

                leads.append(
                    RawLead(
                        company_domain=self._resolve_domain(org),
                        source="github_hunter",
                        pain_snippet=pain_snippet,
                        evidence_url=issue.html_url,
                        pain_type=pain_type,
                        team_size_signal=f"{org.public_members_count or 0} public members",
                        stack_signals=stack_signals,
                        dm_hints=dm_hints,
                        raw_score=base_score,
                    )
                )

        except GithubException as exc:
            self.logger.debug("Cannot fetch issues for %s: %s", repo.full_name, exc)

        return leads

    def _check_signal_dirs(self, repo: Repository) -> list[str]:
        found: list[str] = []
        for path in _SIGNAL_DIRS:
            try:
                repo.get_contents(path)
                found.append(path.split("/")[0])
            except GithubException:
                pass
        return found

    def _resolve_domain(self, org: Organization) -> str:
        if org.blog:
            m = _DOMAIN_RE.match(org.blog.strip())
            if m:
                return m.group(1).lower()
        if org.email and "@" in org.email:
            return org.email.split("@")[-1].lower()
        return f"{org.login}.com"

    def _extract_stack_signals(self, topics: list[str], text: str) -> list[str]:
        signals: set[str] = set(topics)
        lowered = text.lower()
        for token in _STACK_TOKENS:
            if token in lowered:
                signals.add(token)
        return sorted(signals)
