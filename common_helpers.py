"""Common helpers shared by the NMEA scripts."""

from __future__ import annotations

import os
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any


def format_utc_datetime(value: datetime) -> str:
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


def parse_query_time_range(value: str) -> tuple[datetime, datetime]:
    text = value.strip()
    formats = [
        ("%Y", "year"),
        ("%Y-%m", "month"),
        ("%Y-%m-%d", "day"),
        ("%Y-%m-%d:%H", "hour"),
        ("%Y-%m-%d:%H:%M", "minute"),
        ("%Y-%m-%d:%H:%M:%S", "second"),
    ]
    for format_text, precision in formats:
        try:
            start = datetime.strptime(text, format_text)
        except ValueError:
            continue
        return start, next_boundary(start, precision)

    raise ValueError("time values must use yyyy[-mm[-dd[:hh[:mm[:ss]]]]]")


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


def log_exception(
    program_name: str,
    script_file: str,
    message: str,
    exc: BaseException,
    sentence: Any | None = None,
) -> None:
    logs_dir = logs_directory(script_file)
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = os.path.join(logs_dir, f"{program_name}-{today}.log")
    timestamp = log_timestamp()
    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} [ERROR] {program_name} - {message}: {exc}\n")
        if sentence is not None:
            handle.write(
                f"{timestamp} [ERROR] {program_name} - NMEA sentence: "
                f"{sentence_text(sentence)}\n"
            )
        handle.write(details)


def log_error_message(program_name: str, script_file: str, message: str) -> None:
    logs_dir = logs_directory(script_file)
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = os.path.join(logs_dir, f"{program_name}-{today}.log")
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(f"{log_timestamp()} [ERROR] {program_name} - {message}\n")


def logs_directory(script_file: str) -> str:
    script_dir = os.path.dirname(os.path.abspath(script_file))
    logs_dir = os.path.join(script_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    return logs_dir


def log_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]


def sentence_text(sentence: Any) -> str:
    raw = getattr(sentence, "raw", None)
    if isinstance(raw, str):
        return raw
    return str(sentence).strip()
