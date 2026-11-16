from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import BinaryIO, Callable, ContextManager
import xml.etree.ElementTree as ET
import zipfile

from app.models.orm_models import SourceFormat
from app.schemas.telemetry_schema import BiometricPointSchema, NormalizedActivitySchema

APPLE_WORKOUT_MAP = {
    "HKWorkoutActivityTypeRunning": "Run",
    "HKWorkoutActivityTypeCycling": "Cycle",
    "HKWorkoutActivityTypeSwimming": "Swim",
    "HKWorkoutActivityTypeTraditionalStrengthTraining": "Strength",
    "HKWorkoutActivityTypeFunctionalStrengthTraining": "Strength",
    "HKWorkoutActivityTypeWalking": "Walk",
}

ASLEEP_VALUES = {
    "HKCategoryValueSleepAnalysisAsleep",
    "HKCategoryValueSleepAnalysisAsleepCore",
    "HKCategoryValueSleepAnalysisAsleepDeep",
    "HKCategoryValueSleepAnalysisAsleepUnspecified",
    "HKCategoryValueSleepAnalysisAsleepREM",
}


class TelemetryParseError(Exception):
    """Raised when an uploaded telemetry file is malformed or unreadable."""


def _parse_apple_dt(raw: str) -> datetime:
    """Converts Apple Health timestamps (e.g., '2026-11-15 07:00:00 -0600') to UTC."""
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S %z").astimezone(timezone.utc)
    except ValueError:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)


def _map_workout_type(hk_type: str) -> str:
    if hk_type in APPLE_WORKOUT_MAP:
        return APPLE_WORKOUT_MAP[hk_type]
    cleaned = hk_type.replace("HKWorkoutActivityType", "").strip()
    return cleaned or "Other"


def _iter_top_level_elements(xml_stream: BinaryIO):
    """Yields direct children of <HealthData> and clears root references to cap RAM."""
    context = ET.iterparse(xml_stream, events=("start", "end"))
    _, root = next(context)
    if root.tag != "HealthData":
        raise TelemetryParseError(f"Expected <HealthData> root tag, found <{root.tag}>.")

    depth = 1
    for event, elem in context:
        if event == "start":
            depth += 1
        elif event == "end":
            if depth == 2:
                yield elem
                root.clear()
            depth -= 1


def _build_stream_opener(
    file_obj: BinaryIO, filename: str
) -> tuple[Callable[[], ContextManager[BinaryIO]], zipfile.ZipFile | None]:
    lower_name = filename.lower()
    if lower_name.endswith(".zip"):
        try:
            zf = zipfile.ZipFile(file_obj)
        except zipfile.BadZipFile as exc:
            raise TelemetryParseError("Corrupted .ZIP archive uploaded.") from exc

        xml_members = [
            m for m in zf.namelist()
            if m.lower().endswith(".xml") and not m.lower().endswith("export_cda.xml")
        ]
        if not xml_members:
            zf.close()
            raise TelemetryParseError("No Apple Health export.xml found inside .ZIP archive.")

        target_member = xml_members[0]

        @contextmanager
        def open_zip_xml():
            with zf.open(target_member, "r") as member_stream:
                yield member_stream

        return open_zip_xml, zf

    @contextmanager
    def open_raw_xml():
        file_obj.seek(0)
        yield file_obj

    return open_raw_xml, None


def parse_apple_health_stream(
    file_obj: BinaryIO, filename: str
) -> list[NormalizedActivitySchema]:
    """
    Streams Apple Health .XML or .ZIP exports in two low-memory passes (FR6-FR8):
    Pass 1 extracts <Workout> envelopes; Pass 2 attaches in-window <Record> biometrics
    and aggregates nightly Sleep + HRV recovery sessions.
    """
    open_stream, zf_handle = _build_stream_opener(file_obj, filename)
    workouts: list[dict] = []

    try:
        with open_stream() as xml_stream:
            for elem in _iter_top_level_elements(xml_stream):
                if elem.tag != "Workout":
                    continue
                start_utc = _parse_apple_dt(elem.attrib["startDate"])
                end_utc = _parse_apple_dt(elem.attrib["endDate"])
                duration_min = float(
                    elem.attrib.get(
                        "duration",
                        max((end_utc - start_utc).total_seconds() / 60.0, 0.1),
                    )
                )
                if elem.attrib.get("durationUnit", "min").lower() == "s":
                    duration_min /= 60.0

                kcal = float(elem.attrib.get("totalEnergyBurned", 0.0) or 0.0)
                avg_hr: float | None = None

                for stat in elem.findall("WorkoutStatistics"):
                    stat_type = stat.attrib.get("type", "")
                    if stat_type == "HKQuantityTypeIdentifierActiveEnergyBurned" and "sum" in stat.attrib:
                        kcal = float(stat.attrib["sum"])
                    elif stat_type == "HKQuantityTypeIdentifierHeartRate" and "average" in stat.attrib:
                        avg_hr = float(stat.attrib["average"])

                workouts.append(
                    {
                        "activity_type": _map_workout_type(elem.attrib.get("workoutActivityType", "")),
                        "start_time_utc": start_utc,
                        "end_time_utc": end_utc,
                        "duration_minutes": round(max(duration_min, 0.1), 2),
                        "active_calories_kcal": kcal,
                        "avg_heart_rate_bpm": avg_hr,
                        "hr_samples": [],
                        "kcal_samples": 0.0,
                        "biometrics": {},
                    }
                )

        sleep_by_day: dict[date, dict] = defaultdict(
            lambda: {"minutes": 0.0, "start": None, "end": None}
        )
        hrv_by_day: dict[date, list[float]] = defaultdict(list)

        with open_stream() as xml_stream:
            for elem in _iter_top_level_elements(xml_stream):
                if elem.tag != "Record":
                    continue

                rec_type = elem.attrib.get("type", "")
                start_utc = _parse_apple_dt(elem.attrib["startDate"])
                end_utc = _parse_apple_dt(elem.attrib.get("endDate", elem.attrib["startDate"]))

                if rec_type == "HKCategoryTypeIdentifierSleepAnalysis":
                    if elem.attrib.get("value") in ASLEEP_VALUES:
                        mins = max((end_utc - start_utc).total_seconds() / 60.0, 0.0)
                        if mins > 0:
                            bucket = sleep_by_day[end_utc.date()]
                            bucket["minutes"] += mins
                            bucket["start"] = min(bucket["start"], start_utc) if bucket["start"] else start_utc
                            bucket["end"] = max(bucket["end"], end_utc) if bucket["end"] else end_utc

                elif rec_type == "HKQuantityTypeIdentifierHeartRateVariabilitySDNN":
                    hrv_by_day[start_utc.date()].append(float(elem.attrib["value"]))

                elif rec_type in (
                    "HKQuantityTypeIdentifierHeartRate",
                    "HKQuantityTypeIdentifierActiveEnergyBurned",
                ):
                    val = float(elem.attrib["value"])
                    for w in workouts:
                        if w["start_time_utc"] <= start_utc <= w["end_time_utc"]:
                            pt = w["biometrics"].setdefault(
                                start_utc, BiometricPointSchema(timestamp_utc=start_utc)
                            )
                            if rec_type == "HKQuantityTypeIdentifierHeartRate":
                                hr_int = int(round(val))
                                pt.heart_rate_bpm = hr_int
                                w["hr_samples"].append(hr_int)
                            else:
                                pt.instant_calories_kcal = round(val, 3)
                                w["kcal_samples"] += val

    except (ET.ParseError, KeyError, ValueError, StopIteration) as exc:
        raise TelemetryParseError(f"Malformed Apple Health XML structure: {exc}") from exc
    finally:
        if zf_handle is not None:
            zf_handle.close()

    normalized: list[NormalizedActivitySchema] = []

    for w in workouts:
        final_kcal = w["active_calories_kcal"] if w["active_calories_kcal"] > 0 else w["kcal_samples"]
        final_hr = (
            w["avg_heart_rate_bpm"]
            if w["avg_heart_rate_bpm"] is not None
            else (sum(w["hr_samples"]) / len(w["hr_samples"]) if w["hr_samples"] else None)
        )
        sorted_points = [w["biometrics"][ts] for ts in sorted(w["biometrics"].keys())]

        normalized.append(
            NormalizedActivitySchema(
                source_format=SourceFormat.APPLE_XML,
                activity_type=w["activity_type"],
                start_time_utc=w["start_time_utc"],
                end_time_utc=w["end_time_utc"],
                duration_minutes=w["duration_minutes"],
                active_calories_kcal=round(final_kcal, 2),
                avg_heart_rate_bpm=round(final_hr, 1) if final_hr is not None else None,
                biometrics=sorted_points,
            )
        )

    for sleep_date, info in sorted(sleep_by_day.items()):
        hrv_vals = hrv_by_day.get(sleep_date, [])
        avg_hrv = round(sum(hrv_vals) / len(hrv_vals), 2) if hrv_vals else None
        normalized.append(
            NormalizedActivitySchema(
                source_format=SourceFormat.APPLE_XML,
                activity_type="Sleep",
                start_time_utc=info["start"],
                end_time_utc=info["end"],
                duration_minutes=round(info["minutes"], 2),
                sleep_duration_minutes=round(info["minutes"], 2),
                hrv_rmssd_ms=avg_hrv,
            )
        )

    return normalized
