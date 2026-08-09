import time
from dataclasses import dataclass, field

from AnonX_3 import logger


@dataclass
class PlaybackTrace:
    """Phase-level latency trace for /play and /vplay critical path."""

    command: str
    video: bool = False
    chat_load: int | None = None
    tier: str | None = None
    mode: str | None = None
    started: float = field(default_factory=time.perf_counter)
    last: float = field(default_factory=time.perf_counter)
    phases: dict[str, float] = field(default_factory=dict)
    _finished: bool = field(default=False, repr=False)

    def set_meta(
        self,
        *,
        video: bool | None = None,
        chat_load: int | None = None,
        tier: str | None = None,
        mode: str | None = None,
    ) -> None:
        if video is not None:
            self.video = bool(video)
        if chat_load is not None:
            self.chat_load = int(chat_load)
        if tier is not None:
            self.tier = str(tier)
        if mode is not None:
            self.mode = str(mode)

    def mark(self, phase: str) -> None:
        now = time.perf_counter()
        self.phases[phase] = (now - self.started) * 1000.0
        self.last = now

    def finish(self, outcome: str = "ready") -> None:
        if self._finished:
            return
        self._finished = True
        self.mark(outcome)
        ordered = " ".join(
            f"{name}={value:.0f}ms" for name, value in self.phases.items()
        )
        total = self.phases.get(outcome, (time.perf_counter() - self.started) * 1000.0)
        logger.info(
            "playback_trace command=%s video=%s chat_load=%s tier=%s mode=%s total_ms=%.0f %s",
            self.command,
            1 if self.video else 0,
            self.chat_load if self.chat_load is not None else -1,
            self.tier or "n/a",
            self.mode or "n/a",
            total,
            ordered,
        )
