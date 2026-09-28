"""Persistent storage for one appliance: historical runs, pending/resolved
suggestions (which double as ML training examples), and the personalization
model, if one has ever been promoted.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
import homeassistant.util.dt as dt_util

from .const import DOMAIN, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

MAX_RUNS_KEPT = 500
MAX_SUGGESTIONS_KEPT = 200

OPEN_STATUSES = ("pending", "accepted")

@dataclass
class RunRecord:
    start_time: str
    end_time: str
    duration_s: float
    energy_wh: float
    source: str = "live"


@dataclass
class SuggestionRecord:
    target_date: str
    suggested_hour: int
    expected_coverage_pct: float
    confidence: str
    basis: str
    created_at: str
    features: list = field(default_factory=list)
    heuristic_score: float = 0.0
    status: str = "pending"
    actual_start_hour: int | None = None
    resolved_at: str | None = None


class ApplianceStore:
    def __init__(self, hass: HomeAssistant, entry_id: str):
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
        self.runs: list[RunRecord] = []
        self.suggestions: list[SuggestionRecord] = []
        self.backfill_done: bool = False
        self.ml_model: dict | None = None

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if not data:
            return
        self.runs = [RunRecord(**r) for r in data.get("runs", [])]
        self.suggestions = [SuggestionRecord(**s) for s in data.get("suggestions", [])]
        self.backfill_done = data.get("backfill_done", False)
        self.ml_model = data.get("ml_model")

    async def async_save(self) -> None:
        await self._store.async_save({
            "runs": [asdict(r) for r in self.runs[-MAX_RUNS_KEPT:]],
            "suggestions": [asdict(s) for s in self.suggestions[-MAX_SUGGESTIONS_KEPT:]],
            "backfill_done": self.backfill_done,
            "ml_model": self.ml_model,
        })

    def add_run(self, start_time: datetime, end_time: datetime, duration_s: float,
                energy_wh: float, source: str = "live") -> None:
        self.runs.append(RunRecord(
            start_time=dt_util.as_local(start_time).isoformat(),
            end_time=dt_util.as_local(end_time).isoformat(),
            duration_s=duration_s,
            energy_wh=energy_wh,
            source=source,
        ))

    def add_suggestion(self, target_date, suggested_hour, expected_coverage_pct,
                        confidence, basis, features=None, heuristic_score=0.0) -> SuggestionRecord:
        rec = SuggestionRecord(
            target_date=target_date.isoformat(),
            suggested_hour=suggested_hour,
            expected_coverage_pct=expected_coverage_pct,
            confidence=confidence,
            basis=basis,
            created_at=dt_util.now().isoformat(),
            features=features or [],
            heuristic_score=heuristic_score,
        )
        self.suggestions.append(rec)
        return rec

    def expire_stale_suggestions(self, today: date) -> int:
        now_iso = dt_util.now().isoformat()
        expired = 0
        for rec in self.suggestions:
            if rec.status == "pending" and date.fromisoformat(rec.target_date) < today:
                rec.status = "expired"
                rec.resolved_at = now_iso
                expired += 1
        return expired

    def suggestion_for_cycle(self, cycle_date: date) -> SuggestionRecord | None:
        day_iso = cycle_date.isoformat()
        open_recs = [
            r for r in reversed(self.suggestions)
            if r.status in OPEN_STATUSES and r.actual_start_hour is None
        ]
        for rec in open_recs:
            if rec.target_date == day_iso:
                return rec
        for rec in open_recs:
            if rec.target_date > day_iso:
                return rec
        return None

    def latest_pending_suggestion(self) -> SuggestionRecord | None:
        for rec in reversed(self.suggestions):
            if rec.status == "pending":
                return rec
        return None
