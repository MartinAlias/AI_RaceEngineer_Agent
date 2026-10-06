"""Typed records returned by the repository."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import BaseModel


def from_row(model: type[BaseModel], row: sqlite3.Row, json_columns: tuple[str, ...] = ()) -> Any:
    data = dict(row)
    for column in json_columns:
        data[column] = json.loads(data[column])
    return model(**data)


class Bike(BaseModel):
    id: int
    name: str
    make: str | None = None
    model: str | None = None
    year: int | None = None
    notes: str | None = None
    baseline_setup_id: int | None = None
    current_setup_id: int | None = None
    created_at: str

    @property
    def description(self) -> str:
        parts = [str(p) for p in (self.year, self.make, self.model) if p]
        return " ".join(parts)


class Track(BaseModel):
    id: int
    name: str
    length_m: float | None = None
    direction: str | None = None
    notes: str | None = None


class Corner(BaseModel):
    id: int
    track_id: int
    number: int
    name: str | None = None
    type: str | None = None


class Setup(BaseModel):
    id: int
    bike_id: int
    parent_id: int | None = None
    params: dict[str, Any]
    notes: str | None = None
    created_at: str


class Session(BaseModel):
    id: int
    bike_id: int
    track_id: int
    setup_id: int
    date: str
    type: str
    air_temp: float | None = None
    track_temp: float | None = None
    weather: str | None = None
    tire_front: str | None = None
    tire_rear: str | None = None
    tire_front_laps: int | None = None
    tire_rear_laps: int | None = None
    notes: str | None = None
    created_at: str


class Lap(BaseModel):
    id: int
    session_id: int
    lap_number: int
    time_ms: int
    sectors_ms: list[int]
    valid: bool


class Feedback(BaseModel):
    id: int
    session_id: int
    text: str
    end: str | None = None
    phase: str | None = None
    corners: list[str]
    symptom: str | None = None
    severity: int | None = None
    created_at: str


class Change(BaseModel):
    id: int
    bike_id: int
    session_id: int | None = None
    before_setup_id: int
    after_setup_id: int
    deltas: list[dict[str, Any]]
    hypothesis: str | None = None
    watch_for: str | None = None
    reasoning: str | None = None
    proposed_by: str
    created_at: str
