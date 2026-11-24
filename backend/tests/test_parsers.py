import io
import struct
import zipfile
import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.orm_models import Activity, Base, BiometricPoint, SourceFormat, User, UserRole
from app.schemas.telemetry_schema import BiometricPointSchema
from app.services.fit_parser import parse_garmin_fit_stream
from app.services.gpx_parser import parse_spatial_gpx_stream
from app.services.normalizer import (
    DuplicateTimestampConflictError,
    persist_normalized_activities,
)
from app.services.spatial_utils import compress_spatial_biometrics
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

SAMPLE_GPX_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="OmniFitSyncTest"
     xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
  <trk>
    <name>Morning Tempo Run</name>
    <type>running</type>
    <trkseg>
      <trkpt lat="31.117100" lon="-97.727800">
        <ele>250.0</ele>
        <time>2026-11-18T12:00:00Z</time>
        <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>142</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions>
      </trkpt>
      <trkpt lat="31.118500" lon="-97.726000">
        <ele>252.0</ele>
        <time>2026-11-18T12:10:00Z</time>
        <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>154</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions>
      </trkpt>
      <trkpt lat="31.121000" lon="-97.722000">
        <ele>255.0</ele>
        <time>2026-11-18T12:20:00Z</time>
        <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>160</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions>
      </trkpt>
    </trkseg>
  </trk>
</gpx>
"""


def build_sample_garmin_fit_bytes() -> bytes:
    """Constructs a binary ANT/Garmin .FIT payload containing 2 GPS/HR records and 1 running session."""
    fit_epoch = datetime(1989, 12, 31, 0, 0, 0, tzinfo=timezone.utc)
    crc_table = (
        0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
        0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
    )

    def calc_crc(buf: bytes) -> int:
        crc = 0
        for b in buf:
            tmp = crc_table[crc & 0xF]
            crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ crc_table[b & 0xF]
            tmp = crc_table[crc & 0xF]
            crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ crc_table[(b >> 4) & 0xF]
        return crc

    def to_sc(deg: float) -> int:
        return int(round(deg * (2**31 / 180.0)))

    def_record = struct.pack(
        "<BBBHB BBB BBB BBB BBB",
        0x40, 0, 0, 20, 4,
        253, 4, 0x86,
        0, 4, 0x85,
        1, 4, 0x85,
        3, 1, 0x02,
    )
    def_session = struct.pack(
        "<BBBHB BBB BBB BBB BBB BBB BBB",
        0x41, 0, 0, 18, 6,
        253, 4, 0x86,
        2, 4, 0x86,
        7, 4, 0x86,
        11, 2, 0x84,
        16, 1, 0x02,
        5, 1, 0x00,
    )

    t0 = int((datetime(2026, 11, 20, 14, 0, 0, tzinfo=timezone.utc) - fit_epoch).total_seconds())
    rec1 = struct.pack("<BIiiB", 0x00, t0, to_sc(31.1171), to_sc(-97.7278), 146)
    rec2 = struct.pack("<BIiiB", 0x00, t0 + 900, to_sc(31.1205), to_sc(-97.7210), 158)
    sess = struct.pack("<BIIIHBB", 0x01, t0 + 1800, t0, 1800 * 1000, 380, 152, 1)

    body = def_record + rec1 + rec2 + def_session + sess
    header_12 = struct.pack("<BBHI4s", 14, 0x10, 2084, len(body), b".FIT")
    header = header_12 + struct.pack("<H", 0)
    return header + body + struct.pack("<H", calc_crc(header + body))


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


def test_garmin_fit_binary_normalization():
    fit_bytes = build_sample_garmin_fit_bytes()
    results = parse_garmin_fit_stream(io.BytesIO(fit_bytes))
    assert len(results) == 1

    workout = results[0]
    assert workout.source_format == SourceFormat.GARMIN_FIT
    assert workout.activity_type == "Run"
    assert workout.duration_minutes == 30.0
    assert workout.active_calories_kcal == 380.0
    assert workout.avg_heart_rate_bpm == 152.0
    assert len(workout.biometrics) == 2
    assert abs(workout.biometrics[0].latitude_deg - 31.1171) < 0.001
    assert abs(workout.biometrics[0].longitude_deg - (-97.7278)) < 0.001


def test_corrupt_fit_raises_clean_error():
    with pytest.raises(TelemetryParseError):
        parse_garmin_fit_stream(io.BytesIO(b"NOT_A_VALID_FIT_HEADER"))


def test_spatial_gpx_parsing_and_hr_extraction():
    results = parse_spatial_gpx_stream(io.BytesIO(SAMPLE_GPX_XML))
    assert len(results) == 1

    route = results[0]
    assert route.source_format == SourceFormat.SPATIAL_GPX
    assert route.activity_type == "Run"
    assert route.duration_minutes == 20.0
    assert route.avg_heart_rate_bpm == 152.0
    assert route.active_calories_kcal > 100.0
    assert len(route.biometrics) >= 2


def test_rdp_spatial_coordinate_compression():
    base_ts = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
    collinear_pts = [
        BiometricPointSchema(
            timestamp_utc=base_ts,
            latitude_deg=31.1000 + i * 0.0001,
            longitude_deg=-97.7000 + i * 0.0001,
            heart_rate_bpm=145,
        )
        for i in range(10)
    ]
    compressed = compress_spatial_biometrics(collinear_pts, epsilon_meters=5.0)
    assert len(compressed) == 2


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
