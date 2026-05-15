# Project conventions

## Architecture rules
- Every agent inherits BaseAgent from agents/base.py
- Agents never call the LLM directly — they return raw structured data
- All LLM calls happen in coordinator.py and enricher.py only
- Knowledge store is read-only at agents/knowledge/atuin.json
- No agent writes to the database — only connector.py does

## Code style
- Async everywhere — all agents are async functions
- Type hints on every function signature
- Each function does one thing — no functions over 30 lines
- Errors are caught at the coordinator level, not inside agents
- No print statements — use the logger from config.py

## Adding a new agent
1. Create agents/new_agent.py inheriting BaseAgent
2. Implement async def run(self) -> list[RawLead]
3. Register in run.py AGENTS list — nothing else changes

## Demo constraints
- Pipeline must complete in under 3 minutes for demo
- Cap GitHub API calls to 100 per run during demo
- Reddit PRAW calls limited to top 25 posts per subreddit
- All outputs written to leads.json for Streamlit to read
- Rate limit the usage so that it doesn't spend too many tokens (token-concerned)
