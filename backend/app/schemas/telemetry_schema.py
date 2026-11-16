from datetime import datetime, timezone
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator

from app.models.orm_models import SourceFormat

ConflictMode = Literal["prompt", "overwrite", "skip"]


class BiometricPointSchema(BaseModel):
    timestamp_utc: datetime
    heart_rate_bpm: Optional[int] = None
    latitude_deg: Optional[float] = None
    longitude_deg: Optional[float] = None
    instant_calories_kcal: Optional[float] = None

    @field_validator("timestamp_utc")
    @classmethod
    def ensure_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class NormalizedActivitySchema(BaseModel):
    """Unified JSON representation across .XML, .FIT, .GPX, and Manual Logs (FR8)."""
    source_format: SourceFormat
    activity_type: str
    start_time_utc: datetime
    end_time_utc: datetime
    duration_minutes: float = Field(..., gt=0)
    active_calories_kcal: float = Field(default=0.0, ge=0.0)
    avg_heart_rate_bpm: Optional[float] = None
    sleep_duration_minutes: float = Field(default=0.0, ge=0.0)
    hrv_rmssd_ms: Optional[float] = None
    raw_backup_gcs_uri: Optional[str] = None
    biometrics: list[BiometricPointSchema] = Field(default_factory=list)

    @field_validator("start_time_utc", "end_time_utc")
    @classmethod
    def ensure_activity_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class ManualActivityInput(BaseModel):
    """Validates Manual Log Fallback entries (FR20)."""
    user_id: int
    activity_type: str = Field(..., pattern="^(Run|Cycle|Swim|Strength|Walk)$")
    start_time_utc: datetime
    duration_minutes: float = Field(..., gt=0, le=1440)
    estimated_avg_hr_bpm: int = Field(..., ge=30, le=240)
    estimated_active_calories_kcal: float = Field(..., ge=0, le=15000)
