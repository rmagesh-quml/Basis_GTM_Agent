# Basis GTM Agent

Automated outbound pipeline for Atuin Desktop. Finds engineering teams that
are already feeling the pain Atuin Desktop solves — scattered runbooks, deploy
chaos, slow onboarding — and routes them into Apollo sequences without manual
research.

## What it does

1. **Sourcing** — three agents run concurrently to find signal:
   - `GitHubHunter` — walks stargazers of `atuinsh/atuin`, resolves their org
     memberships, and checks repos for `runbooks/`, `scripts/`, `playbooks/`,
     or `.github/workflows/` directories. Matches open issues against ICP pain
     keywords.
   - `CommunityScanner` — scans r/devops, r/sysadmin, r/platform_engineering,
     and r/commandline for posts and comments that signal the target pains.
   - `JobScanner` — polls Greenhouse and Lever for 30 seed companies, filtering
     for platform/DevOps/SRE roles whose job descriptions mention ICP keywords.

2. **Scoring** — `Coordinator` deduplicates by company domain, then scores each
   lead 1–5 across six ICP metrics (team size fit, pain specificity, stack
   density, DM proximity, CLI signal, urgency) for a max of 30 points. Leads
   scoring ≥22 are Tier A, 15–21 are Tier B, below 15 are dropped.

3. **Enrichment** — `Enricher` calls Apollo's people-match API to resolve a
   decision-maker (DevOps/Platform/SRE lead) for each company domain. Leads
   with no Apollo match are flagged for manual review.

4. **Review** — `ui/review.py` is a Streamlit app that shows one lead at a time
   as a card: ICP score breakdown, pain snippet, DM contact info, evidence link.
   Operators approve, drop, or annotate leads. Tier A leads appear first.

5. **Push** — `Connector` creates or updates an Apollo contact with ICP metadata
   as custom fields, attaches the pain snippet as a note for Apollo AI writer
   context, and enrolls the contact in the appropriate tier sequence.

## How to run

```bash
# Install dependencies
pip install PyGitHub praw httpx streamlit

# Set credentials
export GITHUB_TOKEN=...
export REDDIT_CLIENT_ID=...
export REDDIT_CLIENT_SECRET=...
export REDDIT_USER_AGENT="atuin-gtm/1.0"
export APOLLO_API_KEY=...

# Run the full pipeline
python run.py

# Test sourcing without spending Apollo credits
python run.py --dry-run

# Launch the review UI (after run.py writes leads.json)
streamlit run ui/review.py
```

## Project layout

```
knowledge/atuin.json      # Shared product/ICP knowledge — pain types, stack signals,
                          #   tone rules, competitor positioning. Read-only at runtime.
agents/
  base.py                 # RawLead dataclass + BaseAgent ABC with keyword matching
  github_hunter.py        # GitHub stargazer → org → issue scraper
  community_scanner.py    # Reddit post/comment scanner
  job_scanner.py          # Greenhouse + Lever job board scraper
core/
  coordinator.py          # Dedup, ICP scoring, tiering → ScoredLead
  enricher.py             # Apollo people match → EnrichedLead, writes leads.json
  connector.py            # Pushes approved leads to Apollo contacts + sequences
ui/
  review.py               # Streamlit one-at-a-time lead review UI
run.py                    # Pipeline entrypoint (orchestrates everything above)
```

## Architecture rules

- No LLM calls in agents or coordinator — raw structured data only.
- All LLM calls (if added) belong in `coordinator.py` or `enricher.py`.
- Agents never write to storage — only `connector.py` writes to Apollo,
  only `enricher.py` writes `leads.json`.
- The knowledge store (`knowledge/atuin.json`) is read-only at runtime.