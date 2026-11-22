"""Configuration loading. Behavior comes from YAML, not hardcoded assumptions."""

from __future__ import annotations

import datetime as dt
import os
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"


class PlanningSettings(BaseModel):
    discretionary_minutes_per_day: int = Field(180, ge=0)
    default_task_estimate_minutes: int = Field(30, ge=1)
    upcoming_window_days: int = Field(7, ge=1)
    max_backfill_days: int = Field(7, ge=0)


class ControlSettings(BaseModel):
    downgrade_window_days: int = 14
    downgrade_per_task_threshold: int = 2
    downgrade_total_threshold: int = 3
    reschedule_threshold: int = 3
    adherence_window_days: int = 14
    adherence_min_rate: float = Field(0.5, ge=0, le=1)
    adherence_min_samples: int = 3


class CascadeSettings(BaseModel):
    vision_min_years: int = 3
    vision_max_years: int = 5
    goal_min_months: int = 6
    goal_max_months: int = 12
    objective_default_days: int = 91
    objective_max_days: int = 100


class Settings(BaseModel):
    timezone: str = "America/New_York"
    database_path: str = "data/lifeos.db"
    planning: PlanningSettings = PlanningSettings()
    controls: ControlSettings = ControlSettings()
    cascade: CascadeSettings = CascadeSettings()

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def database_url(self) -> str:
        override = os.environ.get("LIFEOS_DB")
        if override:
            return override if "://" in override else f"sqlite:///{override}"
        path = Path(self.database_path)
        if not path.is_absolute():
            path = REPO_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path}"

    def today(self) -> dt.date:
        return dt.datetime.now(self.tz).date()

    def local_date(self, utc_naive: dt.datetime) -> dt.date:
        """Convert a stored naive-UTC timestamp to the user's local calendar date."""
        return utc_naive.replace(tzinfo=dt.timezone.utc).astimezone(self.tz).date()


@lru_cache
def get_settings() -> Settings:
    path = CONFIG_DIR / "settings.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    return Settings(**(data or {}))


def load_pillars() -> list[dict]:
    data = yaml.safe_load((CONFIG_DIR / "pillars.yaml").read_text())
    return data["pillars"]


def utcnow() -> dt.datetime:
    """All timestamps are stored as naive UTC."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
