"""
Streamlit entry point for the debouw permit risk dashboard.

Run with: streamlit run debouw/ui/app.py
"""

from __future__ import annotations

import streamlit as st

from debouw.config import Settings
from debouw.storage.read import (
    get_project_with_assessment,
    latest_assessments,
    list_projects,
    make_sync_engine,
)
from debouw.ui.components import (
    clear_risk_table_selection,
    render_header,
    render_portfolio_summary,
    render_project_detail,
    render_risk_table,
)
from debouw.ui.theme import LOGO_PATH, inject_global_css

# ---------------------------------------------------------------------------
# Cached resources
# ---------------------------------------------------------------------------

@st.cache_resource
def _engine(settings_repr: str):
    """Cache the sync SQLAlchemy engine (one per settings identity)."""
    return make_sync_engine(Settings())


@st.cache_data(ttl=60)
def _projects(engine_id: str) -> list[dict]:
    """Cached list of all permit projects (refreshes every 60 s)."""
    settings = Settings()
    engine = _engine(settings_repr=repr(settings))
    return list_projects(engine)


@st.cache_data(ttl=60)
def _assessments(engine_id: str) -> dict[str, dict]:
    """Cached latest assessment per project (one query, unfiltered)."""
    settings = Settings()
    engine = _engine(settings_repr=repr(settings))
    return latest_assessments(engine)


def _close_dossier() -> None:
    """Button callback: clear the active dossier selection and the URL param."""
    clear_risk_table_selection()
    st.session_state.pop("_active_dossier", None)
    if "pid" in st.query_params:
        del st.query_params["pid"]


# ---------------------------------------------------------------------------
# App layout
# ---------------------------------------------------------------------------

def main() -> None:
    settings = Settings()
    st.set_page_config(
        page_title="debouw — Belgische permit risico-monitor",
        page_icon="🏗️",
        layout="wide",
        initial_sidebar_state="auto",
    )
    inject_global_css()
    st.logo(str(LOGO_PATH), size="large")
    render_header(engine_version=settings.engine_version)

    # Load data
    engine_key = str(settings.db_path)
    with st.spinner("Dossiers laden…"):
        all_projects = _projects(engine_id=engine_key)
        assessments = _assessments(engine_id=engine_key)

    last_seen = max(
        (str(p.get("first_seen_at") or "") for p in all_projects), default=""
    )
    st.caption(
        f"Engine {settings.engine_version} · {len(all_projects)} dossiers · "
        f"laatst bijgewerkt: {last_seen[:10] or '—'}"
    )

    # Region label → internal value mapping (Phase 5)
    _REGION_LABEL_MAP = {
        "Vlaanderen": "vl",
        "Brussel": "brussels",
    }

    # Sidebar filters
    with st.sidebar:
        st.markdown("#### Filters")
        projects_for_filter = all_projects
        selected_regio = st.multiselect(
            "Regio",
            options=list(_REGION_LABEL_MAP.keys()),
            default=[],
            help="Laat leeg om alle regio's te tonen",
        )
        # Filter the gemeente options by the selected region(s) — Brussel
        # gemeenten should not appear in the dropdown when only Vlaanderen
        # is selected, otherwise the filter combination produces an empty
        # set and looks broken to the user.
        if selected_regio:
            _selected_region_enums = {
                _REGION_LABEL_MAP[label] for label in selected_regio
            }
            _gemeente_pool = [
                p for p in projects_for_filter
                if p.get("region") in _selected_region_enums
            ]
        else:
            _gemeente_pool = projects_for_filter
        _municipalities_raw = {
            (p.get("address") or {}).get("municipality")
            for p in _gemeente_pool
            if isinstance(p.get("address"), dict)
            and p.get("address", {}).get("municipality")
        }
        _municipalities: list[str] = sorted(
            m for m in _municipalities_raw if isinstance(m, str)
        )
        selected_gemeenten = st.multiselect(
            "Gemeenten", options=_municipalities, default=[],
            help="Laat leeg om alle gemeenten te tonen",
        )
        min_score = st.slider("Minimum risicoscore", 0.0, 1.0, 0.0, 0.05)
        if st.button("Vernieuwen"):
            st.cache_data.clear()
            st.rerun()

    projects = all_projects

    # Apply region filter (AND with gemeente)
    if selected_regio:
        selected_region_enums = {_REGION_LABEL_MAP[label] for label in selected_regio}
        projects = [p for p in projects if p.get("region") in selected_region_enums]
        # Vlaanderen coverage is still ramping up — surface the row count so
        # users do not mistake the sparse list for a filter bug.
        if "vl" in selected_region_enums:
            vl_count = sum(1 for p in projects if p.get("region") == "vl")
            if vl_count < 10:
                st.info(
                    f"ℹ️ Vlaamse dekking wordt uitgebreid — {vl_count} "
                    f"dossier(s) beschikbaar. Brusselse dossiers worden "
                    f"dagelijks ververst."
                )

    # Apply gemeente filter
    if selected_gemeenten:
        selected_lower = {g.lower() for g in selected_gemeenten}

        def _muni(p: dict) -> str:
            addr = p.get("address") or {}
            if isinstance(addr, dict):
                return (addr.get("municipality") or "").lower()
            return ""

        projects = [p for p in projects if _muni(p) in selected_lower]

    if min_score > 0:
        projects = [
            p for p in projects
            if assessments.get(p["external_id"], {}).get("overall_score", 0.0) >= min_score
        ]

    # Permalink support
    query_pid = st.query_params.get("pid", None)

    st.markdown(
        '<div class="db-section-title">Portefeuille</div>',
        unsafe_allow_html=True,
    )
    render_portfolio_summary(projects, assessments)

    filters_active = bool(selected_regio or selected_gemeenten or min_score > 0)
    selected_id = render_risk_table(
        projects, assessments, filters_active=filters_active
    )

    # Scores of the filtered portfolio feed the dossier-page distribution
    # chart so the open dossier is positioned against its peers.
    portfolio_scores = [
        a.get("overall_score")
        for a in assessments.values()
        if isinstance(a.get("overall_score"), (int, float))
    ]

    # A fresh widget selection always wins over the URL param; the param only
    # seeds the detail panel on a clean page load (permalink).
    previous_id = st.session_state.get("_active_dossier")
    if selected_id and selected_id != previous_id:
        display_id = selected_id
    else:
        display_id = query_pid or selected_id

    if display_id:
        st.session_state["_active_dossier"] = display_id
        engine = _engine(settings_repr=repr(settings))
        p, a = get_project_with_assessment(engine, display_id)
        if p:
            st.query_params["pid"] = display_id
            st.button("✕ Sluit dossier", on_click=_close_dossier)
            with st.spinner("Dossier laden…"):
                render_project_detail(p, a, portfolio_scores=portfolio_scores)
        else:
            st.warning(f"Project '{display_id}' niet gevonden.")

    st.caption(
        "Onderzoeksprototype — geen juridisch advies. Zie LIMITATIONS.md."
    )


main()
