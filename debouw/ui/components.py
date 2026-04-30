"""
Streamlit UI components for the debouw risk dashboard.

Renders three surfaces:

- ``render_risk_table``: sortable, color-banded score table that drives row
  selection. Uses ``st.column_config`` so column widths, number formats, and
  the Score progress bar render natively rather than falling back to the
  default 80-px text columns that made the original table unreadable.
- ``render_project_detail``: header + metrics + map + risk-factor table
  + precedent expander. Address is displayed as a single human-readable line
  ("street, postcode municipality") instead of a JSON dump.
- ``render_map``: Folium map with overlay annotations.

JSON columns are hydrated upstream in ``debouw.storage.read``; this module
trusts ``project['address']`` to be a ``dict`` and ``assessment['top_risks']``
to be a ``list[dict]``. Defensive ``isinstance`` guards remain because the
upstream hydrator is best-effort (returns the raw string for malformed JSON).
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

_STATUS_LABELS_NL: dict[str, str] = {
    "intake": "Intake",
    "in_public_inquiry": "Openbaar onderzoek",
    "decided": "Beslissing genomen",
    "rejected": "Geweigerd",
    "withdrawn": "Ingetrokken",
}

# Human-readable explanations rendered in the legend expander above the
# table. Mirrors the PermitProjectStatus enum semantics from
# debouw/models/permit.py.
_STATUS_LEGEND_NL: dict[str, str] = {
    "Intake": (
        "Dossier is ingediend en geregistreerd, maar het openbaar onderzoek "
        "is nog niet gestart."
    ),
    "Openbaar onderzoek": (
        "Het openbaar onderzoek loopt — bezwaarschriften kunnen nog worden "
        "ingediend (typisch 30 dagen)."
    ),
    "Beslissing genomen": (
        "De vergunningverlenende overheid heeft een beslissing genomen "
        "(verleend of geweigerd). Beroep bij de Raad voor "
        "Vergunningsbetwistingen blijft mogelijk."
    ),
    "Geweigerd": "De vergunning is expliciet geweigerd.",
    "Ingetrokken": "De aanvrager heeft de aanvraag ingetrokken.",
}

_REGION_LABELS_NL: dict[str, str] = {
    "vl": "Vlaanderen",
    "brussels": "Brussel",
    "wl": "Wallonië",
}


def _format_address(addr: Any) -> str:
    """Render an Address dict as a one-line human-readable string."""
    if not isinstance(addr, dict):
        return str(addr or "")
    raw = addr.get("raw") or ""
    street = (addr.get("street") or "").rstrip(",").strip()
    postcode = addr.get("postcode")
    municipality = addr.get("municipality")
    if street and postcode and municipality:
        return f"{street}, {postcode} {municipality}"
    return raw


def _street_only(addr: Any) -> str:
    """Return the street + house-number component without postcode/municipality.

    Used in the risk-table ``Adres`` column where Gemeente is its own
    column — repeating the postcode and municipality there is redundant
    and chops off the visible street name in narrow cells.
    """
    if isinstance(addr, dict):
        street = (addr.get("street") or "").rstrip(",").strip()
        if street:
            return street
        raw = addr.get("raw") or ""
        if raw:
            # Strip trailing "<NNNN> <Municipality>" if structured fields
            # are missing — best-effort cleanup so the column doesn't show
            # the same data as Gemeente.
            import re
            m = re.match(r"^(.*?)\s+\d{4}\s+[A-Za-zÀ-ÿ\-' ]+$", raw)
            return (m.group(1) if m else raw).strip()
    return ""


def _coerce_top_risks(value: Any) -> list[dict]:
    """Return ``value`` as a list[dict], else []. Hydrator is best-effort."""
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)]
    return []


def _render_precedent_block(top_risks: list[dict]) -> None:
    """Render an "Vergelijkbare RvVb-arresten" block per top risk.

    Sections with no precedents are skipped silently.
    """
    has_any = any((rf.get("precedents") or []) for rf in top_risks)
    if not has_any:
        return

    with st.expander("Vergelijkbare RvVb-arresten", expanded=False):
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

            # Render each precedent as a markdown card so the citation
            # text wraps naturally and the link is clickable. Streamlit
            # dataframes truncate long strings inside ProgressColumn-bearing
            # tables and clip the markdown link syntax in the Arrest column,
            # which is why we moved away from the table layout for citations.
            for p in precedents:
                arrest_id = p.get("precedent_id", "")
                outcome = p.get("outcome", "")
                outcome_label = _OUTCOME_LABELS_NL.get(outcome, outcome)
                similarity = p.get("similarity", 0.0)
                similarity = (
                    float(similarity)
                    if isinstance(similarity, (int, float))
                    else 0.0
                )
                summary = (p.get("summary") or "").strip()
                arrest_url = (
                    f"https://www.dbrc.be/rechtspraak/arrest/{arrest_id}"
                    if arrest_id
                    else ""
                )
                # Card header line: arrest link · uitkomst · gelijkenis bar
                if arrest_url:
                    header = (
                        f"[`{arrest_id}`]({arrest_url}) — **{outcome_label}** · "
                        f"gelijkenis {similarity:.2f}"
                    )
                else:
                    header = f"`{arrest_id or '—'}` — **{outcome_label}** · gelijkenis {similarity:.2f}"
                st.markdown(header)
                st.progress(min(max(similarity, 0.0), 1.0))
                if summary:
                    st.markdown(f"> {summary}")
                st.markdown("")  # spacing between cards


def render_risk_table(
    projects: list[dict], assessments: dict[str, dict]
) -> str | None:
    """Render a selectable risk table; return selected external_id or None.

    UX choices:
    - Pre-sorted by Score DESC so highest-risk dossiers surface first.
    - ``selection_mode="single-row"`` adds a left-margin checkbox column;
      clicking the checkbox triggers ``on_select="rerun"`` and the parent
      app re-renders with the detail panel populated. Clicking individual
      cells only highlights the cell — Streamlit's native dataframe does
      not support whole-row click-to-select. A complementary
      ``st.selectbox`` is rendered above the table for keyboard-friendly
      drill-in.
    - ``Betrouwbaarheid`` and ``Kans`` are stored 0-1 in the model but
      rendered as integer percentages: we multiply * 100 in the row dict
      and set ``max_value=100`` because Streamlit's ProgressColumn applies
      the format string to the raw value (no auto-percent), so a value of
      0.48 with ``format="%.0f%%"`` would render as "0%" not "48%".
    """
    if not projects:
        st.info("Geen dossiers gevonden.")
        return None

    rows = []
    for p in projects:
        a = assessments.get(p["external_id"], {})
        addr = p.get("address")
        if isinstance(addr, dict):
            municipality = addr.get("municipality") or ""
        else:
            municipality = ""
        status_raw = p.get("status", "")
        score = a.get("overall_score")
        conf = a.get("confidence")
        rows.append(
            {
                "external_id": p["external_id"],
                "Referentie": p.get("omv_reference", ""),
                "Adres": _street_only(addr) or p.get("title", "").split(" — ")[0],
                "Gemeente": municipality,
                "Status": _STATUS_LABELS_NL.get(status_raw, status_raw),
                "Score": score,
                "Vertraging (d)": a.get("expected_delay_days", None),
                "Betrouwbaarheid": (
                    float(conf) * 100.0 if isinstance(conf, (int, float)) else None
                ),
            }
        )

    # Sort highest-risk first; missing scores sink to the bottom.
    rows.sort(key=lambda r: (r["Score"] is None, -(r["Score"] or 0.0)))

    # Selectbox for keyboard-friendly drill-in (mirrors the table's
    # checkbox-column UX which Streamlit does not surface as a click-cell
    # affordance). A sentinel "" entry sits at the top so users see "— kies
    # dossier —" by default; the empty string keeps the options list a
    # homogeneous list[str] for Pyright.
    _SENTINEL = ""

    def _label_for(r: dict) -> str:
        score = r.get("Score")
        prefix = f"[{score:.2f}]" if isinstance(score, (int, float)) else "[—]"
        return f"{prefix} {r['Referentie']} — {r['Adres']}"

    options: list[str] = [_SENTINEL] + [str(r["external_id"]) for r in rows]
    labels: dict[str, str] = {
        str(r["external_id"]): _label_for(r) for r in rows
    }

    def _format(eid: str) -> str:
        if eid == _SENTINEL:
            return "— kies dossier —"
        return labels.get(eid, eid)

    sb_col, _spacer = st.columns([2, 3])
    with sb_col:
        chosen = st.selectbox(
            "🔎 Open dossier",
            options=options,
            format_func=_format,
            key="dossier_picker",
            help="Of klik het selectievakje links van een rij in de tabel hieronder.",
        )

    df = pd.DataFrame(rows)

    def _color_score(val: Any) -> str:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return ""
        return f"background-color: {color_for_score(float(val))}33"  # 20% alpha

    styled = df.drop(columns=["external_id"]).style.map(
        _color_score, subset=["Score"]
    )

    with st.expander("ℹ️ Wat betekenen de statuslabels?", expanded=False):
        for label, blurb in _STATUS_LEGEND_NL.items():
            st.markdown(f"**{label}** — {blurb}")

    st.caption(
        "Klik het selectievakje links van een rij om een dossier te openen — "
        "of gebruik de keuzelijst hierboven. Sorteer door op een kolomkop te klikken."
    )

    event = st.dataframe(
        styled,
        on_select="rerun",
        selection_mode="single-row",
        use_container_width=True,
        hide_index=True,
        height=560,
        column_config={
            "Referentie": st.column_config.TextColumn(width="small"),
            "Adres": st.column_config.TextColumn(width="medium"),
            "Gemeente": st.column_config.TextColumn(width="small"),
            "Status": st.column_config.TextColumn(width="small"),
            "Score": st.column_config.ProgressColumn(
                format="%.2f",
                min_value=0.0,
                max_value=1.0,
                help="Geaggregeerd risico (0=laag, 1=hoog)",
                width="small",
            ),
            "Vertraging (d)": st.column_config.NumberColumn(
                format="%.0f d",
                help="Verwachte vertraging in dagen",
                width="small",
            ),
            "Betrouwbaarheid": st.column_config.ProgressColumn(
                format="%d%%",
                min_value=0.0,
                max_value=100.0,
                help="Modelvertrouwen op deze inschatting (0–100%)",
                width="small",
            ),
        },
    )

    selection = event.get("selection") if event else None  # type: ignore[union-attr]
    selected_rows = selection.get("rows", []) if isinstance(selection, dict) else []
    if selected_rows:
        idx = selected_rows[0]
        return rows[idx]["external_id"]
    if chosen and chosen != _SENTINEL:
        return chosen
    return None


def render_map(point: dict | None, overlay_flags: dict) -> None:
    """Render a Folium map with overlay annotations."""
    import folium
    from streamlit_folium import st_folium

    if point is None:
        st.info("📍 Geen geocodering beschikbaar.")
        return

    lat = point.get("lat") or point.get("latitude")
    lon = point.get("lon") or point.get("longitude")
    if lat is None or lon is None:
        st.info("📍 Geen geocodering beschikbaar.")
        return

    m = folium.Map(
        location=[lat, lon],
        zoom_start=15,
        tiles="OpenStreetMap",
        attr="© OpenStreetMap contributors — debouw-research/0.x",
    )
    folium.Marker(
        location=[lat, lon],
        popup=f"{lat:.4f}, {lon:.4f}",
    ).add_to(m)

    overlay_colors = {
        "in_natura_2000": ("#10b981", "Natura 2000"),
        "in_signaalgebied": ("#f59e0b", "Signaalgebied"),
        "in_protected_heritage": ("#ef4444", "Beschermd erfgoed"),
    }
    for flag, (color, label) in overlay_colors.items():
        if overlay_flags.get(flag):
            folium.Circle(
                location=[lat, lon],
                radius=400,
                color=color,
                fill=True,
                fill_opacity=0.15,
                tooltip=label,
            ).add_to(m)

    st_folium(m, width=None, height=320, returned_objects=[])


def _render_metrics(assessment: dict) -> None:
    """Three-column metric strip: Score / Vertraging / Betrouwbaarheid.

    A score of ~0.08 with delay ~121 d and confidence ~48% indicates the
    engine baseline (5 always-on rule categories — binding_advice_ignored,
    motivation_defect, mer_screening, gro_height, nature_2000_n) without
    any project-specific triggers. Most Brussels intake-only dossiers land
    here because the source page exposes no overlays / decision-regime /
    parsed attachments yet. We surface the explanation inline so users do
    not mistake the flat baseline for a bug.
    """
    score = float(assessment.get("overall_score") or 0.0)
    delay = float(assessment.get("expected_delay_days") or 0.0)
    conf = float(assessment.get("confidence") or 0.0)

    if score < 0.3:
        score_label = "🟢 Laag"
    elif score < 0.6:
        score_label = "🟠 Matig"
    else:
        score_label = "🔴 Hoog"

    c1, c2, c3 = st.columns(3)
    c1.metric("Risicoscore", f"{score:.2f}", score_label)
    c2.metric("Verwachte vertraging", f"{delay:.0f} dagen")
    c3.metric("Betrouwbaarheid", f"{conf:.0%}")

    # Baseline annotation — applies when only the 5 always-on rules fire.
    if 0.05 <= score <= 0.10:
        st.caption(
            "ℹ️ Basislijn-score (~0.08) — alleen de 5 altijd-actieve regels "
            "vuurden. Geen MER-, overlay-, of beslissings-trigger gevonden in "
            "dit dossier. Verwacht profiel voor Brusselse intake-dossiers "
            "voordat het openbaar onderzoek extra signalen toevoegt."
        )


def _render_risk_factors_table(top_risks: list[dict]) -> None:
    """Risk-factor cards — one expander per factor with full rationale.

    Earlier iterations rendered this as a Streamlit dataframe but that
    truncated the rationale and bezwaarmaker columns at ~80 px regardless
    of ``column_config`` width. The card layout gives unbounded width to
    the prose and keeps the at-a-glance metrics (Ernst / Kans / Vertraging)
    on the header row.
    """
    if not top_risks:
        st.info(
            "Geen risicofactoren — engine fallback (intake-fase dossier zonder triggerdata)."
        )
        return

    for rf in top_risks:
        cat_label = rf.get("category", "")
        try:
            from debouw.models.permit import RiskCategory
            cat = RiskCategory(cat_label)
            cat_label = RISK_CATEGORY_LABELS_NL.get(cat, cat_label)
        except (ValueError, KeyError):
            pass
        sev = float(rf.get("severity", 0) or 0)
        prob = float(rf.get("probability", 0) or 0)
        delay = float(rf.get("expected_delay_days", 0) or 0)
        objector = (rf.get("typical_objector", "") or "—").strip() or "—"
        rationale = (rf.get("rationale", "") or "").strip()

        header = (
            f"**{cat_label}** — Ernst {sev:.0%} · Kans {prob:.0%} · "
            f"~{delay:.0f} d vertraging"
        )
        with st.expander(header, expanded=False):
            mc1, mc2, mc3 = st.columns(3)
            mc1.metric("Ernst", f"{sev:.0%}")
            mc2.metric("Kans", f"{prob:.0%}")
            mc3.metric("Vertraging", f"{delay:.0f} d")
            st.markdown(f"**Typische bezwaarmaker:** {objector}")
            if rationale:
                st.markdown(f"**Toelichting:** {rationale}")


def render_project_detail(project: dict, assessment: dict | None) -> None:
    """Render a full project detail panel."""
    title = project.get("title", project.get("external_id", ""))
    st.markdown(f"### {title}")

    addr = project.get("address") or {}
    addr_line = _format_address(addr)
    region_raw = project.get("region") or ""
    status_raw = project.get("status", "")

    # Header strip — three columns: address / status / source
    h1, h2, h3 = st.columns([3, 1, 1])
    with h1:
        st.markdown(f"**📍 {addr_line}**")
        st.caption(
            f"Referentie: `{project.get('omv_reference','')}` · "
            f"Regio: {_REGION_LABELS_NL.get(region_raw, region_raw)}"
        )
    with h2:
        st.markdown(f"**Status**\n\n{_STATUS_LABELS_NL.get(status_raw, status_raw)}")
    with h3:
        first_seen = str(project.get("first_seen_at", ""))[:10]
        st.markdown(f"**Eerste aanmelding**\n\n{first_seen}")

    st.divider()

    # Two-column: left = metrics + summary; right = map
    left, right = st.columns([1, 1])

    with left:
        if assessment is None:
            st.info("Risico-engine nog niet uitgevoerd voor dit project.")
        else:
            _render_metrics(assessment)
            summary = assessment.get("summary") or ""
            if summary:
                st.markdown("**Samenvatting**")
                st.write(summary)

    with right:
        if isinstance(addr, dict):
            point = addr.get("point")
        else:
            point = None
        overlays = project.get("overlays") or {}
        if isinstance(overlays, dict):
            overlay_flags = {
                "in_natura_2000": bool(overlays.get("in_natura_2000")),
                "in_signaalgebied": bool(overlays.get("in_signaalgebied")),
                "in_protected_heritage": bool(overlays.get("in_protected_heritage")),
            }
        else:
            overlay_flags = {}
        render_map(point, overlay_flags)

    if assessment is None:
        return

    st.divider()
    top_risks = _coerce_top_risks(assessment.get("top_risks"))
    st.markdown("**Top risicofactoren**")
    _render_risk_factors_table(top_risks)
    if top_risks:
        _render_precedent_block(top_risks)
