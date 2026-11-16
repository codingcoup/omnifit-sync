from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import engine, SessionLocal
from app.models.orm_models import Base, User, UserRole
from app.routers import ingest


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    if settings.seed_dev_user:
        with SessionLocal() as db:
            if not db.query(User).filter(User.id == 1).first():
                db.add(
                    User(
                        id=1,
                        email="athlete.dev@omnifitsync.local",
                        password_hash="dev_placeholder_hash",
                        role=UserRole.ATHLETE,
                        is_verified=True,
                    )
                )
                db.commit()
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ingest.router)


@app.get("/health", tags=["System"])
def health_check():
    return {"status": "healthy", "service": settings.app_name}
