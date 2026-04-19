#!/usr/bin/env python3
"""Read NMEA-0183 from a file or serial port and emit aggregated ZDA/MWD/MDA/GGA output."""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Iterator, Optional, TextIO

try:
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
except ImportError:  # pragma: no cover - depends on local environment
    serial = None
    list_ports = None


COMMON_BAUD_RATES = [4800, 9600, 19200, 38400, 115200]
PREFERRED_DEVICE_NAMES = [
    "/dev/serial0",
    "/dev/ttyAMA0",
    "/dev/ttyS0",
    "/dev/ttyUSB0",
    "/dev/ttyACM0",
]
ZDA_WARNING_INTERVAL_SECONDS = 60.0
ZDA_TIMEOUT_SECONDS = 600.0
DEBUG_LOG_HANDLE: Optional[TextIO] = None


class NMEAError(ValueError):
    """Raised when an NMEA sentence is malformed."""


def open_debug_log() -> None:
    global DEBUG_LOG_HANDLE
    script_path = os.path.abspath(__file__)
    script_dir = os.path.dirname(script_path)
    script_name = os.path.splitext(os.path.basename(script_path))[0]
    logs_dir = os.path.join(script_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.join(logs_dir, f"{script_name}.log")
    DEBUG_LOG_HANDLE = open(log_path, "a", encoding="utf-8")
    DEBUG_LOG_HANDLE.write(f"=== Debug session started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    DEBUG_LOG_HANDLE.flush()


def close_debug_log() -> None:
    global DEBUG_LOG_HANDLE
    if DEBUG_LOG_HANDLE is None:
        return
    DEBUG_LOG_HANDLE.write(f"=== Debug session ended {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    DEBUG_LOG_HANDLE.close()
    DEBUG_LOG_HANDLE = None


def log_nmea_error(context: str, error: NMEAError, raw: Optional[str] = None) -> None:
    if DEBUG_LOG_HANDLE is None:
        return
    DEBUG_LOG_HANDLE.write(f"[{context}] {error}\n")
    if raw is not None:
        DEBUG_LOG_HANDLE.write(f"  raw: {raw.rstrip()}\n")
    DEBUG_LOG_HANDLE.flush()


def log_debug_message(context: str, message: str) -> None:
    if DEBUG_LOG_HANDLE is None:
        return
    DEBUG_LOG_HANDLE.write(f"[{context}] {message}\n")
    DEBUG_LOG_HANDLE.flush()


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
        true_direction_sin, true_direction_cos = polar_components(sample.true_direction)
        magnetic_direction_sin, magnetic_direction_cos = polar_components(sample.magnetic_direction)
        self.true_direction_sin_sum += true_direction_sin
        self.true_direction_cos_sum += true_direction_cos
        self.magnetic_direction_sin_sum += magnetic_direction_sin
        self.magnetic_direction_cos_sum += magnetic_direction_cos
        self.knots_sum += sample.knots
        self.metres_per_second_sum += sample.metres_per_second
        self.count += 1

    def average_sentence(self) -> str:
        if self.count == 0:
            averages = (0.0, 0.0, 0.0, 0.0)
        else:
            averages = (
                average_direction_degrees(self.true_direction_sin_sum, self.true_direction_cos_sum),
                average_direction_degrees(
                    self.magnetic_direction_sin_sum,
                    self.magnetic_direction_cos_sum,
                ),
                self.knots_sum / self.count,
                self.metres_per_second_sum / self.count,
            )

        body = (
            f"WIMWD,{averages[0]:.1f},T,{averages[1]:.1f},M,"
            f"{averages[2]:.1f},N,{averages[3]:.1f},M"
        )
        return build_sentence(body)

    def has_data(self) -> bool:
        return self.count > 0

    def reset(self) -> None:
        self.true_direction_sin_sum = 0.0
        self.true_direction_cos_sum = 0.0
        self.magnetic_direction_sin_sum = 0.0
        self.magnetic_direction_cos_sum = 0.0
        self.knots_sum = 0.0
        self.metres_per_second_sum = 0.0
        self.count = 0


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


@dataclass
class MDAAggregator:
    sums: list[float]
    counts: list[int]
    direction_sin_sums: list[float]
    direction_cos_sums: list[float]

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
                direction_sin, direction_cos = polar_components(value)
                self.direction_sin_sums[index] += direction_sin
                self.direction_cos_sums[index] += direction_cos
            else:
                self.sums[index] += value
            self.counts[index] += 1

    def has_data(self) -> bool:
        return any(count > 0 for count in self.counts)

    def average_sentence(self) -> str:
        averages = [
            ""
            if count == 0
            else format_average(average_mda_field(self, index), index)
            for index, (total, count) in enumerate(zip(self.sums, self.counts))
        ]
        body = (
            f"WIMDA,{averages[0]},I,{averages[1]},B,{averages[2]},C,{averages[3]},C,"
            f"{averages[4]},{averages[5]},{averages[6]},C,{averages[7]},T,"
            f"{averages[8]},M,{averages[9]},N,{averages[10]},M"
        )
        return build_sentence(body)

    def reset(self) -> None:
        self.sums = [0.0] * 11
        self.counts = [0] * 11
        self.direction_sin_sums = [0.0] * 11
        self.direction_cos_sums = [0.0] * 11


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
        satellites = int(round(self.satellites_sum / self.count))
        hdop = self.hdop_sum / self.count
        altitude = self.altitude_sum / self.count

        if self.geoid_separation_count > 0:
            geoid_separation_text = f"{self.geoid_separation_sum / self.geoid_separation_count:.1f}"
            geoid_unit = "M"
        else:
            geoid_separation_text = ""
            geoid_unit = ""

        dgps_age_text = (
            ""
            if self.last_dgps_age_seconds is None
            else f"{self.last_dgps_age_seconds:.1f}"
        )
        reference_station_id = self.last_reference_station_id or ""

        body = (
            f"GPGGA,{self.last_fix_time},{latitude_text},{latitude_hemisphere},"
            f"{longitude_text},{longitude_hemisphere},{self.best_fix_quality},"
            f"{satellites:02d},{hdop:.1f},{altitude:.1f},M,"
            f"{geoid_separation_text},{geoid_unit},{dgps_age_text},{reference_station_id}"
        )
        return build_sentence(body)

    def reset(self) -> None:
        self.latitude_sum = 0.0
        self.longitude_sum = 0.0
        self.satellites_sum = 0.0
        self.hdop_sum = 0.0
        self.altitude_sum = 0.0
        self.geoid_separation_sum = 0.0
        self.geoid_separation_count = 0
        self.count = 0
        self.best_fix_quality = 0
        self.last_fix_time = None
        self.last_dgps_age_seconds = None
        self.last_reference_station_id = None


def compute_checksum(body: str) -> int:
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return checksum


def build_sentence(body: str) -> str:
    return f"${body}*{compute_checksum(body):02X}"


def polar_components(direction_degrees: float) -> tuple[float, float]:
    radians = math.radians(direction_degrees)
    return math.sin(radians), math.cos(radians)


def average_direction_degrees(sin_sum: float, cos_sum: float) -> float:
    return math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0


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


def parse_latitude(latitude_text: str, hemisphere: str) -> float:
    if latitude_text == "" or hemisphere not in ("N", "S"):
        raise NMEAError("GGA latitude is invalid")
    if len(latitude_text) < 4:
        raise NMEAError("GGA latitude is too short")
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
    if len(longitude_text) < 5:
        raise NMEAError("GGA longitude is too short")
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
        raise NMEAError("checksum mismatch")

    fields = body.split(",")
    formatter = fields[0]
    if len(formatter) < 5:
        raise NMEAError("formatter is too short")

    return NMEASentence(
        raw=raw,
        formatter=formatter,
        sentence_type=formatter[-3:],
        fields=fields[1:],
    )


def parse_zda(sentence: NMEASentence) -> None:
    if sentence.sentence_type != "ZDA":
        raise NMEAError("not a ZDA sentence")
    if len(sentence.fields) < 4:
        raise NMEAError("ZDA sentence does not contain enough fields")

    time_text = sentence.fields[0]
    day_text = sentence.fields[1]
    month_text = sentence.fields[2]
    year_text = sentence.fields[3]

    if len(time_text) < 6:
        raise NMEAError("ZDA time is too short")

    try:
        hours = int(time_text[0:2])
        minutes = int(time_text[2:4])
        seconds = float(time_text[4:])
        day_value = int(day_text)
        month_value = int(month_text)
        year_value = int(year_text)
    except ValueError as exc:
        raise NMEAError("ZDA fields are not numeric") from exc

    if not (0 <= hours <= 23 and 0 <= minutes <= 59 and 0.0 <= seconds < 60.0):
        raise NMEAError("ZDA time is out of range")

    try:
        date(year_value, month_value, day_value)
    except ValueError as exc:
        raise NMEAError("ZDA date is out of range") from exc


def parse_mwd(sentence: NMEASentence) -> MWDData:
    if sentence.sentence_type != "MWD":
        raise NMEAError("not an MWD sentence")
    if len(sentence.fields) < 8:
        raise NMEAError("MWD sentence does not contain enough fields")

    true_direction, true_ref, magnetic_direction, magnetic_ref, knots, knots_unit, ms, ms_unit = (
        sentence.fields[:8]
    )

    if true_ref != "T" or magnetic_ref != "M" or knots_unit != "N" or ms_unit != "M":
        raise NMEAError("MWD reference or units are invalid")

    try:
        return MWDData(
            true_direction=float(true_direction),
            magnetic_direction=float(magnetic_direction),
            knots=float(knots),
            metres_per_second=float(ms),
        )
    except ValueError as exc:
        raise NMEAError("MWD numeric fields are invalid") from exc


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


def parse_mda(sentence: NMEASentence) -> MDAData:
    if sentence.sentence_type != "MDA":
        raise NMEAError("not an MDA sentence")
    if len(sentence.fields) < 20:
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
    ) = sentence.fields[:20]

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
        barometric_pressure_inches=parse_optional_float(pressure_inches, "MDA pressure inches"),
        barometric_pressure_bars=parse_optional_float(pressure_bars, "MDA pressure bars"),
        air_temperature_celsius=parse_optional_float(air_temperature, "MDA air temperature"),
        water_temperature_celsius=parse_optional_float(water_temperature, "MDA water temperature"),
        relative_humidity=parse_optional_float(relative_humidity, "MDA relative humidity"),
        absolute_humidity=parse_optional_float(absolute_humidity, "MDA absolute humidity"),
        dew_point_celsius=parse_optional_float(dew_point, "MDA dew point"),
        true_wind_direction=parse_optional_float(true_direction, "MDA true wind direction"),
        magnetic_wind_direction=parse_optional_float(magnetic_direction, "MDA magnetic wind direction"),
        wind_speed_knots=parse_optional_float(wind_knots, "MDA wind speed knots"),
        wind_speed_metres_per_second=parse_optional_float(
            wind_metres_per_second,
            "MDA wind speed metres/second",
        ),
    )


def parse_gga(sentence: NMEASentence) -> GGAData:
    if sentence.sentence_type != "GGA":
        raise NMEAError("not a GGA sentence")
    if len(sentence.fields) < 14:
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
    ) = sentence.fields[:14]

    if len(geoid_separation_unit) > 1:
        geoid_separation_unit = geoid_separation_unit[0:1]

    try:
        fix_quality = int(fix_quality_text)
    except ValueError as exc:
        raise NMEAError("GGA fix quality is not numeric") from exc

    if fix_quality <= 0:
        raise NMEAError("GGA fix is not valid")

    validate_optional_unit(altitude_text, altitude_unit, "M", "GGA altitude")
    validate_optional_unit(geoid_separation_text, geoid_separation_unit, "M", "GGA geoid separation")

    try:
        satellites_in_use = int(satellites_text)
        hdop = float(hdop_text)
        altitude_metres = float(altitude_text)
    except ValueError as exc:
        raise NMEAError("GGA numeric fields are invalid") from exc

    if fix_time == "":
        raise NMEAError("GGA fix time is missing")

    return GGAData(
        fix_time=fix_time,
        latitude_degrees=parse_latitude(latitude, latitude_hemisphere),
        longitude_degrees=parse_longitude(longitude, longitude_hemisphere),
        fix_quality=fix_quality,
        satellites_in_use=satellites_in_use,
        hdop=hdop,
        altitude_metres=altitude_metres,
        geoid_separation_metres=parse_optional_float(
            geoid_separation_text,
            "GGA geoid separation",
        ),
        dgps_age_seconds=parse_optional_float(dgps_age_text, "GGA DGPS age"),
        reference_station_id=reference_station_id or None,
    )


class InputStream(Iterator[str]):
    def close(self) -> None:
        """Release any underlying resources."""


class FileStream(InputStream):
    def __init__(self, handle: TextIO) -> None:
        self.handle = handle

    def __iter__(self) -> "FileStream":
        return self

    def __next__(self) -> str:
        line = self.handle.readline()
        if line == "":
            raise StopIteration
        return line

    def close(self) -> None:
        self.handle.close()


class SerialStream(InputStream):
    def __init__(self, connection: "serial.Serial") -> None:
        self.connection = connection

    def __iter__(self) -> "SerialStream":
        return self

    def __next__(self) -> str:
        while True:
            line = self.connection.readline()
            if not line:
                return ""
            return line.decode("ascii", errors="ignore")

    def close(self) -> None:
        self.connection.close()


def open_serial_stream(port: str, baudrate: int, timeout: float = 1.0) -> SerialStream:
    if serial is None:
        raise RuntimeError("pyserial is required for serial port input")
    connection = serial.Serial(port=port, baudrate=baudrate, timeout=timeout)
    return SerialStream(connection)


def open_file_stream(path: str) -> FileStream:
    handle = open(path, "r", encoding="ascii", errors="ignore")
    return FileStream(handle)


def ordered_ports() -> list[str]:
    if list_ports is None:
        raise RuntimeError("pyserial is required to scan serial ports")

    discovered_ports = [port.device for port in list_ports.comports()]
    preferred_ports = [
        device for device in PREFERRED_DEVICE_NAMES if device in discovered_ports or os.path.exists(device)
    ]
    remaining_ports = [
        device for device in discovered_ports if device not in preferred_ports
    ]
    return preferred_ports + remaining_ports


def port_has_valid_nmea(port: str, baudrate: int, probe_seconds: float) -> bool:
    deadline = time.monotonic() + probe_seconds
    print(
        f"Testing {port} at {baudrate} baud",
        file=sys.stderr,
        flush=True,
    )
    try:
        if serial is None:
            raise RuntimeError("pyserial is required for serial port input")
        connection = serial.Serial(port=port, baudrate=baudrate, timeout=0.5)
    except Exception:
        return False

    try:
        while time.monotonic() < deadline:
            raw_bytes = connection.readline()
            if not raw_bytes:
                continue
            try:
                raw = raw_bytes.decode("ascii", errors="ignore")
                parse_sentence(raw)
                return True
            except NMEAError as exc:
                log_nmea_error(
                    f"scan {port} {baudrate}",
                    exc,
                    raw,
                )
                continue
        return False
    finally:
        connection.close()


def scan_for_nmea_stream(probe_seconds: float) -> tuple[SerialStream, str, int]:
    ports = ordered_ports()
    if not ports:
        raise RuntimeError("no serial ports are available")

    for baudrate in COMMON_BAUD_RATES:
        for port in ports:
            if port_has_valid_nmea(port, baudrate, probe_seconds):
                print(
                    f"Found NMEA stream on {port} at {baudrate} baud",
                    file=sys.stderr,
                    flush=True,
                )
                return open_serial_stream(port, baudrate), port, baudrate

    raise RuntimeError("no serial port with valid inbound NMEA-0183 data was found")


def read_until_first_zda(stream: Iterable[str]) -> NMEASentence:
    wait_started_at = time.monotonic()
    next_warning_at = wait_started_at + ZDA_WARNING_INTERVAL_SECONDS
    timeout_at = wait_started_at + ZDA_TIMEOUT_SECONDS

    for raw in stream:
        now = time.monotonic()
        if now >= timeout_at:
            message = (
                "timed out after 10 minutes waiting for a valid ZDA sentence"
            )
            log_debug_message("read_until_first_zda", message)
            raise RuntimeError(message)
        if now >= next_warning_at:
            elapsed_minutes = int((now - wait_started_at) // 60)
            message = (
                f"Warning: still waiting for a valid ZDA sentence after "
                f"{elapsed_minutes} minute{'s' if elapsed_minutes != 1 else ''}"
            )
            print(message, file=sys.stderr, flush=True)
            log_debug_message("read_until_first_zda", message)
            next_warning_at += ZDA_WARNING_INTERVAL_SECONDS

        if raw.strip() == "":
            continue
        try:
            sentence = parse_sentence(raw)
            parse_zda(sentence)
            return sentence
        except NMEAError as exc:
            log_nmea_error("read_until_first_zda", exc, raw)
            continue
    raise RuntimeError("the input ended before a valid ZDA sentence was received")


def flush_interval(
    last_zda: NMEASentence,
    mwd_accumulator: MWDAggregator,
    mda_accumulator: MDAAggregator,
    gga_accumulator: GGAAggregator,
) -> None:
    if not (
        mwd_accumulator.has_data()
        or mda_accumulator.has_data()
        or gga_accumulator.has_data()
    ):
        return

    print(last_zda.raw, flush=True)
    if mwd_accumulator.has_data():
        print(mwd_accumulator.average_sentence(), flush=True)
    if mda_accumulator.has_data():
        print(mda_accumulator.average_sentence(), flush=True)
    if gga_accumulator.has_data():
        print(gga_accumulator.average_sentence(), flush=True)


def process_stream(stream: Iterable[str]) -> None:
    last_zda = read_until_first_zda(stream)
    mwd_accumulator = MWDAggregator()
    mda_accumulator = MDAAggregator()
    gga_accumulator = GGAAggregator()

    for raw in stream:
        try:
            sentence = parse_sentence(raw)
        except NMEAError as exc:
            log_nmea_error("parse_sentence", exc, raw)
            continue

        if sentence.sentence_type == "MWD":
            try:
                mwd_accumulator.add(parse_mwd(sentence))
            except NMEAError as exc:
                log_nmea_error("parse_mwd", exc, raw)
                continue
        elif sentence.sentence_type == "MDA":
            try:
                mda_accumulator.add(parse_mda(sentence))
            except NMEAError as exc:
                log_nmea_error("parse_mda", exc, raw)
                continue
        elif sentence.sentence_type == "GGA":
            try:
                gga_accumulator.add(parse_gga(sentence))
            except NMEAError as exc:
                log_nmea_error("parse_gga", exc, raw)
                continue
        elif sentence.sentence_type == "ZDA":
            try:
                parse_zda(sentence)
            except NMEAError as exc:
                log_nmea_error("parse_zda", exc, raw)
                continue

            flush_interval(last_zda, mwd_accumulator, mda_accumulator, gga_accumulator)
            last_zda = sentence
            mwd_accumulator.reset()
            mda_accumulator.reset()
            gga_accumulator.reset()

    flush_interval(last_zda, mwd_accumulator, mda_accumulator, gga_accumulator)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read NMEA-0183 sentences from a file or serial port."
    )
    parser.add_argument(
        "-p",
        "--port",
        help="Serial device to read, for example COM3 or /dev/ttyUSB0",
    )
    parser.add_argument("-f", "--file", dest="file_path", help="Text file to read")
    parser.add_argument(
        "-b",
        "--baud",
        type=int,
        default=4800,
        help="Serial baud rate to use with --port (default: 4800)",
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=2.0,
        help="Seconds to probe each serial port/baud combination during auto-scan",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Log rejected NMEA sentences and parser errors to nmea_monitor.log",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    if args.port and args.file_path:
        parser.error("use either --port or --file, not both")

    stream: Optional[InputStream] = None

    try:
        if args.debug:
            open_debug_log()

        if args.file_path:
            stream = open_file_stream(args.file_path)
        elif args.port:
            stream = open_serial_stream(args.port, args.baud)
        else:
            stream, port, baudrate = scan_for_nmea_stream(args.scan_timeout)
            print(
                f"Using {port} at {baudrate} baud",
                file=sys.stderr,
                flush=True,
            )

        process_stream(stream)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if stream is not None:
            stream.close()
        close_debug_log()


if __name__ == "__main__":
    raise SystemExit(main())
