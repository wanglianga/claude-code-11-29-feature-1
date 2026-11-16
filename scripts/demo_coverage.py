"""End-to-end demonstration of the observation-capability coverage feature.

Drives a *running* service over the four data conditions named in the task and
prints one PASS/FAIL line per invariant:

1. collinear stations   -> observed but not localizable, mirror error sources;
2. offline stations     -> direction/area level below three effective stations;
3. stale propagation    -> expired approved profile downgrades the cells;
4. coordinate correction -> a new independently numbered map supersedes the
   old one while the old map replays with the frozen, pre-correction evidence.

Usage::

    python scripts/demo_coverage.py --base-url http://127.0.0.1:8000 --api-key dev-api-key
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from simulator.run_simulator import load_scenario  # noqa: E402
import simulator.run_simulator as sim  # noqa: E402


class _CheckPrinter:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, name: str, ok: bool, detail: str) -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        if not ok:
            self.failures += 1


def _install_api(api_key: str, base_url: str) -> None:
    """Redirect the simulator's HTTP client at this script's session base."""

    class _Api(sim.Api):
        def __init__(self, base: str = base_url):
            super().__init__(base_url, api_key=api_key)

    sim.Api = _Api


def _get(session: requests.Session, base: str, path: str, **params):
    r = session.get(base + path, params=params, timeout=120)
    r.raise_for_status()
    return r.json()


def _post(session: requests.Session, base: str, path: str, body: dict):
    r = session.post(base + path, json=body, timeout=120)
    r.raise_for_status()
    return r.json()


def _run(session: requests.Session, base: str) -> dict:
    return _post(session, base, "/runs", {"wait": True})


def _latest_map(session: requests.Session, base: str) -> dict:
    return _get(session, base, "/coverage/maps")[0]


def _cells(session: requests.Session, base: str, version: int, **params):
    return _get(
        session, base, f"/coverage/maps/{version}/cells", limit=5000, **params
    )["cells"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default="dev-api-key")
    args = parser.parse_args(argv)
    base = args.base_url.rstrip("/")
    http = requests.Session()
    http.headers.update({"X-API-Key": args.api_key})
    out = _CheckPrinter()
    tmp = Path(tempfile.mkdtemp(prefix="coverage-demo-"))

    # --- 1. Collinear geometry ------------------------------------------------
    print("\n=== 1. collinear stations ===")
    _install_api(args.api_key, base)
    load_scenario("collinear", base, truth_dir=tmp / "collinear", reset=True)
    _run(http, base)
    m = _latest_map(http, base)
    collinear_cells = _cells(http, base, m["map_version"])
    reliable = [c for c in collinear_cells if c["state"] == "reliably_localized"]
    not_localizable = [
        c for c in collinear_cells if c["state"] == "observed_not_localizable"
    ]
    out.check(
        "collinear map has no reliable cell",
        not reliable,
        f"localizable_rate={m['summary']['localizable_rate']}",
    )
    mirror = all(
        any(e["code"] == "stations_collinear" for e in c["error_sources"])
        and any(e["code"] == "multiple_disconnected_feasible_regions" for e in c["error_sources"])
        for c in not_localizable
    )
    out.check(
        "every not-localizable cell records collinear + multi-region error sources",
        bool(not_localizable) and mirror,
        f"{len(not_localizable)} observed_not_localizable cells",
    )

    # --- 2. Offline stations --------------------------------------------------
    print("\n=== 2. offline stations ===")
    load_scenario("overlapping", base, truth_dir=tmp / "offline", reset=True,
                  up_to_wave=2)
    _run(http, base)
    m2 = _latest_map(http, base)
    weak = _cells(http, base, m2["map_version"], state="observed_not_localizable")
    direction = all(
        any(e["code"] == "fewer_than_three_effective_stations" for e in c["error_sources"])
        for c in weak
    )
    out.check(
        "two effective stations only give a direction/area-level conclusion",
        bool(weak) and direction and m2["summary"]["localizable_rate"] == 0.0,
        f"{len(weak)} not-localizable cells, rate={m2['summary']['localizable_rate']}",
    )

    # --- 3. Stale propagation parameters --------------------------------------
    print("\n=== 3. stale propagation parameters ===")
    load_scenario("overlapping", base, truth_dir=tmp / "stale", reset=True)
    _post(
        http, base, "/propagation-profiles",
        {
            "version": "v-expired-demo",
            "speed_mps": 343.0,
            "valid_from_at": "2026-01-01T00:00:00.000000Z",
            "valid_to_at": "2026-11-13T00:00:00.000000Z",
            "approved_by": "coverage-demo",
            "note": "expired before the survey window",
        },
    )
    run3 = _run(http, base)
    m3 = _latest_map(http, base)
    stale_cells = _cells(http, base, m3["map_version"], state="observed_not_localizable")
    stale_ok = all(
        any(e["code"] == "stale_propagation_parameters" for e in c["error_sources"])
        for c in stale_cells
    )
    out.check(
        "expired profile is frozen and downgrades every observed cell",
        run3["summary"]["propagation_version"] == "v-expired-demo"
        and bool(stale_cells)
        and stale_ok
        and m3["summary"]["localizable_rate"] == 0.0,
        f"{len(stale_cells)} stale cells under {m3['propagation_version']}",
    )

    # --- 4. Coordinate correction and independent map numbering ---------------
    print("\n=== 4. coordinate correction -> new map version, old map frozen ===")
    load_scenario("overlapping", base, truth_dir=tmp / "correction", reset=True)
    _run(http, base)
    first = _latest_map(http, base)
    old_tally = dict(first["summary"]["cells"])
    sample = next(
        c for c in _cells(http, base, first["map_version"])
        if c["state"] != "unobserved"
    )
    old_evidence = _get(
        http, base,
        f"/coverage/maps/{first['map_version']}/cells/{sample['ix']}/{sample['iy']}/evidence",
    )
    old_x = next(
        s["coordinates"]["x"]
        for e in old_evidence["events"]
        for s in e["frozen_context"]["stations"]
        if s["station_id"] == "S1"
    )
    station = next(s for s in _get(http, base, "/stations") if s["id"] == "S1")
    r = requests.put(
        base + "/stations/S1",
        json={
            "id": "S1",
            "name": station["name"],
            "x": station["x"] + 5.0,
            "y": station["y"],
            "hearing_radius_m": station["hearing_radius_m"],
            "active": True,
            "coordinate_correction_reason": "GNSS resurvey after marker disturbance",
        },
        headers={"X-API-Key": args.api_key},
        timeout=120,
    )
    out.check("coordinate correction accepted with reason", r.status_code == 200, str(r.status_code))
    _run(http, base)
    all_maps = {m["map_version"]: m for m in _get(
        http, base, "/coverage/maps", include_superseded="true"
    )}
    old = all_maps[first["map_version"]]
    new_version = old["superseded_by_map_version"]
    replay = _get(
        http, base,
        f"/coverage/maps/{first['map_version']}/cells/{sample['ix']}/{sample['iy']}/evidence",
    )
    replayed_x = next(
        s["coordinates"]["x"]
        for e in replay["events"]
        for s in e["frozen_context"]["stations"]
        if s["station_id"] == "S1"
    )
    out.check(
        "new map independently numbers and supersedes the old one",
        new_version is not None
        and new_version != first["map_version"]
        and "station_coordinate_correction" in (old["supersession_reason"] or ""),
        f"v{first['map_version']} -> v{new_version} ({old['supersession_reason']})",
    )
    out.check(
        "old map replays with its original frozen coordinates and cell tally",
        replayed_x == old_x
        and dict(_latest_map_summary(http, base, first["map_version"])) == old_tally,
        f"frozen S1 x={replayed_x} (was {old_x}), tally unchanged",
    )

    print(
        f"\ncoverage demo finished: {'ALL PASS' if out.failures == 0 else str(out.failures) + ' FAILURE(S)'}"
    )
    return 0 if out.failures == 0 else 1


def _latest_map_summary(http: requests.Session, base: str, version: int) -> dict:
    return _get(http, base, f"/coverage/maps/{version}")["summary"]["cells"]


if __name__ == "__main__":
    raise SystemExit(main())
