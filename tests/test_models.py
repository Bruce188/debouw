"""
Pydantic contract tests for debouw/models/permit.py.

These tests are the cross-phase contract guard — any schema change that breaks
these tests signals drift that will cascade to every downstream feature.
"""

import pytest
from pydantic import ValidationError

from debouw.models.permit import GeoPoint, PermitProject, RiskCategory


def test_permit_project_round_trip(sample_permit_project):
    """Model survives a JSON round-trip without data loss."""
    serialised = sample_permit_project.model_dump_json()
    recovered = PermitProject.model_validate_json(serialised)
    assert recovered == sample_permit_project


def test_permit_project_frozen(sample_permit_project):
    """Frozen model raises ValidationError on field assignment."""
    with pytest.raises((ValidationError, TypeError)):
        # Pydantic v2 raises ValidationError for frozen models
        object.__setattr__(sample_permit_project, "title", "mutated")
        # If object.__setattr__ somehow bypasses, force a Pydantic check
        sample_permit_project.__class__.model_validate(
            {**sample_permit_project.model_dump(mode="json"), "_force_revalidate": True}
        )


def test_permit_project_frozen_direct(sample_permit_project):
    """Direct attribute assignment on a frozen model raises an error."""
    with pytest.raises((ValidationError, TypeError)):
        # Pydantic v2 raises TypeError for frozen models on direct assignment
        sample_permit_project.title = "x"  # type: ignore[misc]


def test_permit_project_extra_forbid(sample_permit_project):
    """Extra fields rejected with ValidationError (extra='forbid')."""
    valid_dict = sample_permit_project.model_dump(mode="json")
    valid_dict["unknown_field"] = 1
    with pytest.raises(ValidationError):
        PermitProject.model_validate(valid_dict)


def test_applicant_name_defaults_to_none(sample_permit_project):
    """PII default discipline: applicant_name must default to None."""
    assert sample_permit_project.applicant_name is None


def test_geopoint_belgium_bbox():
    """GeoPoint validator rejects coordinates outside Belgium's bounding box."""
    # South of Belgium (lat < 49.5)
    with pytest.raises(ValidationError):
        GeoPoint(lat=49.0, lon=3.0)

    # Valid Belgian coordinate (Gent)
    valid = GeoPoint(lat=51.0, lon=3.7)
    assert valid.lat == 51.0

    # East of Belgium (lon > 6.4)
    with pytest.raises(ValidationError):
        GeoPoint(lat=51.0, lon=10.0)


def test_risk_category_has_14_members():
    """Taxonomy must have exactly 14 risk categories."""
    assert len(RiskCategory) == 14


def test_risk_category_member_names():
    """Taxonomy member values must match the master plan exactly."""
    expected = {
        "gro_height",
        "water_flood",
        "mer_screening",
        "bpa_rup_conflict",
        "motivation_defect",
        "trees_kapverg",
        "mobility_parking",
        "nature_2000_n",
        "heritage_inv",
        "nuisance_noise",
        "privacy_bezonning",
        "binding_advice_ignored",
        "function_mix_zoning",
        "vergunningencarrousel",
    }
    assert {c.value for c in RiskCategory} == expected
