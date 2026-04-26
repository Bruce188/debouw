"""
SQLAlchemy 2.0 declarative schema for debouw.

Field-by-field mapping (PermitProject → permit_projects columns):
  PermitProject.external_id              → external_id    String PK
  PermitProject.source                   → source         String
  PermitProject.omv_reference            → omv_reference  String
  PermitProject.detail_url               → detail_url     String
  PermitProject.title                    → title          String
  PermitProject.description              → description    String nullable
  PermitProject.applicant_name           → applicant_name String nullable (PII default None)
  PermitProject.address                  → address        JSON (Address)
  PermitProject.project_type             → project_type   String nullable
  PermitProject.floors                   → floors         Integer nullable
  PermitProject.height_m                 → height_m       Float nullable
  PermitProject.units                    → units          Integer nullable
  PermitProject.parking_spaces           → parking_spaces Integer nullable
  PermitProject.trees_to_fell            → trees_to_fell  Integer nullable
  PermitProject.mer_status               → mer_status     String nullable
  PermitProject.iioa_class               → iioa_class     Integer nullable
  PermitProject.status                   → status         String
  PermitProject.decision_date            → decision_date  Date nullable
  PermitProject.decision_outcome         → decision_outcome String nullable
  PermitProject.attachments              → attachments    JSON (list[HttpUrl])
  PermitProject.dossier_pdfs             → dossier_pdfs   JSON (list[Path])
  PermitProject.overlays                 → overlays       JSON nullable (GeoOverlays)
  PermitProject.raw_html_path            → raw_html_path  String
  PermitProject.first_seen_at            → first_seen_at  DateTime
  PermitProject.last_changed_at          → last_changed_at DateTime
  PermitProject.content_hash             → content_hash   String
  PermitProject.decision_regime          → decision_regime String
"""

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class PermitProjectRow(Base):
    __tablename__ = "permit_projects"

    external_id: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    omv_reference: Mapped[str] = mapped_column(String, nullable=False)
    detail_url: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    applicant_name: Mapped[str | None] = mapped_column(String, nullable=True)
    address: Mapped[dict] = mapped_column(JSON, nullable=False)  # Address JSON
    project_type: Mapped[str | None] = mapped_column(String, nullable=True)
    floors: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parking_spaces: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trees_to_fell: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mer_status: Mapped[str | None] = mapped_column(String, nullable=True)
    iioa_class: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    decision_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    decision_outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    attachments: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    dossier_pdfs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    overlays: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_html_path: Mapped[str] = mapped_column(String, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    decision_regime: Mapped[str] = mapped_column(String, nullable=False)


class RiskAssessmentRow(Base):
    __tablename__ = "risk_assessments"

    # Composite PK: re-classifications produce new rows per engine_version
    project_external_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("permit_projects.external_id"),
        primary_key=True,
    )
    engine_version: Mapped[str] = mapped_column(String, primary_key=True)
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    expected_delay_days: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    summary: Mapped[str] = mapped_column(String, nullable=False)
    top_risks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    calibration_regime: Mapped[str] = mapped_column(String, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    inputs_hash: Mapped[str] = mapped_column(String, nullable=False)


class PublicInquiryRow(Base):
    __tablename__ = "public_inquiries"

    external_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_external_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("permit_projects.external_id"),
        nullable=False,
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    objection_deadline: Mapped[date] = mapped_column(Date, nullable=False)
    days_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    objection_count_known: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ScrapeStateRow(Base):
    """Helper table for tracking scrape cursors per source."""

    __tablename__ = "scrape_state"

    source: Mapped[str] = mapped_column(String, primary_key=True)
    cursor: Mapped[str | None] = mapped_column(String, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RiskNarrationCacheRow(Base):
    """Cache table for LLM narration keyed by (project_external_id, engine_version).

    No FK on project_external_id by design: orphan cache rows survive
    engine_version bumps and can be pruned manually (LIMITATIONS.md § Engine version policy).
    """

    __tablename__ = "risk_narration_cache"

    project_external_id: Mapped[str] = mapped_column(String, primary_key=True)
    engine_version: Mapped[str] = mapped_column(String, primary_key=True)
    rationales_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    summary: Mapped[str] = mapped_column(String, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
