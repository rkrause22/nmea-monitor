"""Reusable NMEA-0183 data parsers and sentence aggregators."""

from __future__ import annotations

import math
import os
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


class NMEAError(ValueError):
    """Raised when an NMEA sentence is malformed or unsupported."""


@dataclass
class NMEASentence:
    raw: str
    formatter: str
    sentence_type: str
    fields: list[str]


@dataclass
class MWDData:
    true_direction: float
    magnetic_direction: float
    knots: float
    metres_per_second: float


@dataclass
class MWDAggregator:
    true_direction_sin_sum: float = 0.0
    true_direction_cos_sum: float = 0.0
    magnetic_direction_sin_sum: float = 0.0
    magnetic_direction_cos_sum: float = 0.0
    knots_sum: float = 0.0
    metres_per_second_sum: float = 0.0
    count: int = 0

    def add(self, sample: MWDData) -> None:
        true_direction_sin, true_direction_cos = polar_components(
            sample.true_direction,
            sample.knots,
        )
        magnetic_direction_sin, magnetic_direction_cos = polar_components(
            sample.magnetic_direction,
            sample.knots,
        )
        self.true_direction_sin_sum += true_direction_sin
        self.true_direction_cos_sum += true_direction_cos
        self.magnetic_direction_sin_sum += magnetic_direction_sin
        self.magnetic_direction_cos_sum += magnetic_direction_cos
        self.knots_sum += sample.knots
        self.metres_per_second_sum += sample.metres_per_second
        self.count += 1

    def extend(self, other: "MWDAggregator") -> None:
        self.true_direction_sin_sum += other.true_direction_sin_sum
        self.true_direction_cos_sum += other.true_direction_cos_sum
        self.magnetic_direction_sin_sum += other.magnetic_direction_sin_sum
        self.magnetic_direction_cos_sum += other.magnetic_direction_cos_sum
        self.knots_sum += other.knots_sum
        self.metres_per_second_sum += other.metres_per_second_sum
        self.count += other.count

    def has_data(self) -> bool:
        return self.count > 0

    def average_sentence(self) -> str:
        if self.count == 0:
            raise NMEAError("cannot emit averaged MWD without samples")
        body = (
            f"WIMWD,{average_direction_degrees(self.true_direction_sin_sum, self.true_direction_cos_sum):.1f},T,"
            f"{average_direction_degrees(self.magnetic_direction_sin_sum, self.magnetic_direction_cos_sum):.1f},M,"
            f"{self.knots_sum / self.count:.1f},N,{self.metres_per_second_sum / self.count:.1f},M"
        )
        return build_sentence(body)


@dataclass
class MDAData:
    barometric_pressure_inches: Optional[float]
    barometric_pressure_bars: Optional[float]
    air_temperature_celsius: Optional[float]
    water_temperature_celsius: Optional[float]
    relative_humidity: Optional[float]
    absolute_humidity: Optional[float]
    dew_point_celsius: Optional[float]
    true_wind_direction: Optional[float]
    magnetic_wind_direction: Optional[float]
    wind_speed_knots: Optional[float]
    wind_speed_metres_per_second: Optional[float]


class MDAAggregator:
    def __init__(self) -> None:
        self.sums = [0.0] * 11
        self.counts = [0] * 11
        self.direction_sin_sums = [0.0] * 11
        self.direction_cos_sums = [0.0] * 11

    def add(self, sample: MDAData) -> None:
        values = [
            sample.barometric_pressure_inches,
            sample.barometric_pressure_bars,
            sample.air_temperature_celsius,
            sample.water_temperature_celsius,
            sample.relative_humidity,
            sample.absolute_humidity,
            sample.dew_point_celsius,
            sample.true_wind_direction,
            sample.magnetic_wind_direction,
            sample.wind_speed_knots,
            sample.wind_speed_metres_per_second,
        ]
        for index, value in enumerate(values):
            if value is None:
                continue
            if index in (7, 8):
                weight = sample.wind_speed_knots
                if weight is None:
                    weight = sample.wind_speed_metres_per_second
                direction_sin, direction_cos = polar_components(value, weight or 0.0)
                self.direction_sin_sums[index] += direction_sin
                self.direction_cos_sums[index] += direction_cos
            else:
                self.sums[index] += value
            self.counts[index] += 1

    def extend(self, other: "MDAAggregator") -> None:
        for index in range(11):
            self.sums[index] += other.sums[index]
            self.counts[index] += other.counts[index]
            self.direction_sin_sums[index] += other.direction_sin_sums[index]
            self.direction_cos_sums[index] += other.direction_cos_sums[index]

    def has_data(self) -> bool:
        return any(count > 0 for count in self.counts)

    def average_sentence(self) -> str:
        if not self.has_data():
            raise NMEAError("cannot emit averaged MDA without samples")
        averages = [
            ""
            if count == 0
            else format_average(average_mda_field(self, index), index)
            for index, count in enumerate(self.counts)
        ]
        body = (
            f"WIMDA,{averages[0]},I,{averages[1]},B,{averages[2]},C,{averages[3]},C,"
            f"{averages[4]},{averages[5]},{averages[6]},C,{averages[7]},T,"
            f"{averages[8]},M,{averages[9]},N,{averages[10]},M"
        )
        return build_sentence(body)


@dataclass
class GGAData:
    fix_time: str
    latitude_degrees: float
    longitude_degrees: float
    fix_quality: int
    satellites_in_use: int
    hdop: float
    altitude_metres: float
    geoid_separation_metres: Optional[float]
    dgps_age_seconds: Optional[float]
    reference_station_id: Optional[str]


@dataclass
class GGAAggregator:
    latitude_sum: float = 0.0
    longitude_sum: float = 0.0
    satellites_sum: float = 0.0
    hdop_sum: float = 0.0
    altitude_sum: float = 0.0
    geoid_separation_sum: float = 0.0
    geoid_separation_count: int = 0
    count: int = 0
    best_fix_quality: int = 0
    last_fix_time: Optional[str] = None
    last_dgps_age_seconds: Optional[float] = None
    last_reference_station_id: Optional[str] = None

    def add(self, sample: GGAData) -> None:
        self.latitude_sum += sample.latitude_degrees
        self.longitude_sum += sample.longitude_degrees
        self.satellites_sum += sample.satellites_in_use
        self.hdop_sum += sample.hdop
        self.altitude_sum += sample.altitude_metres
        self.count += 1
        self.best_fix_quality = max(self.best_fix_quality, sample.fix_quality)
        self.last_fix_time = sample.fix_time
        if sample.geoid_separation_metres is not None:
            self.geoid_separation_sum += sample.geoid_separation_metres
            self.geoid_separation_count += 1
        if sample.dgps_age_seconds is not None:
            self.last_dgps_age_seconds = sample.dgps_age_seconds
        if sample.reference_station_id:
            self.last_reference_station_id = sample.reference_station_id

    def extend(self, other: "GGAAggregator") -> None:
        self.latitude_sum += other.latitude_sum
        self.longitude_sum += other.longitude_sum
        self.satellites_sum += other.satellites_sum
        self.hdop_sum += other.hdop_sum
        self.altitude_sum += other.altitude_sum
        self.geoid_separation_sum += other.geoid_separation_sum
        self.geoid_separation_count += other.geoid_separation_count
        self.count += other.count
        self.best_fix_quality = max(self.best_fix_quality, other.best_fix_quality)
        if other.last_fix_time is not None:
            self.last_fix_time = other.last_fix_time
        if other.last_dgps_age_seconds is not None:
            self.last_dgps_age_seconds = other.last_dgps_age_seconds
        if other.last_reference_station_id:
            self.last_reference_station_id = other.last_reference_station_id

    def has_data(self) -> bool:
        return self.count > 0

    def average_sentence(self) -> str:
        if self.count == 0:
            raise NMEAError("cannot emit averaged GGA without samples")
        if self.last_fix_time is None:
            raise NMEAError("cannot emit averaged GGA without a fix time")
        latitude_text, latitude_hemisphere = decimal_degrees_to_nmea_latitude(
            self.latitude_sum / self.count
        )
        longitude_text, longitude_hemisphere = decimal_degrees_to_nmea_longitude(
            self.longitude_sum / self.count
        )
        geoid_separation_text = ""
        geoid_unit = ""
        if self.geoid_separation_count > 0:
            geoid_separation_text = f"{self.geoid_separation_sum / self.geoid_separation_count:.1f}"
            geoid_unit = "M"
        dgps_age_text = "" if self.last_dgps_age_seconds is None else f"{self.last_dgps_age_seconds:.1f}"
        body = (
            f"GPGGA,{self.last_fix_time},{latitude_text},{latitude_hemisphere},"
            f"{longitude_text},{longitude_hemisphere},{self.best_fix_quality},"
            f"{int(round(self.satellites_sum / self.count)):02d},{self.hdop_sum / self.count:.1f},"
            f"{self.altitude_sum / self.count:.1f},M,{geoid_separation_text},{geoid_unit},"
            f"{dgps_age_text},{self.last_reference_station_id or ''}"
        )
        return build_sentence(body)


def compute_checksum(body: str) -> int:
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return checksum


def parse_sentence(raw_line: str) -> NMEASentence:
    raw = raw_line.strip()
    if not raw:
        raise NMEAError("empty input")
    if not raw.startswith("$"):
        raise NMEAError("missing '$' prefix")
    if "*" not in raw:
        raise NMEAError("missing checksum separator")

    body, checksum_text = raw[1:].split("*", 1)
    if len(checksum_text) != 2:
        raise NMEAError("checksum must be two hex characters")

    try:
        expected_checksum = int(checksum_text, 16)
    except ValueError as exc:
        raise NMEAError("checksum is not valid hexadecimal") from exc

    actual_checksum = compute_checksum(body)
    if actual_checksum != expected_checksum:
        raise NMEAError(
            f"checksum mismatch: expected {expected_checksum:02X}, got {actual_checksum:02X}"
        )

    fields = body.split(",")
    formatter = fields[0]
    if len(formatter) < 5:
        raise NMEAError("formatter is too short")

    return NMEASentence(
        raw=raw,
        formatter=formatter,
        sentence_type=formatter[-3:].upper(),
        fields=fields[1:],
    )


def parse_zda_datetime(sentence: NMEASentence) -> datetime:
    if sentence.sentence_type != "ZDA":
        raise NMEAError("not a ZDA sentence")
    if len(sentence.fields) < 4:
        raise NMEAError("ZDA sentence does not contain enough fields")

    time_text, day_text, month_text, year_text = sentence.fields[:4]
    if len(time_text) < 6:
        raise NMEAError("ZDA time is too short")

    try:
        hours = int(time_text[0:2])
        minutes = int(time_text[2:4])
        seconds = float(time_text[4:])
        day = int(day_text)
        month = int(month_text)
        year = int(year_text)
    except ValueError as exc:
        raise NMEAError("ZDA fields are not numeric") from exc

    whole_seconds = int(seconds)
    microseconds = int(round((seconds - whole_seconds) * 1_000_000))
    if microseconds == 1_000_000:
        whole_seconds += 1
        microseconds = 0

    try:
        return datetime(
            year,
            month,
            day,
            hours,
            minutes,
            whole_seconds,
            microseconds,
            tzinfo=timezone.utc,
        )
    except ValueError as exc:
        raise NMEAError("ZDA date/time is out of range") from exc


def format_utc_datetime(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def sentence_text(sentence: NMEASentence | str) -> str:
    if isinstance(sentence, NMEASentence):
        return sentence.raw
    return sentence.strip()


def log_exception(
    program_name: str,
    script_file: str,
    message: str,
    exc: BaseException,
    sentence: Optional[NMEASentence | str] = None,
) -> None:
    script_dir = os.path.dirname(os.path.abspath(script_file))
    logs_dir = os.path.join(script_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = os.path.join(logs_dir, f"{program_name}-{today}.log")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
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
    script_dir = os.path.dirname(os.path.abspath(script_file))
    logs_dir = os.path.join(script_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = os.path.join(logs_dir, f"{program_name}-{today}.log")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} [ERROR] {program_name} - {message}\n")


def build_sentence(body: str) -> str:
    return f"${body}*{compute_checksum(body):02X}"


def polar_components(direction_degrees: float, weight: float = 1.0) -> tuple[float, float]:
    radians = math.radians(direction_degrees)
    return math.sin(radians) * weight, math.cos(radians) * weight


def average_direction_degrees(sin_sum: float, cos_sum: float) -> float:
    if math.hypot(sin_sum, cos_sum) < 1e-12:
        return 0.0
    degrees = math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0
    if math.isclose(degrees, 360.0, abs_tol=1e-9):
        return 0.0
    return degrees


def average_mda_field(aggregator: MDAAggregator, field_index: int) -> float:
    if field_index in (7, 8):
        return average_direction_degrees(
            aggregator.direction_sin_sums[field_index],
            aggregator.direction_cos_sums[field_index],
        )
    return aggregator.sums[field_index] / aggregator.counts[field_index]


def format_average(value: float, field_index: int) -> str:
    if field_index in (0, 1):
        return f"{value:.4f}"
    return f"{value:.1f}"


def parse_optional_float(value: str, field_name: str) -> Optional[float]:
    if value == "":
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise NMEAError(f"{field_name} is not numeric") from exc


def validate_optional_unit(value: str, unit: str, expected_unit: str, field_name: str) -> None:
    if value == "":
        if unit not in ("", expected_unit):
            raise NMEAError(f"{field_name} unit is invalid")
        return
    if unit != expected_unit:
        raise NMEAError(f"{field_name} unit is invalid")


def parse_latitude(latitude_text: str, hemisphere: str) -> float:
    if latitude_text == "" or hemisphere not in ("N", "S"):
        raise NMEAError("GGA latitude is invalid")
    try:
        degrees = int(latitude_text[:2])
        minutes = float(latitude_text[2:])
    except ValueError as exc:
        raise NMEAError("GGA latitude is not numeric") from exc
    value = degrees + (minutes / 60.0)
    return -value if hemisphere == "S" else value


def parse_longitude(longitude_text: str, hemisphere: str) -> float:
    if longitude_text == "" or hemisphere not in ("E", "W"):
        raise NMEAError("GGA longitude is invalid")
    try:
        degrees = int(longitude_text[:3])
        minutes = float(longitude_text[3:])
    except ValueError as exc:
        raise NMEAError("GGA longitude is not numeric") from exc
    value = degrees + (minutes / 60.0)
    return -value if hemisphere == "W" else value


def decimal_degrees_to_nmea_latitude(value: float) -> tuple[str, str]:
    hemisphere = "S" if value < 0 else "N"
    absolute_value = abs(value)
    degrees = int(absolute_value)
    minutes = (absolute_value - degrees) * 60.0
    return f"{degrees:02d}{minutes:07.4f}", hemisphere


def decimal_degrees_to_nmea_longitude(value: float) -> tuple[str, str]:
    hemisphere = "W" if value < 0 else "E"
    absolute_value = abs(value)
    degrees = int(absolute_value)
    minutes = (absolute_value - degrees) * 60.0
    return f"{degrees:03d}{minutes:07.4f}", hemisphere


def parse_mwd(sentence: object) -> MWDData:
    fields = getattr(sentence, "fields")
    if getattr(sentence, "sentence_type") != "MWD":
        raise NMEAError("not an MWD sentence")
    if len(fields) < 8:
        raise NMEAError("MWD sentence does not contain enough fields")
    true_direction, true_ref, magnetic_direction, magnetic_ref, knots, knots_unit, ms, ms_unit = fields[:8]
    if true_ref != "T" or magnetic_ref != "M" or knots_unit != "N" or ms_unit != "M":
        raise NMEAError("MWD reference or units are invalid")
    try:
        return MWDData(float(true_direction), float(magnetic_direction), float(knots), float(ms))
    except ValueError as exc:
        raise NMEAError("MWD numeric fields are invalid") from exc


def parse_mda(sentence: object) -> MDAData:
    fields = getattr(sentence, "fields")
    if getattr(sentence, "sentence_type") != "MDA":
        raise NMEAError("not an MDA sentence")
    if len(fields) < 20:
        raise NMEAError("MDA sentence does not contain enough fields")
    (
        pressure_inches,
        pressure_inches_unit,
        pressure_bars,
        pressure_bars_unit,
        air_temperature,
        air_temperature_unit,
        water_temperature,
        water_temperature_unit,
        relative_humidity,
        absolute_humidity,
        dew_point,
        dew_point_unit,
        true_direction,
        true_direction_unit,
        magnetic_direction,
        magnetic_direction_unit,
        wind_knots,
        wind_knots_unit,
        wind_metres_per_second,
        wind_metres_per_second_unit,
    ) = fields[:20]
    if pressure_inches_unit != "I" or pressure_bars_unit != "B":
        raise NMEAError("MDA pressure units are invalid")
    if air_temperature_unit != "C":
        raise NMEAError("MDA air temperature unit is invalid")
    validate_optional_unit(water_temperature, water_temperature_unit, "C", "MDA water temperature")
    validate_optional_unit(dew_point, dew_point_unit, "C", "MDA dew point")
    validate_optional_unit(true_direction, true_direction_unit, "T", "MDA true wind direction")
    validate_optional_unit(magnetic_direction, magnetic_direction_unit, "M", "MDA magnetic wind direction")
    validate_optional_unit(wind_knots, wind_knots_unit, "N", "MDA wind speed knots")
    validate_optional_unit(wind_metres_per_second, wind_metres_per_second_unit, "M", "MDA wind speed metres/second")
    return MDAData(
        parse_optional_float(pressure_inches, "MDA pressure inches"),
        parse_optional_float(pressure_bars, "MDA pressure bars"),
        parse_optional_float(air_temperature, "MDA air temperature"),
        parse_optional_float(water_temperature, "MDA water temperature"),
        parse_optional_float(relative_humidity, "MDA relative humidity"),
        parse_optional_float(absolute_humidity, "MDA absolute humidity"),
        parse_optional_float(dew_point, "MDA dew point"),
        parse_optional_float(true_direction, "MDA true wind direction"),
        parse_optional_float(magnetic_direction, "MDA magnetic wind direction"),
        parse_optional_float(wind_knots, "MDA wind speed knots"),
        parse_optional_float(wind_metres_per_second, "MDA wind speed metres/second"),
    )


def parse_gga(sentence: object) -> GGAData:
    fields = getattr(sentence, "fields")
    if getattr(sentence, "sentence_type") != "GGA":
        raise NMEAError("not a GGA sentence")
    if len(fields) < 14:
        raise NMEAError("GGA sentence does not contain enough fields")
    (
        fix_time,
        latitude,
        latitude_hemisphere,
        longitude,
        longitude_hemisphere,
        fix_quality_text,
        satellites_text,
        hdop_text,
        altitude_text,
        altitude_unit,
        geoid_separation_text,
        geoid_separation_unit,
        dgps_age_text,
        reference_station_id,
    ) = fields[:14]
    if len(geoid_separation_unit) > 1:
        geoid_separation_unit = geoid_separation_unit[0:1]
    try:
        fix_quality = int(fix_quality_text)
        satellites_in_use = int(satellites_text)
        hdop = float(hdop_text)
        altitude_metres = float(altitude_text)
    except ValueError as exc:
        raise NMEAError("GGA numeric fields are invalid") from exc
    if fix_quality <= 0:
        raise NMEAError("GGA fix is not valid")
    if fix_time == "":
        raise NMEAError("GGA fix time is missing")
    validate_optional_unit(altitude_text, altitude_unit, "M", "GGA altitude")
    validate_optional_unit(geoid_separation_text, geoid_separation_unit, "M", "GGA geoid separation")
    return GGAData(
        fix_time,
        parse_latitude(latitude, latitude_hemisphere),
        parse_longitude(longitude, longitude_hemisphere),
        fix_quality,
        satellites_in_use,
        hdop,
        altitude_metres,
        parse_optional_float(geoid_separation_text, "GGA geoid separation"),
        parse_optional_float(dgps_age_text, "GGA DGPS age"),
        reference_station_id or None,
    )


class FrameAggregator:
    """Aggregate MWD, MDA, and GGA samples collected within one ZDA frame."""

    def __init__(self) -> None:
        self.mwd = MWDAggregator()
        self.mda = MDAAggregator()
        self.gga = GGAAggregator()

    def add_sentence(self, sentence: object) -> None:
        sentence_type = getattr(sentence, "sentence_type")
        if sentence_type == "MWD":
            self.mwd.add(parse_mwd(sentence))
        elif sentence_type == "MDA":
            self.mda.add(parse_mda(sentence))
        elif sentence_type == "GGA":
            self.gga.add(parse_gga(sentence))

    def extend(self, other: "FrameAggregator") -> None:
        self.mwd.extend(other.mwd)
        self.mda.extend(other.mda)
        self.gga.extend(other.gga)


def combine_frames(frames: list[FrameAggregator]) -> FrameAggregator:
    combined = FrameAggregator()
    for frame in frames:
        combined.extend(frame)
    return combined
