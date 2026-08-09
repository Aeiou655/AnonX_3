"""Playback transition capabilities.

PyTgCalls/ntgcalls exposes one active input per group call in this project.
Therefore gapless preloading is supported, while overlapping crossfade is
explicitly rejected instead of pretending a timed hard switch is a crossfade.
"""

from __future__ import annotations

from dataclasses import dataclass

from AnonX_3 import config


@dataclass(frozen=True)
class TransitionPlan:
    gapless: bool
    crossfade: bool
    seconds: float
    reason: str


def select_transition_plan(*, next_ready: bool, overlap_capable: bool = False) -> TransitionPlan:
    gapless = bool(getattr(config, "GAPLESS_PLAYBACK_ENABLED", True) and next_ready)
    requested = bool(getattr(config, "CROSSFADE_ENABLED", False))
    if requested and overlap_capable:
        return TransitionPlan(
            gapless=gapless,
            crossfade=True,
            seconds=max(0.25, float(getattr(config, "CROSSFADE_SECONDS", 2.5))),
            reason="overlap_capable",
        )
    return TransitionPlan(
        gapless=gapless,
        crossfade=False,
        seconds=0.0,
        reason=(
            "voice_engine_no_overlap_capability"
            if requested
            else "crossfade_feature_disabled"
        ),
    )
