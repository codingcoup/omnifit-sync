import os
from datetime import timedelta
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.orm_models import SourceFormat, User
from app.schemas.telemetry_schema import (
    ConflictMode,
    ManualActivityInput,
    NormalizedActivitySchema,
)
from app.services.fit_parser import parse_garmin_fit_stream
from app.services.gpx_parser import parse_spatial_gpx_stream
from app.services.normalizer import (
    DuplicateTimestampConflictError,
    persist_normalized_activities,
)
from app.services.security import get_current_user
from app.services.xml_parser import TelemetryParseError, parse_apple_health_stream

router = APIRouter(prefix="/api/v1/ingest", tags=["Data Ingestion Pipeline"])
secured_upload_router = APIRouter(prefix="/api/v1/upload", tags=["Secured Provider Uploads (Week 4)"])

MAX_FIT_BYTES = 50 * 1024 * 1024        # 50 MB limit (FR4)
MAX_GPX_BYTES = 25 * 1024 * 1024        # 25 MB limit (FR5)
MAX_XML_ZIP_BYTES = 1024 * 1024 * 1024  # 1 GB limit (FR6)


def _get_stream_size(upload: UploadFile) -> int:
    upload.file.seek(0, os.SEEK_END)
    size = upload.file.tell()
    upload.file.seek(0)
    return size


def _process_telemetry_upload(
    db: Session,
    user_id: int,
    upload: UploadFile,
    conflict_mode: ConflictMode,
    allowed_extensions: tuple[str, ...] | None = None,
) -> dict:
    filename = (upload.filename or "").lower()
    if allowed_extensions and not filename.endswith(allowed_extensions):
        expected = ", ".join(allowed_extensions)
        raise HTTPException(status_code=400, detail=f"Expected file extension in ({expected}).")

    file_size = _get_stream_size(upload)

    try:
        if filename.endswith(".xml") or filename.endswith(".zip"):
            if file_size > MAX_XML_ZIP_BYTES:
                raise HTTPException(status_code=400, detail="Apple Health .XML archive exceeds 1GB limit (FR6).")
            payloads = parse_apple_health_stream(upload.file, filename)
            source_type = SourceFormat.APPLE_XML.value

        elif filename.endswith(".fit"):
            if file_size > MAX_FIT_BYTES:
                raise HTTPException(status_code=400, detail="Garmin .FIT file exceeds 50MB limit (FR4).")
            payloads = parse_garmin_fit_stream(upload.file)
            source_type = SourceFormat.GARMIN_FIT.value

        elif filename.endswith(".gpx"):
            if file_size > MAX_GPX_BYTES:
                raise HTTPException(status_code=400, detail="Spatial .GPX file exceeds 25MB limit (FR5).")
            payloads = parse_spatial_gpx_stream(upload.file)
            source_type = SourceFormat.SPATIAL_GPX.value

        else:
            raise HTTPException(status_code=400, detail="Unsupported format. Upload .FIT, .GPX, .XML, or .ZIP.")

        summary = persist_normalized_activities(db, user_id, payloads, conflict_mode=conflict_mode)
        return {
            "status": "success",
            "user_id": user_id,
            "source_format": source_type,
            "filename": upload.filename,
            "bytes_processed": file_size,
            **summary,
        }

    except DuplicateTimestampConflictError as conflict_err:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Overlapping activity timestamps detected within +/-60s (FR9).",
                "conflicts": conflict_err.conflicts,
            },
        ) from conflict_err
    except TelemetryParseError as parse_err:
        raise HTTPException(status_code=400, detail=str(parse_err)) from parse_err
    except SQLAlchemyError as db_err:
        raise HTTPException(
            status_code=500,
            detail="Database transaction rolled back safely due to an ingestion error (NFR7).",
        ) from db_err


@router.post("/upload", status_code=status.HTTP_200_OK)
def upload_telemetry_file(
    user_id: int = Query(..., gt=0),
    conflict_mode: ConflictMode = Query(default="prompt"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Multi-format ingestion endpoint supporting .XML, .ZIP, .FIT, and .GPX (FR4-FR9)."""
    return _process_telemetry_upload(db, user_id, file, conflict_mode)


@secured_upload_router.post("/apple", status_code=status.HTTP_200_OK)
def upload_apple_secured(
    conflict_mode: ConflictMode = Query(default="prompt"),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """JWT-protected Apple Health .XML/.ZIP upload endpoint (Week 4 Phase 2)."""
    return _process_telemetry_upload(
        db, current_user.id, file, conflict_mode, allowed_extensions=(".xml", ".zip")
    )


@secured_upload_router.post("/garmin", status_code=status.HTTP_200_OK)
def upload_garmin_secured(
    conflict_mode: ConflictMode = Query(default="prompt"),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """JWT-protected Garmin .FIT / spatial .GPX upload endpoint (Week 4 Phase 2)."""
    return _process_telemetry_upload(
        db, current_user.id, file, conflict_mode, allowed_extensions=(".fit", ".gpx")
    )


@router.post("/manual", status_code=status.HTTP_201_CREATED)
def log_manual_activity(
    entry: ManualActivityInput,
    conflict_mode: ConflictMode = Query(default="prompt"),
    db: Session = Depends(get_db),
):
    """Normalizes Manual Log Fallback entries into the primary schema (FR20)."""
    normalized = NormalizedActivitySchema(
        source_format=SourceFormat.MANUAL_LOG,
        activity_type=entry.activity_type,
        start_time_utc=entry.start_time_utc,
        end_time_utc=entry.start_time_utc + timedelta(minutes=entry.duration_minutes),
        duration_minutes=entry.duration_minutes,
        active_calories_kcal=entry.estimated_active_calories_kcal,
        avg_heart_rate_bpm=float(entry.estimated_avg_hr_bpm),
    )
    try:
        summary = persist_normalized_activities(db, entry.user_id, [normalized], conflict_mode=conflict_mode)
        return {"status": "success", **summary}
    except DuplicateTimestampConflictError as conflict_err:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Manual log overlaps existing activity within +/-60s.",
                "conflicts": conflict_err.conflicts,
            },
        ) from conflict_err
