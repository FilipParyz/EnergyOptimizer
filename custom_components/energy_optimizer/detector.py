"""A deliberately simple power-threshold cycle detector.

Unlike a full program-matching detector, this only needs to answer "did a
cycle start, and when did it end" -- not "which program is this". The same
class is used both for live tracking (fed one reading at a time from
state_changed events) and for batch backfill (fed a list of historical
readings in order), so detection logic never diverges between the two.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


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
                 end_threshold_w: float, end_duration_s: float):
        self.start_threshold_w = start_threshold_w
        self.start_duration_s = start_duration_s
        self.end_threshold_w = end_threshold_w
        self.end_duration_s = end_duration_s

        self.state = DetectorState.IDLE
        self._state_since: datetime | None = None
        self._cycle_start: datetime | None = None
        self._energy_wh = 0.0
        self._last_reading: tuple[datetime, float] | None = None

    def process_reading(self, timestamp: datetime, power_w: float) -> CycleResult | None:
        result: CycleResult | None = None

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

        elif self.state == DetectorState.STARTING:
            if power_w < self.start_threshold_w:
                self.state = DetectorState.IDLE
                self._state_since = None
                self._cycle_start = None
            elif (timestamp - self._state_since).total_seconds() >= self.start_duration_s:
                self.state = DetectorState.RUNNING

        elif self.state == DetectorState.RUNNING:
            if power_w < self.end_threshold_w:
                self.state = DetectorState.ENDING
                self._state_since = timestamp

        elif self.state == DetectorState.ENDING:
            if power_w >= self.end_threshold_w:
                self.state = DetectorState.RUNNING
                self._state_since = None
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

        self._last_reading = (timestamp, power_w)
        return result
