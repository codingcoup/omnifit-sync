import io
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import app
from app.models.orm_models import Base, User, UserRole
from app.services.security import hash_password
from tests.test_parsers import (
    SAMPLE_APPLE_XML,
    SAMPLE_GPX_XML,
    build_sample_garmin_fit_bytes,
)

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)

with TestingSessionLocal() as seed_db:
    seed_db.add(
        User(
            id=1,
            email="api@omnifitsync.com",
            password_hash=hash_password("OmniFit#2026"),
            role=UserRole.ATHLETE,
            is_verified=True,
        )
    )
    seed_db.commit()


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


def test_health_endpoint():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "healthy"


def test_upload_apple_xml_and_conflict_detection():
    res1 = client.post(
        "/api/v1/ingest/upload?user_id=1&conflict_mode=prompt",
        files={"file": ("export.xml", io.BytesIO(SAMPLE_APPLE_XML), "application/xml")},
    )
    assert res1.status_code == 200
    assert res1.json()["inserted_count"] == 2

    res2 = client.post(
        "/api/v1/ingest/upload?user_id=1&conflict_mode=prompt",
        files={"file": ("export.xml", io.BytesIO(SAMPLE_APPLE_XML), "application/xml")},
    )
    assert res2.status_code == 409


def test_upload_garmin_fit_and_gpx_endpoints():
    fit_bytes = build_sample_garmin_fit_bytes()
    fit_res = client.post(
        "/api/v1/ingest/upload?user_id=1&conflict_mode=prompt",
        files={"file": ("morning_run.fit", io.BytesIO(fit_bytes), "application/octet-stream")},
    )
    assert fit_res.status_code == 200
    assert fit_res.json()["source_format"] == "GARMIN_FIT"
    assert fit_res.json()["inserted_count"] == 1

    gpx_res = client.post(
        "/api/v1/ingest/upload?user_id=1&conflict_mode=prompt",
        files={"file": ("tempo_route.gpx", io.BytesIO(SAMPLE_GPX_XML), "application/gpx+xml")},
    )
    assert gpx_res.status_code == 200
    assert gpx_res.json()["source_format"] == "SPATIAL_GPX"
    assert gpx_res.json()["inserted_count"] == 1


def test_week4_auth_register_verify_login_and_athlete_profile():
    bad_pw = client.post(
        "/api/v1/auth/register",
        json={"email": "runner@omnifitsync.com", "password": "weakpassword", "role": "ATHLETE"},
    )
    assert bad_pw.status_code == 422

    reg_res = client.post(
        "/api/v1/auth/register",
        json={"email": "runner@omnifitsync.com", "password": "StrongRun#2026", "role": "ATHLETE"},
    )
    assert reg_res.status_code == 201
    token_6 = reg_res.json()["verification_token_preview"]
    assert len(token_6) == 6

    early_login = client.post(
        "/api/v1/auth/login",
        json={"email": "runner@omnifitsync.com", "password": "StrongRun#2026"},
    )
    assert early_login.status_code == 403

    ver_res = client.post(
        "/api/v1/auth/verify-email",
        json={"email": "runner@omnifitsync.com", "token": token_6},
    )
    assert ver_res.status_code == 200
    assert ver_res.json()["status"] == "verified"

    login_res = client.post(
        "/api/v1/auth/login",
        json={"email": "runner@omnifitsync.com", "password": "StrongRun#2026"},
    )
    assert login_res.status_code == 200
    jwt_token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {jwt_token}"}

    prof_res = client.put(
        "/api/v1/auth/profile",
        headers=headers,
        json={
            "birth_year": 1994,
            "biological_sex": "Male",
            "height_cm": 180.5,
            "weight_kg": 78.2,
            "target_daily_active_calories": 750,
        },
    )
    assert prof_res.status_code == 200
    assert prof_res.json()["target_daily_active_calories"] == 750


def test_week4_secured_upload_endpoints_enforce_jwt():
    unauth = client.post(
        "/api/v1/upload/garmin",
        files={"file": ("workout.fit", io.BytesIO(build_sample_garmin_fit_bytes()), "application/octet-stream")},
    )
    assert unauth.status_code == 401

    login_res = client.post(
        "/api/v1/auth/login",
        json={"email": "api@omnifitsync.com", "password": "OmniFit#2026"},
    )
    assert login_res.status_code == 200
    headers = {"Authorization": f"Bearer {login_res.json()['access_token']}"}

    auth_upload = client.post(
        "/api/v1/upload/garmin?conflict_mode=overwrite",
        headers=headers,
        files={"file": ("workout.fit", io.BytesIO(build_sample_garmin_fit_bytes()), "application/octet-stream")},
    )
    assert auth_upload.status_code == 200
    assert auth_upload.json()["status"] == "success"

