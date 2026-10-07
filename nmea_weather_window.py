"""Windowed weather summaries built from stored NMEA message records."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from nmea_helpers import average_value, format_utc_datetime, round_to_position
from nmea_aggregation import (
    NMEAError,
    parse_gga,
    parse_mda,
    parse_mwd,
    parse_sentence,
)
from repository_store import MessageRecord


MB_PER_BAR = 1000.0
MB_PER_INHG = 33.8638866667


@dataclass(frozen=True)
class WeatherSample:
    utc: datetime
    wind_direction: float | None = None
    wind_direction_units: str | None = None
    wind_speed_knots: float | None = None
    temperature_celsius: float | None = None
    pressure_mb: float | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True)
class WindSummary:
    speed_knots: float | None
    direction: float | None
    direction_units: str | None


@dataclass(frozen=True)
class WeatherPeriodSummary:
    start: datetime
    end: datetime
    sample_count: int
    average_wind: WindSummary
    gust_wind: WindSummary
    temperature_celsius: float | None
    pressure_mb: float | None
    latitude: float | None
    longitude: float | None


@dataclass(frozen=True)
class WeatherWindowSummary:
    start: datetime
    end: datetime
    segment_count: int
    overall: WeatherPeriodSummary
    segments: list[WeatherPeriodSummary]


def weather_samples_from_records(
    records: list[MessageRecord],
    log_exception_callback,
) -> list[WeatherSample]:
    samples: list[WeatherSample] = []

    for record in records:
        sample = weather_sample_from_record(record, log_exception_callback)
        if sample is not None:
            samples.append(sample)

    return sorted(samples, key=lambda sample: sample.utc)


def weather_sample_from_record(
    record: MessageRecord,
    log_exception_callback,
) -> WeatherSample | None:
    wind_direction: float | None = None
    wind_direction_units: str | None = None
    wind_speed_knots: float | None = None
    temperature_celsius: float | None = None
    pressure_mb: float | None = None
    latitude: float | None = None
    longitude: float | None = None

    for raw_sentence in record.sentences:
        if not raw_sentence.strip():
            continue

        try:
            sentence = parse_sentence(raw_sentence)
            sentence_type = sentence.sentence_type
            if sentence_type == "MWD":
                mwd = parse_mwd(sentence)
                wind_direction = mwd.magnetic_direction
                wind_direction_units = "M"
                wind_speed_knots = mwd.knots
            elif sentence_type == "MDA":
                mda = parse_mda(sentence)
                if temperature_celsius is None:
                    temperature_celsius = mda.air_temperature_celsius
                if pressure_mb is None:
                    pressure_mb = pressure_mb_from_mda(mda)
                if wind_direction is None:
                    if mda.magnetic_wind_direction is not None:
                        wind_direction = mda.magnetic_wind_direction
                        wind_direction_units = "M"
                    elif mda.true_wind_direction is not None:
                        wind_direction = mda.true_wind_direction
                        wind_direction_units = "T"
                if wind_speed_knots is None:
                    wind_speed_knots = mda.wind_speed_knots
                    if (
                        wind_speed_knots is None
                        and mda.wind_speed_metres_per_second is not None
                    ):
                        wind_speed_knots = mda.wind_speed_metres_per_second * 1.943844
            elif sentence_type == "GGA":
                gga = parse_gga(sentence)
                latitude = gga.latitude_degrees
                longitude = gga.longitude_degrees
        except NMEAError as exc:
            log_exception_callback("invalid weather window sentence skipped", exc, raw_sentence)

    if (
        wind_direction is None
        and wind_speed_knots is None
        and temperature_celsius is None
        and pressure_mb is None
        and latitude is None
        and longitude is None
    ):
        return None

    return WeatherSample(
        utc=record.utc,
        wind_direction=wind_direction,
        wind_direction_units=wind_direction_units,
        wind_speed_knots=wind_speed_knots,
        temperature_celsius=temperature_celsius,
        pressure_mb=pressure_mb,
        latitude=latitude,
        longitude=longitude,
    )


def pressure_mb_from_mda(mda) -> float | None:
    if mda.barometric_pressure_bars is not None:
        return mda.barometric_pressure_bars * MB_PER_BAR
    if mda.barometric_pressure_inches is not None:
        return mda.barometric_pressure_inches * MB_PER_INHG
    return None


def build_weather_window_summary(
    samples: list[WeatherSample],
    *,
    start: datetime,
    end: datetime,
    segment_count: int,
) -> WeatherWindowSummary:
    if segment_count < 1:
        raise ValueError("segment_count must be greater than zero")
    if start >= end:
        raise ValueError("summary start must be before end")

    window_samples = [
        sample
        for sample in samples
        if start <= sample.utc <= end
    ]
    segment_duration = (end - start) / segment_count
    segments: list[WeatherPeriodSummary] = []

    for index in range(segment_count):
        segment_start = start + segment_duration * index
        segment_end = start + segment_duration * (index + 1)
        segment_samples = [
            sample
            for sample in window_samples
            if sample.utc >= segment_start
            and (
                sample.utc <= segment_end
                if index == segment_count - 1
                else sample.utc < segment_end
            )
        ]
        segments.append(summarize_weather_period(segment_samples, segment_start, segment_end))

    return WeatherWindowSummary(
        start=start,
        end=end,
        segment_count=segment_count,
        overall=summarize_weather_period(window_samples, start, end),
        segments=segments,
    )


def summarize_weather_period(
    samples: list[WeatherSample],
    start: datetime,
    end: datetime,
) -> WeatherPeriodSummary:
    latitude, longitude = average_position(samples)
    return WeatherPeriodSummary(
        start=start,
        end=end,
        sample_count=len(samples),
        average_wind=average_wind(samples),
        gust_wind=gust_wind(samples),
        temperature_celsius=average_optional_values(
            sample.temperature_celsius for sample in samples
        ),
        pressure_mb=average_optional_values(sample.pressure_mb for sample in samples),
        latitude=latitude,
        longitude=longitude,
    )


def average_wind(samples: list[WeatherSample]) -> WindSummary:
    speed = average_optional_values(sample.wind_speed_knots for sample in samples)
    direction = average_direction(
        [
            (sample.wind_direction, sample.wind_speed_knots)
            for sample in samples
            if sample.wind_direction is not None
        ]
    )
    return WindSummary(
        speed_knots=speed,
        direction=direction,
        direction_units=dominant_direction_units(samples),
    )


def gust_wind(samples: list[WeatherSample]) -> WindSummary:
    gust_sample: WeatherSample | None = None
    for sample in samples:
        if sample.wind_speed_knots is None:
            continue
        if gust_sample is None or sample.wind_speed_knots > (gust_sample.wind_speed_knots or 0.0):
            gust_sample = sample

    if gust_sample is None:
        return WindSummary(None, None, dominant_direction_units(samples))
    return WindSummary(
        speed_knots=gust_sample.wind_speed_knots,
        direction=gust_sample.wind_direction,
        direction_units=gust_sample.wind_direction_units,
    )


def average_optional_values(values) -> float | None:
    valid_values = [
        value
        for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if not valid_values:
        return None
    return average_value(sum(valid_values), len(valid_values))


def average_direction(values: list[tuple[float | None, float | None]]) -> float | None:
    sin_sum = 0.0
    cos_sum = 0.0
    for direction, speed in values:
        if direction is None:
            continue
        weight = max(speed or 0.0, 0.25)
        radians = math.radians(direction)
        sin_sum += math.sin(radians) * weight
        cos_sum += math.cos(radians) * weight

    if sin_sum == 0.0 and cos_sum == 0.0:
        return None
    return math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0


def average_position(samples: list[WeatherSample]) -> tuple[float | None, float | None]:
    x_sum = 0.0
    y_sum = 0.0
    z_sum = 0.0
    count = 0
    for sample in samples:
        if sample.latitude is None or sample.longitude is None:
            continue
        latitude = math.radians(sample.latitude)
        longitude = math.radians(sample.longitude)
        x_sum += math.cos(latitude) * math.cos(longitude)
        y_sum += math.cos(latitude) * math.sin(longitude)
        z_sum += math.sin(latitude)
        count += 1

    if count == 0:
        return None, None

    longitude = math.atan2(y_sum, x_sum)
    horizontal = math.hypot(x_sum, y_sum)
    latitude = math.atan2(z_sum, horizontal)
    return math.degrees(latitude), math.degrees(longitude)


def dominant_direction_units(samples: list[WeatherSample]) -> str | None:
    counts = {"M": 0, "T": 0}
    for sample in samples:
        if sample.wind_direction_units in counts:
            counts[sample.wind_direction_units] += 1
    if counts["M"] == 0 and counts["T"] == 0:
        return None
    return "M" if counts["M"] >= counts["T"] else "T"


def weather_period_to_dict(period: WeatherPeriodSummary) -> dict[str, object]:
    return {
        "start": format_utc_datetime(period.start),
        "end": format_utc_datetime(period.end),
        "sample_count": period.sample_count,
        "average_wind": wind_summary_to_dict(period.average_wind),
        "gust_wind": wind_summary_to_dict(period.gust_wind),
        "temperature": {
            "value": round_to_position(period.temperature_celsius, 1),
            "units": "C",
        },
        "barometric_pressure": {
            "value": round_to_position(period.pressure_mb, 1),
            "units": "mb",
        },
        "latitude": round_to_position(period.latitude, 6),
        "longitude": round_to_position(period.longitude, 6),
    }


def wind_summary_to_dict(wind: WindSummary) -> dict[str, object]:
    return {
        "speed": {
            "value": round_to_position(wind.speed_knots, 1),
            "units": "knots",
        },
        "direction": {
            "value": round_to_position(wind.direction, 1),
            "units": wind.direction_units,
        },
    }
