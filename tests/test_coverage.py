"""Acceptance tests for observation-capability coverage publications.

The four data conditions demanded by the task each get an end-to-end test:

* **collinear** geometry  -> ``observed_not_localizable`` cells, mirror
  ambiguity error sources and a localizable rate of zero;
* **offline** stations    -> fewer than three effective stations can only
  publish a direction/area-level conclusion, while a sound three-station
  geometry still localizes;
* **stale propagation**   -> an expired approved profile downgrades every
  affected cell and is recorded on the frozen event context;
* **coordinate correction** -> a new, independently numbered map supersedes
  the old one, while the old map and its frozen evidence replay unchanged.

Every observed cell must carry an actual uncertainty range (analytic
covariance plus the grid feasible region); a station count is never accepted
as an error figure.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from simulator.run_simulator import load_scenario

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def _run(client, **body):
    res = client.post("/runs", json={"wait": True, **body})
    assert res.status_code == 201, res.text
    return res.json()


def _latest_map(client, **params):
    maps = client.get("/coverage/maps", params=params).json()
    assert maps, "expected at least one coverage map"
    return maps[0]


def _cells(client, version, **params):
    return client.get(
        f"/coverage/maps/{version}/cells",
        params={"limit": 5000, **params},
    ).json()["cells"]


def _states(cells):
    tally: dict[str, int] = {}
    for cell in cells:
        tally[cell["state"]] = tally.get(cell["state"], 0) + 1
    return tally


# --------------------------------------------------------------------------- #
# Analytic covariance unit checks (no database)
# --------------------------------------------------------------------------- #
def test_analytic_covariance_sound_geometry_is_finite_and_well_conditioned():
    from app.covariance import analytic_uncertainty, min_subtended_angle_deg
    from app.localize import StationPoint

    stations = [
        StationPoint("S1", 0.0, 0.0, 4000.0),
        StationPoint("S2", 1200.0, 0.0, 4000.0),
        StationPoint("S3", 1200.0, 900.0, 4000.0),
        StationPoint("S4", 0.0, 900.0, 4000.0),
    ]
    out = analytic_uncertainty(
        stations, [20_000.0] * 4, 500.0, 400.0, 343.0, max_condition=1e4
    )
    assert not out.degenerate
    assert math.isfinite(out.semi_major_m) and out.semi_major_m > 0
    assert out.condition_number < 50
    # S1 and S3 sit on nearly opposite rays through this source, so the
    # tightest bearing pair is that diagonal even though the four-station
    # array stays well conditioned as a whole.
    angle, pair = min_subtended_angle_deg(stations, 500.0, 400.0)
    assert set(pair) == {"S1", "S3"}
    assert angle < 5.0
    # Moving the source off the diagonal restores a healthy angular spread.
    wide, _ = min_subtended_angle_deg(stations, 600.0, 300.0)
    assert wide > 15.0


def test_analytic_covariance_two_stations_is_rank_deficient():
    from app.covariance import analytic_uncertainty
    from app.localize import StationPoint

    stations = [
        StationPoint("S1", 0.0, 0.0, 4000.0),
        StationPoint("S2", 1200.0, 0.0, 4000.0),
    ]
    out = analytic_uncertainty(
        stations, [20_000.0, 25_000.0], 500.0, 400.0, 343.0, max_condition=1e4
    )
    assert out.degenerate
    assert "rank_deficient" in (out.reason or "")
    # JSON-serialised form must not contain infinities (JSONB rejects them).
    payload = out.as_dict(2.0)
    assert payload["ellipse_semi_axes_m"]["major"] is None
    assert payload["degenerate"] is True


def test_collinear_bearings_give_near_zero_minimum_angle():
    from app.covariance import min_subtended_angle_deg
    from app.localize import StationPoint

    stations = [
        StationPoint("S1", 150.0, 500.0, 1900.0),
        StationPoint("S2", 750.0, 500.0, 1900.0),
        StationPoint("S3", 1350.0, 500.0, 1900.0),
    ]
    # A source on the station line sees every station along one bearing line.
    angle, pair = min_subtended_angle_deg(stations, 900.0, 500.0)
    assert angle == pytest.approx(0.0, abs=1e-9)
    assert pair is not None
    # A source off the line still gets a finite, non-degenerate spread.
    off_line, _ = min_subtended_angle_deg(stations, 900.0, 180.0)
    assert off_line > 20.0


# --------------------------------------------------------------------------- #
# 1. Three-state publication, localizable rate and real uncertainty ranges
# --------------------------------------------------------------------------- #
def test_coverage_map_publishes_three_state_tally_and_rate(client, api, tmp_path):
    load_scenario("overlapping", "http://testserver", truth_dir=tmp_path)
    run = _run(client)
    assert run["status"] == "done"
    m = _latest_map(client)
    summary = m["summary"]
    cells = _cells(client, m["map_version"])
    for state in ("unobserved", "observed_not_localizable", "reliably_localized"):
        assert _states(cells).get(state, 0) == summary["cells"][state]
    assert set(summary["cells"]) == {
        "unobserved",
        "observed_not_localizable",
        "reliably_localized",
    }
    observed = summary["observed_cells"]
    assert observed > 0
    assert summary["localizable_rate"] == pytest.approx(1.0)
    # A rate is a ratio, never a station count proxy.
    assert summary["cells"]["reliably_localized"] == observed
    for cell in cells:
        if cell["state"] == "unobserved":
            assert cell["event_count"] == 0
            assert cell["uncertainty"] is None
            assert cell["error_sources"] == []
        else:
            assert cell["uncertainty"] is not None
            # Both uncertainty treatments ship together.
            u = cell["uncertainty"]
            assert u["grid_feasible_region"]["method"] == "coarse_to_fine_grid_search"
            assert u["analytic"]["method"] == "analytic_jacobian_covariance"
            assert u["grid_feasible_region"]["grid_resolution_m"] > 0
    # A tight bearing pair is a warning, never silently dropped; it only
    # becomes a hard reason when the covariance is also degenerate.
    any_event = client.get("/events").json()["events"][0]
    ctx = client.get(f"/events/{any_event['id']}/context").json()
    assert "angle_threshold_deg" in ctx["reliability"]
    assert isinstance(ctx["reliability"]["angle_warnings"], list)


def test_reliable_cell_evidence_points_at_frozen_point_level_events(client, api, tmp_path):
    load_scenario("overlapping", "http://testserver", truth_dir=tmp_path)
    _run(client)
    m = _latest_map(client)
    reliable = _cells(client, m["map_version"], state="reliably_localized")
    assert reliable
    cell = reliable[0]
    evidence = client.get(
        f"/coverage/maps/{m['map_version']}/cells/{cell['ix']}/{cell['iy']}/evidence"
    ).json()
    assert evidence["events"]
    for entry in evidence["events"]:
        assert entry["contribution"] == "reliably_localized"
        ctx = entry["frozen_context"]
        assert ctx is not None
        assert ctx["reliability"]["conclusion_level"] == "point"
        assert ctx["reliability"]["reliable"] is True
        assert len(ctx["stations"]) >= 3


# --------------------------------------------------------------------------- #
# 2. Collinear geometry: observed but not localizable
# --------------------------------------------------------------------------- #
def test_collinear_grid_cells_are_not_localizable_with_mirror_error_sources(
    client, api, tmp_path
):
    load_scenario("collinear", "http://testserver", truth_dir=tmp_path)
    _run(client)
    m = _latest_map(client)
    assert m["summary"]["localizable_rate"] == 0.0
    assert m["summary"]["cells"]["reliably_localized"] == 0
    observed = _cells(client, m["map_version"], state="observed_not_localizable")
    assert observed
    for cell in observed:
        codes = {e["code"] for e in cell["error_sources"]}
        assert "stations_collinear" in codes
        assert "multiple_disconnected_feasible_regions" in codes
        # The range is still published even though localization is impossible.
        assert cell["uncertainty"]["grid_feasible_region"]["region_count"] >= 2
        evidence = client.get(
            f"/coverage/maps/{m['map_version']}/cells/{cell['ix']}/{cell['iy']}/evidence"
        ).json()
        assert all(
            e["frozen_context"]["reliability"]["conclusion_level"] == "area"
            for e in evidence["events"]
        )


# --------------------------------------------------------------------------- #
# 3. Offline stations: direction/area level below three effective stations
# --------------------------------------------------------------------------- #
def test_two_effective_stations_only_allow_direction_level(client, api, tmp_path):
    # Waves 0 and 1 are the first two stations' early uploads; the other two
    # stations are still offline at the cutoff.
    load_scenario(
        "overlapping", "http://testserver", truth_dir=tmp_path, up_to_wave=2
    )
    run = _run(client)
    assert run["status"] == "awaiting_review"
    events = client.get("/events").json()["events"]
    assert events and all(e["station_count"] == 2 for e in events)
    m = _latest_map(client)
    assert m["summary"]["localizable_rate"] == 0.0
    cells = _cells(client, m["map_version"], state="observed_not_localizable")
    assert cells
    for cell in cells:
        codes = {e["code"] for e in cell["error_sources"]}
        assert "fewer_than_three_effective_stations" in codes
    evidence = client.get(
        f"/coverage/maps/{m['map_version']}/cells/{cells[0]['ix']}/{cells[0]['iy']}/evidence"
    ).json()
    ctx = evidence["events"][0]["frozen_context"]
    assert ctx["reliability"]["conclusion_level"] == "direction"
    assert ctx["reliability"]["analytic_uncertainty"]["degenerate"] is True


def test_three_stations_with_sound_geometry_still_localize(client, api, tmp_path):
    # One station offline (only its wave is missing) leaves three effective
    # stations with a non-degenerate array.
    load_scenario(
        "overlapping", "http://testserver", truth_dir=tmp_path, up_to_wave=3
    )
    _run(client)
    m = _latest_map(client)
    reliable = _cells(client, m["map_version"], state="reliably_localized")
    assert reliable
    ctx = client.get(f"/events/{reliable[0]['uncertainty']['event_id']}/context").json()
    assert len(ctx["stations"]) == 3
    assert ctx["reliability"]["conclusion_level"] == "point"
    assert ctx["reliability"]["analytic_uncertainty"]["degenerate"] is False


# --------------------------------------------------------------------------- #
# 4. Stale approved propagation parameters
# --------------------------------------------------------------------------- #
def test_expired_propagation_profile_downgrades_cells_and_is_frozen(
    client, api, tmp_path
):
    load_scenario("overlapping", "http://testserver", truth_dir=tmp_path)
    client.post(
        "/propagation-profiles",
        json={
            "version": "v-expired",
            "speed_mps": 343.0,
            "valid_from_at": "2026-01-01T00:00:00.000000Z",
            "valid_to_at": "2026-11-13T00:00:00.000000Z",
            "approved_by": "tester",
            "note": "expired before the survey window",
        },
    )
    run = _run(client)
    assert run["summary"]["propagation_version"] == "v-expired"
    m = _latest_map(client)
    assert m["propagation_version"] == "v-expired"
    assert m["summary"]["localizable_rate"] == 0.0
    cells = _cells(client, m["map_version"], state="observed_not_localizable")
    assert cells
    for cell in cells:
        assert "stale_propagation_parameters" in {
            e["code"] for e in cell["error_sources"]
        }
    event_id = cells[0]["uncertainty"]["event_id"]
    ctx = client.get(f"/events/{event_id}/context").json()
    assert ctx["propagation"]["stale"] is True
    assert ctx["propagation"]["version"] == "v-expired"
    assert ctx["reliability"]["conclusion_level"] == "area"


def test_default_profile_is_fresh_and_events_are_not_flagged_stale(
    client, api, tmp_path
):
    load_scenario("overlapping", "http://testserver", truth_dir=tmp_path)
    _run(client)
    m = _latest_map(client)
    reliable = _cells(client, m["map_version"], state="reliably_localized")
    ctx = client.get(
        f"/events/{reliable[0]['uncertainty']['event_id']}/context"
    ).json()
    assert ctx["propagation"]["stale"] is False
    assert ctx["propagation"]["version"] == "default-v1"


# --------------------------------------------------------------------------- #
# 5. Frozen per-event evidence context
# --------------------------------------------------------------------------- #
def test_event_context_freezes_coordinates_residuals_sensitivity_downtime(
    client, api, tmp_path
):
    load_scenario("overlapping", "http://testserver", truth_dir=tmp_path)
    _run(client)
    event = client.get("/events").json()["events"][0]
    ctx = client.get(f"/events/{event['id']}/context").json()
    assert ctx["event_id"] == event["id"]
    assert ctx["propagation"]["speed_mps"] > 0
    assert "station_count" in ctx["geometry"]
    assert len(ctx["stations"]) == event["station_count"]
    for entry in ctx["stations"]:
        assert set(entry["coordinates"]) == {"x", "y"}
        assert entry["sensitivity"]["hearing_radius_m"] > 0
        assert isinstance(entry["timing_residual_us"], int)
        assert entry["calibration_version_hash"]
        assert entry["calibration_anchor_ids"]
        assert "downtime" in entry and "mode" in entry["downtime"]
    # The same snapshot is reachable from a coverage cell's evidence trail.
    m = _latest_map(client)
    cell = _cells(client, m["map_version"], state="reliably_localized")[0]
    evidence = client.get(
        f"/coverage/maps/{m['map_version']}/cells/{cell['ix']}/{cell['iy']}/evidence"
    ).json()
    ids = {e["event_id"] for e in evidence["events"]}
    assert event["id"] in ids


# --------------------------------------------------------------------------- #
# 6. Coordinate corrections: independent map numbering, immutable replay
# --------------------------------------------------------------------------- #
def test_coordinate_correction_requires_reason_and_records_history(
    client, api, tmp_path
):
    load_scenario("overlapping", "http://testserver", truth_dir=tmp_path)
    _run(client)
    body = {
        "id": "S1",
        "name": "Station S1",
        "x": 305.0,
        "y": 102.0,
        "hearing_radius_m": 1700.0,
        "active": True,
    }
    rejected = client.put("/stations/S1", json=body)
    assert rejected.status_code == 400
    ok = client.put(
        "/stations/S1", json={**body, "coordinate_correction_reason": "GNSS resurvey"}
    )
    assert ok.status_code == 200
    corrections = client.get("/stations/S1/coordinate-corrections").json()
    assert len(corrections) == 1
    assert corrections[0]["old_x"] == 300.0 and corrections[0]["new_x"] == 305.0


def test_new_map_after_correction_supersedes_old_but_keeps_old_cells(
    client, api, tmp_path
):
    load_scenario("overlapping", "http://testserver", truth_dir=tmp_path)
    _run(client)
    first = _latest_map(client)
    old_cells = _cells(client, first["map_version"])
    old_tally = _states(old_cells)
    # Capture the frozen coordinate used by an event of the old map.
    sample = next(c for c in old_cells if c["state"] != "unobserved")
    old_evidence = client.get(
        f"/coverage/maps/{first['map_version']}/cells/{sample['ix']}/{sample['iy']}/evidence"
    ).json()
    old_s1 = next(
        s["coordinates"]["x"]
        for e in old_evidence["events"]
        for s in e["frozen_context"]["stations"]
        if s["station_id"] == "S1"
    )

    client.put(
        "/stations/S1",
        json={
            "id": "S1",
            "name": "Station S1",
            "x": 305.0,
            "y": 100.0,
            "hearing_radius_m": 1700.0,
            "active": True,
            "coordinate_correction_reason": "GNSS resurvey after marker disturbance",
        },
    )
    _run(client)
    all_maps = client.get(
        "/coverage/maps", params={"include_superseded": "true"}
    ).json()
    by_version = {m["map_version"]: m for m in all_maps}
    old = by_version[first["map_version"]]
    assert old["superseded_by_map_version"] is not None
    assert "station_coordinate_correction" in (old["supersession_reason"] or "")
    # Independent numbering: the new version is a distinct row.
    new_version = old["superseded_by_map_version"]
    assert new_version != first["map_version"]
    assert by_version[new_version]["superseded_by_map_version"] is None
    # The old map still replays exactly as published.
    assert _states(_cells(client, first["map_version"])) == old_tally
    replayed = client.get(
        f"/coverage/maps/{first['map_version']}/cells/{sample['ix']}/{sample['iy']}/evidence"
    ).json()
    replayed_s1 = next(
        s["coordinates"]["x"]
        for e in replayed["events"]
        for s in e["frozen_context"]["stations"]
        if s["station_id"] == "S1"
    )
    assert replayed_s1 == old_s1 == 300.0


# --------------------------------------------------------------------------- #
# 7. On-demand publication and filtering
# --------------------------------------------------------------------------- #
def test_on_demand_map_publication_and_state_filter(client, api, tmp_path):
    load_scenario("overlapping", "http://testserver", truth_dir=tmp_path)
    _run(client)
    republished = client.post(
        "/coverage/maps",
        json={
            "class_label": "chirp",
            "period_start_at": "2026-11-14T00:00:00.000000Z",
            "period_end_at": "2026-11-14T02:00:00.000000Z",
        },
    )
    assert republished.status_code == 201
    body = republished.json()
    assert body["summary"]["observed_cells"] > 0
    # Same series, so the auto-published map is now superseded by this one.
    older = client.get(
        "/coverage/maps", params={"include_superseded": "true"}
    ).json()
    assert any(m["superseded_by_map_version"] == body["map_version"] for m in older)
    # A finer grid is a different series and must not supersede anything.
    fine = client.post(
        "/coverage/maps",
        json={
            "class_label": "chirp",
            "period_start_at": "2026-11-14T00:00:00.000000Z",
            "period_end_at": "2026-11-14T02:00:00.000000Z",
            "grid_size_m": 100.0,
        },
    ).json()
    assert fine["superseded_by_map_version"] is None
    fine_cells = client.get(
        f"/coverage/maps/{fine['map_version']}/cells",
        params={"state": "unobserved", "limit": 5000},
    ).json()
    assert fine_cells["total"] > 0
    assert all(c["state"] == "unobserved" for c in fine_cells["cells"])
    # Unknown scope is rejected rather than publishing an empty map.
    missing = client.post(
        "/coverage/maps",
        json={
            "class_label": "does-not-exist",
            "period_start_at": "2026-11-14T00:00:00.000000Z",
            "period_end_at": "2026-11-14T02:00:00.000000Z",
        },
    )
    assert missing.status_code == 404
    # A disjoint/future window is rejected too, rather than publishing a
    # vacuously all-unobserved map.
    disjoint = client.post(
        "/coverage/maps",
        json={
            "class_label": "chirp",
            "period_start_at": "2030-01-01T00:00:00.000000Z",
            "period_end_at": "2030-01-02T00:00:00.000000Z",
        },
    )
    assert disjoint.status_code == 404


def test_coverage_endpoints_reject_unknown_versions_and_states(client, api, tmp_path):
    load_scenario("overlapping", "http://testserver", truth_dir=tmp_path)
    _run(client)
    assert client.get("/coverage/maps/9999").status_code == 404
    m = _latest_map(client)
    bad = client.get(f"/coverage/maps/{m['map_version']}/cells", params={"state": "nope"})
    assert bad.status_code == 400
