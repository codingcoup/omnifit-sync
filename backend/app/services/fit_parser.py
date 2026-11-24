from datetime import datetime, timedelta, timezone
from typing import BinaryIO
from fitparse import FitFile, FitParseError

from app.models.orm_models import SourceFormat
from app.schemas.telemetry_schema import BiometricPointSchema, NormalizedActivitySchema
from app.services.spatial_utils import compress_spatial_biometrics, estimate_active_calories
from app.services.xml_parser import TelemetryParseError

SEMICIRCLE_TO_DEG = 180.0 / (2 ** 31)

GARMIN_SPORT_MAP = {
    "running": "Run",
    "cycling": "Cycle",
    "swimming": "Swim",
    "training": "Strength",
    "fitness_equipment": "Strength",
    "walking": "Walk",
    "hiking": "Walk",
}


def _to_utc(dt_val: datetime) -> datetime:
    if dt_val.tzinfo is None:
        return dt_val.replace(tzinfo=timezone.utc)
    return dt_val.astimezone(timezone.utc)


def _semicircles_to_degrees(raw_val: int | float | None) -> float | None:
    if raw_val is None:
        return None
    deg = float(raw_val) * SEMICIRCLE_TO_DEG
    if -180.0 <= deg <= 180.0:
        return round(deg, 6)
    return None


def _map_garmin_sport(raw_sport) -> str:
    if raw_sport is None:
        return "Other"
    key = str(raw_sport).strip().lower()
    return GARMIN_SPORT_MAP.get(key, key.capitalize() or "Other")


def parse_garmin_fit_stream(
    file_obj: BinaryIO, compress_epsilon_m: float = 0.0
) -> list[NormalizedActivitySchema]:
    """
    Parses binary Garmin .FIT streams using fitparse (FR4, FR7, FR8), converting
    semicircle GPS coordinates to WGS84 degrees and normalizing session summaries.
    """
    file_obj.seek(0)
    raw_bytes = file_obj.read()
    if len(raw_bytes) < 14 or raw_bytes[8:12] != b".FIT":
        raise TelemetryParseError("Invalid or corrupted Garmin .FIT binary header.")

    try:
        fit_file = FitFile(raw_bytes, check_crc=False)
        fit_file.parse()
    except (FitParseError, ValueError, TypeError, EOFError) as exc:
        raise TelemetryParseError(f"Failed to decode Garmin .FIT telemetry: {exc}") from exc

    biometrics: list[BiometricPointSchema] = []
    hr_samples: list[int] = []

    for record in fit_file.get_messages("record"):
        values = record.get_values()
        ts = values.get("timestamp")
        if not isinstance(ts, datetime):
            continue

        ts_utc = _to_utc(ts)
        lat_deg = _semicircles_to_degrees(values.get("position_lat"))
        lon_deg = _semicircles_to_degrees(values.get("position_long"))

        hr_val = values.get("heart_rate")
        hr_int = int(round(hr_val)) if isinstance(hr_val, (int, float)) and hr_val > 0 else None
        if hr_int is not None:
            hr_samples.append(hr_int)

        cal_val = values.get("calories")
        inst_cal = float(cal_val) if isinstance(cal_val, (int, float)) and cal_val >= 0 else None

        biometrics.append(
            BiometricPointSchema(
                timestamp_utc=ts_utc,
                heart_rate_bpm=hr_int,
                latitude_deg=lat_deg,
                longitude_deg=lon_deg,
                instant_calories_kcal=inst_cal,
            )
        )

    biometrics.sort(key=lambda p: p.timestamp_utc)
    if compress_epsilon_m > 0 and len(biometrics) > 2:
        biometrics = compress_spatial_biometrics(biometrics, epsilon_meters=compress_epsilon_m)

    normalized: list[NormalizedActivitySchema] = []

    for session in fit_file.get_messages("session"):
        s_vals = session.get_values()
        start_dt = s_vals.get("start_time") or s_vals.get("timestamp")
        if not isinstance(start_dt, datetime):
            if biometrics:
                start_dt = biometrics[0].timestamp_utc
            else:
                continue

        start_utc = _to_utc(start_dt)
        elapsed_sec = s_vals.get("total_elapsed_time") or s_vals.get("total_timer_time")
        if isinstance(elapsed_sec, (int, float)) and elapsed_sec > 0:
            duration_min = round(float(elapsed_sec) / 60.0, 2)
            end_utc = start_utc + timedelta(seconds=float(elapsed_sec))
        elif isinstance(s_vals.get("timestamp"), datetime):
            end_utc = _to_utc(s_vals["timestamp"])
            duration_min = round(max((end_utc - start_utc).total_seconds() / 60.0, 0.1), 2)
        elif len(biometrics) >= 2:
            end_utc = biometrics[-1].timestamp_utc
            duration_min = round(max((end_utc - start_utc).total_seconds() / 60.0, 0.1), 2)
        else:
            duration_min = 1.0
            end_utc = start_utc + timedelta(minutes=1)

        activity_type = _map_garmin_sport(s_vals.get("sport"))

        session_points = [
            pt for pt in biometrics if start_utc <= pt.timestamp_utc <= end_utc
        ] or biometrics

        avg_hr_raw = s_vals.get("avg_heart_rate")
        if isinstance(avg_hr_raw, (int, float)) and avg_hr_raw > 0:
            avg_hr = round(float(avg_hr_raw), 1)
        else:
            pts_hr = [pt.heart_rate_bpm for pt in session_points if pt.heart_rate_bpm is not None]
            avg_hr = round(sum(pts_hr) / len(pts_hr), 1) if pts_hr else None

        total_kcal_raw = s_vals.get("total_calories")
        if isinstance(total_kcal_raw, (int, float)) and total_kcal_raw > 0:
            active_kcal = round(float(total_kcal_raw), 2)
        else:
            active_kcal = estimate_active_calories(activity_type, duration_min, avg_hr)

        normalized.append(
            NormalizedActivitySchema(
                source_format=SourceFormat.GARMIN_FIT,
                activity_type=activity_type,
                start_time_utc=start_utc,
                end_time_utc=end_utc,
                duration_minutes=max(duration_min, 0.1),
                active_calories_kcal=active_kcal,
                avg_heart_rate_bpm=avg_hr,
                biometrics=session_points,
            )
        )

    if not normalized and biometrics:
        start_utc = biometrics[0].timestamp_utc
        end_utc = biometrics[-1].timestamp_utc
        duration_min = round(max((end_utc - start_utc).total_seconds() / 60.0, 0.1), 2)
        avg_hr = round(sum(hr_samples) / len(hr_samples), 1) if hr_samples else None
        active_kcal = estimate_active_calories("Run", duration_min, avg_hr)
        normalized.append(
            NormalizedActivitySchema(
                source_format=SourceFormat.GARMIN_FIT,
                activity_type="Run",
                start_time_utc=start_utc,
                end_time_utc=end_utc,
                duration_minutes=duration_min,
                active_calories_kcal=active_kcal,
                avg_heart_rate_bpm=avg_hr,
                biometrics=biometrics,
            )
        )

    if not normalized:
        raise TelemetryParseError("Garmin .FIT file contained no session or biometric records.")

    return normalized
