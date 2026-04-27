"""
Streamlit entry point for the debouw permit risk dashboard.

Run with: streamlit run debouw/ui/app.py
"""

from __future__ import annotations

import streamlit as st

from debouw.config import Settings
from debouw.storage.read import (
    get_project_with_assessment,
    list_projects,
    make_sync_engine,
)
from debouw.ui.components import render_project_detail, render_risk_table

# ---------------------------------------------------------------------------
# Cached resources
# ---------------------------------------------------------------------------

@st.cache_resource
def _engine(settings_repr: str):
    """Cache the sync SQLAlchemy engine (one per settings identity)."""
    return make_sync_engine(Settings())


@st.cache_data(ttl=60)
def _projects(_engine_id: str) -> list[dict]:
    """Cached list of all permit projects (refreshes every 60 s)."""
    settings = Settings()
    engine = _engine(settings_repr=repr(settings))
    return list_projects(engine)


@st.cache_data(ttl=60)
def _assessments(_engine_id: str, _project_ids: tuple) -> dict[str, dict]:
    """Cached assessment lookup keyed by external_id."""
    settings = Settings()
    engine = _engine(settings_repr=repr(settings))
    result: dict[str, dict] = {}
    for eid in _project_ids:
        _, asm = get_project_with_assessment(engine, eid)
        if asm:
            result[eid] = asm
    return result


# ---------------------------------------------------------------------------
# App layout
# ---------------------------------------------------------------------------

def main() -> None:
    settings = Settings()
    st.title("debouw — Belgische omgevingsvergunningen risico-monitor")
    st.caption(
        f"Engine: {settings.engine_version} | Data: {settings.data_root}"
    )

    # Load data
    engine_key = str(settings.db_path)

    # Region label → internal value mapping (Phase 5)
    _REGION_LABEL_MAP = {
        "Vlaanderen": "vl",
        "Brussel": "brussels",
        "Wallonië": "wl",
    }

    # Sidebar filters
    with st.sidebar:
        # Build gemeente options from in-memory projects (deduped, sorted).
        projects_for_filter = _projects(_engine_id=engine_key)
        _municipalities_raw = {
            (p.get("address") or {}).get("municipality")
            for p in projects_for_filter
            if isinstance(p.get("address"), dict)
            and p.get("address", {}).get("municipality")
        }
        _municipalities: list[str] = sorted(
            m for m in _municipalities_raw if isinstance(m, str)
        )
        selected_regio = st.multiselect(
            "Regio",
            options=list(_REGION_LABEL_MAP.keys()),
            default=[],
            help="Laat leeg om alle regio's te tonen",
        )
        selected_gemeenten = st.multiselect(
            "Gemeenten", options=_municipalities, default=[],
            help="Laat leeg om alle gemeenten te tonen",
        )
        min_score = st.slider("Minimum risicoscore", 0.0, 1.0, 0.0, 0.05)
        if st.button("Refresh"):
            st.cache_data.clear()
            st.rerun()

    projects = _projects(_engine_id=engine_key)

    # Apply region filter (AND with gemeente)
    if selected_regio:
        selected_region_enums = {_REGION_LABEL_MAP[label] for label in selected_regio}
        # Warn if Wallonië selected but no rows expected (not yet ingested)
        if "wl" in selected_region_enums:
            st.warning("Nog geen Waalse dossiers — Fase 6+")
        projects = [p for p in projects if p.get("region") in selected_region_enums]

    # Apply gemeente filter
    if selected_gemeenten:
        selected_lower = {g.lower() for g in selected_gemeenten}

        def _muni(p: dict) -> str:
            addr = p.get("address") or {}
            if isinstance(addr, dict):
                return (addr.get("municipality") or "").lower()
            return ""

        projects = [p for p in projects if _muni(p) in selected_lower]

    project_ids = tuple(p["external_id"] for p in projects)
    assessments = _assessments(_engine_id=engine_key, _project_ids=project_ids)

    if min_score > 0:
        projects = [
            p for p in projects
            if assessments.get(p["external_id"], {}).get("overall_score", 0.0) >= min_score
        ]

    # Permalink support
    query_pid = st.query_params.get("pid", None)

    selected_id = render_risk_table(projects, assessments)

    # Prefer URL param over click-selected row
    display_id = query_pid or selected_id
    if display_id:
        engine = _engine(settings_repr=repr(settings))
        p, a = get_project_with_assessment(engine, display_id)
        if p:
            st.query_params["pid"] = display_id
            render_project_detail(p, a)
        else:
            st.warning(f"Project '{display_id}' niet gevonden.")

    st.caption(
        "Onderzoeksprototype — geen juridisch advies. Zie LIMITATIONS.md."
    )


main()
