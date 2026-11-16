"""Analytic propagation of arrival-time covariance into position uncertainty.

Two uncertainty treatments are supported by the service, and this module is
the first one:

1. **Analytic (here).**  The arrival-time model
   ``t_i = emission + d_i(x, y) / v`` is linearized at the grid solution.
   Its Jacobian and the per-station arrival covariance are propagated into a
   2-D position covariance ``Sigma = (J^T C^-1 J)^-1``.  Eigen-decomposition
   gives uncertainty ellipse axes, an axis-aligned range and a condition
   number.  It is cheap, so it can be evaluated for every event of every
   coverage map, but it is a *local Gaussian* approximation: skewed errors or
   geometric degeneracy make it understate the true range, which is why the
   condition number and array angle are checked explicitly and why the
   coarse-to-fine grid regions (``app.localize``) always ship alongside it.

2. **Feasible-region grid (``app.localize``).**  Re-evaluates the full
   non-linear arrival model on a grid and returns every disconnected
   feasible region.  Conservative, but more expensive and stored per event.

A coverage publication always carries both figures and never replaces an
error range with a station count.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .localize import StationPoint


@dataclass
class AnalyticUncertainty:
    sigma_xx_m2: float
    sigma_yy_m2: float
    sigma_xy_m2: float
    condition_number: float
    semi_major_m: float
    semi_minor_m: float
    rotation_deg: float
    range_x_m: float
    range_y_m: float
    degenerate: bool
    reason: str | None

    def as_dict(self, sigma_k: float) -> dict:
        def num(value: float):
            # JSON (and therefore JSONB) cannot encode +/-infinity; a null
            # numeric field together with degenerate=true carries the meaning.
            return round(value, 6) if math.isfinite(value) else None

        return {
            "method": "analytic_jacobian_covariance",
            "sigma_k": sigma_k,
            "covariance_m2": {
                "xx": num(self.sigma_xx_m2),
                "yy": num(self.sigma_yy_m2),
                "xy": num(self.sigma_xy_m2),
            },
            "condition_number": num(self.condition_number),
            "ellipse_semi_axes_m": {
                "major": num(self.semi_major_m * sigma_k),
                "minor": num(self.semi_minor_m * sigma_k),
            },
            "rotation_deg": num(self.rotation_deg),
            # k-sigma axis-aligned span, i.e. full width of the ellipse box.
            "range_m": {
                "x": num(self.range_x_m),
                "y": num(self.range_y_m),
            },
            "degenerate": self.degenerate,
            "reason": self.reason,
        }


def analytic_uncertainty(
    stations: list[StationPoint],
    uncertainties_us: list[float],
    x: float,
    y: float,
    speed_mps: float,
    *,
    max_condition: float,
) -> AnalyticUncertainty:
    """Propagate arrival-time variances to a position covariance at ``(x, y)``.

    The unknown emission time is profiled out: rows of the design matrix are
    unit direction vectors from the source to each station (the spatial
    gradient of the propagation delay), weighted by the inverse arrival
    variance.  With fewer than three stations the 2-D normal matrix is rank
    deficient, which is reported as a degenerate (but still finite) result
    rather than raised, so callers can publish an area/direction conclusion.
    """
    n = len(stations)
    # Accumulate J^T C^-1 J for the two spatial unknowns.  Arrival variances
    # are in (microseconds)^2 and delays are in microseconds, so the gradient
    # of delay w.r.t. position is (us/m): unit vector * 1e6 / v.
    g = 1.0e6 / speed_mps
    sxx = syy = sxy = 0.0
    for st, u_us in zip(stations, uncertainties_us):
        dx = st.x - x
        dy = st.y - y
        dist = math.hypot(dx, dy)
        if dist <= 1e-9:
            # A station coincident with the candidate gives no directional
            # information; skip it instead of dividing by zero.
            continue
        ux = dx / dist
        uy = dy / dist
        var_us2 = max(float(u_us), 1.0) ** 2
        w = (g * g) / var_us2
        sxx += w * ux * ux
        sxy += w * ux * uy
        syy += w * uy * uy

    det = sxx * syy - sxy * sxy
    if n < 3 or det <= 1e-30:
        # Rank deficient: mirror/line ambiguity.  Report an effectively
        # infinite condition with whatever finite diagonal exists.
        return AnalyticUncertainty(
            sigma_xx_m2=math.inf,
            sigma_yy_m2=math.inf,
            sigma_xy_m2=0.0,
            condition_number=math.inf,
            semi_major_m=math.inf,
            semi_minor_m=0.0,
            rotation_deg=0.0,
            range_x_m=math.inf,
            range_y_m=math.inf,
            degenerate=True,
            reason=(
                "rank_deficient_normal_matrix: fewer than three effective "
                "stations or all stations share one bearing"
            ),
        )

    inv_det = 1.0 / det
    c_xx = syy * inv_det
    c_yy = sxx * inv_det
    c_xy = -sxy * inv_det

    # Eigen-decomposition of the symmetric 2x2 covariance.
    tr = c_xx + c_yy
    disc = math.sqrt(max(0.0, ((c_xx - c_yy) / 2.0) ** 2 + c_xy * c_xy))
    lambda1 = tr / 2.0 + disc
    lambda2 = tr / 2.0 - disc
    lambda2 = max(lambda2, 1e-12)
    semi_major = math.sqrt(max(lambda1, 0.0))
    semi_minor = math.sqrt(lambda2)
    condition = semi_major / semi_minor
    # Rotation of the major axis; atan2 formula for a symmetric matrix.
    rotation = 0.5 * math.degrees(math.atan2(2.0 * c_xy, c_xx - c_yy))
    # Axis-aligned k-sigma box needs rotated ellipse extrema.
    theta = math.radians(rotation)
    range_x = 2.0 * math.sqrt(
        (semi_major * math.cos(theta)) ** 2 + (semi_minor * math.sin(theta)) ** 2
    )
    range_y = 2.0 * math.sqrt(
        (semi_major * math.sin(theta)) ** 2 + (semi_minor * math.cos(theta)) ** 2
    )
    degenerate = condition > max_condition or not math.isfinite(condition)
    return AnalyticUncertainty(
        sigma_xx_m2=c_xx,
        sigma_yy_m2=c_yy,
        sigma_xy_m2=c_xy,
        condition_number=condition,
        semi_major_m=semi_major,
        semi_minor_m=semi_minor,
        rotation_deg=rotation,
        range_x_m=range_x,
        range_y_m=range_y,
        degenerate=degenerate,
        reason=(
            f"condition_number {condition:.1f} exceeds limit {max_condition:g}; "
            "analytic range likely understates skewed/degenerate error"
            if degenerate
            else None
        ),
    )


def min_subtended_angle_deg(
    stations: list[StationPoint], x: float, y: float
) -> tuple[float, tuple[str, str] | None]:
    """Smallest angle between source-to-station bearing lines, in degrees.

    A small minimum angle means two stations lie on nearly the same bearing
    from the source, so their TDOA contributes almost no cross-range
    information.  Collinear arrays whose source sits on the station line
    yield 0 degrees (all bearings are 0/180).
    """
    bearings: list[tuple[str, float]] = []
    for st in stations:
        angle = math.atan2(st.y - y, st.x - x)
        bearings.append((st.id, angle))
    best = 180.0
    pair: tuple[str, str] | None = None
    for i in range(len(bearings)):
        for j in range(i + 1, len(bearings)):
            # Bearing *lines*, not rays: theta and theta+180 are the same
            # line, so fold the difference into [0, 90].
            delta = abs(math.degrees(bearings[i][1] - bearings[j][1])) % 180.0
            delta = min(delta, 180.0 - delta)
            if delta < best:
                best = delta
                pair = (bearings[i][0], bearings[j][0])
    return best, pair


@dataclass
class ReliabilityVerdict:
    reliable: bool
    conclusion_level: str  # point | area | direction
    reasons: list[str]
    angle_warnings: list[str]
    analytic: AnalyticUncertainty
    min_angle_deg: float
    min_angle_pair: tuple[str, str] | None
    angle_threshold_deg: float

    def as_dict(self, sigma_k: float) -> dict:
        return {
            "reliable": self.reliable,
            "conclusion_level": self.conclusion_level,
            "reasons": self.reasons,
            "angle_warnings": self.angle_warnings,
            "angle_threshold_deg": self.angle_threshold_deg,
            "min_subtended_angle_deg": round(self.min_angle_deg, 3),
            "min_angle_pair": list(self.min_angle_pair) if self.min_angle_pair else None,
            "analytic_uncertainty": self.analytic.as_dict(sigma_k),
        }


def classify_reliability(
    stations: list[StationPoint],
    uncertainties_us: list[float],
    x: float,
    y: float,
    speed_mps: float,
    *,
    cfg,
    geometry_collinear: bool,
    region_count: int,
    region_extent_m: float,
    propagation_stale: bool,
) -> ReliabilityVerdict:
    """Decide whether an event supports a point, area or direction conclusion.

    Hard rules, in order of evidence: fewer than three effective stations or
    collinear geometry can only give a direction-level result; multiple grid
    regions or an over-large feasible region forbid a single coordinate; a
    degenerate analytic covariance forbids calling the position reliable;
    stale propagation parameters downgrade every conclusion to area level
    even when the geometry itself is sound.

    A single tight bearing pair is not enough on its own: a four-station
    array with one close pair still has other well-separated pairs, and the
    covariance condition number is the quantitative measure of the whole
    array.  The tight pair is always recorded as an angle warning (and, when
    the covariance is also degenerate, promoted to a hard reason).
    """
    reasons: list[str] = []
    angle_warnings: list[str] = []
    angle, pair = min_subtended_angle_deg(stations, x, y)
    analytic = analytic_uncertainty(
        stations,
        uncertainties_us,
        x,
        y,
        speed_mps,
        max_condition=cfg.covariance_max_condition,
    )
    threshold = cfg.min_localization_angle_deg

    effective = len(stations)
    if effective < 3:
        reasons.append(
            f"fewer_than_three_effective_stations: {effective} station(s); "
            "only a direction/area-level conclusion is allowed"
        )
    if angle < threshold:
        message = (
            f"array_angle_too_small: minimum subtended angle {angle:.2f} deg "
            f"< {threshold:g} deg between {pair}"
        )
        if analytic.degenerate:
            # The tight pair is what makes the inversion ill-conditioned.
            reasons.append(message)
        else:
            angle_warnings.append(message)
    if geometry_collinear:
        reasons.append(
            "stations_collinear: mirror positions are timing-indistinguishable"
        )
    if analytic.degenerate:
        reasons.append(analytic.reason or "analytic_covariance_degenerate")
    if region_count > 1:
        reasons.append(
            f"multiple_disconnected_feasible_regions: {region_count} regions"
        )
    if region_extent_m > cfg.definite_region_max_extent_m:
        reasons.append(
            f"feasible_region_extent {region_extent_m:.1f} m exceeds "
            f"{cfg.definite_region_max_extent_m:g} m definite limit"
        )
    if propagation_stale:
        reasons.append(
            "stale_propagation_parameters: event was frozen with a propagation "
            "profile that is no longer approved for its period"
        )

    reliable = not reasons
    if effective < 3:
        # Under-determined array: no position, only a bearing/direction hint.
        level = "direction"
    elif reliable:
        level = "point"
    else:
        # Enough stations, but geometry/region/propagation limits confine the
        # conclusion to one or more candidate areas.
        level = "area"
    return ReliabilityVerdict(
        reliable=reliable,
        conclusion_level=level,
        reasons=reasons,
        angle_warnings=angle_warnings,
        analytic=analytic,
        min_angle_deg=angle,
        min_angle_pair=pair,
        angle_threshold_deg=threshold,
    )
