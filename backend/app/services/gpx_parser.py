from datetime import timedelta, timezone
from typing import BinaryIO
import gpxpy
import gpxpy.gpx

from app.models.orm_models import SourceFormat
from app.schemas.telemetry_schema import BiometricPointSchema, NormalizedActivitySchema
from app.services.spatial_utils import compress_spatial_biometrics, estimate_active_calories
from app.services.xml_parser import TelemetryParseError

GPX_TYPE_MAP = {
    "running": "Run",
    "run": "Run",
    "cycling": "Cycle",
    "biking": "Cycle",
    "ride": "Cycle",
    "swimming": "Swim",
    "walking": "Walk",
    "hiking": "Walk",
}


def _extract_extension_hr(point) -> int | None:
    """Extracts Garmin/Strava TrackPointExtension heart rate (<gpxtpx:hr>) if present."""
    for ext in getattr(point, "extensions", []) or []:
        for elem in ext.iter():
            tag_lower = elem.tag.lower()
            if tag_lower.endswith("}hr") or tag_lower == "hr" or tag_lower.endswith("heartrate"):
                if elem.text and elem.text.strip().isdigit():
                    val = int(elem.text.strip())
                    if 25 <= val <= 250:
                        return val
    return None


def _map_gpx_type(raw_type: str | None) -> str:
    if not raw_type:
        return "Run"
    cleaned = raw_type.strip().lower()
    return GPX_TYPE_MAP.get(cleaned, raw_type.strip().capitalize())


def parse_spatial_gpx_stream(
    file_obj: BinaryIO, compress_epsilon_m: float = 3.0
) -> list[NormalizedActivitySchema]:
    """
    Parses structured .GPX spatial routes (FR5, FR7, FR8), applies RDP coordinate
    compression, extracts extension heart rates, and normalizes active calories.
    """
    file_obj.seek(0)
    try:
        raw_text = file_obj.read().decode("utf-8-sig")
        gpx = gpxpy.parse(raw_text)
    except (UnicodeDecodeError, gpxpy.gpx.GPXXMLSyntaxException, gpxpy.gpx.GPXException, ValueError) as exc:
        raise TelemetryParseError(f"Malformed .GPX spatial file: {exc}") from exc

    normalized: list[NormalizedActivitySchema] = []

    for track in gpx.tracks:
        raw_points: list[BiometricPointSchema] = []
        hr_values: list[int] = []

        for segment in track.segments:
            for pt in segment.points:
                if pt.time is None:
                    continue
                ts_utc = (
                    pt.time.replace(tzinfo=timezone.utc)
                    if pt.time.tzinfo is None
                    else pt.time.astimezone(timezone.utc)
                )
                hr_bpm = _extract_extension_hr(pt)
                if hr_bpm is not None:
                    hr_values.append(hr_bpm)

                raw_points.append(
                    BiometricPointSchema(
                        timestamp_utc=ts_utc,
                        heart_rate_bpm=hr_bpm,
                        latitude_deg=round(float(pt.latitude), 6),
                        longitude_deg=round(float(pt.longitude), 6),
                    )
                )

        if not raw_points:
            continue

        raw_points.sort(key=lambda p: p.timestamp_utc)
        compressed_points = compress_spatial_biometrics(raw_points, epsilon_meters=compress_epsilon_m)

        start_utc = raw_points[0].timestamp_utc
        end_utc = raw_points[-1].timestamp_utc
        duration_min = round(max((end_utc - start_utc).total_seconds() / 60.0, 0.1), 2)
        if end_utc <= start_utc:
            end_utc = start_utc + timedelta(seconds=6)

        activity_type = _map_gpx_type(track.type)
        avg_hr = round(sum(hr_values) / len(hr_values), 1) if hr_values else None
        est_kcal = estimate_active_calories(activity_type, duration_min, avg_hr)

        normalized.append(
            NormalizedActivitySchema(
                source_format=SourceFormat.SPATIAL_GPX,
                activity_type=activity_type,
                start_time_utc=start_utc,
                end_time_utc=end_utc,
                duration_minutes=duration_min,
                active_calories_kcal=est_kcal,
                avg_heart_rate_bpm=avg_hr,
                biometrics=compressed_points,
            )
        )

    if not normalized:
        raise TelemetryParseError("No timestamped GPS trackpoints found in .GPX file.")

    return normalized
