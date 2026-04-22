#!/usr/bin/env python3
"""Read NMEA-0183 sentences and emit JSON batches delimited by ZDA sentences."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from functools import partial
from typing import Iterable, Optional

from nmea_helpers import (
    FrameAggregator,
    NMEAError,
    NMEASentence,
    combine_frames,
    format_utc_datetime,
    log_error_message as write_log_error_message,
    log_exception as write_log_exception,
    parse_sentence,
    parse_zda_datetime,
)

try:
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
except ImportError:  # pragma: no cover - depends on local environment
    serial = None
    list_ports = None


COMMON_BAUD_RATES = [4800, 9600, 19200, 38400, 57600, 115200]
DEFAULT_FILTER_TYPES = ["ZDA", "MWD", "MDA", "GGA"]
PERMITTED_AGGREGATE_TYPES = ("MWD", "MDA", "GGA")
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
log_exception = partial(write_log_exception, PROGRAM_NAME, __file__)
log_error_message = partial(write_log_error_message, PROGRAM_NAME, __file__)


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


def upload_payload(url: str, payload: dict[str, object]) -> None:
    endpoint = f"{url.rstrip('/')}"
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


def get_raw_sentence(sentence: NMEASentence | str) -> str:
    if isinstance(sentence, NMEASentence):
        return sentence.raw
    return sentence


def build_aggregated_buffer(
    starting_zda: NMEASentence,
    frame_window: deque[FrameAggregator],
    filter_types: set[str],
) -> list[str]:
    combined_frame = combine_frames(list(frame_window))
    sentences: list[str] = []
    if "ZDA" in filter_types:
        sentences.append(starting_zda.raw)
    if "MWD" in filter_types and combined_frame.mwd.has_data():
        sentences.append(combined_frame.mwd.average_sentence())
    if "MDA" in filter_types and combined_frame.mda.has_data():
        sentences.append(combined_frame.mda.average_sentence())
    if "GGA" in filter_types and combined_frame.gga.has_data():
        sentences.append(combined_frame.gga.average_sentence())
    return sentences


def emit_buffer(
    args: argparse.Namespace,
    buffer: list[NMEASentence | str],
    starting_zda: NMEASentence,
    ending_zda: Optional[NMEASentence],
) -> None:
    if not buffer:
        return

    utc_start = parse_zda_datetime(starting_zda)
    payload = {
        "start": format_utc_datetime(utc_start),
    }
    if ending_zda is not None:
        payload["end"] = format_utc_datetime(parse_zda_datetime(ending_zda))
    payload["sentences"] = [get_raw_sentence(sentence) for sentence in buffer]

    if args.debug:
        output = json.dumps(payload, indent=2)
        print(output, flush=True)

    if args.url:
        upload_payload(args.url, payload)

    if args.data_folder:
        os.makedirs(args.data_folder, exist_ok=True)
        output_date = utc_start.strftime("%Y-%m-%d")
        data_folder = os.path.join(
            args.data_folder,
            f"{args.data_prefix}-{output_date}.nmea",
        )
        with open(data_folder, "a", encoding="utf-8") as handle:
            for sentence in buffer:
                handle.write(get_raw_sentence(sentence))
                handle.write("\n")


def process_stream(stream: InputStream, args: argparse.Namespace) -> None:
    if args.aggregation > 0:
        process_aggregated_stream(stream, args)
        return

    interval_start_zda = read_until_first_zda(stream)
    last_sentence = interval_start_zda
    buffer: list[NMEASentence] = []

    while True:
        if last_sentence.sentence_type in args.filter:
            buffer.append(last_sentence)

        while True:
            new_sentence = next_valid_sentence(stream)
            if new_sentence is None:
                emit_buffer(args, buffer, interval_start_zda, None)
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
            emit_buffer(args, buffer, interval_start_zda, new_sentence)
            buffer.clear()
            interval_start_zda = new_sentence

        last_sentence = new_sentence


def process_aggregated_stream(stream: InputStream, args: argparse.Namespace) -> None:
    interval_start_zda = read_until_first_zda(stream)
    last_sentence = interval_start_zda
    current_frame = FrameAggregator()
    frame_window: deque[FrameAggregator] = deque(maxlen=args.aggregation)

    while True:
        if last_sentence.sentence_type in PERMITTED_AGGREGATE_TYPES:
            try:
                current_frame.add_sentence(last_sentence)
            except NMEAError as exc:
                log_exception("invalid aggregate sentence skipped", exc, last_sentence)

        while True:
            new_sentence = next_valid_sentence(stream)
            if new_sentence is None:
                frame_window.append(current_frame)
                buffer = build_aggregated_buffer(
                    interval_start_zda,
                    frame_window,
                    args.filter,
                )
                emit_buffer(args, buffer, interval_start_zda, None)
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
            frame_window.append(current_frame)
            buffer = build_aggregated_buffer(
                interval_start_zda,
                frame_window,
                args.filter,
            )
            emit_buffer(args, buffer, interval_start_zda, new_sentence)
            interval_start_zda = new_sentence
            current_frame = FrameAggregator()

        last_sentence = new_sentence


def build_argument_parser() -> argparse.ArgumentParser:
    default_filter_types = set(DEFAULT_FILTER_TYPES)
    if "ZDA" not in default_filter_types:
        raise RuntimeError("DEFAULT_FILTER_TYPES must include ZDA")

    parser = argparse.ArgumentParser(
        description="Read NMEA-0183 sentences from a file or COM port, filter and emit to file or ZDA-delimited JSON."
    )
    parser.fromfile_prefix_chars = "@"
    parser.convert_arg_line_to_args = shlex.split
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "-p",
        "--port",
        help="COM port or serial device to receive NMEA data (eg. COM3 or /dev/ttyUSB0)",
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
        dest="input_file",
        help="Text file containing raw NMEA-0183 sentences to be filtered",
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
        "-d",
        "--data",
        dest="data_folder",
        help="Data folder where filtered NMEA output should be written",
    )
    parser.add_argument(
        "-dp",
        "--prefix",
        dest="data_prefix",
        help="Filename prefix for filtered NMEA output in data folder",
    )
    parser.add_argument(
        "-a",
        "--aggregation",
        type=int,
        default=0,
        help="Number of ZDA frames to aggregate over (default: 0, disabled)",
    )
    parser.add_argument(
        "-u",
        "--url",
        help="URL where filtered and aggregated NMEA output should be uploaded (eg. https://nmea.myorg.com/add/myorg/mydevice)",
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


def validate_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.data_folder and not args.data_prefix:
        parser.error("--data requires --prefix")
    if args.data_prefix and not re.fullmatch(r"[A-Za-z0-9]+", args.data_prefix):
        parser.error("--prefix may contain only letters and numbers")
    if args.aggregation < 0:
        parser.error("--aggregation must be greater than or equal to zero")
    if args.aggregation > 0:
        permitted_filter_types = {"ZDA", *PERMITTED_AGGREGATE_TYPES}
        unsupported_types = sorted(args.filter - permitted_filter_types)
        if unsupported_types:
            parser.error(
                "--aggregation is only supported when --filter contains ZDA "
                f"and these aggregate types: {','.join(PERMITTED_AGGREGATE_TYPES)}; "
                f"unsupported type(s): {','.join(unsupported_types)}"
            )


def run_once(args: argparse.Namespace) -> None:
    stream: Optional[InputStream] = None
    try:
        if args.input_file:
            stream = FileStream(args.input_file)
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
    validate_arguments(parser, args)

    while True:
        try:
            run_once(args)
            if args.input_file:
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
