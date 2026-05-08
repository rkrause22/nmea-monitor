"""Analysis helpers for plot-oriented NMEA history views."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from nmea_weather import round_weather_value, symbolic_wind_direction


ROLLING_WINDOW = timedelta(minutes=5)
TREND_WINDOW = timedelta(minutes=5)


def enrich_plot_samples(samples: list[dict[str, object]]) -> list[dict[str, object]]:
    enriched: list[dict[str, object]] = []

    for index, sample in enumerate(samples):
        window = trailing_samples(samples, index, ROLLING_WINDOW, time_key="utc_time")
        rolling_direction = circular_mean_direction(window)
        enriched_sample = dict(sample)
        enriched_sample["rolling_wind_direction"] = direction_measurement(
            rolling_direction,
            dominant_direction_units(window),
        )
        enriched.append(enriched_sample)

    return enriched


def build_plot_analysis(samples: list[dict[str, object]]) -> dict[str, object]:
    if not samples:
        return {
            "wind": empty_wind_analysis(),
            "pressure": empty_pressure_analysis(),
            "temperature": empty_temperature_analysis(),
        }

    mean_direction = circular_mean_direction(samples)
    mean_speed = average_measurement(samples, "wind_speed")
    current_sample = samples[-1]
    wind_units = dominant_direction_units(samples)
    rolling_directions = [
        measurement_value(sample.get("rolling_wind_direction"))
        for sample in samples
    ]

    return {
        "wind": {
            "mean": {
                "headline": format_wind_headline(mean_direction, mean_speed),
                "detail": format_wind_detail(mean_direction, wind_units),
                "direction": direction_measurement(mean_direction, wind_units),
                "speed": measurement(mean_speed, "kn"),
            },
            "current": {
                "headline": format_wind_headline(
                    measurement_value(current_sample.get("wind_direction")),
                    speed_knots(current_sample.get("wind_speed")),
                ),
                "detail": format_wind_detail(
                    measurement_value(current_sample.get("wind_direction")),
                    measurement_units(current_sample.get("wind_direction"), "T"),
                ),
                "direction": current_sample.get("wind_direction"),
                "speed": measurement(
                    speed_knots(current_sample.get("wind_speed")),
                    "kn",
                ),
            },
            "analysis": wind_oscillation(samples, mean_direction, rolling_directions),
            "trend": wind_trend(samples),
        },
        "pressure": {
            "mean": pressure_mean(samples),
            "current": pressure_current(current_sample),
            "analysis": pressure_analysis(samples),
            "trend": pressure_trend(samples),
        },
        "temperature": {
            "mean": temperature_mean(samples),
            "current": temperature_current(current_sample),
            "analysis": temperature_analysis(samples),
            "trend": temperature_trend(samples),
        },
    }


def empty_wind_analysis() -> dict[str, object]:
    return {
        "mean": {"headline": "-", "detail": "-", "direction": None, "speed": None},
        "current": {"headline": "-", "detail": "-", "direction": None, "speed": None},
        "analysis": {"headline": "-", "detail": "-"},
        "trend": {"headline": "-", "detail": "-"},
    }


def empty_pressure_analysis() -> dict[str, object]:
    return {
        "mean": {"headline": "-", "detail": "-", "measurement": None},
        "current": {"headline": "-", "detail": "-", "measurement": None},
        "analysis": {"headline": "-", "detail": "-"},
        "trend": {"headline": "-", "detail": "-"},
    }


def empty_temperature_analysis() -> dict[str, object]:
    return {
        "mean": {"headline": "-", "detail": "-", "measurement": None},
        "current": {"headline": "-", "detail": "-", "measurement": None},
        "analysis": {"headline": "-", "detail": "-"},
        "trend": {"headline": "-", "detail": "-"},
    }


def wind_oscillation(
    samples: list[dict[str, object]],
    mean_direction: float,
    rolling_directions: list[float | None],
) -> dict[str, object]:
    offsets = [
        signed_angle_difference(
            measurement_value(sample.get("wind_direction")),
            rolling if rolling is not None else mean_direction,
        )
        for sample, rolling in zip(samples, rolling_directions, strict=False)
        if measurement_value(sample.get("wind_direction")) is not None
    ]

    if not offsets:
        return {"headline": "-", "detail": "-"}

    minimum = min(offsets)
    maximum = max(offsets)
    total_swing = maximum - minimum
    half_range = total_swing / 2.0
    crossings = count_crossings(offsets)

    headline = "Stable"
    if half_range >= 12 and crossings >= 4:
        headline = "Oscillating"
    elif half_range >= 7:
        headline = "Active"

    return {
        "headline": headline,
        "detail": f"{format_angle_size(total_swing)} total swing, {format_angle_size(half_range)} half-range",
    }


def wind_trend(samples: list[dict[str, object]]) -> dict[str, object]:
    latest_window, prior_window = split_trend_windows(samples, "utc_time")
    if not latest_window or not prior_window:
        return {"headline": "Watching", "detail": "Need more history for shift comparison"}

    latest_mean = circular_mean_direction(latest_window)
    prior_mean = circular_mean_direction(prior_window)
    shift = signed_angle_difference(latest_mean, prior_mean)
    magnitude = abs(shift)

    if magnitude < 4:
        return {
            "headline": "Steady",
            "detail": f"Last 10 minutes within {format_angle_size(magnitude)}",
        }

    side = "Right" if shift > 0 else "Left"
    strength = "Significant" if magnitude >= 12 else "Minor"
    return {
        "headline": f"{strength} {side}",
        "detail": f"{format_signed_angle(shift)} versus prior 5-minute average",
    }


def pressure_mean(samples: list[dict[str, object]]) -> dict[str, object]:
    mean_pressure = average_measurement(samples, "barometric_pressure")
    units = dominant_measurement_units(samples, "barometric_pressure", "mb")
    return {
        "headline": format_pressure(mean_pressure, units),
        "detail": "Window average",
        "measurement": measurement(mean_pressure, units),
    }


def pressure_current(sample: dict[str, object]) -> dict[str, object]:
    value = measurement_value(sample.get("barometric_pressure"))
    units = measurement_units(sample.get("barometric_pressure"), "mb")
    return {
        "headline": format_pressure(value, units),
        "detail": "Latest sample",
        "measurement": measurement(value, units),
    }


def pressure_analysis(samples: list[dict[str, object]]) -> dict[str, object]:
    values = [
        measurement_value(sample.get("barometric_pressure"))
        for sample in samples
        if measurement_value(sample.get("barometric_pressure")) is not None
    ]
    units = dominant_measurement_units(samples, "barometric_pressure", "mb")
    if not values:
        return {"headline": "-", "detail": "-"}

    total_swing = max(values) - min(values)
    headline = "Stable"
    if total_swing >= 2.5:
        headline = "Active"
    if total_swing >= 5.0:
        headline = "Volatile"

    return {
        "headline": headline,
        "detail": f"{format_pressure_delta(total_swing, units)} total swing",
    }


def pressure_trend(samples: list[dict[str, object]]) -> dict[str, object]:
    latest_window, prior_window = split_trend_windows(samples, "utc_time")
    units = dominant_measurement_units(samples, "barometric_pressure", "mb")
    if not latest_window or not prior_window:
        return {"headline": "Watching", "detail": "Need more history for pressure trend"}

    latest_mean = average_measurement(latest_window, "barometric_pressure")
    prior_mean = average_measurement(prior_window, "barometric_pressure")
    if latest_mean is None or prior_mean is None:
        return {"headline": "Watching", "detail": "Need more history for pressure trend"}

    delta = latest_mean - prior_mean
    magnitude = abs(delta)
    if magnitude < 0.5:
        return {"headline": "Steady", "detail": f"Last 10 minutes within {format_pressure_delta(magnitude, units)}"}

    direction = "Rising" if delta > 0 else "Falling"
    strength = "Strongly" if magnitude >= 2.0 else "Gently"
    return {
        "headline": f"{strength} {direction}",
        "detail": f"{format_signed_pressure_delta(delta, units)} versus prior 5-minute average",
    }


def temperature_mean(samples: list[dict[str, object]]) -> dict[str, object]:
    mean_temperature = average_measurement(samples, "temperature")
    units = dominant_measurement_units(samples, "temperature", "C")
    return {
        "headline": format_temperature(mean_temperature, units),
        "detail": "Window average",
        "measurement": measurement(mean_temperature, units),
    }


def temperature_current(sample: dict[str, object]) -> dict[str, object]:
    value = measurement_value(sample.get("temperature"))
    units = measurement_units(sample.get("temperature"), "C")
    return {
        "headline": format_temperature(value, units),
        "detail": "Latest sample",
        "measurement": measurement(value, units),
    }


def temperature_analysis(samples: list[dict[str, object]]) -> dict[str, object]:
    values = [
        measurement_value(sample.get("temperature"))
        for sample in samples
        if measurement_value(sample.get("temperature")) is not None
    ]
    units = dominant_measurement_units(samples, "temperature", "C")
    if not values:
        return {"headline": "-", "detail": "-"}

    total_swing = max(values) - min(values)
    headline = "Stable"
    if total_swing >= 1.0:
        headline = "Active"
    if total_swing >= 2.0:
        headline = "Volatile"

    return {
        "headline": headline,
        "detail": f"{format_temperature_delta(total_swing, units)} total swing",
    }


def temperature_trend(samples: list[dict[str, object]]) -> dict[str, object]:
    latest_window, prior_window = split_trend_windows(samples, "utc_time")
    units = dominant_measurement_units(samples, "temperature", "C")
    if not latest_window or not prior_window:
        return {"headline": "Watching", "detail": "Need more history for temperature trend"}

    latest_mean = average_measurement(latest_window, "temperature")
    prior_mean = average_measurement(prior_window, "temperature")
    if latest_mean is None or prior_mean is None:
        return {"headline": "Watching", "detail": "Need more history for temperature trend"}

    delta = latest_mean - prior_mean
    magnitude = abs(delta)
    if magnitude < 0.3:
        return {"headline": "Steady", "detail": f"Last 10 minutes within {format_temperature_delta(magnitude, units)}"}

    direction = "Rising" if delta > 0 else "Falling"
    strength = "Strongly" if magnitude >= 1.0 else "Gently"
    return {
        "headline": f"{strength} {direction}",
        "detail": f"{format_signed_temperature_delta(delta, units)} versus prior 5-minute average",
    }


def split_trend_windows(
    samples: list[dict[str, object]],
    time_key: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    latest_time = parse_sample_time(samples[-1], time_key)
    if latest_time is None:
        return [], []
    latest_window_start = latest_time - TREND_WINDOW
    prior_window_start = latest_window_start - TREND_WINDOW

    latest_window = [
        sample
        for sample in samples
        if (sample_time := parse_sample_time(sample, time_key)) is not None
        and sample_time >= latest_window_start
    ]
    prior_window = [
        sample
        for sample in samples
        if (sample_time := parse_sample_time(sample, time_key)) is not None
        and prior_window_start <= sample_time < latest_window_start
    ]
    return latest_window, prior_window


def trailing_samples(
    samples: list[dict[str, object]],
    index: int,
    window: timedelta,
    *,
    time_key: str,
) -> list[dict[str, object]]:
    end_time = parse_sample_time(samples[index], time_key)
    if end_time is None:
        return []
    start_time = end_time - window

    window_samples: list[dict[str, object]] = []
    for position in range(index, -1, -1):
        sample_time = parse_sample_time(samples[position], time_key)
        if sample_time is None or sample_time < start_time:
            break
        window_samples.append(samples[position])
    window_samples.reverse()
    return window_samples


def parse_sample_time(sample: dict[str, object], key: str) -> datetime | None:
    value = sample.get(key)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def circular_mean_direction(samples: list[dict[str, object]]) -> float:
    sin_sum = 0.0
    cos_sum = 0.0

    for sample in samples:
        direction = measurement_value(sample.get("wind_direction"))
        if direction is None:
            continue
        weight = max(speed_knots(sample.get("wind_speed")) or 0.0, 0.25)
        radians = math.radians(direction)
        sin_sum += math.sin(radians) * weight
        cos_sum += math.cos(radians) * weight

    if sin_sum == 0.0 and cos_sum == 0.0:
        return 0.0
    return normalize_degrees(math.degrees(math.atan2(sin_sum, cos_sum)))


def average_measurement(samples: list[dict[str, object]], key: str) -> float | None:
    values = [
        measurement_value(sample.get(key))
        for sample in samples
        if measurement_value(sample.get(key)) is not None
    ]
    if not values:
        return None
    return sum(values) / len(values)


def dominant_direction_units(samples: list[dict[str, object]]) -> str:
    counts = {"T": 0, "M": 0}
    for sample in samples:
        units = measurement_units(sample.get("wind_direction"), "T")
        counts["M" if units == "M" else "T"] += 1
    return "M" if counts["M"] > counts["T"] else "T"


def dominant_measurement_units(
    samples: list[dict[str, object]],
    key: str,
    default: str,
) -> str:
    counts: dict[str, int] = {}
    for sample in samples:
        units = measurement_units(sample.get(key), default)
        counts[units] = counts.get(units, 0) + 1
    if not counts:
        return default
    return max(counts, key=counts.get)


def speed_knots(measurement_value_object: object) -> float | None:
    value = measurement_value(measurement_value_object)
    if value is None:
        return None
    units = measurement_units(measurement_value_object, "knots").lower()
    if units == "m/s":
        return value * 1.943844
    return value


def measurement_value(measurement_value_object: object) -> float | None:
    if not isinstance(measurement_value_object, dict):
        return None
    value = measurement_value_object.get("value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def measurement_units(measurement_value_object: object, default: str) -> str:
    if not isinstance(measurement_value_object, dict):
        return default
    units = measurement_value_object.get("units")
    if isinstance(units, str) and units.strip():
        return units.strip()
    return default


def measurement(value: float | None, units: str) -> dict[str, object] | None:
    if value is None:
        return None
    return {"value": round_weather_value(value), "units": units}


def direction_measurement(value: float | None, units: str) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "value": round_weather_value(value),
        "units": units,
        "symbol": symbolic_wind_direction(value),
    }


def normalize_degrees(value: float) -> float:
    return ((value % 360.0) + 360.0) % 360.0


def signed_angle_difference(angle: float | None, reference: float | None) -> float:
    if angle is None or reference is None:
        return 0.0
    return ((angle - reference + 540.0) % 360.0) - 180.0


def count_crossings(values: list[float]) -> int:
    crossings = 0
    previous_sign = 0

    for value in values:
        sign = 0 if abs(value) < 1 else (1 if value > 0 else -1)
        if sign == 0:
            continue
        if previous_sign != 0 and sign != previous_sign:
            crossings += 1
        previous_sign = sign
    return crossings


def format_wind_headline(direction: float | None, speed: float | None) -> str:
    if direction is None or speed is None:
        return "-"
    return f"{speed:.1f} kn {symbolic_wind_direction(direction) or '-'}"


def format_wind_detail(direction: float | None, units: str) -> str:
    if direction is None:
        return "-"
    symbol = symbolic_wind_direction(direction) or "-"
    return f"{symbol} ({normalize_degrees(direction):.1f}\u00b0 {units})"


def format_angle_size(value: float) -> str:
    return f"{abs(value):.1f}\u00b0"


def format_signed_angle(value: float) -> str:
    prefix = "+" if value > 0 else ""
    return f"{prefix}{value:.1f}\u00b0"


def format_pressure(value: float | None, units: str) -> str:
    if value is None:
        return "-"
    return f"{value:.1f} {units}"


def format_pressure_delta(value: float, units: str) -> str:
    return f"{abs(value):.1f} {units}"


def format_signed_pressure_delta(value: float, units: str) -> str:
    prefix = "+" if value > 0 else ""
    return f"{prefix}{value:.1f} {units}"


def format_temperature(value: float | None, units: str) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}\u00b0 {units}"


def format_temperature_delta(value: float, units: str) -> str:
    return f"{abs(value):.1f}\u00b0 {units}"


def format_signed_temperature_delta(value: float, units: str) -> str:
    prefix = "+" if value > 0 else ""
    return f"{prefix}{value:.1f}\u00b0 {units}"
