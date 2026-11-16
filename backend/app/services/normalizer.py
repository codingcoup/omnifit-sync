from datetime import timedelta
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.orm_models import Activity, BiometricPoint
from app.schemas.telemetry_schema import ConflictMode, NormalizedActivitySchema


class DuplicateTimestampConflictError(Exception):
    """Raised when an incoming activity overlaps within +/-60s and conflict_mode='prompt' (FR9)."""

    def __init__(self, conflicts: list[dict]):
        self.conflicts = conflicts
        super().__init__(f"Detected {len(conflicts)} overlapping timestamp window(s).")


def find_overlapping_activity(
    db: Session, user_id: int, start_time_utc
) -> Activity | None:
    """Queries Activities for records within +/-60 seconds of start_time_utc (FR9)."""
    window_start = start_time_utc - timedelta(seconds=60)
    window_end = start_time_utc + timedelta(seconds=60)
    return (
        db.query(Activity)
        .filter(
            Activity.user_id == user_id,
            Activity.start_time_utc >= window_start,
            Activity.start_time_utc <= window_end,
        )
        .first()
    )


def persist_normalized_activities(
    db: Session,
    user_id: int,
    payloads: list[NormalizedActivitySchema],
    conflict_mode: ConflictMode = "prompt",
) -> dict:
    """
    Validates +/-60s timestamp conflicts (FR9) and commits normalized activities
    and biometric points inside an atomic transaction (NFR7).
    """
    if conflict_mode == "prompt":
        detected_conflicts = []
        for item in payloads:
            existing = find_overlapping_activity(db, user_id, item.start_time_utc)
            if existing is not None:
                detected_conflicts.append(
                    {
                        "existing_activity_id": existing.id,
                        "existing_type": existing.activity_type,
                        "existing_start_utc": existing.start_time_utc.isoformat(),
                        "incoming_type": item.activity_type,
                        "incoming_start_utc": item.start_time_utc.isoformat(),
                    }
                )
        if detected_conflicts:
            raise DuplicateTimestampConflictError(detected_conflicts)

    inserted = 0
    overwritten = 0
    skipped = 0

    try:
        for item in payloads:
            existing = find_overlapping_activity(db, user_id, item.start_time_utc)
            if existing is not None:
                if conflict_mode == "skip":
                    skipped += 1
                    continue
                if conflict_mode == "overwrite":
                    db.delete(existing)
                    db.flush()
                    overwritten += 1

            activity_row = Activity(
                user_id=user_id,
                source_format=item.source_format,
                activity_type=item.activity_type,
                start_time_utc=item.start_time_utc,
                end_time_utc=item.end_time_utc,
                duration_minutes=item.duration_minutes,
                active_calories_kcal=item.active_calories_kcal,
                avg_heart_rate_bpm=item.avg_heart_rate_bpm,
                sleep_duration_minutes=item.sleep_duration_minutes,
                hrv_rmssd_ms=item.hrv_rmssd_ms,
                raw_backup_gcs_uri=item.raw_backup_gcs_uri,
            )
            activity_row.biometrics = [
                BiometricPoint(
                    timestamp_utc=pt.timestamp_utc,
                    heart_rate_bpm=pt.heart_rate_bpm,
                    latitude_deg=pt.latitude_deg,
                    longitude_deg=pt.longitude_deg,
                    instant_calories_kcal=pt.instant_calories_kcal,
                )
                for pt in item.biometrics
            ]
            db.add(activity_row)
            if existing is None:
                inserted += 1

        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise

    return {
        "inserted_count": inserted,
        "overwritten_count": overwritten,
        "skipped_count": skipped,
    }
