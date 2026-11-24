import math
from app.schemas.telemetry_schema import BiometricPointSchema


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates great-circle distance in meters between two WGS84 coordinates."""
    radius_m = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2.0) ** 2
    return 2.0 * radius_m * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _perpendicular_distance_m(
    pt: BiometricPointSchema, start: BiometricPointSchema, end: BiometricPointSchema
) -> float:
    """Approximates perpendicular point-to-segment distance in meters for RDP compression."""
    if (
        pt.latitude_deg is None or pt.longitude_deg is None
        or start.latitude_deg is None or start.longitude_deg is None
        or end.latitude_deg is None or end.longitude_deg is None
    ):
        return 0.0

    if start.latitude_deg == end.latitude_deg and start.longitude_deg == end.longitude_deg:
        return haversine_meters(pt.latitude_deg, pt.longitude_deg, start.latitude_deg, start.longitude_deg)

    lat_scale = 111320.0
    lon_scale = 111320.0 * math.cos(math.radians(start.latitude_deg))

    px = (pt.longitude_deg - start.longitude_deg) * lon_scale
    py = (pt.latitude_deg - start.latitude_deg) * lat_scale
    ex = (end.longitude_deg - start.longitude_deg) * lon_scale
    ey = (end.latitude_deg - start.latitude_deg) * lat_scale

    seg_len_sq = ex * ex + ey * ey
    if seg_len_sq == 0.0:
        return math.hypot(px, py)

    t = max(0.0, min(1.0, (px * ex + py * ey) / seg_len_sq))
    proj_x, proj_y = t * ex, t * ey
    return math.hypot(px - proj_x, py - proj_y)


def compress_spatial_biometrics(
    points: list[BiometricPointSchema], epsilon_meters: float = 3.0
) -> list[BiometricPointSchema]:
    """
    Compresses dense GPS trackpoints using iterative Ramer-Douglas-Peucker (RDP)
    simplification while preserving route geometry and endpoints.
    """
    if len(points) <= 2 or epsilon_meters <= 0:
        return points

    gps_points = [p for p in points if p.latitude_deg is not None and p.longitude_deg is not None]
    if len(gps_points) <= 2:
        return points

    keep = [False] * len(points)
    keep[0] = True
    keep[-1] = True

    stack: list[tuple[int, int]] = [(0, len(points) - 1)]
    while stack:
        first_idx, last_idx = stack.pop()
        if last_idx - first_idx <= 1:
            continue

        max_dist = -1.0
        split_idx = -1
        start_pt = points[first_idx]
        end_pt = points[last_idx]

        for idx in range(first_idx + 1, last_idx):
            dist = _perpendicular_distance_m(points[idx], start_pt, end_pt)
            if dist > max_dist:
                max_dist = dist
                split_idx = idx

        if max_dist > epsilon_meters and split_idx != -1:
            keep[split_idx] = True
            stack.append((first_idx, split_idx))
            stack.append((split_idx, last_idx))

    return [pt for idx, pt in enumerate(points) if keep[idx]]


def estimate_active_calories(
    activity_type: str,
    duration_minutes: float,
    avg_hr_bpm: float | None = None,
    weight_kg: float = 75.0,
) -> float:
    """
    Normalizes missing ecosystem calorie values using heart-rate intensity or standard METs
    so .GPX routes align cleanly with Garmin .FIT and Apple Health .XML workouts.
    """
    if duration_minutes <= 0:
        return 0.0

    if avg_hr_bpm is not None and avg_hr_bpm > 60:
        kcal_per_min = max(1.5, (-37.5 + 0.54 * avg_hr_bpm + 0.16 * weight_kg) / 4.184)
        return round(kcal_per_min * duration_minutes, 2)

    met_lookup = {
        "Run": 9.8,
        "Cycle": 7.5,
        "Swim": 8.0,
        "Strength": 5.0,
        "Walk": 3.8,
    }
    met = met_lookup.get(activity_type, 6.0)
    hours = duration_minutes / 60.0
    return round(met * weight_kg * hours, 2)
