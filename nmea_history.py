"""History-oriented helpers for plot-ready NMEA wind data."""

from __future__ import annotations

from common_helpers import average_value, format_utc_datetime
from nmea_plot_analysis import build_plot_analysis, enrich_plot_samples
from nmea_aggregation import FrameAggregator, NMEAError, parse_sentence
from nmea_weather import (
    round_weather_value,
    symbolic_wind_direction,
    weather_temperature,
    weather_wind_direction,
    weather_wind_speed,
)
from repository_store import MessageRecord


def build_history_summary(
    org: str,
    source: str,
    records: list[MessageRecord],
    log_exception_callback,
) -> dict[str, object]:
    samples: list[dict[str, object]] = []

    for record in records:
        frame = history_frame_from_record(record, log_exception_callback)
        sample = history_sample_from_frame(record, frame)
        if sample is not None:
            samples.append(sample)

    samples = enrich_plot_samples(samples)

    analysis = build_plot_analysis(samples)
    public_samples = [history_public_sample(sample) for sample in samples]

    return {
        "org": org,
        "source": source,
        "record_count": len(records),
        "sample_count": len(public_samples),
        "oldest_utc_time": format_utc_datetime(records[0].utc) if records else None,
        "latest_utc_time": format_utc_datetime(records[-1].utc) if records else None,
        "samples": public_samples,
        "analysis": analysis,
    }


def history_frame_from_record(
    record: MessageRecord,
    log_exception_callback,
) -> FrameAggregator:
    frame = FrameAggregator()

    for raw_sentence in record.sentences:
        if not raw_sentence.strip():
            continue

        try:
            sentence = parse_sentence(raw_sentence)
            if sentence.sentence_type != "ZDA":
                frame.add_sentence(sentence)
        except NMEAError as exc:
            log_exception_callback("invalid history sentence skipped", exc, raw_sentence)

    return frame


def history_sample_from_frame(
    record: MessageRecord,
    frame: FrameAggregator,
) -> dict[str, object] | None:
    direction, direction_units = weather_wind_direction(frame)
    speed, speed_units = weather_wind_speed(frame)
    pressure, pressure_units = history_barometric_pressure(frame)
    temperature = weather_temperature(frame)

    return build_history_sample(
        record,
        direction,
        direction_units,
        speed,
        speed_units,
        pressure,
        pressure_units,
        temperature,
        "C" if temperature is not None else None,
    )


def build_history_sample(
    record: MessageRecord,
    direction: float | None,
    direction_units: str | None,
    speed: float | None,
    speed_units: str | None,
    pressure: float | None,
    pressure_units: str | None,
    temperature: float | None,
    temperature_units: str | None,
) -> dict[str, object] | None:
    if direction is None:
        return None

    return {
        "utc_time": format_utc_datetime(record.utc),
        "wind_direction": {
            "value": round_weather_value(direction),
            "units": direction_units,
            "symbol": symbolic_wind_direction(direction),
        },
        "wind_speed": {
            "value": round_weather_value(speed),
            "units": speed_units,
        },
        "barometric_pressure": {
            "value": round_weather_value(pressure),
            "units": pressure_units,
        },
        "temperature": {
            "value": round_weather_value(temperature),
            "units": temperature_units,
        },
    }


def history_barometric_pressure(frame: FrameAggregator) -> tuple[float | None, str | None]:
    pressure_bars_index = 1
    count = frame.mda.counts[pressure_bars_index]
    if count > 0:
        return average_value(frame.mda.sums[pressure_bars_index], count) * 1000.0, "mb"

    pressure_inches_index = 0
    count = frame.mda.counts[pressure_inches_index]
    if count > 0:
        return average_value(frame.mda.sums[pressure_inches_index], count), "inHg"

    return None, None


def history_public_sample(sample: dict[str, object]) -> dict[str, object]:
    return {
        "utc_time": sample["utc_time"],
        "wind_direction": sample["wind_direction"],
        "wind_speed": sample["wind_speed"],
        "rolling_wind_direction": sample.get("rolling_wind_direction"),
    }
