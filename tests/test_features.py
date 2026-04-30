"""Tests for risk/features.py — extract() determinism and correctness."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from debouw.models.permit import (
    Address,
    GeoOverlays,
    GeoPoint,
    PermitProject,
    PermitProjectStatus,
)
from debouw.risk.features import FeatureSet, extract

_T = datetime(2026, 1, 1, tzinfo=timezone.utc)
_POINT = GeoPoint(lat=51.0543, lon=3.7174)


def _simple_project(**kwargs) -> PermitProject:
    defaults = dict(
        external_id="test:0001",
        source="gent_consultatie",
        omv_reference="OMV_TEST_0001",
        detail_url="https://gent.consultatieomgeving.net/burger/dossier/OMV_TEST_0001",
        title="Test project",
        description=None,
        applicant_name=None,
        address=Address(
            raw="Teststraat 1, 9000 Gent",
            street="Teststraat",
            house_number="1",
            postcode="9000",
            municipality="Gent",
            point=_POINT,
            parcel_id=None,
        ),
        project_type=None,
        floors=None,
        height_m=None,
        units=None,
        parking_spaces=None,
        trees_to_fell=None,
        mer_status=None,
        iioa_class=None,
        status=PermitProjectStatus.INTAKE,
        decision_date=None,
        decision_outcome=None,
        attachments=[],
        dossier_pdfs=[],
        overlays=None,
        raw_html_path=Path("/tmp/test.html"),
        first_seen_at=_T,
        last_changed_at=_T,
        content_hash="a" * 64,
        decision_regime="post_2026_reform",
    )
    defaults.update(kwargs)
    return PermitProject(**defaults)


def _overlays(**kwargs) -> GeoOverlays:
    return GeoOverlays(**kwargs)


def test_extract_pure_path_deterministic():
    """extract() called twice with same inputs produces identical FeatureSet."""
    p = _simple_project(floors=5, units=20)
    o = _overlays(flood_risk_fluvial="medium", in_signaalgebied=True)
    f1 = extract(p, o)
    f2 = extract(p, o)
    assert f1 == f2


def test_extract_handles_none_overlays():
    """extract() with overlays=None produces sensible defaults."""
    p = _simple_project(floors=3)
    f = extract(p, None)
    assert f.in_natura_2000 is False
    assert f.in_signaalgebied is False
    assert f.flood_risk_fluvial_ord == 0
    assert f.flood_risk_pluvial_ord == 0
    assert f.in_protected_heritage is False
    assert f.rup_zone_present is False
    assert f.distance_to_residential_m is None
    assert f.distance_to_school_m is None


def test_parking_ratio_computed():
    """Parking ratio = parking_spaces / units."""
    p = _simple_project(units=20, parking_spaces=10)
    f = extract(p, None)
    assert f.parking_ratio == pytest.approx(0.5, abs=1e-9)


def test_parking_ratio_none_when_units_zero():
    """Parking ratio is None when units is None."""
    p = _simple_project(units=None, parking_spaces=10)
    f = extract(p, None)
    assert f.parking_ratio is None


def test_flood_ord_mapping():
    """Flood literal 'high' maps to ordinal 3."""
    p = _simple_project()
    o = _overlays(flood_risk_fluvial="high", flood_risk_pluvial="medium")
    f = extract(p, o)
    assert f.flood_risk_fluvial_ord == 3
    assert f.flood_risk_pluvial_ord == 2


def test_flood_ord_none():
    """Flood literal 'none' maps to ordinal 0."""
    p = _simple_project()
    o = _overlays(flood_risk_fluvial="none")
    f = extract(p, o)
    assert f.flood_risk_fluvial_ord == 0


def test_mentions_ongunstig_advies_regex():
    """Regex fires on 'ongunstig advies van ANB'; benign description → False."""
    p_fired = _simple_project(description="ongunstig advies van ANB inzake de zaak")
    f_fired = extract(p_fired, None)
    assert f_fired.mentions_ongunstig_advies is True

    p_clean = _simple_project(description="Normaal project zonder bezwaar")
    f_clean = extract(p_clean, None)
    assert f_clean.mentions_ongunstig_advies is False


def test_mentions_ongunstig_case_insensitive():
    """Regex is case-insensitive."""
    p = _simple_project(description="ONGUNSTIG ADVIES van VMM")
    f = extract(p, None)
    assert f.mentions_ongunstig_advies is True


def test_mentions_vmm_fires():
    """VMM alone triggers binding_advice flag."""
    p = _simple_project(description="Advies van VMM was negatief")
    f = extract(p, None)
    assert f.mentions_ongunstig_advies is True


def test_featureset_is_frozen():
    """FeatureSet instances cannot be mutated."""
    p = _simple_project(floors=3)
    f = extract(p, None)
    with pytest.raises(Exception):
        f.floors = 99  # type: ignore


def test_inv_capped_at_one():
    """Distance inverse is capped: distance 0.5 m → inv = 1.0."""
    p = _simple_project()
    o = _overlays(distance_to_residential_m=0.5)
    f = extract(p, o)
    assert f.distance_to_residential_m_inv == pytest.approx(1.0, abs=1e-9)


def test_parcel_repeat_count_forwarded():
    """parcel_repeat_count kwarg is surfaced in FeatureSet."""
    p = _simple_project()
    f = extract(p, None, parcel_repeat_count=3)
    assert f.parcel_repeat_count == 3


def test_mer_status_none_ord_when_missing():
    """mer_status_none_ord is 1.0 when mer_status is None."""
    p = _simple_project(mer_status=None)
    f = extract(p, None)
    assert f.mer_status_none_ord == pytest.approx(1.0, abs=1e-9)


def test_mer_status_none_ord_when_set():
    """mer_status_none_ord is 0.0 when mer_status is 'mer_plicht'."""
    p = _simple_project(mer_status="mer_plicht")
    f = extract(p, None)
    assert f.mer_status_none_ord == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Phase 6 tests: Brussels score-differentiation
# ---------------------------------------------------------------------------

def _bru_project(**kwargs) -> PermitProject:
    """Helper for a minimal Brussels project."""
    defaults = dict(
        external_id="brussels:01/PU/test",
        source="brussels_openpermits",
        region="brussels",
        omv_reference="01/PU/0000001",
        detail_url="https://openpermits.brussels/fr/_01/PU/0000001",
        title="Test Brussels project",
        description=None,
        applicant_name=None,
        address=Address(
            raw="Rue du Midi 12 1000 Bruxelles",
            street="Rue du Midi 12",
            postcode="1000",
            municipality="Bruxelles",
        ),
        project_type="PU",
        floors=None,
        height_m=None,
        units=None,
        parking_spaces=None,
        trees_to_fell=None,
        mer_status=None,
        iioa_class=None,
        status=PermitProjectStatus.INTAKE,
        decision_date=None,
        decision_outcome=None,
        attachments=[],
        dossier_pdfs=[],
        overlays=None,
        raw_html_path=Path("/tmp/brussels_test.html"),
        first_seen_at=_T,
        last_changed_at=_T,
        content_hash="b" * 64,
        decision_regime="post_2026_reform",
    )
    defaults.update(kwargs)
    return PermitProject(**defaults)


def test_brussels_ongunstig_fr() -> None:
    """FR binding-advice pattern triggers mentions_ongunstig_advies for brussels region."""
    p = _bru_project(description="avis défavorable de Bruxelles Environnement sur ce dossier")
    f = extract(p, None)
    assert f.mentions_ongunstig_advies is True


def test_brussels_ongunstig_nl_br() -> None:
    """NL-Brussels binding-advice pattern triggers mentions_ongunstig_advies."""
    p = _bru_project(description="ongunstig advies van Leefmilieu Brussel werd uitgebracht")
    f = extract(p, None)
    assert f.mentions_ongunstig_advies is True


def test_vl_ongunstig_unchanged() -> None:
    """VL ongunstig advies ANB still triggers for 'vl' region (byte parity)."""
    p = _simple_project(description="ongunstig advies van ANB inzake de omgeving")
    f = extract(p, None)
    assert f.mentions_ongunstig_advies is True


def test_mer_status_from_has_impact_study() -> None:
    """mer_status='mer_plicht' flows correctly through FeatureSet."""
    p = _bru_project(mer_status="mer_plicht")
    f = extract(p, None)
    assert f.mer_status == "mer_plicht"
    assert f.mer_status_none_ord == pytest.approx(0.0, abs=1e-9)


def test_mer_heuristic_fires_on_floor_area_5000(monkeypatch) -> None:
    """MER heuristic infers 'screening' when mer_status=None and floor_area>=5000."""
    monkeypatch.setenv("ENABLE_IIOA_HEURISTIC", "1")
    p = _bru_project(mer_status=None, floor_area_m2=6000.0)
    f = extract(p, None)
    # Heuristic fires → effective_mer_status = "screening" → mer_status_none_ord = 0.0
    assert f.mer_status_none_ord == pytest.approx(0.0, abs=1e-9)
    assert f.mer_status == "screening"


def test_iioa_heuristic_fires_on_floor_area_1500(monkeypatch) -> None:
    """IIOA heuristic infers class 2 for large Brussels projects without explicit class."""
    monkeypatch.setenv("ENABLE_IIOA_HEURISTIC", "1")
    p = _bru_project(iioa_class=None, floor_area_m2=2000.0)
    f = extract(p, None)
    assert f.iioa_class == 2


def test_iioa_heuristic_gated_by_toggle(monkeypatch) -> None:
    """IIOA heuristic does NOT fire when ENABLE_IIOA_HEURISTIC=0."""
    monkeypatch.setenv("ENABLE_IIOA_HEURISTIC", "0")
    p = _bru_project(iioa_class=None, floor_area_m2=2000.0)
    f = extract(p, None)
    assert f.iioa_class is None


def test_featureset_new_fields_default_none() -> None:
    """error_weight and floor_area_m2 default to None in a baseline FeatureSet."""
    from debouw.risk.features import FeatureSet
    fs = FeatureSet()
    assert fs.error_weight is None
    assert fs.floor_area_m2 is None


def test_featureset_new_fields_roundtrip() -> None:
    """FeatureSet(error_weight=1.5, floor_area_m2=4200.0) stores and returns values."""
    from debouw.risk.features import FeatureSet
    fs = FeatureSet(error_weight=1.5, floor_area_m2=4200.0)
    assert fs.error_weight == 1.5
    assert fs.floor_area_m2 == 4200.0


def test_extract_passthrough_new_fields() -> None:
    """extract() passes error_weight and floor_area_m2 from PermitProject to FeatureSet."""
    p = _bru_project(error_weight=12.5, floor_area_m2=4200.0)
    f = extract(p, None)
    assert f.error_weight == 12.5
    assert f.floor_area_m2 == 4200.0


def test_extract_purity() -> None:
    """extract() source must not contain forbidden I/O tokens."""
    import inspect
    from debouw.risk.features import extract as extract_fn
    src = inspect.getsource(extract_fn)
    forbidden = ["requests", "httpx", "select(", "session.execute"]
    for token in forbidden:
        assert token not in src, f"extract() contains forbidden token: {token!r}"
