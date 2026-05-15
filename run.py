from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
from pathlib import Path
from typing import Any

from agents.base import RawLead
from agents.community_scanner import CommunityScanner
from agents.github_hunter import GitHubHunter
from agents.job_scanner import JobScanner
from core.coordinator import Coordinator, ScoredLead
from core.enricher import Enricher

_KNOWLEDGE_PATH = Path(__file__).parent / "knowledge" / "atuin.json"
_LEADS_PATH = Path(__file__).parent / "leads.json"

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Agent runner — wraps run() with start/completion logging
# ------------------------------------------------------------------

async def _run_agent(agent: Any, name: str) -> list[RawLead]:
    logger.info("Starting %s", name)
    try:
        leads = await agent.run()
    except Exception as exc:
        logger.error("%s raised an unexpected error: %s", name, exc)
        return []
    logger.info("%s complete — %d leads", name, len(leads))
    return leads


# ------------------------------------------------------------------
# Dry-run writer — scored leads without Apollo enrichment
# ------------------------------------------------------------------

def _write_dry_run(scored: list[ScoredLead]) -> None:
    payload = [dataclasses.asdict(l) for l in scored]
    _LEADS_PATH.write_text(json.dumps(payload, indent=2, default=str))


# ------------------------------------------------------------------
# Main pipeline
# ------------------------------------------------------------------

async def main(dry_run: bool = False) -> None:
    # 1. Knowledge store
    knowledge = json.loads(_KNOWLEDGE_PATH.read_text())
    n_pains = len(knowledge["product"]["pain_to_feature_map"])
    n_signals = len(knowledge["icp"]["stack_signals"])
    logger.info("Knowledge store loaded: %d pain types, %d stack signals", n_pains, n_signals)

    # 2. Agents — concurrent
    results = await asyncio.gather(
        _run_agent(GitHubHunter(), "GitHubHunter"),
        _run_agent(CommunityScanner(), "CommunityScanner"),
        _run_agent(JobScanner(), "JobScanner"),
    )

    # 3. Flatten
    raw_leads: list[RawLead] = [lead for bucket in results for lead in bucket]
    logger.info("Raw leads collected: %d", len(raw_leads))

    # 4. Coordinator
    coordinator = Coordinator()
    scored = coordinator.run(raw_leads)
    tier_a = sum(1 for l in scored if l.tier == "A")
    tier_b = sum(1 for l in scored if l.tier == "B")
    n_dropped = len(raw_leads) - len(scored)
    logger.info("After scoring — Tier A: %d, Tier B: %d, Dropped: %d", tier_a, tier_b, n_dropped)

    if dry_run:
        _write_dry_run(scored)
        logger.info(
            "leads.json written (dry-run, no enrichment) — launch Streamlit with: "
            "streamlit run ui/review.py"
        )
        return

    # 5. Enricher — Tier A and B only (coordinator already dropped <15)
    enricher = Enricher()
    enriched = await enricher.run(scored)
    n_review = sum(1 for l in enriched if l.needs_manual_review)
    logger.info("Enrichment complete — %d needs manual review", n_review)

    # 6. leads.json written by enricher.run(); confirm and log
    logger.info(
        "leads.json written — launch Streamlit with: streamlit run ui/review.py"
    )


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Atuin GTM lead pipeline")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Apollo enrichment; write scored leads to leads.json to test sourcing only.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(main(dry_run=args.dry_run))
