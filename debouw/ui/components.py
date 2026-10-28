"""
Streamlit UI components for the debouw risk dashboard.

Renders the investor-facing surfaces:

- ``render_header``: compact branded header bar (logo + subtitle + badges)
  that replaces the oversized default ``st.title``.
- ``render_portfolio_summary``: KPI cards above the table (dossier count,
  elevated-risk count, mean score, mean delay).
- ``render_risk_table``: sortable, color-banded score table that drives row
  selection.
- ``render_project_detail``: dossier deep-dive — metric cards, narrative,
  charts (category breakdown + portfolio score distribution), the full
  risk-factor list (first ``PRIMARY_RISK_COUNT`` as rich cards, remainder
  behind a "Toon alle N risicofactoren" expander), and precedent cards.
- ``render_map``: Folium map with overlay annotations.

JSON columns are hydrated upstream in ``debouw.storage.read``; this module
trusts ``project['address']`` to be a ``dict`` and ``assessment['top_risks']``
to be a ``list[dict]``. Defensive ``isinstance`` guards remain because the
upstream hydrator is best-effort (returns the raw string for malformed JSON).

Engine compatibility: engine ``0.7.0`` persists ALL triggered risk factors in
``top_risks`` (and adds a Dutch ``label`` per factor); older ``0.3.0`` rows
carry at most five factors and no label. Both shapes render through the same
card path.

All dynamic text routed into ``unsafe_allow_html`` blocks is escaped with
``html.escape`` — rationales and summaries are LLM/scraper output.
"""

from __future__ import annotations

import html as _html
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from debouw.ui.theme import (
    BORDER,
    PRIMARY,
    RISK_BANDS,
    RISK_CATEGORY_LABELS_NL,
    TEXT_MUTED,
    band_for_score,
    color_for_score,
    logo_data_uri,
)

# Number of risk factors shown as always-visible rich cards; the remainder
# renders inside the "Toon alle N risicofactoren" expander.
PRIMARY_RISK_COUNT = 5


# NL labels for the 6 RvVb outcome enum values. Used to render precedent blocks.
_OUTCOME_LABELS_NL: dict[str, str] = {
    "vernietigd": "Vernietigd",
    "gedeeltelijk": "Gedeeltelijk vernietigd",
    "verworpen": "Verworpen",
    "onontvankelijk": "Onontvankelijk",
    "afstand": "Afstand van geding",
    "andere": "Andere",
}

# Outcome → (fg, bg) chip tints. "Vernietigd" is the bad outcome for a
# builder (permit annulled), so it gets the red tint.
_OUTCOME_CHIP_COLORS: dict[str, tuple[str, str]] = {
    "vernietigd": ("#991b1b", "#fee2e2"),
    "gedeeltelijk": ("#92400e", "#fef3c7"),
    "verworpen": ("#065f46", "#d1fae5"),
    "onontvankelijk": ("#475569", "#f1f5f9"),
    "afstand": ("#475569", "#f1f5f9"),
    "andere": ("#475569", "#f1f5f9"),
}

_STATUS_LABELS_NL: dict[str, str] = {
    "intake": "Intake",
    "in_public_inquiry": "Openbaar onderzoek",
    "decided": "Beslissing genomen",
    "rejected": "Geweigerd",
    "withdrawn": "Ingetrokken",
}

# Status → (fg, bg) chip tints for the dossier header.
_STATUS_CHIP_COLORS: dict[str, tuple[str, str]] = {
    "intake": ("#475569", "#f1f5f9"),
    "in_public_inquiry": ("#92400e", "#fef3c7"),
    "decided": ("#065f46", "#d1fae5"),
    "rejected": ("#991b1b", "#fee2e2"),
    "withdrawn": ("#475569", "#f1f5f9"),
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


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


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


def _category_label(rf: dict) -> str:
    """Dutch display title for a risk factor.

    Engine 0.7.0 ships a per-factor ``label`` (already Dutch); older rows
    fall back to the NL category label, then to the raw category string.
    """
    label = (rf.get("label") or "").strip()
    if label:
        return label
    return _category_label_short(rf)


def _category_label_short(rf: dict) -> str:
    """Short NL category label (chip + chart axis); raw string as fallback."""
    cat_raw = str(rf.get("category", "") or "")
    try:
        from debouw.models.permit import RiskCategory

        return RISK_CATEGORY_LABELS_NL.get(RiskCategory(cat_raw), cat_raw)
    except (ValueError, KeyError):
        return cat_raw


def _rank_risks(top_risks: list[dict]) -> list[dict]:
    """Return risks sorted by expected impact (probability x severity) DESC.

    Stable sort: engine order is preserved within equal-impact groups, so
    pre-ranked 0.3.0 rows keep their original ordering.
    """

    def _impact(rf: dict) -> float:
        try:
            prob = float(rf.get("probability") or 0.0)
            sev = float(rf.get("severity") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return prob * sev

    return sorted(top_risks, key=_impact, reverse=True)


def _split_risks(
    top_risks: list[dict], primary_count: int = PRIMARY_RISK_COUNT
) -> tuple[list[dict], list[dict]]:
    """Split ranked risks into (always-visible, behind-expander) groups."""
    ranked = _rank_risks(top_risks)
    return ranked[:primary_count], ranked[primary_count:]


# Distinctive (non-overlapping) stopwords for the FR/NL summary-language
# heuristic. Ambiguous tokens shared by both languages ("de", "en", "in",
# "is") are deliberately excluded.
_FR_STOPWORDS = frozenset(
    "le la les des du une et est dans pour avec sur au aux ce cette par "
    "qui que ne pas sont être risque projet présente liée lié".split()
)
_NL_STOPWORDS = frozenset(
    "het een van voor met aan dit deze door wordt worden niet bij ook dat "
    "te zijn naar onder tussen risico dossier".split()
)


def _summary_language(project: dict, summary: str) -> str | None:
    """Best-effort language ("fr" | "nl") of the narration summary.

    The dossier's stored ``case_language`` (Brussels parser) wins when
    present; otherwise a tiny stopword count over the summary text decides.
    Returns None when undeterminable — callers skip the chip then.
    """
    lang = project.get("case_language")
    if lang in ("fr", "nl"):
        return lang
    if not summary:
        return None
    import re

    words = re.findall(r"[a-zà-ÿœ]+", summary.lower())
    fr = sum(1 for w in words if w in _FR_STOPWORDS)
    nl = sum(1 for w in words if w in _NL_STOPWORDS)
    if fr > nl:
        return "fr"
    if nl > fr:
        return "nl"
    return None


def _chip(text: str, fg: str, bg: str) -> str:
    return (
        f'<span class="db-chip" style="color:{fg};background:{bg};">'
        f"{_html.escape(text)}</span>"
    )


def _join_html(*parts: str) -> str:
    """Join HTML fragments, dropping empties, one fragment per line.

    ``st.markdown`` runs CommonMark over the payload: a blank (or
    whitespace-only) line terminates an HTML block, and any following line
    indented by 4+ spaces renders as a literal code block. Emitting only
    non-empty, left-aligned lines keeps the whole card a single HTML block.
    """
    return "\n".join(p for p in parts if p and p.strip())


# ---------------------------------------------------------------------------
# Header bar + portfolio summary
# ---------------------------------------------------------------------------


def render_header(engine_version: str, subtitle: str | None = None) -> None:
    """Compact branded header bar: logo, subtitle, engine + prototype badges."""
    sub = subtitle or "Belgische omgevingsvergunningen — risico-monitor"
    st.markdown(
        _join_html(
            '<div class="db-header">',
            # Visually-hidden brand name: the logo is an <img>, which
            # inner_text()/assistive tech skip — keep "debouw" findable.
            '<span style="position:absolute;left:-10000px">debouw</span>',
            f'<img src="{logo_data_uri()}" alt="debouw"/>',
            "<div>",
            '<div class="db-header-title">Risico-monitor</div>',
            f'<div class="db-header-sub">{_html.escape(sub)}</div>',
            "</div>",
            '<div class="db-header-spacer"></div>',
            f'<span class="db-badge">Engine {_html.escape(str(engine_version))}</span>',
            '<span class="db-badge db-badge-muted">Onderzoeksprototype</span>',
            "</div>",
        ),
        unsafe_allow_html=True,
    )


def _portfolio_stats(projects: list[dict], assessments: dict[str, dict]) -> dict:
    """Aggregate KPI stats over the currently filtered portfolio."""
    scores: list[float] = []
    delays: list[float] = []
    for p in projects:
        a = assessments.get(p.get("external_id", ""), {})
        s = a.get("overall_score")
        if isinstance(s, (int, float)):
            scores.append(float(s))
        d = a.get("expected_delay_days")
        if isinstance(d, (int, float)):
            delays.append(float(d))
    elevated = sum(1 for s in scores if s >= 0.3)
    return {
        "n_projects": len(projects),
        "n_scored": len(scores),
        "n_elevated": elevated,
        "avg_score": (sum(scores) / len(scores)) if scores else None,
        "avg_delay": (sum(delays) / len(delays)) if delays else None,
    }


def render_portfolio_summary(
    projects: list[dict], assessments: dict[str, dict]
) -> None:
    """KPI card strip above the risk table."""
    s = _portfolio_stats(projects, assessments)
    avg_score = s["avg_score"]
    avg_delay = s["avg_delay"]
    avg_score_html = (
        f"{avg_score:.2f}<span class='db-metric-unit'>/ 1.00</span>"
        if avg_score is not None
        else "—"
    )
    avg_delay_html = (
        f"{avg_delay:.0f}<span class='db-metric-unit'>dagen</span>"
        if avg_delay is not None
        else "—"
    )
    elevated_accent = (
        RISK_BANDS["matig"]["color"] if s["n_elevated"] else RISK_BANDS["laag"]["color"]
    )
    st.markdown(
        _join_html(
            '<div class="db-metric-row">',
            f'<div class="db-metric-card" style="--accent:{PRIMARY};">',
            '<div class="db-metric-label">Dossiers in beeld</div>',
            f'<div class="db-metric-value">{s["n_projects"]}</div>',
            f'<div class="db-metric-sub">{s["n_scored"]} met risico-inschatting</div>',
            "</div>",
            f'<div class="db-metric-card" style="--accent:{elevated_accent};">',
            '<div class="db-metric-label">Verhoogd risico</div>',
            f'<div class="db-metric-value">{s["n_elevated"]}</div>',
            '<div class="db-metric-sub">score &#8805; 0.30 (matig of hoog)</div>',
            "</div>",
            f'<div class="db-metric-card" style="--accent:{PRIMARY};">',
            '<div class="db-metric-label">Gemiddelde score</div>',
            f'<div class="db-metric-value">{avg_score_html}</div>',
            '<div class="db-metric-sub">over de gefilterde selectie</div>',
            "</div>",
            f'<div class="db-metric-card" style="--accent:{PRIMARY};">',
            '<div class="db-metric-label">Gem. verwachte vertraging</div>',
            f'<div class="db-metric-value">{avg_delay_html}</div>',
            '<div class="db-metric-sub">t.o.v. standaard doorlooptijd</div>',
            "</div>",
            "</div>",
        ),
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Precedents
# ---------------------------------------------------------------------------


def _render_precedent_block(top_risks: list[dict]) -> None:
    """Render a "Vergelijkbare RvVb-arresten" block per top risk.

    Sections with no precedents are skipped silently.
    """
    has_any = any((rf.get("precedents") or []) for rf in top_risks)
    if not has_any:
        return

    st.markdown(
        '<div class="db-section-title">Vergelijkbare RvVb-arresten</div>',
        unsafe_allow_html=True,
    )
    with st.expander("Toon precedenten per risicofactor", expanded=False):
        for rf in top_risks:
            precedents = rf.get("precedents") or []
            if not precedents:
                continue
            st.markdown(f"**{_category_label(rf)}**")

            # Render each precedent as an HTML card so the citation text
            # wraps naturally and the link stays clickable. Streamlit
            # dataframes truncate long strings inside ProgressColumn-bearing
            # tables, which is why the table layout was abandoned here.
            for p in precedents:
                arrest_id = str(p.get("precedent_id", "") or "")
                outcome = str(p.get("outcome", "") or "")
                outcome_label = _OUTCOME_LABELS_NL.get(outcome, outcome)
                fg, bg = _OUTCOME_CHIP_COLORS.get(outcome, ("#475569", "#f1f5f9"))
                similarity = p.get("similarity", 0.0)
                similarity = (
                    float(similarity)
                    if isinstance(similarity, (int, float))
                    else 0.0
                )
                similarity = min(max(similarity, 0.0), 1.0)
                summary = (p.get("summary") or "").strip()
                # dbrc.be exposes no per-arrest detail page; the public
                # search filters exactly on the rolnummer when the dots are
                # written as hyphens (RVVB-A-2223-1006) and lists the arrest
                # PDF as its single result.
                if arrest_id:
                    url = (
                        "https://www.dbrc.be/rechtspraak?search_api_fulltext="
                        + arrest_id.replace(".", "-")
                    )
                    link = (
                        f'<a href="{_html.escape(url, quote=True)}" '
                        f'target="_blank" rel="noopener">'
                        f"{_html.escape(arrest_id)}</a>"
                    )
                else:
                    link = "&#8212;"
                summary_html = (
                    f'<div class="db-precedent-summary">{_html.escape(summary)}</div>'
                    if summary
                    else ""
                )
                st.markdown(
                    _join_html(
                        '<div class="db-precedent">',
                        '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">',
                        link,
                        _chip(outcome_label, fg, bg),
                        f'<span style="font-size:0.74rem;color:{TEXT_MUTED};">gelijkenis {similarity:.2f}</span>',
                        "</div>",
                        f'<div class="db-meter" style="--accent:{PRIMARY};"><span style="width:{similarity * 100:.0f}%;"></span></div>',
                        summary_html,
                        "</div>",
                    ),
                    unsafe_allow_html=True,
                )


# ---------------------------------------------------------------------------
# Risk table (portfolio page)
# ---------------------------------------------------------------------------


# Sentinel option that keeps the dossier selectbox on "— kies dossier —" by
# default; module-level so clear_risk_table_selection can reset to it.
_PICKER_SENTINEL = ""


def clear_risk_table_selection() -> None:
    """Reset the dossier selection in both the table and the selectbox.

    The dataframe's row selection lives in keyed widget state; bumping the
    generation counter gives the widget a fresh key on the next run, which
    drops the persisted selection. The selectbox value is set back to the
    sentinel directly. Safe to call from a button callback (runs before any
    widget is instantiated).
    """
    st.session_state["_risk_table_gen"] = (
        st.session_state.get("_risk_table_gen", 0) + 1
    )
    st.session_state["dossier_picker"] = _PICKER_SENTINEL
    st.session_state.pop("_risk_table_row_choice", None)
    st.session_state.pop("_risk_table_sb_choice", None)


def render_risk_table(
    projects: list[dict],
    assessments: dict[str, dict],
    filters_active: bool = False,
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
        if filters_active:
            st.info(
                "Geen dossiers binnen de huidige filters — verlaag de "
                "minimumscore of wis filters."
            )
        else:
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
                "Adres": _street_only(addr) or (p.get("title") or "").split(" — ")[0],
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
    _SENTINEL = _PICKER_SENTINEL

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

    # The generation counter in the key lets clear_risk_table_selection drop
    # the persisted row selection by handing the widget a fresh key.
    _gen = st.session_state.get("_risk_table_gen", 0)
    event = st.dataframe(
        styled,
        on_select="rerun",
        selection_mode="single-row",
        key=f"risk_table_{_gen}",
        width="stretch",
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
    row_choice: str | None = None
    if selected_rows:
        idx = selected_rows[0]
        if 0 <= idx < len(rows):
            row_choice = str(rows[idx]["external_id"])
    sb_choice = chosen if chosen and chosen != _SENTINEL else None

    # Most-recent interaction wins: compare each widget against its value on
    # the previous run, so a fresh pick in one control overrides a stale
    # (persisted) pick in the other.
    prev_row = st.session_state.get("_risk_table_row_choice")
    prev_sb = st.session_state.get("_risk_table_sb_choice")
    st.session_state["_risk_table_row_choice"] = row_choice
    st.session_state["_risk_table_sb_choice"] = sb_choice
    if row_choice and row_choice != prev_row:
        return row_choice
    if sb_choice and sb_choice != prev_sb:
        return sb_choice
    return row_choice or sb_choice


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------


def render_map(
    point: dict | None, overlay_flags: dict, address_line: str = ""
) -> None:
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
        attr="© OpenStreetMap contributors",
    )
    folium.Marker(
        location=[lat, lon],
        popup=address_line or f"{lat:.4f}, {lon:.4f}",
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


# ---------------------------------------------------------------------------
# Metric cards (dossier page)
# ---------------------------------------------------------------------------


def _metric_cards_html(assessment: dict, n_factors: int | None = None) -> str:
    """Build the metric-card strip HTML for one assessment.

    Four clearly separated cards (label / big value / unit / context line)
    replace the run-together ``st.metric`` strip: Risicoscore (band-tinted),
    Verwachte vertraging, Betrouwbaarheid (with meter), Risicofactoren.
    """
    score = float(assessment.get("overall_score") or 0.0)
    delay = float(assessment.get("expected_delay_days") or 0.0)
    conf = float(assessment.get("confidence") or 0.0)
    conf_pct = min(max(conf, 0.0), 1.0) * 100.0

    band = RISK_BANDS[band_for_score(score)]
    cards = [
        _join_html(
            f'<div class="db-metric-card" style="--accent:{band["color"]};">',
            '<div class="db-metric-label">Risicoscore</div>',
            f'<div class="db-metric-value">{score:.2f}<span class="db-metric-unit">/ 1.00</span></div>',
            f'<span class="db-metric-pill" style="color:{band["fg"]};background:{band["bg"]};">{band["label"]}</span>',
            "</div>",
        ),
        _join_html(
            f'<div class="db-metric-card" style="--accent:{PRIMARY};">',
            '<div class="db-metric-label">Verwachte vertraging</div>',
            f'<div class="db-metric-value">{delay:.0f}<span class="db-metric-unit">dagen</span></div>',
            '<div class="db-metric-sub">t.o.v. standaard doorlooptijd</div>',
            "</div>",
        ),
        _join_html(
            f'<div class="db-metric-card" style="--accent:{PRIMARY};">',
            '<div class="db-metric-label">Betrouwbaarheid</div>',
            f'<div class="db-metric-value">{conf_pct:.0f}<span class="db-metric-unit">%</span></div>',
            f'<div class="db-meter" style="--accent:{PRIMARY};"><span style="width:{conf_pct:.0f}%;"></span></div>',
            '<div class="db-metric-sub">modelvertrouwen op deze inschatting</div>',
            "</div>",
        ),
    ]
    if n_factors is not None:
        cards.append(
            _join_html(
                f'<div class="db-metric-card" style="--accent:{PRIMARY};">',
                '<div class="db-metric-label">Risicofactoren</div>',
                f'<div class="db-metric-value">{int(n_factors)}</div>',
                '<div class="db-metric-sub">getriggerde bezwaargronden</div>',
                "</div>",
            )
        )
    return _join_html('<div class="db-metric-row">', *cards, "</div>")


def _render_metrics(assessment: dict, n_factors: int | None = None) -> None:
    """Metric-card strip: Score / Vertraging / Betrouwbaarheid / Factoren.

    Scores in the 0.05–0.10 band correspond to dossiers without any
    project-specific risk triggers; a short caption marks them as a low
    risk profile so the flat score is not mistaken for a bug.
    """
    st.markdown(_metric_cards_html(assessment, n_factors), unsafe_allow_html=True)

    score = float(assessment.get("overall_score") or 0.0)
    # Low-score annotation — no elevated risk factors fired for this dossier.
    if 0.05 <= score <= 0.10:
        st.caption(
            "Laag risicoprofiel — geen verhoogde risicofactoren gedetecteerd "
            "voor dit dossier."
        )


# ---------------------------------------------------------------------------
# Charts (dossier page)
# ---------------------------------------------------------------------------


def _chart_axis_config(chart: alt.Chart | alt.LayerChart):
    """Apply the shared axis/view styling to a chart."""
    return (
        chart.configure_view(strokeWidth=0)
        .configure_axis(
            gridColor="#eef2f6",
            domainColor=BORDER,
            tickColor=BORDER,
            labelColor=TEXT_MUTED,
            titleColor=TEXT_MUTED,
            labelFontSize=11,
            titleFontSize=11,
        )
        .configure_legend(labelColor=TEXT_MUTED, titleColor=TEXT_MUTED)
    )


def _category_breakdown_chart(top_risks: list[dict]):
    """Horizontal bar chart: probability per risk category, tinted by severity.

    Returns an Altair chart, or None when there is nothing to plot.
    """
    rows = []
    for rf in top_risks:
        try:
            prob = float(rf.get("probability") or 0.0)
            sev = float(rf.get("severity") or 0.0)
            delay = float(rf.get("expected_delay_days") or 0.0)
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "Categorie": _category_label_short(rf) or "(onbekend)",
                "Kans (%)": round(prob * 100.0, 1),
                "Ernst (%)": round(sev * 100.0, 1),
                "Vertraging (d)": round(delay),
            }
        )
    if not rows:
        return None

    df = pd.DataFrame(rows)
    chart = (
        alt.Chart(df)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            y=alt.Y(
                "Categorie:N",
                sort="-x",
                title=None,
                axis=alt.Axis(labelLimit=240),
            ),
            x=alt.X(
                "Kans (%):Q",
                title="Kans op bezwaar (%)",
                scale=alt.Scale(domain=[0, 100]),
            ),
            color=alt.Color(
                "Ernst (%):Q",
                title="Ernst (%)",
                scale=alt.Scale(
                    domain=[0, 50, 100],
                    range=["#10b981", "#f59e0b", "#ef4444"],
                ),
            ),
            tooltip=[
                alt.Tooltip("Categorie:N"),
                alt.Tooltip("Kans (%):Q", format=".1f"),
                alt.Tooltip("Ernst (%):Q", format=".1f"),
                alt.Tooltip("Vertraging (d):Q", format=".0f"),
            ],
        )
        .properties(height=max(180, 26 * len(df)))
    )
    return _chart_axis_config(chart)


@st.cache_data(show_spinner=False)
def _score_distribution_df(scores: tuple[float, ...]) -> pd.DataFrame:
    """Cached histogram dataframe for the portfolio distribution chart.

    Keyed on the actual score tuple (hashable, hashed by st.cache_data), so
    a changed portfolio invalidates the entry while repeated dossier opens
    against the same portfolio reuse it.
    """
    df = pd.DataFrame({"score": list(scores)})
    df["band"] = [band_for_score(v) for v in scores]
    return df


def _score_distribution_chart(scores: list[float], current: float | None = None):
    """Histogram of portfolio risk scores with a marker for the open dossier.

    Returns an Altair chart, or None when there are no numeric scores.
    """
    vals = tuple(float(s) for s in scores if isinstance(s, (int, float)))
    if not vals:
        return None

    df = _score_distribution_df(vals)
    # Pinned bin extent + x/y scale domains: Streamlit attaches the Arrow
    # data after the Vega view is constructed, so data-driven scale domains
    # are computed once over an empty dataset and spam the JS console with
    # "Infinite extent for field ..." warnings. The y-max is the largest
    # stacked bin count, computed here over the same 0.05-wide bins.
    max_bin_count = int(
        df["score"].clip(0.0, 1.0).floordiv(0.05).clip(upper=19).value_counts().max()
    )
    bars = (
        alt.Chart(df)
        .mark_bar(opacity=0.9, cornerRadiusTopLeft=2, cornerRadiusTopRight=2)
        .encode(
            x=alt.X(
                "score:Q",
                bin=alt.Bin(extent=[0, 1], maxbins=20),
                scale=alt.Scale(domain=[0, 1]),
                title="Risicoscore",
            ),
            y=alt.Y(
                "count():Q",
                title="Aantal dossiers",
                scale=alt.Scale(domain=[0, max_bin_count]),
            ),
            color=alt.Color(
                "band:N",
                scale=alt.Scale(
                    domain=["laag", "matig", "hoog"],
                    range=["#10b981", "#f59e0b", "#ef4444"],
                ),
                legend=None,
            ),
            tooltip=[alt.Tooltip("count():Q", title="Dossiers")],
        )
    )
    layers: list = [bars]
    if current is not None:
        cur = pd.DataFrame({"score": [float(current)], "lbl": ["Dit dossier"]})
        layers.append(
            alt.Chart(cur)
            .mark_rule(color=PRIMARY, strokeWidth=2, strokeDash=[5, 3])
            .encode(x="score:Q")
        )
        layers.append(
            alt.Chart(cur)
            .mark_text(align="left", dx=6, fontWeight="bold", color=PRIMARY)
            .encode(x="score:Q", y=alt.value(12), text="lbl:N")
        )
    chart = alt.layer(*layers).properties(height=230)
    return _chart_axis_config(chart)


# ---------------------------------------------------------------------------
# Risk factor cards
# ---------------------------------------------------------------------------


def _risk_card_html(rf: dict, rank: int) -> str:
    """Build one risk-factor card: rank, title, chip, stats, rationale, meta."""
    title = _category_label(rf) or "(onbekende categorie)"
    chip_label = _category_label_short(rf)
    try:
        sev = float(rf.get("severity", 0) or 0)
    except (TypeError, ValueError):
        sev = 0.0
    try:
        prob = float(rf.get("probability", 0) or 0)
    except (TypeError, ValueError):
        prob = 0.0
    try:
        delay = float(rf.get("expected_delay_days", 0) or 0)
    except (TypeError, ValueError):
        delay = 0.0
    objector = (rf.get("typical_objector", "") or "—").strip() or "—"
    rationale = (rf.get("rationale", "") or "").strip()
    n_precedents = len(rf.get("precedents") or [])
    evidence = [str(e) for e in (rf.get("evidence") or []) if str(e).strip()]

    band = RISK_BANDS[band_for_score(prob)]
    prob_pct = min(max(prob, 0.0), 1.0) * 100.0

    chip_html = ""
    if chip_label and chip_label != title:
        chip_html = _chip(chip_label, band["fg"], band["bg"])
    precedent_chip = (
        _chip(f"{n_precedents} precedent{'en' if n_precedents != 1 else ''}",
              "#475569", "#f1f5f9")
        if n_precedents
        else ""
    )

    rationale_html = (
        f'<div class="db-risk-rationale">{_html.escape(rationale)}</div>'
        if rationale
        else ""
    )
    meta_parts = [f"Typische bezwaarmaker: {_html.escape(objector)}"]
    if evidence:
        meta_parts.append(
            "Bewijs: " + _html.escape("; ".join(evidence[:3]))
        )
    meta_html = f'<div class="db-risk-meta">{" &nbsp;·&nbsp; ".join(meta_parts)}</div>'

    head_html = _join_html(
        f'<span class="db-risk-rank">#{rank}</span>',
        f'<span class="db-risk-title">{_html.escape(title)}</span>',
        chip_html,
        precedent_chip,
    )
    stats_html = (
        '<div class="db-risk-stats">'
        f'<span class="db-risk-stat">Kans<b>{prob:.0%}</b></span>'
        f'<span class="db-risk-stat">Ernst<b>{sev:.0%}</b></span>'
        f'<span class="db-risk-stat">Verwachte vertraging<b>~{delay:.0f} d</b></span>'
        "</div>"
    )
    meter_html = (
        f'<div class="db-meter" style="--accent:{band["color"]};">'
        f'<span style="width:{prob_pct:.0f}%;"></span></div>'
    )
    return _join_html(
        f'<div class="db-risk-card" style="--band:{band["color"]};">',
        '<div class="db-risk-head">',
        head_html,
        "</div>",
        stats_html,
        meter_html,
        rationale_html,
        meta_html,
        "</div>",
    )


def render_risk_factors(top_risks: list[dict]) -> None:
    """Render ALL risk factors: top cards inline, the rest in an expander.

    Engine 0.7.0 persists every triggered factor; the first
    ``PRIMARY_RISK_COUNT`` (by probability x severity) render as rich cards,
    the remainder sits behind "Toon alle N risicofactoren" so the page stays
    scannable on 0.7.0 dossiers with 10+ factors.
    """
    if not top_risks:
        st.info("Nog geen risicofactoren beschikbaar voor dit dossier.")
        return

    primary, rest = _split_risks(top_risks)
    for i, rf in enumerate(primary, start=1):
        st.markdown(_risk_card_html(rf, i), unsafe_allow_html=True)

    if rest:
        total = len(top_risks)
        with st.expander(
            f"Toon alle {total} risicofactoren ({len(rest)} extra)",
            expanded=False,
        ):
            for i, rf in enumerate(rest, start=len(primary) + 1):
                st.markdown(_risk_card_html(rf, i), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Dossier detail page
# ---------------------------------------------------------------------------


def _dossier_header_html(project: dict, assessment: dict | None = None) -> str:
    """Title block: dossier title, address line, status/region/date chips.

    ``assessment`` feeds the narration-language chip ("Taal: FR" / "Taal: NL")
    so a French summary on a Brussels FR dossier is announced, not surprising.
    """
    title = project.get("title", project.get("external_id", ""))
    addr_line = _format_address(project.get("address") or {})
    region_raw = str(project.get("region") or "")
    status_raw = str(project.get("status", "") or "")
    reference = str(project.get("omv_reference", "") or "")
    # NULL first_seen_at must render as an em-dash, never the string "None".
    first_seen = str(project.get("first_seen_at") or "")[:10] or "—"

    status_label = _STATUS_LABELS_NL.get(status_raw, status_raw)
    fg, bg = _STATUS_CHIP_COLORS.get(status_raw, ("#475569", "#f1f5f9"))
    chips = [_chip(f"Status: {status_label}", fg, bg)]
    region_label = _REGION_LABELS_NL.get(region_raw, region_raw)
    if region_label:
        chips.append(_chip(f"Regio: {region_label}", "#155e75", "#e8f4f8"))
    if reference:
        chips.append(_chip(f"Ref. {reference}", "#475569", "#f1f5f9"))
    chips.append(_chip(f"Eerste aanmelding: {first_seen}", "#475569", "#f1f5f9"))
    summary = ((assessment or {}).get("summary") or "").strip()
    lang = _summary_language(project, summary)
    if lang:
        chips.append(_chip(f"Taal: {lang.upper()}", "#475569", "#f1f5f9"))

    addr_html = (
        f'<div class="db-dossier-sub">📍 {_html.escape(addr_line)}</div>'
        if addr_line
        else ""
    )
    return _join_html(
        f'<div class="db-dossier-title">{_html.escape(str(title))}</div>',
        addr_html,
        f'<div class="db-chip-row">{"".join(chips)}</div>',
    )


def render_project_detail(
    project: dict,
    assessment: dict | None,
    portfolio_scores: list[float] | None = None,
) -> None:
    """Render a full project detail panel (the dossier deep-dive).

    ``portfolio_scores`` feeds the score-distribution chart so the open
    dossier can be positioned against the filtered portfolio; pass None to
    skip that chart.
    """
    st.markdown(
        '<div class="db-section-title">Dossier-analyse</div>',
        unsafe_allow_html=True,
    )
    st.markdown(_dossier_header_html(project, assessment), unsafe_allow_html=True)

    top_risks = (
        _coerce_top_risks(assessment.get("top_risks")) if assessment else []
    )

    if assessment is not None:
        _render_metrics(assessment, n_factors=len(top_risks))

    # Two-column: left = narrative; right = map
    left, right = st.columns([1, 1])

    with left:
        if assessment is None:
            st.info("Risico-engine nog niet uitgevoerd voor dit project.")
        else:
            summary = (assessment.get("summary") or "").strip()
            if summary:
                summary_html = _html.escape(summary).replace("\n", "<br/>")
                st.markdown(
                    _join_html(
                        '<div class="db-panel">',
                        '<div class="db-panel-title">Samenvatting</div>',
                        f'<div class="db-panel-body">{summary_html}</div>',
                        "</div>",
                    ),
                    unsafe_allow_html=True,
                )

    with right:
        addr = project.get("address") or {}
        if isinstance(addr, dict):
            point = addr.get("point")
        else:
            point = None
        addr_line = _format_address(addr)
        overlays = project.get("overlays") or {}
        if isinstance(overlays, dict):
            overlay_flags = {
                "in_natura_2000": bool(overlays.get("in_natura_2000")),
                "in_signaalgebied": bool(overlays.get("in_signaalgebied")),
                "in_protected_heritage": bool(overlays.get("in_protected_heritage")),
            }
        else:
            overlay_flags = {}
        render_map(point, overlay_flags, addr_line)

    if assessment is None:
        return

    # Charts row: category breakdown + portfolio score distribution
    breakdown = _category_breakdown_chart(top_risks)
    score = assessment.get("overall_score")
    current = float(score) if isinstance(score, (int, float)) else None
    distribution = (
        _score_distribution_chart(portfolio_scores, current=current)
        if portfolio_scores
        else None
    )
    if breakdown is not None or distribution is not None:
        c1, c2 = st.columns([1, 1])
        with c1:
            st.markdown(
                '<div class="db-section-title">Risico per categorie</div>',
                unsafe_allow_html=True,
            )
            if breakdown is not None:
                st.altair_chart(breakdown, use_container_width=True)
            else:
                st.caption("Geen categoriedata beschikbaar.")
        with c2:
            st.markdown(
                '<div class="db-section-title">Positie in portefeuille</div>',
                unsafe_allow_html=True,
            )
            if distribution is not None:
                st.altair_chart(distribution, use_container_width=True)
            else:
                st.caption("Geen portefeuillescores beschikbaar.")

    st.markdown(
        '<div class="db-section-title">Risicofactoren</div>',
        unsafe_allow_html=True,
    )
    render_risk_factors(top_risks)
    if top_risks:
        _render_precedent_block(top_risks)
