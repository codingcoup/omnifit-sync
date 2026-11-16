import io
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import app
from app.models.orm_models import Base, User, UserRole
from tests.test_parsers import SAMPLE_APPLE_XML

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)

with TestingSessionLocal() as seed_db:
    seed_db.add(
        User(id=1, email="api@omnifit.local", password_hash="hash", role=UserRole.ATHLETE, is_verified=True)
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
