"""A deliberately simple power-threshold cycle detector.

Unlike a full program-matching detector, this only needs to answer "did a
cycle start, and when did it end" -- not "which program is this". The same
class is used both for live tracking (fed one reading at a time from
state_changed events) and for batch backfill (fed a list of historical
readings in order), so detection logic never diverges between the two.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import logging

_LOGGER = logging.getLogger(__name__)


class DetectorState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    ENDING = "ending"


@dataclass
class CycleResult:
    start_time: datetime
    end_time: datetime
    duration_s: float
    energy_wh: float


class CycleDetector:
    def __init__(self, start_threshold_w: float, start_duration_s: float,
                 end_threshold_w: float, end_duration_s: float, name: str | None = None):
        self.start_threshold_w = start_threshold_w
        self.start_duration_s = start_duration_s
        self.end_threshold_w = end_threshold_w
        self.end_duration_s = end_duration_s
        self.name = name

        self.state = DetectorState.IDLE
        self._state_since: datetime | None = None
        self._cycle_start: datetime | None = None
        self._energy_wh = 0.0
        self._last_reading: tuple[datetime, float] | None = None

    def next_deadline(self) -> datetime | None:
        if self._state_since is None:
            return None
        if self.state == DetectorState.STARTING:
            return self._state_since + timedelta(seconds=self.start_duration_s)
        if self.state == DetectorState.ENDING:
            return self._state_since + timedelta(seconds=self.end_duration_s)
        return None

    def _log_transition(self, old: DetectorState, timestamp: datetime, power_w: float, note: str) -> None:
        if self.name is not None and old != self.state:
            _LOGGER.debug(
                "[%s] Detector %s -> %s at %s (%.0f W): %s",
                self.name, old.value, self.state.value, timestamp.isoformat(), power_w, note,
            )

    def process_reading(self, timestamp: datetime, power_w: float) -> CycleResult | None:
        result: CycleResult | None = None
        old_state = self.state
        note = ""

        if self._last_reading is not None and self.state in (
            DetectorState.STARTING, DetectorState.RUNNING, DetectorState.ENDING,
        ):
            prev_ts, prev_w = self._last_reading
            dt_h = (timestamp - prev_ts).total_seconds() / 3600.0
            if 0 < dt_h < 1:  # ignore absurd gaps, e.g. a Home Assistant restart
                self._energy_wh += (prev_w + power_w) / 2 * dt_h

        if self.state == DetectorState.IDLE:
            if power_w >= self.start_threshold_w:
                self.state = DetectorState.STARTING
                self._state_since = timestamp
                self._cycle_start = timestamp
                self._energy_wh = 0.0
                note = f">= start threshold {self.start_threshold_w} W, confirming for {self.start_duration_s} s"

        elif self.state == DetectorState.STARTING:
            if power_w < self.start_threshold_w:
                self.state = DetectorState.IDLE
                self._state_since = None
                self._cycle_start = None
                note = "dropped below start threshold before confirmation -- false start"
            elif (timestamp - self._state_since).total_seconds() >= self.start_duration_s:
                self.state = DetectorState.RUNNING
                note = f"cycle confirmed, started at {self._cycle_start.isoformat()}"

        elif self.state == DetectorState.RUNNING:
            if power_w < self.end_threshold_w:
                self.state = DetectorState.ENDING
                self._state_since = timestamp
                note = f"< end threshold {self.end_threshold_w} W, confirming for {self.end_duration_s} s"

        elif self.state == DetectorState.ENDING:
            if power_w >= self.end_threshold_w:
                self.state = DetectorState.RUNNING
                self._state_since = None
                note = "power came back -- pause within the same cycle"
            elif (timestamp - self._state_since).total_seconds() >= self.end_duration_s:
                end_time = self._state_since  # power actually dropped here, not "now"
                duration_s = (end_time - self._cycle_start).total_seconds()
                result = CycleResult(
                    start_time=self._cycle_start,
                    end_time=end_time,
                    duration_s=duration_s,
                    energy_wh=self._energy_wh,
                )
                self.state = DetectorState.IDLE
                self._state_since = None
                self._cycle_start = None
                self._energy_wh = 0.0
                note = f"cycle ended: {duration_s / 60:.0f} min, {result.energy_wh:.0f} Wh"

        self._last_reading = (timestamp, power_w)
        self._log_transition(old_state, timestamp, power_w, note)
        return result
