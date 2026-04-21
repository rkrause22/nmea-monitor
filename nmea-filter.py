#!/usr/bin/env python3
"""Read NMEA-0183 sentences and emit JSON batches delimited by ZDA sentences."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

try:
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
except ImportError:  # pragma: no cover - depends on local environment
    serial = None
    list_ports = None


COMMON_BAUD_RATES = [4800, 9600, 19200, 38400, 57600, 115200]
DEFAULT_FILTER_TYPES = ["ZDA", "MWD", "MDA", "GGA"]
PREFERRED_DEVICE_NAMES = [
    "/dev/serial0",
    "/dev/ttyAMA0",
    "/dev/ttyS0",
    "/dev/ttyUSB0",
    "/dev/ttyACM0",
]
SERIAL_WARNING_INTERVAL_SECONDS = 60.0
SERIAL_TIMEOUT_SECONDS = 600.0
ZDA_WARNING_INTERVAL_SECONDS = 60.0
ZDA_TIMEOUT_SECONDS = 600.0
RESTART_DELAY_SECONDS = 60.0
PROGRAM_NAME = os.path.splitext(os.path.basename(__file__))[0]


class NMEAError(ValueError):
    """Raised when an NMEA sentence is malformed."""


@dataclass
class NMEASentence:
    raw: str
    formatter: str
    sentence_type: str
    fields: list[str]


class InputStream:
    source_name: str

    def __iter__(self) -> "InputStream":
        return self

    def __next__(self) -> str:
        raise NotImplementedError

    def close(self) -> None:
        return


class FileStream(InputStream):
    def __init__(self, path: str) -> None:
        self.path = path
        self.source_name = path
        self.handle = open(path, "r", encoding="utf-8")

    def __next__(self) -> str:
        line = self.handle.readline()
        if line == "":
            raise StopIteration
        return line

    def close(self) -> None:
        self.handle.close()


class SerialStream(InputStream):
    def __init__(self, connection: "serial.Serial", port: str, baudrate: int) -> None:
        self.connection = connection
        self.port = port
        self.baudrate = baudrate
        self.source_name = port

    def __next__(self) -> str:
        raw = self.connection.readline()
        if raw == b"":
            return ""
        return raw.decode("ascii", errors="ignore")

    def close(self) -> None:
        self.connection.close()


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


def log_exception(
    message: str,
    exc: BaseException,
    sentence: Optional[NMEASentence | str] = None,
) -> None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    logs_dir = os.path.join(script_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = os.path.join(logs_dir, f"{PROGRAM_NAME}-{today}.log")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} [ERROR] {PROGRAM_NAME} - {message}: {exc}\n")
        if sentence is not None:
            if isinstance(sentence, NMEASentence):
                sentence_text = sentence.raw
            else:
                sentence_text = sentence.strip()
            handle.write(f"{timestamp} [ERROR] {PROGRAM_NAME} - NMEA sentence: {sentence_text}\n")
        handle.write(details)


def log_error_message(message: str) -> None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    logs_dir = os.path.join(script_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = os.path.join(logs_dir, f"{PROGRAM_NAME}-{today}.log")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} [ERROR] {PROGRAM_NAME} - {message}\n")


def parse_filter_types(value: str) -> set[str]:
    sentence_types = {item.strip().upper() for item in value.split(",") if item.strip()}
    if not sentence_types:
        raise argparse.ArgumentTypeError("--filter must contain at least one sentence type")
    invalid = sorted(item for item in sentence_types if len(item) != 3 or not item.isalnum())
    if invalid:
        raise argparse.ArgumentTypeError(
            f"invalid NMEA sentence type(s): {', '.join(invalid)}"
        )
    if "ZDA" not in sentence_types:
        raise argparse.ArgumentTypeError("--filter must include ZDA")
    return sentence_types


def open_serial_stream(port: str, baudrate: int, timeout: float = 1.0) -> SerialStream:
    if serial is None:
        raise RuntimeError("pyserial is required for serial port input")
    connection = serial.Serial(port=port, baudrate=baudrate, timeout=timeout)
    return SerialStream(connection, port, baudrate)


def ordered_ports() -> list[str]:
    if list_ports is None:
        raise RuntimeError("pyserial is required to scan serial ports")

    discovered_ports = [port.device for port in list_ports.comports()]
    preferred_ports = [
        device
        for device in PREFERRED_DEVICE_NAMES
        if device in discovered_ports or os.path.exists(device)
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
            raw = connection.readline()
            if raw == b"":
                continue
            try:
                parse_sentence(raw.decode("ascii", errors="ignore"))
                return True
            except NMEAError:
                continue
    finally:
        connection.close()
    return False


def wait_for_valid_nmea_on_port(port: str, baudrate: int) -> SerialStream:
    wait_started_at = time.monotonic()
    next_warning_at = wait_started_at + SERIAL_WARNING_INTERVAL_SECONDS
    timeout_at = wait_started_at + SERIAL_TIMEOUT_SECONDS

    while True:
        now = time.monotonic()
        if now >= timeout_at:
            message = (
                f"timed out after 10 minutes waiting for valid NMEA-0183 data "
                f"on {port} at {baudrate} baud"
            )
            log_error_message(message)
            raise RuntimeError(message)
        if now >= next_warning_at:
            elapsed_minutes = int((now - wait_started_at) // 60)
            message = (
                f"Warning: still waiting for valid NMEA-0183 data on {port} "
                f"at {baudrate} baud after "
                f"{elapsed_minutes} minute{'s' if elapsed_minutes != 1 else ''}"
            )
            print(message, file=sys.stderr, flush=True)
            log_error_message(message)
            next_warning_at += SERIAL_WARNING_INTERVAL_SECONDS

        remaining_seconds = max(0.1, timeout_at - now)
        if port_has_valid_nmea(port, baudrate, min(2.0, remaining_seconds)):
            print(
                f"Found NMEA stream on {port} at {baudrate} baud",
                file=sys.stderr,
                flush=True,
            )
            return open_serial_stream(port, baudrate)


def scan_for_nmea_stream(probe_seconds: float) -> SerialStream:
    wait_started_at = time.monotonic()
    next_warning_at = wait_started_at + SERIAL_WARNING_INTERVAL_SECONDS
    timeout_at = wait_started_at + SERIAL_TIMEOUT_SECONDS

    while True:
        now = time.monotonic()
        if now >= timeout_at:
            message = (
                "timed out after 10 minutes scanning serial ports for "
                "valid inbound NMEA-0183 data"
            )
            log_error_message(message)
            raise RuntimeError(message)
        if now >= next_warning_at:
            elapsed_minutes = int((now - wait_started_at) // 60)
            message = (
                f"Warning: still scanning serial ports for valid NMEA-0183 "
                f"data after {elapsed_minutes} "
                f"minute{'s' if elapsed_minutes != 1 else ''}"
            )
            print(message, file=sys.stderr, flush=True)
            log_error_message(message)
            next_warning_at += SERIAL_WARNING_INTERVAL_SECONDS

        ports = ordered_ports()
        if not ports:
            time.sleep(min(probe_seconds, max(0.1, timeout_at - now)))
            continue

        for baudrate in COMMON_BAUD_RATES:
            for port in ports:
                now = time.monotonic()
                if now >= timeout_at:
                    message = (
                        "timed out after 10 minutes scanning serial ports for "
                        "valid inbound NMEA-0183 data"
                    )
                    log_error_message(message)
                    raise RuntimeError(message)

                remaining_seconds = max(0.1, timeout_at - now)
                if port_has_valid_nmea(port, baudrate, min(probe_seconds, remaining_seconds)):
                    print(
                        f"Found NMEA stream on {port} at {baudrate} baud",
                        file=sys.stderr,
                        flush=True,
                    )
                    return open_serial_stream(port, baudrate)


def next_valid_sentence(stream: Iterable[str]) -> Optional[NMEASentence]:
    for raw in stream:
        if not raw.strip():
            continue
        try:
            return parse_sentence(raw)
        except NMEAError as exc:
            log_exception("invalid NMEA sentence skipped", exc, raw)
            continue
    return None


def read_until_first_zda(stream: Iterable[str]) -> NMEASentence:
    wait_started_at = time.monotonic()
    next_warning_at = wait_started_at + ZDA_WARNING_INTERVAL_SECONDS
    timeout_at = wait_started_at + ZDA_TIMEOUT_SECONDS

    for raw in stream:
        now = time.monotonic()
        if now >= timeout_at:
            message = "timed out after 10 minutes waiting for a valid ZDA sentence"
            log_error_message(message)
            raise RuntimeError(message)
        if now >= next_warning_at:
            elapsed_minutes = int((now - wait_started_at) // 60)
            message = (
                f"Warning: still waiting for a valid ZDA sentence after "
                f"{elapsed_minutes} minute{'s' if elapsed_minutes != 1 else ''}"
            )
            print(message, file=sys.stderr, flush=True)
            log_error_message(message)
            next_warning_at += ZDA_WARNING_INTERVAL_SECONDS

        if not raw.strip():
            continue
        try:
            sentence = parse_sentence(raw)
        except NMEAError:
            continue
        if sentence.sentence_type != "ZDA":
            continue
        try:
            parse_zda_datetime(sentence)
            return sentence
        except NMEAError:
            continue

    raise RuntimeError("the input ended before a valid ZDA sentence was received")


def format_utc_datetime(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def sanitize_filename_prefix(value: str) -> str:
    sanitized = "".join(
        char for char in value if char not in '<>:"/\\|?*' and ord(char) >= 32
    ).strip()
    return sanitized or "source"


def upload_payload(url: str, payload: dict[str, object]) -> None:
    endpoint = f"{url.rstrip('/')}/add"
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"API upload failed with HTTP {exc.code} for {endpoint}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"API upload failed for {endpoint}: {exc.reason}") from exc


def emit_buffer(
    args: argparse.Namespace,
    source_name: str,
    buffer: list[NMEASentence],
    starting_zda: NMEASentence,
    ending_zda: Optional[NMEASentence],
) -> None:
    if not buffer:
        return

    utc_start = parse_zda_datetime(starting_zda)
    payload = {
        "source": source_name,
        "start": format_utc_datetime(utc_start),
    }
    if ending_zda is not None:
        payload["end"] = format_utc_datetime(parse_zda_datetime(ending_zda))
    payload["sentences"] = [sentence.raw for sentence in buffer]

    if args.debug:
        output = json.dumps(payload, indent=2)
        print(output, flush=True)

    if args.output_path:
        os.makedirs(args.output_path, exist_ok=True)
        output_date = utc_start.strftime("%Y-%m-%d")
        filename_prefix = sanitize_filename_prefix(source_name)
        output_path = os.path.join(args.output_path, f"{filename_prefix}-{output_date}.nmea")
        with open(output_path, "a", encoding="utf-8") as handle:
            for sentence in buffer:
                handle.write(sentence.raw)
                handle.write("\n")

    if args.url:
        upload_payload(args.url, payload)


def process_stream(stream: InputStream, args: argparse.Namespace) -> None:
    output_source = args.source if args.source is not None else stream.source_name
    interval_start_zda = read_until_first_zda(stream)
    last_sentence = interval_start_zda
    buffer: list[NMEASentence] = []

    while True:
        if last_sentence.sentence_type in args.filter:
            buffer.append(last_sentence)

        while True:
            new_sentence = next_valid_sentence(stream)
            if new_sentence is None:
                emit_buffer(args, output_source, buffer, interval_start_zda, None)
                break
            if new_sentence.sentence_type != "ZDA":
                break
            try:
                parse_zda_datetime(new_sentence)
                break
            except NMEAError as exc:
                log_exception("invalid ZDA sentence skipped", exc, new_sentence)
                continue

        if new_sentence is None:
            break

        if new_sentence.sentence_type == "ZDA":
            emit_buffer(args, output_source, buffer, interval_start_zda, new_sentence)
            buffer.clear()
            interval_start_zda = new_sentence

        last_sentence = new_sentence


def build_argument_parser() -> argparse.ArgumentParser:
    default_filter_types = set(DEFAULT_FILTER_TYPES)
    if "ZDA" not in default_filter_types:
        raise RuntimeError("DEFAULT_FILTER_TYPES must include ZDA")

    parser = argparse.ArgumentParser(
        description="Read NMEA-0183 sentences from a file or COM port, filter and emit to file or ZDA-delimited JSON."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "-p",
        "--port",
        help="COM port or serial device to read, for example COM3 or /dev/ttyUSB0",
    )
    parser.add_argument(
        "-b",
        "--baud",
        type=int,
        default=4800,
        help="Serial baud rate to use with --port (default: 4800)",
    )
    source.add_argument(
        "-i",
        "--input",
        dest="input_path",
        help="Text file containing NMEA-0183 sentences to be filtered",
    )
    parser.add_argument(
        "-o",
        "--output",
        dest="output_path",
        help="Folder where NMEA output should be written",
    )
    parser.add_argument(
        "-f",
        "--filter",
        type=parse_filter_types,
        default=default_filter_types,
        help=(
            "Comma separated sentence types to output "
            f"(default: {','.join(DEFAULT_FILTER_TYPES)})"
        ),
    )
    parser.add_argument(
        "-s",
        "--source",
        help="Override the source name emitted in JSON output",
    )
    parser.add_argument(
        "-u",
        "--url",
        help="API endpoint base URL where JSON NMEA payloads should be uploaded",
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
        help="Show filtered NMEA output on stdout",
    )
    return parser


def run_once(args: argparse.Namespace) -> None:
    stream: Optional[InputStream] = None
    try:
        if args.input_path:
            stream = FileStream(args.input_path)
        elif args.port:
            stream = wait_for_valid_nmea_on_port(args.port, args.baud)
        else:
            stream = scan_for_nmea_stream(args.scan_timeout)

        process_stream(stream, args)
    finally:
        if stream is not None:
            stream.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    while True:
        try:
            run_once(args)
            if args.input_path:
                return 0
        except KeyboardInterrupt:
            return 130
        except Exception as exc:
            log_exception("unhandled exception; restarting after delay", exc)
            log_error_message(
                f"Restarting initialization in {int(RESTART_DELAY_SECONDS)} seconds"
            )
            print(
                f"{PROGRAM_NAME}: {exc}; restarting in {int(RESTART_DELAY_SECONDS)} seconds",
                file=sys.stderr,
                flush=True,
            )

        try:
            time.sleep(RESTART_DELAY_SECONDS)
        except KeyboardInterrupt:
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
