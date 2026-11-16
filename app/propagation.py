"""Approved propagation parameters and their validity intervals.

A processing run must freeze *one* approved propagation profile, just as it
freezes one calibration snapshot.  Profiles have reference-time validity
intervals: when the profile used for an event does not cover the event's
window, the event (and every coverage cell it touches) is flagged
``stale_propagation_parameters`` instead of being silently recomputed with
newer parameters.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import PropagationProfile
from .timeutil import now_us

DEFAULT_VERSION = "default-v1"


@dataclass
class ResolvedProfile:
    profile: PropagationProfile
    fallback_stale: bool  # True when no profile was valid at the cutoff


def ensure_default_profile(db: Session, cfg: Settings) -> PropagationProfile:
    """Return the catch-all profile, creating it on first use.

    The default profile is valid for all reference time and carries the
    configured constant propagation speed, so deployments that never approve
    an explicit profile keep the original single-speed behaviour.
    """
    existing = db.execute(
        select(PropagationProfile).where(
            PropagationProfile.version == DEFAULT_VERSION
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = PropagationProfile(
        version=DEFAULT_VERSION,
        speed_mps=cfg.propagation_speed_mps,
        valid_from_us=0,
        valid_to_us=None,
        approved=True,
        approved_by="system",
        note="catch-all profile seeded from PROPAGATION_SPEED_MPS; valid at all times",
        created_at_us=now_us(),
    )
    db.add(row)
    db.flush()
    return row


def resolve_profile_for_run(
    db: Session, cutoff_us: int, cfg: Settings
) -> ResolvedProfile:
    """Pick the profile approved at the run's cutoff.

    Preference order: the newest approved profile whose validity interval
    contains the cutoff; otherwise the newest approved profile overall (the
    run then proceeds but every event is marked stale); otherwise the
    seeded catch-all profile.
    """
    rows = list(
        db.execute(
            select(PropagationProfile)
            .where(PropagationProfile.approved.is_(True))
            .order_by(PropagationProfile.id)
        ).scalars()
    )
    covering = [
        p
        for p in rows
        if p.valid_from_us <= cutoff_us
        and (p.valid_to_us is None or p.valid_to_us > cutoff_us)
    ]
    if covering:
        return ResolvedProfile(profile=covering[-1], fallback_stale=False)
    if rows:
        return ResolvedProfile(profile=rows[-1], fallback_stale=True)
    return ResolvedProfile(
        profile=ensure_default_profile(db, cfg), fallback_stale=False
    )


def profile_is_stale(profile: PropagationProfile, window_end_us: int) -> bool:
    """True when the profile does not stay valid through the event window."""
    return (
        not profile.approved
        or profile.valid_to_us is not None
        and profile.valid_to_us < window_end_us
    )
