"""SQLAlchemy models.

Design notes
------------
* All instants are stored as integer microseconds.  ``*_local_us`` columns are
  station clock readings and are never interpreted as UTC by the association
  or localization code; they only pass through a per-boot calibration map.
* Observations are immutable pieces of evidence.  Duplicate sequence numbers
  with identical content are dropped, identical sequence numbers with
  different content are stored in ``observation_conflicts`` and never
  overwrite the original.
* Each processing run freezes an input cutoff and a calibration snapshot so a
  run can be resumed after an interruption without blending calibration
  parameters or re-reading newer uploads.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Station(Base):
    __tablename__ = "stations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    hearing_radius_m: Mapped[float] = mapped_column(Float, nullable=False, default=1600.0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SoundClass(Base):
    __tablename__ = "sound_classes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str | None] = mapped_column(Text)
    feature_dim: Mapped[int | None] = mapped_column(Integer)
    feature_distance_limit: Mapped[float | None] = mapped_column(Float)
    created_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)


class CalibrationSourceRecord(Base):
    """A calibration source at a *known* position that emits at known instants.

    Stored separately from the API payload tables because anchors are resolved
    against it at calibration time: the emission instant is authoritative and
    the propagation delay is computed from the known position.
    """

    __tablename__ = "calibration_sources"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(200))
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    emissions_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)


class StationBoot(Base):
    __tablename__ = "station_boots"
    __table_args__ = (UniqueConstraint("station_id", "boot_id", name="uq_station_boot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"), nullable=False)
    boot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seen_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)


class UploadBatch(Base):
    __tablename__ = "upload_batches"
    __table_args__ = (
        UniqueConstraint("station_id", "batch_key", name="uq_upload_batch_key"),
        Index("ix_upload_batches_station_received", "station_id", "received_at_us"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"), nullable=False)
    batch_key: Mapped[str] = mapped_column(String(128), nullable=False)
    received_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conflict_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Hash of the canonical record list, so a batch_key reused with *different*
    # content is not mistaken for an idempotent replay.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    declared_complete_through_local_us: Mapped[int | None] = mapped_column(BigInteger)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    gaps_json: Mapped[list | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)


class Observation(Base):
    __tablename__ = "observations"
    __table_args__ = (
        UniqueConstraint("station_id", "boot_id", "source_seq", name="uq_observation_seq"),
        Index("ix_observations_kind_class_time", "kind", "class_label", "ingest_at_us"),
        Index("ix_observations_station_local", "station_id", "boot_id", "local_capture_us"),
        Index("ix_observations_cal_status", "calibration_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"), nullable=False)
    boot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="sound")
    class_label: Mapped[str] = mapped_column(String(64), nullable=False)
    local_capture_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uploaded_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ingest_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    features: Mapped[list] = mapped_column(JSONB, nullable=False)
    uncertainty_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    upload_batch_id: Mapped[int | None] = mapped_column(ForeignKey("upload_batches.id"))
    calibration_source_id: Mapped[str | None] = mapped_column(String(64))
    reference_emission_us: Mapped[int | None] = mapped_column(BigInteger)
    calibration_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    calibration_reason: Mapped[str | None] = mapped_column(Text)
    calibration_model_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibration_models.id", ondelete="SET NULL")
    )
    corrected_arrival_us: Mapped[int | None] = mapped_column(BigInteger)
    corrected_uncertainty_us: Mapped[int | None] = mapped_column(BigInteger)
    current_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL", use_alter=True, name="fk_observation_current_event")
    )
    created_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ObservationConflict(Base):
    __tablename__ = "observation_conflicts"
    __table_args__ = (
        Index("ix_conflicts_station_seq", "station_id", "boot_id", "source_seq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"), nullable=False)
    boot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    existing_observation_id: Mapped[int | None] = mapped_column(ForeignKey("observations.id"))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    received_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolution_note: Mapped[str | None] = mapped_column(Text)


class CalibrationModel(Base):
    __tablename__ = "calibration_models"
    __table_args__ = (
        UniqueConstraint("station_id", "boot_id", "version_hash", name="uq_calibration_version"),
        Index("ix_calibration_lookup", "station_id", "boot_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"), nullable=False)
    boot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    anchor_count: Mapped[int] = mapped_column(Integer, nullable=False)
    anchor_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    segments: Mapped[list | None] = mapped_column(JSONB)
    rms_residual_us: Mapped[int | None] = mapped_column(BigInteger)
    max_residual_us: Mapped[int | None] = mapped_column(BigInteger)
    uncertainty_us: Mapped[int | None] = mapped_column(BigInteger)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    invalid_reason: Mapped[str | None] = mapped_column(Text)
    local_anchor_min_us: Mapped[int | None] = mapped_column(BigInteger)
    local_anchor_max_us: Mapped[int | None] = mapped_column(BigInteger)
    params_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)


class StationProgress(Base):
    __tablename__ = "station_progress"
    __table_args__ = (
        Index("ix_progress_station_boot", "station_id", "boot_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"), nullable=False)
    boot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    upload_batch_id: Mapped[int | None] = mapped_column(ForeignKey("upload_batches.id"))
    declared_local_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mapped_ref_us: Mapped[int | None] = mapped_column(BigInteger)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    gaps_json: Mapped[list | None] = mapped_column(JSONB)
    received_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)


class StationExclusion(Base):
    __tablename__ = "station_exclusions"
    __table_args__ = (
        Index("ix_station_exclusions_active", "station_id", "active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    from_ref_us: Mapped[int | None] = mapped_column(BigInteger)
    to_ref_us: Mapped[int | None] = mapped_column(BigInteger)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ProcessingRun(Base):
    __tablename__ = "processing_runs"
    __table_args__ = (
        UniqueConstraint("batch_id", name="uq_run_batch"),
        Index("ix_runs_status", "status", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="full")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    cutoff_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    params_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    calibration_json: Mapped[dict | None] = mapped_column(JSONB)
    calibration_hash: Mapped[str | None] = mapped_column(String(64))
    checkpoint_json: Mapped[dict | None] = mapped_column(JSONB)
    phase: Mapped[str | None] = mapped_column(String(32))
    phase_log: Mapped[list | None] = mapped_column(JSONB)
    created_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at_us: Mapped[int | None] = mapped_column(BigInteger)
    finished_at_us: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    summary_json: Mapped[dict | None] = mapped_column(JSONB)
    run_options_json: Mapped[dict | None] = mapped_column(JSONB)
    selected_option_id: Mapped[str | None] = mapped_column(String(32))
    resume_of_run_id: Mapped[int | None] = mapped_column(ForeignKey("processing_runs.id"))
    revision_of_run_id: Mapped[int | None] = mapped_column(ForeignKey("processing_runs.id"))
    created_by: Mapped[str | None] = mapped_column(String(128))


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_status_class", "status", "class_label"),
        Index("ix_events_emission", "emission_us"),
        Index("ix_events_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("processing_runs.id"), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False)
    class_label: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="provisional")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reason: Mapped[str | None] = mapped_column(Text)
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    x_min: Mapped[float] = mapped_column(Float, nullable=False)
    x_max: Mapped[float] = mapped_column(Float, nullable=False)
    y_min: Mapped[float] = mapped_column(Float, nullable=False)
    y_max: Mapped[float] = mapped_column(Float, nullable=False)
    position_precision_m: Mapped[float] = mapped_column(Float, nullable=False)
    emission_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    emission_min_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    emission_max_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    residual_rms_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    residual_max_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    station_count: Mapped[int] = mapped_column(Integer, nullable=False)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    region_count: Mapped[int] = mapped_column(Integer, nullable=False)
    regions_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    finalization_json: Mapped[dict | None] = mapped_column(JSONB)
    calibration_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_events: Mapped[list | None] = mapped_column(JSONB)
    superseded_by_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL")
    )
    window_start_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    window_end_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)


class EventObservation(Base):
    __tablename__ = "event_observations"
    __table_args__ = (
        UniqueConstraint("event_id", "observation_id", name="uq_event_observation"),
        Index("ix_event_obs_observation", "observation_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    observation_id: Mapped[int] = mapped_column(ForeignKey("observations.id"), nullable=False)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"), nullable=False)
    corrected_arrival_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uncertainty_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    residual_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    calibration_model_id: Mapped[int | None] = mapped_column(ForeignKey("calibration_models.id"))
    anchor_ids: Mapped[list] = mapped_column(JSONB, nullable=False)


class EventExclusion(Base):
    __tablename__ = "event_exclusions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"), nullable=False)
    exclusion_id: Mapped[int | None] = mapped_column(ForeignKey("station_exclusions.id"))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)


class EventRevisionLink(Base):
    __tablename__ = "event_revision_links"
    __table_args__ = (
        UniqueConstraint("new_event_id", "old_event_id", name="uq_revision_link"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    new_event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    old_event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSONB)
    created_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)


class PropagationProfile(Base):
    """An approved, versioned set of propagation parameters.

    A profile has a reference-time validity interval.  A run freezes the
    profile approved at its cutoff; a profile that has since expired or been
    superseded leaves every event computed with it flagged ``stale`` rather
    than silently re-interpreted.
    """

    __tablename__ = "propagation_profiles"
    __table_args__ = (
        UniqueConstraint("version", name="uq_propagation_version"),
        Index("ix_propagation_validity", "valid_from_us", "valid_to_us"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    speed_mps: Mapped[float] = mapped_column(Float, nullable=False)
    valid_from_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    valid_to_us: Mapped[int | None] = mapped_column(BigInteger)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    approved_by: Mapped[str] = mapped_column(String(128), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)


class StationCoordinateCorrection(Base):
    """Immutable history of station coordinate corrections.

    The ``stations`` row always carries the current coordinates; this table
    keeps the previous values so a coverage map can show which coordinate
    version each frozen event used, and so old maps replay with the old values.
    """

    __tablename__ = "station_coordinate_corrections"
    __table_args__ = (
        Index("ix_station_coord_corr", "station_id", "applied_at_us"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"), nullable=False)
    old_x: Mapped[float] = mapped_column(Float, nullable=False)
    old_y: Mapped[float] = mapped_column(Float, nullable=False)
    new_x: Mapped[float] = mapped_column(Float, nullable=False)
    new_y: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    applied_by: Mapped[str] = mapped_column(String(128), nullable=False)
    applied_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)


class EventContextSnapshot(Base):
    """Frozen evidence context behind one merged event.

    Everything that could later change on the live configuration tables is
    copied here at event creation: the coordinates used, the timing residuals
    and calibration versions, the station sensitivity, the downtime/progress
    state and the approved propagation profile.  A coverage cell's conclusion
    is traceable to exactly these frozen rows, so coordinate or timing
    corrections can never retroactively alter an already published map.
    """

    __tablename__ = "event_context_snapshots"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_event_context"),
        Index("ix_event_context_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[int] = mapped_column(ForeignKey("processing_runs.id"), nullable=False)
    propagation_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("propagation_profiles.id")
    )
    propagation_version: Mapped[str] = mapped_column(String(64), nullable=False)
    propagation_speed_mps: Mapped[float] = mapped_column(Float, nullable=False)
    propagation_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    geometry_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reliability_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    stations_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)


class CoverageMap(Base):
    """An immutable, independently numbered observation-capability publication.

    A map covers a grid over the observation area, one reference-time period
    and, optionally, one sound class (species).  Once published its cells
    never change; a coordinate, timing or propagation correction produces a new
    map with a new ``map_version`` that supersedes the old one, while the old
    map remains available for replay exactly as first published.
    """

    __tablename__ = "coverage_maps"
    __table_args__ = (
        UniqueConstraint("map_version", name="uq_coverage_map_version"),
        Index("ix_coverage_maps_scope", "class_label", "period_start_us"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    map_version: Mapped[int] = mapped_column(Integer, nullable=False)
    series_key: Mapped[str] = mapped_column(String(160), nullable=False)
    run_id: Mapped[int] = mapped_column(ForeignKey("processing_runs.id"), nullable=False)
    class_label: Mapped[str | None] = mapped_column(String(64))
    period_start_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    period_end_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    grid_size_m: Mapped[float] = mapped_column(Float, nullable=False)
    area_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    cutoff_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    calibration_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    propagation_version: Mapped[str] = mapped_column(String(64), nullable=False)
    station_coordinate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    superseded_by_map_id: Mapped[int | None] = mapped_column(
        ForeignKey("coverage_maps.id", ondelete="SET NULL")
    )
    supersession_reason: Mapped[str | None] = mapped_column(Text)
    summary_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)


class CoverageCell(Base):
    """One grid cell of a coverage map and its observation-capability state.

    ``state`` is exactly one of ``unobserved``, ``observed_not_localizable``
    or ``reliably_localized``.  The localizable rate is a property of the
    whole map; a cell never substitutes a station count for an error figure.
    """

    __tablename__ = "coverage_cells"
    __table_args__ = (
        UniqueConstraint("coverage_map_id", "ix", "iy", name="uq_coverage_cell"),
        Index("ix_coverage_cells_state", "coverage_map_id", "state"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    coverage_map_id: Mapped[int] = mapped_column(
        ForeignKey("coverage_maps.id", ondelete="CASCADE"), nullable=False
    )
    ix: Mapped[int] = mapped_column(Integer, nullable=False)
    iy: Mapped[int] = mapped_column(Integer, nullable=False)
    x_min: Mapped[float] = mapped_column(Float, nullable=False)
    x_max: Mapped[float] = mapped_column(Float, nullable=False)
    y_min: Mapped[float] = mapped_column(Float, nullable=False)
    y_max: Mapped[float] = mapped_column(Float, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Error sources are machine-readable codes plus human-readable detail; the
    # same block is what lets a reviewer replay why the cell got its state.
    error_sources_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Best (smallest-range) evidence on the cell, null when unobserved.
    uncertainty_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)


class CoverageCellEvent(Base):
    """Traceability link: which frozen events decided a cell's conclusion."""

    __tablename__ = "coverage_cell_events"
    __table_args__ = (
        UniqueConstraint("coverage_cell_id", "event_id", name="uq_coverage_cell_event"),
        Index("ix_coverage_cell_event_event", "event_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    coverage_cell_id: Mapped[int] = mapped_column(
        ForeignKey("coverage_cells.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), nullable=False)
    contribution: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
