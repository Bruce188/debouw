"""
Streamlit UI components for the debouw risk dashboard.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from debouw.ui.theme import RISK_CATEGORY_LABELS_NL, color_for_score


# NL labels for the 6 RvVb outcome enum values. Used to render precedent blocks.
_OUTCOME_LABELS_NL: dict[str, str] = {
    "vernietigd": "Vernietigd",
    "gedeeltelijk": "Gedeeltelijk vernietigd",
    "verworpen": "Verworpen",
    "onontvankelijk": "Onontvankelijk",
    "afstand": "Afstand van geding",
    "andere": "Andere",
}


def _render_precedent_block(top_risks: list[dict]) -> None:
    """Render an "Vergelijkbare RvVb-arresten" block per top risk.

    Sections with no precedents are skipped silently.
    """
    has_any = any((rf.get("precedents") or []) for rf in top_risks)
    if not has_any:
        return

    with st.expander("Vergelijkbare RvVb-arresten"):
        for rf in top_risks:
            precedents = rf.get("precedents") or []
            if not precedents:
                continue
            cat_label = rf.get("category", "")
            try:
                from debouw.models.permit import RiskCategory
                cat = RiskCategory(cat_label)
                cat_label = RISK_CATEGORY_LABELS_NL.get(cat, cat_label)
            except (ValueError, KeyError):
                pass
            st.markdown(f"**{cat_label}**")
            rows = []
            for p in precedents:
                arrest_id = p.get("precedent_id", "")
                outcome = p.get("outcome", "")
                outcome_label = _OUTCOME_LABELS_NL.get(outcome, outcome)
                similarity = p.get("similarity", 0.0)
                summary = p.get("summary", "")
                rows.append(
                    {
                        "Arrest": (
                            f"[{arrest_id}](https://www.dbrc.be/rechtspraak/arrest/{arrest_id})"
                            if arrest_id
                            else ""
                        ),
                        "Uitkomst": outcome_label,
                        "Gelijkenis": (
                            f"{similarity:.2f}"
                            if isinstance(similarity, (int, float))
                            else ""
                        ),
                        "Citaat": summary[:200],
                    }
                )
            st.dataframe(pd.DataFrame(rows), use_container_width=True)


def render_risk_table(
    projects: list[dict], assessments: dict[str, dict]
) -> str | None:
    """Render a selectable risk table; return selected external_id or None."""
    if not projects:
        st.info("Geen dossiers gevonden.")
        return None

    rows = []
    for p in projects:
        a = assessments.get(p["external_id"], {})
        rows.append(
            {
                "external_id": p["external_id"],
                "Referentie": p.get("omv_reference", ""),
                "Titel": p.get("title", "")[:60],
                "Status": p.get("status", ""),
                "Score": a.get("overall_score", None),
                "Vertraging (d)": a.get("expected_delay_days", None),
                "Betrouwbaarheid": a.get("confidence", None),
            }
        )

    df = pd.DataFrame(rows)

    def _color_score(val: Any) -> str:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return ""
        return f"background-color: {color_for_score(float(val))}"

    styled = df.drop(columns=["external_id"]).style.map(
        _color_score, subset=["Score"]
    )

    event = st.dataframe(
        styled,
        on_select="rerun",
        selection_mode="single-row",
        use_container_width=True,
    )

    selected_rows = event.selection.get("rows", []) if event else []
    if selected_rows:
        idx = selected_rows[0]
        return rows[idx]["external_id"]
    return None


def render_map(point: dict | None, overlay_flags: dict) -> None:
    """Render a Folium map with overlay annotations."""
    import folium
    from streamlit_folium import st_folium

    if point is None:
        st.warning("Geen geocodering beschikbaar.")
        return

    lat = point.get("lat") or point.get("latitude")
    lon = point.get("lon") or point.get("longitude")
    if lat is None or lon is None:
        st.warning("Geen geocodering beschikbaar.")
        return

    m = folium.Map(
        location=[lat, lon],
        zoom_start=14,
        tiles="OpenStreetMap",
        attr="© OpenStreetMap contributors — debouw-research/0.x",
    )
    folium.Marker(location=[lat, lon]).add_to(m)

    overlay_colors = {
        "in_natura_2000": "#10b981",
        "in_signaalgebied": "#f59e0b",
        "in_protected_heritage": "#ef4444",
    }
    for flag, color in overlay_colors.items():
        if overlay_flags.get(flag):
            folium.Circle(
                location=[lat, lon],
                radius=400,
                color=color,
                fill=True,
                fill_opacity=0.15,
                tooltip=flag,
            ).add_to(m)

    st_folium(m, width=600, height=400)


def render_project_detail(project: dict, assessment: dict | None) -> None:
    """Render a full project detail panel."""
    st.subheader(project.get("title", project.get("external_id", "")))

    col_left, col_right = st.columns(2)

    with col_left:
        st.markdown(f"**Referentie:** {project.get('omv_reference', '')}")
        st.markdown(f"**Bron:** {project.get('source', '')}")
        addr = project.get("address") or {}
        if isinstance(addr, dict):
            raw_addr = addr.get("raw", "")
            muni = addr.get("municipality", "")
        else:
            raw_addr = str(addr)
            muni = ""
        st.markdown(f"**Adres:** {raw_addr}")
        if muni:
            st.markdown(f"**Gemeente:** {muni}")
        st.markdown(f"**Status:** {project.get('status', '')}")
        st.markdown(f"**Eerste aanmelding:** {project.get('first_seen_at', '')}")

    with col_right:
        addr_dict = project.get("address") or {}
        if isinstance(addr_dict, dict):
            point = addr_dict.get("point")
        else:
            point = None
        overlays = project.get("overlays") or {}
        if isinstance(overlays, dict):
            overlay_flags = {
                "in_natura_2000": overlays.get("in_natura_2000", False),
                "in_signaalgebied": overlays.get("in_signaalgebied", False),
                "in_protected_heritage": overlays.get("in_protected_heritage", False),
            }
        else:
            overlay_flags = {}
        render_map(point, overlay_flags)

    st.divider()

    if assessment is None:
        st.info("Risico-engine nog niet uitgevoerd voor dit project.")
        return

    st.markdown(f"**Samenvatting:** {assessment.get('summary', '')}")
    st.markdown(
        f"**Score:** {assessment.get('overall_score', 0):.2f} | "
        f"**Vertraging:** {assessment.get('expected_delay_days', 0):.0f}d | "
        f"**Betrouwbaarheid:** {assessment.get('confidence', 0):.0%}"
    )

    top_risks = assessment.get("top_risks", [])
    if top_risks:
        with st.expander("Top risicofactoren"):
            risk_rows = []
            for rf in top_risks:
                cat_label = rf.get("category", "")
                try:
                    from debouw.models.permit import RiskCategory
                    cat = RiskCategory(cat_label)
                    cat_label = RISK_CATEGORY_LABELS_NL.get(cat, cat_label)
                except (ValueError, KeyError):
                    pass
                risk_rows.append(
                    {
                        "Factor": cat_label,
                        "Ernst": rf.get("severity", 0),
                        "Vertraging (d)": rf.get("expected_delay_days", 0),
                        "Rationale": rf.get("rationale", "")[:80],
                    }
                )
            st.dataframe(pd.DataFrame(risk_rows), use_container_width=True)
        _render_precedent_block(top_risks)
