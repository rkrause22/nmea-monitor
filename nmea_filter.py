#!/usr/bin/env python3
"""Read NMEA-0183 sentences and emit filtered or aggregated JSON batches."""

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
from typing import Optional

from nmea_aggregation import (
    FrameAggregator,
    NMEAError,
    NMEASentence,
    parse_sentence,
    parse_zda_datetime,
)
from common_helpers import (
    format_utc_datetime,
    log_error_message as write_log_error_message,
    log_exception as write_log_exception,
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
DEFAULT_MAX_ZDA_SEEK = 5
RAW_SENTENCE_PATTERN = re.compile(r"\$[^$\r\n]*\*[0-9A-Fa-f]{2}")
PREFERRED_DEVICE_NAMES = [
    "/dev/serial0",
    "/dev/ttyAMA0",
    "/dev/ttyS0",
    "/dev/ttyUSB0",
    "/dev/ttyACM0",
]
SERIAL_WARNING_INTERVAL_SECONDS = 60.0
SERIAL_TIMEOUT_SECONDS = 600.0
SERIAL_RETRY_DELAY_SECONDS = 1.0
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
        self.handle = open(path, "r", encoding="utf-8", errors="ignore")

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


class SentenceReader:
    def __init__(self, stream: InputStream) -> None:
        self.stream = stream
        self.pending_sentences: deque[NMEASentence] = deque()

    def next_valid_sentence(self) -> Optional[NMEASentence]:
        while True:
            if self.pending_sentences:
                return self.pending_sentences.popleft()

            for raw in self.stream:
                if not raw.strip():
                    continue
                self.pending_sentences.extend(parse_valid_sentences(raw))
                if self.pending_sentences:
                    return self.pending_sentences.popleft()
            return None


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


def extract_sentence_candidates(raw_line: str) -> list[str]:
    return [match.group(0) for match in RAW_SENTENCE_PATTERN.finditer(raw_line)]


def parse_valid_sentences(raw_line: str) -> list[NMEASentence]:
    sentences: list[NMEASentence] = []
    for candidate in extract_sentence_candidates(raw_line):
        try:
            sentences.append(parse_sentence(candidate))
        except NMEAError:
            continue
    return sentences


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
            decoded = raw.decode("ascii", errors="ignore")
            if parse_valid_sentences(decoded):
                return True
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
        time.sleep(min(SERIAL_RETRY_DELAY_SECONDS, remaining_seconds))


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
                time.sleep(min(SERIAL_RETRY_DELAY_SECONDS, remaining_seconds))


def read_until_first_zda(reader: SentenceReader) -> NMEASentence:
    wait_started_at = time.monotonic()
    next_warning_at = wait_started_at + ZDA_WARNING_INTERVAL_SECONDS
    timeout_at = wait_started_at + ZDA_TIMEOUT_SECONDS

    while True:
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

        sentence = reader.next_valid_sentence()
        if sentence is None:
            raise RuntimeError("the input ended before a valid ZDA sentence was received")
        if sentence.sentence_type != "ZDA":
            continue
        try:
            parse_zda_datetime(sentence)
            return sentence
        except NMEAError:
            continue


def upload_payload(
    url: str,
    payload: dict[str, object],
    auth_key: str | None = None,
) -> None:
    endpoint = f"{url.rstrip('/')}"
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if auth_key is not None:
        headers["Authorization"] = f"Bearer {auth_key}"
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers=headers,
        method="POST",
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
    output_zda: NMEASentence,
    frame_aggregator: FrameAggregator,
    filter_types: set[str],
) -> list[str]:
    sentences: list[str] = []
    if "ZDA" in filter_types:
        sentences.append(output_zda.raw)
    if "MWD" in filter_types and frame_aggregator.mwd.has_data():
        sentences.append(frame_aggregator.mwd.average_sentence())
    if "MDA" in filter_types and frame_aggregator.mda.has_data():
        sentences.append(frame_aggregator.mda.average_sentence())
    if "GGA" in filter_types and frame_aggregator.gga.has_data():
        sentences.append(frame_aggregator.gga.average_sentence())
    return sentences


def requested_aggregate_types(filter_types: set[str]) -> list[str]:
    return [
        sentence_type
        for sentence_type in PERMITTED_AGGREGATE_TYPES
        if sentence_type in filter_types
    ]


def has_met_sample_targets(
    sample_counts: dict[str, int],
    filter_types: set[str],
    samples_per_type: int,
) -> bool:
    if samples_per_type <= 0:
        return False
    aggregate_types = requested_aggregate_types(filter_types)
    if not aggregate_types:
        return True
    return all(
        sample_counts.get(sentence_type, 0) >= samples_per_type
        for sentence_type in aggregate_types
    )


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
        upload_payload(args.url, payload, args.auth_key)

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
    reader = SentenceReader(stream)
    if args.samples_per_type > 0:
        process_aggregated_stream(reader, args)
        return

    interval_start_zda = read_until_first_zda(reader)
    last_sentence = interval_start_zda
    buffer: list[NMEASentence] = []

    while True:
        if last_sentence.sentence_type in args.filter:
            buffer.append(last_sentence)

        while True:
            new_sentence = reader.next_valid_sentence()
            if new_sentence is None:
                emit_buffer(args, buffer, interval_start_zda, None)
                break
            if new_sentence.sentence_type != "ZDA":
                break
            try:
                parse_zda_datetime(new_sentence)
                break
            except NMEAError:
                continue

        if new_sentence is None:
            break

        if new_sentence.sentence_type == "ZDA":
            emit_buffer(args, buffer, interval_start_zda, new_sentence)
            buffer.clear()
            interval_start_zda = new_sentence

        last_sentence = new_sentence


def process_aggregated_stream(reader: SentenceReader, args: argparse.Namespace) -> None:
    interval_start_zda = read_until_first_zda(reader)
    output_zda = interval_start_zda
    current_frame = FrameAggregator()
    sample_counts = {sentence_type: 0 for sentence_type in requested_aggregate_types(args.filter)}
    zda_seek_count = 0

    while True:
        new_sentence = reader.next_valid_sentence()
        if new_sentence is None:
            buffer = build_aggregated_buffer(
                output_zda,
                current_frame,
                args.filter,
            )
            emit_buffer(args, buffer, interval_start_zda, None)
            break

        if new_sentence.sentence_type in PERMITTED_AGGREGATE_TYPES:
            try:
                current_frame.add_sentence(new_sentence)
            except NMEAError:
                pass
            else:
                if new_sentence.sentence_type in sample_counts:
                    sample_counts[new_sentence.sentence_type] += 1
            continue

        if new_sentence.sentence_type != "ZDA":
            continue

        try:
            parse_zda_datetime(new_sentence)
        except NMEAError:
            continue

        zda_seek_count += 1
        if (
            has_met_sample_targets(sample_counts, args.filter, args.samples_per_type)
            or zda_seek_count >= args.max_zda_seek
        ):
            buffer = build_aggregated_buffer(
                output_zda,
                current_frame,
                args.filter,
            )
            emit_buffer(args, buffer, interval_start_zda, new_sentence)
            interval_start_zda = new_sentence
            output_zda = new_sentence
            current_frame = FrameAggregator()
            sample_counts = {
                sentence_type: 0
                for sentence_type in requested_aggregate_types(args.filter)
            }
            zda_seek_count = 0
            continue

        output_zda = new_sentence


def build_argument_parser() -> argparse.ArgumentParser:
    default_filter_types = set(DEFAULT_FILTER_TYPES)
    if "ZDA" not in default_filter_types:
        raise RuntimeError("DEFAULT_FILTER_TYPES must include ZDA")

    parser = argparse.ArgumentParser(
        description="Read NMEA-0183 sentences from a file or COM port, then filter or aggregate valid sentences for output."
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
        "-s",
        "--samples-per-type",
        dest="samples_per_type",
        type=int,
        default=1,
        help="Target number of valid samples to collect for each requested secondary type before emitting (default: 1; use 0 to disable aggregation)",
    )
    parser.add_argument(
        "-x",
        "--max-zda-seek",
        type=int,
        default=DEFAULT_MAX_ZDA_SEEK,
        help=f"Maximum number of valid ZDA boundaries to wait before emitting a partial aggregate (default: {DEFAULT_MAX_ZDA_SEEK})",
    )
    parser.add_argument(
        "-a",
        "--auth",
        dest="auth_key",
        help="Bearer authorization key to include in upload requests",
    )
    parser.add_argument(
        "-u",
        "--url",
        help="URL where filtered and aggregated NMEA output should be uploaded (eg. https://nmea.myorg.com/nmea/add/myorg/mydevice)",
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
    if args.samples_per_type < 0:
        parser.error("--samples-per-type must be greater than or equal to zero")
    if args.max_zda_seek < 1:
        parser.error("--max-zda-seek must be greater than or equal to one")
    if args.auth_key is not None and len(args.auth_key) < 8:
        parser.error("--auth must be at least 8 characters long")
    if args.samples_per_type > 0:
        permitted_filter_types = {"ZDA", *PERMITTED_AGGREGATE_TYPES}
        unsupported_types = sorted(args.filter - permitted_filter_types)
        if unsupported_types:
            parser.error(
                "--samples-per-type aggregation is only supported when --filter contains ZDA "
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
