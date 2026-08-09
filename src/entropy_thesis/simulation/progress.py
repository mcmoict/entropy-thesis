from __future__ import annotations

from dataclasses import dataclass
import sys
import time
from typing import TextIO


def format_duration(seconds: float | None) -> str:
    """Format seconds as HH:MM:SS for stable CLI progress output."""

    if seconds is None or seconds < 0:
        return "--:--:--"
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


@dataclass
class ConsoleProgress:
    """Small reusable wall-clock progress/ETA reporter for long CLI phases.

    ``fraction`` is the caller's best estimate of whole-program completion in
    the range 0..1.  ETA is intentionally labelled as an estimate because the
    simulation and CSV-writing phases do not have identical per-unit costs.
    """

    stream: TextIO = sys.stdout
    started_at: float | None = None
    updates: int = 0

    def start(self, message: str) -> None:
        self.started_at = time.perf_counter()
        self.updates = 0
        self._print(f"[START] {message}")

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        return max(0.0, time.perf_counter() - self.started_at)

    def estimate_eta(self, fraction: float) -> float | None:
        if fraction <= 0.0 or fraction >= 1.0:
            return 0.0 if fraction >= 1.0 else None
        elapsed = self.elapsed_seconds
        return elapsed * (1.0 - fraction) / fraction

    def report(
        self,
        fraction: float,
        message: str,
        *,
        current: str | None = None,
    ) -> None:
        if self.started_at is None:
            self.start("Processing")

        fraction = min(max(float(fraction), 0.0), 1.0)
        self.updates += 1
        dots = "." * (1 + ((self.updates - 1) % 3))
        percent = fraction * 100.0
        eta = self.estimate_eta(fraction)

        parts = [
            f"[RUN{dots:<3}] {percent:6.1f}%",
            message,
        ]
        if current:
            parts.append(f"current={current}")
        parts.append(f"elapsed={format_duration(self.elapsed_seconds)}")
        parts.append(f"ETA={format_duration(eta)}")
        self._print(" | ".join(parts))

    def complete(self, message: str = "Processing completed") -> None:
        self.report(1.0, message)
        self._print(f"[DONE] Total execution time: {format_duration(self.elapsed_seconds)}")

    def _print(self, text: str) -> None:
        print(text, file=self.stream, flush=True)
