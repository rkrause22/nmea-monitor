#!/usr/bin/env python3
"""REST repository for JSON batches emitted by nmea_filter."""

from __future__ import annotations

import os
from functools import partial
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from common_helpers import (
    apply_date_filters,
    log_exception as write_log_exception,
    parse_timespan,
    parse_utc_datetime,
)
from nmea_weather import build_weather_summary
from repository_store_by_file import FileStore
from repository_service import RepositoryService
from repository_store import MessageRecord, RegistrationRecord


PROGRAM_NAME = os.path.splitext(os.path.basename(__file__))[0]
SCRIPT_DIR = Path(os.path.abspath(os.path.dirname(__file__)))
DEFAULT_DATA_ROOT = Path(
    os.environ.get("NMEA_REPOSITORY_DATA_ROOT", str(SCRIPT_DIR / "data"))
)
log_exception = partial(write_log_exception, PROGRAM_NAME, __file__)


def create_app(data_root: Path = DEFAULT_DATA_ROOT) -> Flask:
    app = Flask(__name__)
    data_root.mkdir(parents=True, exist_ok=True)
    service = RepositoryService(FileStore(data_root))

    @app.get("/<filename>.html")
    def get_html_page(filename: str) -> Response:
        return send_from_directory(SCRIPT_DIR, f"{filename}.html")

    @app.post("/nmea/add/<org>/<source>")
    def add_nmea_message(org: str, source: str) -> tuple[Response, int]:
        try:
            auth = require_bearer_token(request)
        except ValueError as exc:
            return handle_value_error("invalid add authorization", exc)

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return json_error("request body must be a JSON object", 400)

        try:
            record = message_record_from_payload(payload)
            created = service.add_message(auth, org, source, record)
        except ValueError as exc:
            return handle_value_error("invalid add payload", exc, add_missing_registration=True)

        return jsonify({"status": "created" if created else "updated"}), (
            201 if created else 200
        )

    @app.post("/nmea/registrations")
    def register_org() -> tuple[Response, int]:
        try:
            auth = require_bearer_token(request)
        except ValueError as exc:
            return handle_value_error("invalid registration authorization", exc)

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return json_error("request body must be a JSON object", 400)

        try:
            registration = registration_record_from_payload(payload)
            created = service.add_registration(auth, registration)
        except ValueError as exc:
            return handle_value_error("invalid registration payload", exc)

        if not created:
            return json_error("registration already exists", 409)
        return jsonify({"status": "created"}), 201

    @app.get("/nmea/registrations")
    @app.get("/nmea/registrations/<org>")
    def get_registrations(org: str | None = None) -> Response:
        try:
            auth = require_bearer_token(request)
        except ValueError as exc:
            return handle_value_error("invalid registrations authorization", exc)

        try:
            registrations = service.get_registrations(auth, org)
        except ValueError as exc:
            return handle_value_error("invalid registrations request", exc)

        return jsonify(
            [
                {
                    "org": registration.org,
                    "span": registration.span,
                    "limit": registration.limit,
                }
                for registration in registrations
            ]
        )

    @app.delete("/nmea/registrations/<org>")
    def delete_registration(org: str) -> tuple[Response, int]:
        try:
            auth = require_bearer_token(request)
        except ValueError as exc:
            return handle_value_error("invalid delete registration authorization", exc)

        try:
            registration = service.get_registration(auth, org)
            if registration is None:
                return json_error("registration not found", 404)
            deleted_volume = service.delete_registration(auth, org)
        except ValueError as exc:
            return handle_value_error("invalid delete registration request", exc)

        return jsonify({"status": "deleted", "volume_deleted": deleted_volume}), 200

    @app.delete("/nmea/purge/<org>/<source>")
    @app.delete("/nmea/purge/<org>/<source>/<what>")
    def purge_nmea_messages(
        org: str,
        source: str,
        what: str | None = None,
    ) -> tuple[Response, int]:
        try:
            auth = require_bearer_token(request)
        except ValueError as exc:
            return handle_value_error("invalid purge authorization", exc)

        try:
            deleted_volume = service.purge_stale_data(auth, org, source, what)
        except ValueError as exc:
            return handle_value_error("invalid purge request", exc)

        return jsonify({"status": "deleted", "volume_deleted": deleted_volume}), 200

    @app.get("/nmea/last/<org>/<source>")
    @app.get("/nmea/last/<org>/<source>/<what>")
    def get_nmea_latest_messages(
        org: str,
        source: str,
        what: str | None = None,
    ) -> Response:
        try:
            records = service.get_latest_messages(org, source, what)
        except ValueError as exc:
            return handle_value_error("invalid last request", exc)

        if not records:
            return plain_text("", 404)
        return plain_text("\n".join(record.sentence_text for record in records))

    @app.get("/nmea/count")
    @app.get("/nmea/count/<org>")
    @app.get("/nmea/count/<org>/<source>")
    def get_nmea_count(
        org: str | None = None,
        source: str | None = None,
    ) -> Response:
        start_text = request.args.get("start")
        end_text = request.args.get("end")
        span_text = request.args.get("span")

        try:
            start, end = apply_date_filters(start_text, end_text, span_text)
            count = service.count_messages(org, source, start=start, end=end)
        except ValueError as exc:
            return handle_value_error("invalid count request", exc)

        return plain_text(str(count))

    @app.get("/nmea/search/<org>/<source>")
    def search_nmea_messages(org: str, source: str) -> Response:
        start_text = request.args.get("start")
        end_text = request.args.get("end")
        span_text = request.args.get("span")

        try:
            start, end = apply_date_filters(start_text, end_text, span_text)
            if start is None and end is None:
                records = service.get_latest_messages(org, source)
            else:
                records = service.find_messages(org, source, start=start, end=end)
        except ValueError as exc:
            return handle_value_error("invalid search query parameters", exc)

        if not records:
            return plain_text("", 404)
        return plain_text("\n".join(record.sentence_text for record in records))

    @app.get("/nmea/weather/<org>/<source>")
    @app.get("/nmea/weather/<org>/<source>/<int:count>")
    def get_nmea_weather(org: str, source: str, count: int = 1) -> Response:
        if count < 1:
            return json_error("count must be greater than zero", 400)

        try:
            records = service.find_messages(org, source, count=count)
        except ValueError as exc:
            return handle_value_error("invalid weather request", exc)

        if not records:
            return jsonify({"error": "no records found"}), 404

        return jsonify(build_weather_summary(org, source, records, log_exception))

    return app


def require_bearer_token(req) -> str:
    authorization = req.headers.get("Authorization")
    if not authorization:
        raise ValueError("access denied")

    scheme, _, token = authorization.partition(" ")
    if scheme != "Bearer" or not token:
        raise ValueError("access denied")
    return token


def message_record_from_payload(payload: dict[str, object]) -> MessageRecord:
    start_value = payload.get("start")
    if not isinstance(start_value, str) or not start_value.strip():
        raise ValueError("JSON object must contain a non-empty string start")

    sentences_value = payload.get("sentences")
    if not isinstance(sentences_value, list) or not all(
        isinstance(sentence, str) for sentence in sentences_value
    ):
        raise ValueError("JSON object must contain a sentences list of strings")

    return MessageRecord(
        utc=parse_utc_datetime(start_value),
        sentences=[sentence for sentence in sentences_value if isinstance(sentence, str)],
    )


def registration_record_from_payload(payload: dict[str, object]) -> RegistrationRecord:
    org_value = payload.get("org")
    if not isinstance(org_value, str) or not org_value.strip():
        raise ValueError("org must be a non-empty string")

    auth_value = payload.get("auth")
    if not isinstance(auth_value, str) or len(auth_value) < 8:
        raise ValueError("auth must be a string with length 8 or longer")

    span_value = payload.get("span")
    if span_value is None:
        span = "1 year"
    elif not isinstance(span_value, str) or not span_value.strip():
        raise ValueError("span must be a non-empty string when provided")
    else:
        parse_timespan(span_value)
        span = span_value.strip()

    limit_value = payload.get("limit")
    if limit_value is None:
        limit = 366
    elif isinstance(limit_value, bool):
        raise ValueError("limit must be an integer when provided")
    elif isinstance(limit_value, int):
        limit = limit_value
    elif isinstance(limit_value, str):
        try:
            limit = int(limit_value.strip())
        except ValueError as exc:
            raise ValueError("limit must be an integer when provided") from exc
    else:
        raise ValueError("limit must be an integer when provided")

    if limit < 1:
        raise ValueError("limit must be greater than zero")

    return RegistrationRecord(
        org=org_value.strip(),
        auth=auth_value,
        span=span,
        limit=limit,
    )


def handle_value_error(
    message: str,
    exc: ValueError,
    *,
    add_missing_registration: bool = False,
) -> tuple[Response, int]:
    error_text = str(exc)
    log_exception(message, exc)
    if error_text == "registration not found":
        return json_error("access denied" if add_missing_registration else "registration not found", 401 if add_missing_registration else 404)
    if error_text == "access denied":
        return json_error("access denied", 402)
    return json_error(error_text, 400)


def plain_text(body: str, status: int = 200) -> Response:
    return Response(body, status=status, mimetype="text/plain")


def json_error(message: str, status: int) -> tuple[Response, int]:
    return jsonify({"error": message}), status


app = create_app()


if __name__ == "__main__":
    app.run(host=os.environ.get("NMEA_REPOSITORY_HOST", "127.0.0.1"), port=5000)
