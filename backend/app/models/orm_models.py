import enum
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey,
    Enum, Boolean, Index
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class UserRole(str, enum.Enum):
    ATHLETE = "ATHLETE"
    COACH = "COACH"


class SourceFormat(str, enum.Enum):
    GARMIN_FIT = "GARMIN_FIT"
    APPLE_XML = "APPLE_XML"
    SPATIAL_GPX = "SPATIAL_GPX"
    MANUAL_LOG = "MANUAL_LOG"


class User(Base):
    """Stores Athlete and Coach accounts, verification tokens, and biometric baselines (FR1-FR3)."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(Enum(UserRole), nullable=False, default=UserRole.ATHLETE)
    is_verified = Column(Boolean, default=False, nullable=False)
    verification_token = Column(String(6), nullable=True)
    token_expires_at = Column(DateTime(timezone=True), nullable=True)

    # Athlete Introductory Fitness Profile (FR3 & FR19)
    birth_year = Column(Integer, nullable=True)
    biological_sex = Column(String(20), nullable=True)
    height_cm = Column(Float, nullable=True)
    weight_kg = Column(Float, nullable=True)
    target_daily_active_calories = Column(Integer, default=600, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    activities = relationship("Activity", back_populates="athlete", cascade="all, delete-orphan")


class CoachClientLink(Base):
    """Manages authorized Coach-to-Athlete data access relationships (FR14-FR19)."""
    __tablename__ = "coach_client_links"

    id = Column(Integer, primary_key=True, index=True)
    coach_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    athlete_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    is_approved = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Activity(Base):
    """Stores normalized workout or sleep summary sessions across all ecosystems (FR7-FR9, FR20)."""
    __tablename__ = "activities"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    source_format = Column(Enum(SourceFormat), nullable=False)
    activity_type = Column(String(50), nullable=False)
    start_time_utc = Column(DateTime(timezone=True), nullable=False, index=True)
    end_time_utc = Column(DateTime(timezone=True), nullable=False)
    duration_minutes = Column(Float, nullable=False)

    # Normalized Aggregate Metrics (FR7, FR8, FR11, FR12)
    active_calories_kcal = Column(Float, default=0.0, nullable=False)
    avg_heart_rate_bpm = Column(Float, nullable=True)
    sleep_duration_minutes = Column(Float, default=0.0, nullable=False)
    hrv_rmssd_ms = Column(Float, nullable=True)
    raw_backup_gcs_uri = Column(String(512), nullable=True)

    athlete = relationship("User", back_populates="activities")
    biometrics = relationship("BiometricPoint", back_populates="activity", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_activities_user_start_utc", "user_id", "start_time_utc"),
    )


class BiometricPoint(Base):
    """Stores high-resolution time-series telemetry for Coach Drill-Down Views (FR7, FR17, NFR5)."""
    __tablename__ = "biometric_points"

    id = Column(Integer, primary_key=True)
    activity_id = Column(Integer, ForeignKey("activities.id", ondelete="CASCADE"), nullable=False)
    timestamp_utc = Column(DateTime(timezone=True), nullable=False)
    heart_rate_bpm = Column(Integer, nullable=True)
    latitude_deg = Column(Float, nullable=True)
    longitude_deg = Column(Float, nullable=True)
    instant_calories_kcal = Column(Float, nullable=True)

    activity = relationship("Activity", back_populates="biometrics")

    __table_args__ = (
        Index("ix_biometrics_activity_timestamp", "activity_id", "timestamp_utc"),
    )
