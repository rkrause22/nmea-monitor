"""Weather-oriented helpers built on top of NMEA aggregation."""

from __future__ import annotations

from datetime import datetime, timedelta

from repository_store import MessageRecord
from common_helpers import (
    average_direction_degrees,
    average_geographic_degrees,
    average_value,
    format_utc_datetime,
    rotate,
    vector_add,
)
from nmea_aggregation import (
    FrameAggregator,
    NMEAError,
    parse_sentence,
    parse_zda_datetime,
)


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
UPWIND_VMG_BY_WIND_SPEED = [
    (5.0, 2.0),
    (8.0, 2.6),
    (10.0, 3.0),
    (12.0, 3.3),
    (15.0, 3.6),
    (18.0, 3.8),
    (22.0, 3.9),
]


def build_weather_summary(
    org: str,
    source: str,
    records: list[MessageRecord],
    log_exception_callback,
) -> dict[str, object]:
    frame = FrameAggregator()
    utc_time = records[0].utc
    utc_end_time = records[-1].utc

    for record in records:
        for raw_sentence in record.sentences:
            if not raw_sentence.strip():
                continue
            try:
                sentence = parse_sentence(raw_sentence)
                if sentence.sentence_type == "ZDA":
                    utc_time = parse_zda_datetime(sentence).replace(tzinfo=None)
                else:
                    frame.add_sentence(sentence)
            except NMEAError as exc:
                log_exception_callback("invalid weather sentence skipped", exc, raw_sentence)

    latitude, longitude = weather_position(frame)
    temperature = weather_temperature(frame)
    wind_direction, wind_direction_units = weather_wind_direction(frame)
    wind_direction_symbol = symbolic_wind_direction(wind_direction)
    wind_speed, wind_speed_units = weather_wind_speed(frame)
    windward_range = weather_windward_range(wind_speed, wind_speed_units)
    windward = weather_offset_position(
        latitude,
        longitude,
        wind_direction,
        windward_range,
    )
    startpin = weather_startpin_position(latitude, longitude, wind_direction, 100.0)

    summary = {
        "org": org,
        "source": source,
        "utc_time": format_utc_datetime(utc_time),
        "latitude": latitude,
        "longitude": longitude,
        "temperature": {
            "value": round_weather_value(temperature),
            "units": "C" if temperature is not None else None,
        },
        "wind_direction": {
            "value": round_weather_value(wind_direction),
            "units": wind_direction_units,
            "symbol": wind_direction_symbol,
        },
        "wind_speed": {
            "value": round_weather_value(wind_speed),
            "units": wind_speed_units,
        },
        "windward": windward,
        "startpin": startpin,
    }
    if utc_end_time - utc_time > timedelta(minutes=1):
        summary["utc_end_time"] = format_utc_datetime(utc_end_time)
    return summary


def weather_position(frame: FrameAggregator) -> tuple[float | None, float | None]:
    if not frame.gga.has_data():
        return None, None
    return average_geographic_degrees(
        frame.gga.position_x_sum,
        frame.gga.position_y_sum,
        frame.gga.position_z_sum,
    )


def weather_offset_position(
    latitude: float | None,
    longitude: float | None,
    bearing: float | None,
    range_metres: float | None,
) -> dict[str, float] | None:
    if latitude is None or longitude is None or bearing is None or range_metres is None:
        return None
    new_latitude, new_longitude = vector_add(
        latitude,
        longitude,
        bearing,
        range_metres,
    )
    return {
        "latitude": new_latitude,
        "longitude": new_longitude,
    }


def weather_windward_range(
    wind_speed: float | None,
    wind_speed_units: str | None,
) -> float | None:
    if wind_speed is None or wind_speed_units is None:
        return None

    if wind_speed_units == "knots":
        wind_speed_knots = wind_speed
    elif wind_speed_units == "m/s":
        wind_speed_knots = wind_speed * 1.943844
    else:
        return None

    for maximum_speed, vmg in UPWIND_VMG_BY_WIND_SPEED:
        if wind_speed_knots <= maximum_speed:
            return 247.0 * vmg

    return 247.0 * UPWIND_VMG_BY_WIND_SPEED[-1][1]


def weather_startpin_position(
    latitude: float | None,
    longitude: float | None,
    wind_direction: float | None,
    range_metres: float,
) -> dict[str, float] | None:
    if wind_direction is None:
        return None
    return weather_offset_position(
        latitude,
        longitude,
        rotate(wind_direction, -90.0),
        range_metres,
    )


def weather_temperature(frame: FrameAggregator) -> float | None:
    air_temperature_index = 2
    count = frame.mda.counts[air_temperature_index]
    if count == 0:
        return None
    return average_value(frame.mda.sums[air_temperature_index], count)


def weather_wind_direction(frame: FrameAggregator) -> tuple[float | None, str | None]:
    if frame.mwd.has_data():
        if frame.mwd.magnetic_direction_cos_sum or frame.mwd.magnetic_direction_sin_sum:
            return (
                average_direction_degrees(
                    frame.mwd.magnetic_direction_sin_sum,
                    frame.mwd.magnetic_direction_cos_sum,
                ),
                "M",
            )
        return (
            average_direction_degrees(
                frame.mwd.true_direction_sin_sum,
                frame.mwd.true_direction_cos_sum,
            ),
            "T",
        )

    magnetic_direction_index = 8
    if frame.mda.counts[magnetic_direction_index] > 0:
        return (
            average_direction_degrees(
                frame.mda.direction_sin_sums[magnetic_direction_index],
                frame.mda.direction_cos_sums[magnetic_direction_index],
            ),
            "M",
        )

    true_direction_index = 7
    if frame.mda.counts[true_direction_index] > 0:
        return (
            average_direction_degrees(
                frame.mda.direction_sin_sums[true_direction_index],
                frame.mda.direction_cos_sums[true_direction_index],
            ),
            "T",
        )

    return None, None


def round_weather_value(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 1)


def symbolic_wind_direction(value: float | None) -> str | None:
    if value is None:
        return None

    normalized = value % 360.0
    for upper_bound, symbol in COMPASS_DIRECTIONS:
        if normalized < upper_bound:
            return symbol
    return COMPASS_DIRECTIONS[-1][1]


def weather_wind_speed(frame: FrameAggregator) -> tuple[float | None, str | None]:
    if frame.mwd.has_data():
        return average_value(frame.mwd.knots_sum, frame.mwd.count), "knots"

    wind_knots_index = 9
    count = frame.mda.counts[wind_knots_index]
    if count > 0:
        return average_value(frame.mda.sums[wind_knots_index], count), "knots"

    wind_metres_per_second_index = 10
    count = frame.mda.counts[wind_metres_per_second_index]
    if count > 0:
        return (
            average_value(frame.mda.sums[wind_metres_per_second_index], count),
            "m/s",
        )

    return None, None
