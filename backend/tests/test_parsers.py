import io
import zipfile
import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.orm_models import Activity, Base, BiometricPoint, User, UserRole
from app.services.normalizer import (
    DuplicateTimestampConflictError,
    persist_normalized_activities,
)
from app.services.xml_parser import TelemetryParseError, parse_apple_health_stream

SAMPLE_APPLE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
  <Record type="HKQuantityTypeIdentifierHeartRate" unit="count/min"
          startDate="2026-11-15 07:05:00 -0600" endDate="2026-11-15 07:05:00 -0600" value="148"/>
  <Record type="HKQuantityTypeIdentifierHeartRate" unit="count/min"
          startDate="2026-11-15 07:15:00 -0600" endDate="2026-11-15 07:15:00 -0600" value="156"/>
  <Record type="HKQuantityTypeIdentifierHeartRate" unit="count/min"
          startDate="2026-11-15 11:00:00 -0600" endDate="2026-11-15 11:00:00 -0600" value="68"/>
  <Record type="HKCategoryTypeIdentifierSleepAnalysis"
          startDate="2026-11-14 23:00:00 -0600" endDate="2026-11-15 06:30:00 -0600"
          value="HKCategoryValueSleepAnalysisAsleepCore"/>
  <Record type="HKQuantityTypeIdentifierHeartRateVariabilitySDNN" unit="ms"
          startDate="2026-11-15 05:00:00 -0600" endDate="2026-11-15 05:00:00 -0600" value="62.4"/>
  <Workout workoutActivityType="HKWorkoutActivityTypeRunning"
           duration="30.0" durationUnit="min"
           totalEnergyBurned="345.6" totalEnergyBurnedUnit="kcal"
           startDate="2026-11-15 07:00:00 -0600" endDate="2026-11-15 07:30:00 -0600"/>
</HealthData>
"""


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine)
    session = TestingSession()
    session.add(
        User(id=1, email="test@omnifit.local", password_hash="hash", role=UserRole.ATHLETE, is_verified=True)
    )
    session.commit()
    yield session
    session.close()


def test_apple_xml_workout_and_sleep_normalization():
    results = parse_apple_health_stream(io.BytesIO(SAMPLE_APPLE_XML), "export.xml")
    assert len(results) == 2

    run = next(r for r in results if r.activity_type == "Run")
    assert run.start_time_utc == datetime(2026, 11, 15, 13, 0, 0, tzinfo=timezone.utc)
    assert run.active_calories_kcal == 345.6
    assert run.avg_heart_rate_bpm == 152.0
    assert len(run.biometrics) == 2

    sleep = next(r for r in results if r.activity_type == "Sleep")
    assert sleep.sleep_duration_minutes == 450.0
    assert sleep.hrv_rmssd_ms == 62.4


def test_apple_xml_zip_archive_streaming():
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("apple_health_export/export.xml", SAMPLE_APPLE_XML)
    zip_buffer.seek(0)

    results = parse_apple_health_stream(zip_buffer, "export.zip")
    assert len(results) == 2


def test_duplicate_timestamp_conflict_prompt_and_overwrite(db_session):
    initial = parse_apple_health_stream(io.BytesIO(SAMPLE_APPLE_XML), "export.xml")
    persist_normalized_activities(db_session, user_id=1, payloads=initial, conflict_mode="prompt")
    assert db_session.query(Activity).count() == 2

    overlapping_run = initial[0].model_copy(
        update={
            "start_time_utc": datetime(2026, 11, 15, 13, 0, 45, tzinfo=timezone.utc),
            "active_calories_kcal": 400.0,
        }
    )

    with pytest.raises(DuplicateTimestampConflictError):
        persist_normalized_activities(db_session, user_id=1, payloads=[overlapping_run], conflict_mode="prompt")

    skip_res = persist_normalized_activities(db_session, user_id=1, payloads=[overlapping_run], conflict_mode="skip")
    assert skip_res["skipped_count"] == 1

    overwrite_res = persist_normalized_activities(db_session, user_id=1, payloads=[overlapping_run], conflict_mode="overwrite")
    assert overwrite_res["overwritten_count"] == 1
    updated_run = db_session.query(Activity).filter(Activity.activity_type == "Run").one()
    assert updated_run.active_calories_kcal == 400.0
    assert db_session.query(BiometricPoint).count() == 2


def test_corrupt_xml_raises_clean_error():
    corrupt_stream = io.BytesIO(b"<HealthData><Workout startDate='bad-xml'")
    with pytest.raises(TelemetryParseError):
        parse_apple_health_stream(corrupt_stream, "corrupt.xml")
