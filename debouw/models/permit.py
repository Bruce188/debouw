"""
Pydantic v2 schemas — the cross-phase contract for debouw.

All storage models are frozen=True, extra="forbid".
Downstream phases (1-5) import these unchanged. Schema drift cascades.
"""

from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class GeoPoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lat: float  # bbox-validated to Belgium
    lon: float
    crs: Literal["EPSG:4326"] = "EPSG:4326"

    @field_validator("lat")
    @classmethod
    def validate_lat(cls, v: float) -> float:
        if not (49.5 <= v <= 51.6):
            raise ValueError(f"lat {v} outside Belgium bbox [49.5, 51.6]")
        return v

    @field_validator("lon")
    @classmethod
    def validate_lon(cls, v: float) -> float:
        if not (2.5 <= v <= 6.4):
            raise ValueError(f"lon {v} outside Belgium bbox [2.5, 6.4]")
        return v


class Address(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    raw: str
    street: str | None = None
    house_number: str | None = None
    postcode: str | None = None
    municipality: str | None = None
    point: GeoPoint | None = None
    parcel_id: str | None = None  # kadastraal — needed for vergunningencarrousel rule


class GeoOverlays(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    in_natura_2000: bool = False
    natura_2000_distance_m: float | None = None
    in_signaalgebied: bool = False
    flood_risk_fluvial: Literal["none", "low", "medium", "high"] = "none"
    flood_risk_pluvial: Literal["none", "low", "medium", "high"] = "none"
    in_protected_heritage: bool = False
    heritage_match_distance_m: float | None = None
    rup_zone: str | None = None
    bpa_voorschriften_text: str | None = None  # if reachable
    distance_to_school_m: float | None = None
    distance_to_residential_m: float | None = None
    raw_layer_responses: dict[str, str] = Field(default_factory=dict)  # traceability


class PermitProjectStatus(str, Enum):
    """Natural lifecycle states for a Belgian omgevingsvergunning."""

    INTAKE = "intake"
    IN_PUBLIC_INQUIRY = "in_public_inquiry"
    DECIDED = "decided"
    APPEALED = "appealed"
    CLOSED = "closed"


class PermitProject(BaseModel):
    """Engine input. Represents one omgevingsvergunning dossier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    external_id: str  # natural key, e.g. "gent:OMV_2026025532"
    source: Literal["gent_consultatie", "vlaanderen_inzage", "brussels_openpermits"]
    omv_reference: str
    detail_url: HttpUrl
    title: str
    description: str | None = None
    applicant_name: str | None = None  # only persist if source displays
    address: Address
    project_type: str | None = None
    floors: int | None = None
    height_m: float | None = None
    units: int | None = None
    parking_spaces: int | None = None
    trees_to_fell: int | None = None
    mer_status: Literal["none", "screening", "exempt", "mer_plicht"] | None = None
    iioa_class: int | None = None  # klasse 1/2/3 voor IIOA
    status: PermitProjectStatus
    decision_date: date | None = None
    decision_outcome: str | None = None
    attachments: list[HttpUrl] = Field(default_factory=list)
    dossier_pdfs: list[Path] = Field(default_factory=list)  # local cached paths
    overlays: GeoOverlays | None = None  # populated by enrichment, BEFORE engine
    raw_html_path: Path
    first_seen_at: datetime
    last_changed_at: datetime
    content_hash: str
    decision_regime: Literal["pre_2026_reform", "post_2026_reform"]  # for calibration


class PublicInquiry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    external_id: str
    period_start: date
    period_end: date
    objection_deadline: date
    days_remaining: int | None = None  # computed downstream
    objection_count_known: int | None = None  # rarely available


class RiskCategory(str, Enum):
    GRO_HEIGHT = "gro_height"
    WATER_FLOOD = "water_flood"
    MER_SCREENING = "mer_screening"
    BPA_RUP_CONFLICT = "bpa_rup_conflict"
    MOTIVATION_DEFECT = "motivation_defect"
    TREES_KAPVERG = "trees_kapverg"
    MOBILITY_PARKING = "mobility_parking"
    NATURE_2000_N = "nature_2000_n"
    HERITAGE_INV = "heritage_inv"
    NUISANCE_NOISE = "nuisance_noise"
    PRIVACY_BEZONNING = "privacy_bezonning"
    BINDING_ADVICE_IGNORED = "binding_advice_ignored"
    FUNCTION_MIX_ZONING = "function_mix_zoning"
    VERGUNNINGENCARROUSEL = "vergunningencarrousel"


class PrecedentMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    precedent_id: str  # e.g. "RVVB.A.2425.0312"
    summary: str  # one-line citation
    similarity: float  # cosine similarity 0..1
    outcome: str  # "vernietigd", "verworpen", "gedeeltelijk", ...


class RiskFactor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    category: RiskCategory
    label: str  # NL human-readable
    rationale: str  # 2-3 sentences from LLM narrator
    severity: float  # 0..1 (rescaled expected delay days)
    probability: float  # trigger × success
    expected_delay_days: float
    confidence: float
    typical_objector: str
    evidence: list[str]  # overlay names, RvVb case ids, similar-project ids
    precedents: list[PrecedentMatch]


class RiskAssessment(BaseModel):
    """Engine output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_external_id: str
    overall_score: float  # = max(probability) across categories
    expected_delay_days: float  # Σ probability_c · severity_raw_days_c
    confidence: float
    summary: str  # 1-paragraph LLM
    top_risks: list[RiskFactor]  # top-K=5, sorted by score desc
    engine_version: str
    calibration_regime: Literal["pre_2026_reform", "post_2026_reform"]
    generated_at: datetime
    inputs_hash: str
