import os
from datetime import timedelta
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.orm_models import SourceFormat
from app.schemas.telemetry_schema import (
    ConflictMode,
    ManualActivityInput,
    NormalizedActivitySchema,
)
from app.services.normalizer import (
    DuplicateTimestampConflictError,
    persist_normalized_activities,
)
from app.services.xml_parser import TelemetryParseError, parse_apple_health_stream

router = APIRouter(prefix="/api/v1/ingest", tags=["Data Ingestion Pipeline"])

MAX_FIT_BYTES = 50 * 1024 * 1024        # 50 MB limit (FR4)
MAX_GPX_BYTES = 25 * 1024 * 1024        # 25 MB limit (FR5)
MAX_XML_ZIP_BYTES = 1024 * 1024 * 1024  # 1 GB limit (FR6)


def _get_stream_size(upload: UploadFile) -> int:
    upload.file.seek(0, os.SEEK_END)
    size = upload.file.tell()
    upload.file.seek(0)
    return size


@router.post("/upload", status_code=status.HTTP_200_OK)
def upload_telemetry_file(
    user_id: int = Query(..., gt=0),
    conflict_mode: ConflictMode = Query(default="prompt"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Validates size limits (FR4-FR6), parses streams (FR7-FR8), and resolves overlaps (FR9)."""
    filename = (file.filename or "").lower()
    file_size = _get_stream_size(file)

    try:
        if filename.endswith(".xml") or filename.endswith(".zip"):
            if file_size > MAX_XML_ZIP_BYTES:
                raise HTTPException(status_code=400, detail="Apple Health .XML archive exceeds 1GB limit.")
            payloads = parse_apple_health_stream(file.file, filename)
            source_type = SourceFormat.APPLE_XML.value

        elif filename.endswith(".fit"):
            if file_size > MAX_FIT_BYTES:
                raise HTTPException(status_code=400, detail="Garmin .FIT file exceeds 50MB limit.")
            raise HTTPException(status_code=501, detail="Garmin .FIT parser scheduled for Week 3 sprint.")

        elif filename.endswith(".gpx"):
            if file_size > MAX_GPX_BYTES:
                raise HTTPException(status_code=400, detail="Spatial .GPX file exceeds 25MB limit.")
            raise HTTPException(status_code=501, detail="Spatial .GPX parser scheduled for Week 3 sprint.")

        else:
            raise HTTPException(status_code=400, detail="Unsupported format. Upload .FIT, .GPX, .XML, or .ZIP.")

        summary = persist_normalized_activities(db, user_id, payloads, conflict_mode=conflict_mode)
        return {
            "status": "success",
            "user_id": user_id,
            "source_format": source_type,
            "filename": file.filename,
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
            detail="Database transaction rolled back safely due to an ingestion error.",
        ) from db_err


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
            detail={"message": "Manual log overlaps existing activity within +/-60s.", "conflicts": conflict_err.conflicts},
        ) from conflict_err
