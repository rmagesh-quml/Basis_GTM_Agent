from __future__ import annotations

import json
import subprocess
from pathlib import Path

import streamlit as st

_LEADS_PATH = Path(__file__).parent.parent / "leads.json"
_CONNECTOR_PATH = Path(__file__).parent.parent / "connector.py"


# ------------------------------------------------------------------
# I/O helpers
# ------------------------------------------------------------------

def _load() -> list[dict]:
    if not _LEADS_PATH.exists():
        return []
    try:
        return json.loads(_LEADS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _save(leads: list[dict]) -> None:
    _LEADS_PATH.write_text(json.dumps(leads, indent=2, default=str))


def _update(leads: list[dict], url: str, **fields) -> None:
    for lead in leads:
        if lead.get("evidence_url") == url:
            lead.update(fields)
            break
    _save(leads)


# ------------------------------------------------------------------
# Rendering helpers
# ------------------------------------------------------------------

def _tier_badge(tier: str) -> str:
    colour = "#22c55e" if tier == "A" else "#3b82f6"
    return (
        f'<span style="background:{colour};color:white;padding:2px 10px;'
        f'border-radius:4px;font-weight:bold;font-size:0.85rem">Tier {tier}</span>'
    )


def _score_table(breakdown: dict) -> None:
    if not breakdown:
        st.caption("No breakdown available.")
        return
    rows = {"Metric": list(breakdown.keys()), "Score": list(breakdown.values())}
    st.table(rows)


def _dm_line(lead: dict) -> str:
    parts: list[str] = []
    name = " ".join(filter(None, [lead.get("dm_first"), lead.get("dm_last")])).strip()
    if name:
        parts.append(f"**{name}**")
    if lead.get("dm_title"):
        parts.append(lead["dm_title"])
    if lead.get("dm_email"):
        parts.append(f"`{lead['dm_email']}`")
    if lead.get("dm_linkedin"):
        parts.append(f"[LinkedIn]({lead['dm_linkedin']})")
    return " · ".join(parts) if parts else "_No DM resolved_"


# ------------------------------------------------------------------
# Card
# ------------------------------------------------------------------

def _render_card(lead: dict, all_leads: list[dict]) -> None:
    url = lead.get("evidence_url", "")

    # Header
    tier = lead.get("tier", "?")
    score = lead.get("icp_score", 0)
    badge = _tier_badge(tier)
    st.markdown(
        f"## {lead.get('company_domain', '—')}&nbsp;&nbsp;{badge}&nbsp;&nbsp;"
        f"<span style='color:#6b7280'>score {score}/30</span>",
        unsafe_allow_html=True,
    )
    st.divider()

    # Manual review warning
    if lead.get("needs_manual_review"):
        st.warning("⚠ Apollo returned no match — manual review needed before outreach.")

    # Score breakdown + pain snippet side by side
    col_left, col_right = st.columns([1, 2])

    with col_left:
        st.subheader("ICP score breakdown")
        _score_table(lead.get("score_breakdown", {}))

    with col_right:
        st.subheader("Pain signal")
        snippet = lead.get("pain_snippet", "")
        st.markdown(
            f'<blockquote style="border-left:4px solid #f59e0b;padding:8px 16px;'
            f'background:#fffbeb;border-radius:0 4px 4px 0;color:#374151">'
            f"{snippet}</blockquote>",
            unsafe_allow_html=True,
        )
        feature = lead.get("feature_match", "")
        if feature:
            st.caption(f"↳ This maps to: **{feature}**")

    st.divider()

    # Evidence + DM
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**Evidence**")
        if url:
            st.markdown(f"[{url[:80]}{'…' if len(url) > 80 else ''}]({url})")
        else:
            st.caption("No URL")
        st.markdown(f"Source: `{lead.get('source','')}`")
        stack = lead.get("stack_signals") or []
        if stack:
            st.markdown("Stack: " + " · ".join(f"`{s}`" for s in stack))

    with col_b:
        st.markdown("**Decision-maker**")
        st.markdown(_dm_line(lead))

    st.divider()

    # Action row
    btn_col1, btn_col2, btn_col3 = st.columns([1, 1, 3])
    with btn_col1:
        if st.button("✓ Approve", type="primary", use_container_width=True):
            _update(all_leads, url, status="approved")
            st.rerun()
    with btn_col2:
        if st.button("✗ Drop", use_container_width=True):
            _update(all_leads, url, status="dropped")
            st.rerun()

    # Note editor
    with st.expander("Edit note"):
        note_val = lead.get("custom_note", "")
        note = st.text_area("Note", value=note_val, key=f"note_{url}", label_visibility="collapsed")
        if st.button("Save note", key=f"save_{url}"):
            _update(all_leads, url, custom_note=note)
            st.success("Note saved.")


# ------------------------------------------------------------------
# Sidebar
# ------------------------------------------------------------------

def _render_sidebar(all_leads: list[dict], filtered: list[dict]) -> str:
    with st.sidebar:
        st.title("Lead Review")
        st.divider()

        total = len(all_leads)
        approved = sum(1 for l in all_leads if l.get("status") == "approved")
        dropped = sum(1 for l in all_leads if l.get("status") == "dropped")
        remaining = sum(
            1 for l in filtered if l.get("status") not in ("approved", "dropped")
        )

        st.metric("Total loaded", total)
        col1, col2 = st.columns(2)
        col1.metric("Approved", approved)
        col2.metric("Dropped", dropped)
        st.metric("Remaining (filtered)", remaining)

        st.divider()
        st.markdown("**Filter**")
        mode = st.radio(
            "Show",
            options=["all", "tier_a", "tier_b"],
            format_func=lambda m: {"all": "All", "tier_a": "Tier A only", "tier_b": "Tier B only"}[m],
            key="filter_mode",
            label_visibility="collapsed",
        )

        st.divider()
        if st.button("Push approved to Apollo", use_container_width=True):
            _push_to_apollo()

    return mode


def _push_to_apollo() -> None:
    if not _CONNECTOR_PATH.exists():
        st.sidebar.error("connector.py not found in project root.")
        return
    result = subprocess.run(
        ["python", str(_CONNECTOR_PATH)],
        capture_output=True,
        text=True,
        cwd=str(_CONNECTOR_PATH.parent),
    )
    if result.returncode == 0:
        st.sidebar.success("Pushed to Apollo.")
    else:
        st.sidebar.error(f"connector.py failed:\n{result.stderr[:300]}")


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Atuin GTM · Lead Review", layout="wide")

    all_leads = _load()
    if not all_leads:
        st.info("No leads found. Run the pipeline first to populate leads.json.")
        return

    mode = _render_sidebar(all_leads, all_leads)

    # Apply tier filter
    if mode == "tier_a":
        filtered = [l for l in all_leads if l.get("tier") == "A"]
    elif mode == "tier_b":
        filtered = [l for l in all_leads if l.get("tier") == "B"]
    else:
        filtered = all_leads

    remaining = [l for l in filtered if l.get("status") not in ("approved", "dropped")]

    if not remaining:
        tier_label = {"tier_a": "Tier A", "tier_b": "Tier B"}.get(mode, "")
        label = f" in {tier_label}" if tier_label else ""
        st.success(f"All leads{label} have been reviewed.")
        return

    _render_card(remaining[0], all_leads)


if __name__ == "__main__":
    main()
