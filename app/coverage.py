"""Observation-capability coverage maps.

A coverage map publishes, for every grid cell of one reference-time period and
one species scope, exactly one of three conclusions:

* ``unobserved`` — no merged event's feasible region reaches the cell;
* ``observed_not_localizable`` — at least one event covers the cell but none
  meets the reliable-localization rules (station count, array angle,
  non-collinear geometry, covariance condition, region size, propagation
  currency);
* ``reliably_localized`` — at least one event covering the cell is reliable.

The localizable rate is ``reliably_localized / observed cells`` and is always
published together with the actual uncertainty ranges (grid feasible regions
plus analytic covariance).  A station count is never used as an error figure.

Maps are immutable and independently numbered.  Re-publishing the same scope
(series) after a coordinate, timing or propagation correction creates a new
map version, marks the previous one superseded with a machine-readable reason,
and leaves every old cell row untouched so the old conclusion still replays.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Area, Settings
from .models import (
    CoverageCell,
    CoverageCellEvent,
    CoverageMap,
    Event,
    EventContextSnapshot,
    ProcessingRun,
    Station,
)
from .timeutil import iso, now_us

STATE_UNOBSERVED = "unobserved"
STATE_OBSERVED_NOT_LOCALIZABLE = "observed_not_localizable"
STATE_RELIABLY_LOCALIZED = "reliably_localized"
STATES = (
    STATE_UNOBSERVED,
    STATE_OBSERVED_NOT_LOCALIZABLE,
    STATE_RELIABLY_LOCALIZED,
)


# --------------------------------------------------------------------------- #
# Grid
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GridSpec:
    x_min: float
    y_min: float
    nx: int
    ny: int
    size_m: float

    def cell_bounds(self, ix: int, iy: int) -> tuple[float, float, float, float]:
        x0 = self.x_min + ix * self.size_m
        y0 = self.y_min + iy * self.size_m
        return x0, y0, x0 + self.size_m, y0 + self.size_m


def build_grid(area: Area, size_m: float) -> GridSpec:
    nx = max(1, math.ceil((area.x_max - area.x_min) / size_m))
    ny = max(1, math.ceil((area.y_max - area.y_min) / size_m))
    return GridSpec(area.x_min, area.y_min, nx, ny, size_m)


def station_coordinate_hash(db: Session) -> str:
    rows = db.execute(select(Station).order_by(Station.id)).scalars()
    payload = [
        {"id": s.id, "x": round(s.x, 6), "y": round(s.y, 6)}
        for s in rows
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]


def _rects_intersect(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0)


def _event_region_rects(event: Event) -> list[tuple[float, float, float, float]]:
    """Coverage rectangles of an event in reference coordinates.

    Prefer the stored fine-grid cells (the actual feasible set); when a region
    was truncated, fall back to its bounding box, which only ever over-covers.
    """
    rects: list[tuple[float, float, float, float]] = []
    for region in event.regions_json or []:
        cells = region.get("cells") or []
        if cells and not region.get("cells_truncated"):
            rects.extend((c[0], c[1], c[2], c[3]) for c in cells)
        else:
            b = region["bounds"]
            rects.append((b["x_min"], b["y_min"], b["x_max"], b["y_max"]))
    return rects


def _windows_overlap(
    event: Event, period_start_us: int, period_end_us: int
) -> bool:
    return not (
        event.window_end_us < period_start_us
        or event.window_start_us > period_end_us
    )


# --------------------------------------------------------------------------- #
# Publication
# --------------------------------------------------------------------------- #
def _series_key(class_label: str | None, grid: GridSpec) -> str:
    # The series identifies the publication scope (species + grid geometry),
    # not the covered window: a later run with a shifted window still publishes
    # the next version of the same map series, and every prior version remains
    # replayable.  The period is stored on each map as plain content.
    scope = class_label or "all_species"
    return f"{scope}|grid={grid.size_m:g}m|{grid.nx}x{grid.ny}"


def _event_uncertainty_block(event: Event, snapshot: EventContextSnapshot) -> dict:
    reliability = snapshot.reliability_json or {}
    best_region = (event.regions_json or [{}])[0]
    return {
        "event_id": event.id,
        "conclusion_level": reliability.get("conclusion_level"),
        "grid_feasible_region": {
            "method": "coarse_to_fine_grid_search",
            "region_count": event.region_count,
            "grid_resolution_m": event.position_precision_m,
            "bounds": best_region.get("bounds"),
            "area_m2": best_region.get("area_m2"),
            "extent_m": best_region.get("extent_m"),
            "emission_interval": {
                "min": iso(event.emission_min_us),
                "max": iso(event.emission_max_us),
            },
        },
        "analytic": reliability.get("analytic_uncertainty"),
        "min_subtended_angle_deg": reliability.get("min_subtended_angle_deg"),
        "residual": {
            "rms_ms": event.residual_rms_us / 1000.0,
            "max_ms": event.residual_max_us / 1000.0,
        },
    }


def _extent_of_event(event: Event) -> float:
    return max(
        (r.get("extent_m", 0.0) for r in event.regions_json or []),
        default=0.0,
    )


def publish_map(
    db: Session,
    *,
    run: ProcessingRun,
    area: Area,
    cfg: Settings,
    class_label: str | None,
    period_start_us: int,
    period_end_us: int,
    grid_size_m: float | None = None,
    events: list[Event] | None = None,
) -> CoverageMap:
    """Build and persist one immutable coverage map.

    ``events`` defaults to every live event of the run; callers may pass a
    pre-filtered list.  Events are filtered again by class and period overlap,
    and each event must have its frozen :class:`EventContextSnapshot`.
    """
    size_m = grid_size_m or cfg.capability_grid_size_m
    grid = build_grid(area, size_m)
    if events is None:
        events = list(
            db.execute(
                select(Event).where(
                    Event.run_id == run.id,
                    Event.status != "superseded",
                )
            ).scalars()
        )
    scoped = [
        e
        for e in events
        if (class_label is None or e.class_label == class_label)
        and _windows_overlap(e, period_start_us, period_end_us)
    ]
    snapshots: dict[int, EventContextSnapshot] = {}
    if scoped:
        snapshots = {
            s.event_id: s
            for s in db.execute(
                select(EventContextSnapshot).where(
                    EventContextSnapshot.event_id.in_([e.id for e in scoped])
                )
            ).scalars()
        }

    # Index events by the coverage cells their feasible regions touch.
    covering: dict[tuple[int, int], list[Event]] = {}
    for event in scoped:
        rects = _event_region_rects(event)
        touched: set[tuple[int, int]] = set()
        for ix in range(grid.nx):
            for iy in range(grid.ny):
                cell_rect = grid.cell_bounds(ix, iy)
                if any(_rects_intersect(cell_rect, r) for r in rects):
                    touched.add((ix, iy))
        for key in touched:
            covering.setdefault(key, []).append(event)

    max_version = db.scalar(select(func.max(CoverageMap.map_version)))
    version_no = (max_version + 1) if max_version else 1
    coord_hash = station_coordinate_hash(db)
    key = _series_key(class_label, grid)

    tally = {state: 0 for state in STATES}
    t = now_us()
    coverage_map = CoverageMap(
        map_version=version_no,
        series_key=key,
        run_id=run.id,
        class_label=class_label,
        period_start_us=period_start_us,
        period_end_us=period_end_us,
        grid_size_m=size_m,
        area_json=area.as_dict(),
        cutoff_us=run.cutoff_us,
        calibration_hash=run.calibration_hash or "",
        propagation_version=(
            _prop_version_of(scoped[0], snapshots) if scoped else "none"
        ),
        station_coordinate_hash=coord_hash,
        summary_json={},
        created_at_us=t,
    )
    db.add(coverage_map)
    db.flush()

    for ix in range(grid.nx):
        for iy in range(grid.ny):
            x0, y0, x1, y1 = grid.cell_bounds(ix, iy)
            cell_events = covering.get((ix, iy), [])
            if not cell_events:
                state = STATE_UNOBSERVED
                error_sources: list[dict] = []
                uncertainty = None
            else:
                reliable_events = [
                    e
                    for e in cell_events
                    if (snapshots.get(e.id) and snapshots[e.id].reliability_json.get("reliable"))
                ]
                if reliable_events:
                    state = STATE_RELIABLY_LOCALIZED
                    chosen_pool = reliable_events
                else:
                    state = STATE_OBSERVED_NOT_LOCALIZABLE
                    chosen_pool = cell_events
                # Union of the frozen error-source codes, each attributed.
                error_sources = []
                for e in cell_events:
                    snap = snapshots.get(e.id)
                    if not snap:
                        continue
                    for reason in snap.reliability_json.get("reasons", []):
                        error_sources.append(
                            {
                                "event_id": e.id,
                                "code": reason.split(":", 1)[0],
                                "detail": reason,
                            }
                        )
                best = min(chosen_pool, key=_extent_of_event)
                best_snapshot = snapshots.get(best.id)
                uncertainty = (
                    _event_uncertainty_block(best, best_snapshot)
                    if best_snapshot is not None
                    else None
                )
            tally[state] += 1
            cell = CoverageCell(
                coverage_map_id=coverage_map.id,
                ix=ix,
                iy=iy,
                x_min=round(x0, 3),
                y_min=round(y0, 3),
                x_max=round(x1, 3),
                y_max=round(y1, 3),
                state=state,
                event_count=len(cell_events),
                error_sources_json=error_sources,
                uncertainty_json=uncertainty,
                created_at_us=t,
            )
            db.add(cell)
            db.flush()
            for e in cell_events:
                snap = snapshots.get(e.id)
                contribution = (
                    "reliably_localized"
                    if snap and snap.reliability_json.get("reliable")
                    else "observed_not_localizable"
                )
                db.add(
                    CoverageCellEvent(
                        coverage_cell_id=cell.id,
                        event_id=e.id,
                        contribution=contribution,
                        created_at_us=t,
                    )
                )

    observed = tally[STATE_OBSERVED_NOT_LOCALIZABLE] + tally[STATE_RELIABLY_LOCALIZED]
    localizable_rate = (
        tally[STATE_RELIABLY_LOCALIZED] / observed if observed else None
    )
    coverage_map.summary_json = {
        "grid": {
            "size_m": size_m,
            "nx": grid.nx,
            "ny": grid.ny,
            "total_cells": grid.nx * grid.ny,
        },
        "period": {"start": iso(period_start_us), "end": iso(period_end_us)},
        "class_label": class_label,
        "cells": dict(tally),
        "observed_cells": observed,
        # Reliable cells per observed cell; null when nothing was observed,
        # because a rate of 0/0 must not be published as 0.
        "localizable_rate": localizable_rate,
        "events_considered": len(scoped),
    }
    db.flush()
    _supersede_previous(db, coverage_map, run, coord_hash)
    db.flush()
    return coverage_map


def _prop_version_of(event: Event, snapshots: dict[int, EventContextSnapshot]) -> str:
    snap = snapshots.get(event.id)
    if snap is not None:
        return snap.propagation_version
    return "unknown"


def _supersede_previous(
    db: Session, new_map: CoverageMap, run: ProcessingRun, coord_hash: str
) -> None:
    """Mark same-series live maps as superseded; old cell rows stay intact."""
    previous = list(
        db.execute(
            select(CoverageMap).where(
                CoverageMap.series_key == new_map.series_key,
                CoverageMap.superseded_by_map_id.is_(None),
                CoverageMap.id != new_map.id,
            )
        ).scalars()
    )
    for old in previous:
        reasons: list[str] = []
        if old.station_coordinate_hash != coord_hash:
            reasons.append("station_coordinate_correction")
        if old.calibration_hash != new_map.calibration_hash:
            reasons.append("timing_calibration_revision")
        if old.propagation_version != new_map.propagation_version:
            reasons.append("propagation_parameter_update")
        if not reasons:
            reasons.append("new_processing_run")
        old.superseded_by_map_id = new_map.id
        old.supersession_reason = ";".join(reasons)


def publish_for_run(
    db: Session,
    run: ProcessingRun,
    events: list[Event],
    cfg: Settings,
    area: Area,
) -> list[CoverageMap]:
    """Publish one map per species present in the run's live events.

    The all-species aggregate is intentionally not auto-published: species
    scopes are the publication unit demanded by the trend surveys.  An
    aggregate can always be requested on demand through the API.
    """
    live = [e for e in events if e.status != "superseded"]
    if not live:
        return []
    start_us = min(e.window_start_us for e in live)
    end_us = max(e.window_end_us for e in live)
    maps: list[CoverageMap] = []
    for class_label in sorted({e.class_label for e in live}):
        class_events = [e for e in live if e.class_label == class_label]
        c_start = min(e.window_start_us for e in class_events)
        c_end = max(e.window_end_us for e in class_events)
        maps.append(
            publish_map(
                db,
                run=run,
                area=area,
                cfg=cfg,
                class_label=class_label,
                period_start_us=c_start,
                period_end_us=c_end,
                events=class_events,
            )
        )
    return maps
