"""Shared helpers for NMEA parsing, weather presentation, and repository scripts."""

from __future__ import annotations

import os
import math
import re
import traceback
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


LOG_RETENTION_DAYS = 365
QUERY_TIME_PATTERN = re.compile(
    r"^"
    r"(?P<year>\d{4})"
    r"(?:-(?P<month>\d{2})"
    r"(?:-(?P<day>\d{2})"
    r"(?:T(?P<hour>\d{2})"
    r"(?::(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2}))?"
    r")?"
    r"(?P<offset>Z|[+-]\d{2}:\d{2})?"
    r")?"
    r")?"
    r")?"
    r"$"
)


def format_utc_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def parse_utc_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("start must be an ISO-8601 date/time") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def parse_utc_date(value: str) -> date:
    text = value.strip()
    if not text:
        raise ValueError("date must not be empty")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("date must use yyyy-mm-dd") from exc


def parse_query_time_range(value: str) -> tuple[datetime, datetime]:
    text = value.strip()
    match = QUERY_TIME_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError(
            "time values must use ISO format like "
            "yyyy[-mm[-dd[Thh[:mm[:ss]][Z|+hh:mm|-hh:mm]]]]"
        )

    year = int(match.group("year"))
    month_text = match.group("month")
    day_text = match.group("day")
    hour_text = match.group("hour")
    minute_text = match.group("minute")
    second_text = match.group("second")
    offset_text = match.group("offset")

    precision = "year"
    month = 1
    day = 1
    hour = 0
    minute = 0
    second = 0

    if month_text is not None:
        month = int(month_text)
        precision = "month"
    if day_text is not None:
        day = int(day_text)
        precision = "day"
    if hour_text is not None:
        hour = int(hour_text)
        precision = "hour"
    if minute_text is not None:
        minute = int(minute_text)
        precision = "minute"
    if second_text is not None:
        second = int(second_text)
        precision = "second"

    tzinfo = timezone.utc
    if offset_text is not None and offset_text != "Z":
        sign = 1 if offset_text[0] == "+" else -1
        offset_hours = int(offset_text[1:3])
        offset_minutes = int(offset_text[4:6])
        offset_delta = timedelta(hours=offset_hours, minutes=offset_minutes)
        tzinfo = timezone(sign * offset_delta)

    try:
        start = datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            tzinfo=tzinfo,
        )
    except ValueError as exc:
        raise ValueError(
            "time values must use ISO format like "
            "yyyy[-mm[-dd[Thh[:mm[:ss]][Z|+hh:mm|-hh:mm]]]]"
        ) from exc

    end = next_boundary(start, precision)
    return (
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def next_boundary(value: datetime, precision: str) -> datetime:
    if precision == "year":
        return value.replace(year=value.year + 1)
    if precision == "month":
        if value.month == 12:
            return value.replace(year=value.year + 1, month=1)
        return value.replace(month=value.month + 1)
    if precision == "day":
        return value + timedelta(days=1)
    if precision == "hour":
        return value + timedelta(hours=1)
    if precision == "minute":
        return value + timedelta(minutes=1)
    return value + timedelta(seconds=1)


def parse_timespan(value: str) -> timedelta:
    text = " ".join(value.strip().lower().replace("+", " ").replace("-", " ").split())
    if text in ("1 year", "1 years"):
        return timedelta(days=365)

    amount_text, _, unit = text.partition(" ")
    try:
        amount = int(amount_text)
    except ValueError as exc:
        raise ValueError(
            "span must look like '<number> second(s)', '<number> minute(s)', "
            "'<number> hour(s)', '<number> day(s)', '<number> month(s)', "
            "or '<number> year(s)'"
        ) from exc

    if amount < 1:
        raise ValueError("span must be greater than zero")

    if unit in ("second", "seconds"):
        return timedelta(seconds=amount)
    if unit in ("minute", "minutes"):
        return timedelta(minutes=amount)
    if unit in ("hour", "hours"):
        return timedelta(hours=amount)
    if unit in ("day", "days"):
        return timedelta(days=amount)
    if unit in ("month", "months"):
        return timedelta(days=amount * 30)
    if unit in ("year", "years"):
        return timedelta(days=amount * 365)

    raise ValueError(
        "span must use second(s), minute(s), hour(s), day(s), month(s), or year(s) units"
    )


def format_timespan(value: timedelta) -> str:
    total_days = value.days
    if total_days % 365 == 0:
        years = total_days // 365
        return f"{years} year" if years == 1 else f"{years} years"
    if total_days % 30 == 0:
        months = total_days // 30
        return f"{months} month" if months == 1 else f"{months} months"
    return f"{total_days} day" if total_days == 1 else f"{total_days} days"


def datesub(value: datetime, span: str) -> datetime:
    return value - parse_timespan(span)


def apply_date_filters(
    start_text: str | None,
    end_text: str | None,
    span_text: str | None = None,
) -> tuple[datetime | None, datetime | None]:
    start = parse_query_time_range(start_text)[0] if start_text is not None else None
    end = parse_query_time_range(end_text)[1] if end_text is not None else None

    if start is not None and end is not None:
        return (end, end) if start >= end else (start, end)

    if span_text is None:
        return start, end

    text = span_text.strip()
    if not text:
        raise ValueError("span must not be empty")

    if text.lower() == "today":
        if start is not None or end is not None:
            raise ValueError("span=today cannot be combined with start or end")
        end = datetime.now(timezone.utc).replace(tzinfo=None)
        start = end.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, end

    span = parse_timespan(text)
    if start is not None:
        return start, start + span
    if end is not None:
        return end - span, end

    end = datetime.now(timezone.utc).replace(tzinfo=None)
    return end - span, end


def vector_add(
    latitude_degrees: float,
    longitude_degrees: float,
    bearing_degrees: float,
    range_metres: float,
) -> tuple[float, float]:
    earth_radius_metres = 6_371_000.0
    angular_distance = range_metres / earth_radius_metres
    latitude_radians = math.radians(latitude_degrees)
    longitude_radians = math.radians(longitude_degrees)
    bearing_radians = math.radians(bearing_degrees)

    destination_latitude = math.asin(
        math.sin(latitude_radians) * math.cos(angular_distance)
        + math.cos(latitude_radians)
        * math.sin(angular_distance)
        * math.cos(bearing_radians)
    )
    destination_longitude = longitude_radians + math.atan2(
        math.sin(bearing_radians)
        * math.sin(angular_distance)
        * math.cos(latitude_radians),
        math.cos(angular_distance)
        - math.sin(latitude_radians) * math.sin(destination_latitude),
    )
    normalized_longitude = (destination_longitude + math.pi) % (2.0 * math.pi) - math.pi

    return (
        math.degrees(destination_latitude),
        math.degrees(normalized_longitude),
    )


def rotate(bearing_degrees: float, rotation_degrees: float) -> float:
    result = (bearing_degrees + rotation_degrees) % 360.0
    if math.isclose(result, 360.0, abs_tol=1e-9):
        return 0.0
    return result


def average_direction_degrees(sin_sum: float, cos_sum: float) -> float:
    if math.hypot(sin_sum, cos_sum) < 1e-12:
        return 0.0
    degrees = math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0
    return round_direction_degrees(degrees)


def round_direction_degrees(value: float, increment: float = 5.0) -> float:
    rounded = round(value / increment) * increment
    rounded %= 360.0
    if math.isclose(rounded, 360.0, abs_tol=1e-9):
        return 0.0
    return rounded


def average_geographic_degrees(
    x_sum: float,
    y_sum: float,
    z_sum: float,
) -> tuple[float, float]:
    horizontal = math.hypot(x_sum, y_sum)
    if math.hypot(horizontal, z_sum) < 1e-12:
        return 0.0, 0.0
    latitude = math.degrees(math.atan2(z_sum, horizontal))
    longitude = math.degrees(math.atan2(y_sum, x_sum))
    return latitude, longitude


def average_value(total: float, count: int) -> float:
    return float(decimal_from_float(total) / Decimal(count))


def round_to_position(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(value, digits)


COMPASS_DIRECTIONS = [
    (11.25, "N"),
    (33.75, "NNE"),
    (56.25, "NE"),
    (78.75, "ENE"),
    (101.25, "E"),
    (123.75, "ESE"),
    (146.25, "SE"),
    (168.75, "SSE"),
    (191.25, "S"),
    (213.75, "SSW"),
    (236.25, "SW"),
    (258.75, "WSW"),
    (281.25, "W"),
    (303.75, "WNW"),
    (326.25, "NW"),
    (348.75, "NNW"),
    (360.0, "N"),
]
def symbolic_wind_direction(value: float | None) -> str | None:
    if value is None:
        return None

    normalized = value % 360.0
    for upper_bound, symbol in COMPASS_DIRECTIONS:
        if normalized < upper_bound:
            return symbol
    return COMPASS_DIRECTIONS[-1][1]


def decimal_from_float(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.000000001"), rounding=ROUND_HALF_UP)


def log_exception(
    program_name: str,
    script_file: str,
    message: str,
    exc: BaseException,
    sentence: Any | None = None,
    include_traceback: bool = False,
) -> None:
    log_path = daily_log_path(program_name, script_file)
    timestamp = log_timestamp()
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} [ERROR] {program_name} - {message}: {exc}\n")
        if sentence is not None:
            handle.write(
                f"{timestamp} [ERROR] {program_name} - NMEA sentence: "
                f"{sentence_text(sentence)}\n"
            )
        if include_traceback:
            details = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            handle.write(details)


def log_error_message(program_name: str, script_file: str, message: str) -> None:
    log_path = daily_log_path(program_name, script_file)
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(f"{log_timestamp()} [ERROR] {program_name} - {message}\n")


def logs_directory(script_file: str) -> str:
    script_dir = os.path.dirname(os.path.abspath(script_file))
    logs_dir = os.path.join(script_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    return logs_dir


def daily_log_path(program_name: str, script_file: str) -> str:
    logs_dir = logs_directory(script_file)
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = os.path.join(logs_dir, f"{program_name}-{today}.log")
    if not os.path.exists(log_path):
        cleanup_old_log_files(logs_dir)
    return log_path


def cleanup_old_log_files(logs_dir: str) -> None:
    cutoff = datetime.now() - timedelta(days=LOG_RETENTION_DAYS)
    for entry in os.scandir(logs_dir):
        if not entry.is_file():
            continue
        try:
            modified = datetime.fromtimestamp(entry.stat().st_mtime)
        except OSError:
            continue
        if modified < cutoff:
            try:
                os.remove(entry.path)
            except OSError:
                continue


def log_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]


def sentence_text(sentence: Any) -> str:
    raw = getattr(sentence, "raw", None)
    if isinstance(raw, str):
        return raw
    return str(sentence).strip()
