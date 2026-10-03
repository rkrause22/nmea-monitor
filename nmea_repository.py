#!/usr/bin/env python3
"""REST repository for JSON batches emitted by nmea_filter."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from functools import partial
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from common_helpers import (
    apply_date_filters,
    format_utc_datetime,
    log_exception as write_log_exception,
    parse_utc_date,
    parse_timespan,
    parse_utc_datetime,
)
from nmea_history import build_history_summary
from nmea_pws import (
    build_pws_upload_fields,
    public_pws_fields,
    upload_pws_observation,
)
from nmea_weather import build_weather_summary
from nmea_weather_window import (
    build_weather_window_summary,
    weather_period_to_dict,
    weather_samples_from_records,
)
from repository_store_by_file import FileStore
from repository_service import RepositoryService
from repository_store import MessageRecord, RegistrationRecord


PROGRAM_NAME = os.path.splitext(os.path.basename(__file__))[0]
SCRIPT_DIR = Path(os.path.abspath(os.path.dirname(__file__)))
DEFAULT_DATA_ROOT = Path(
    os.environ.get("NMEA_REPOSITORY_DATA_ROOT", str(SCRIPT_DIR / "data"))
)
log_exception = partial(write_log_exception, PROGRAM_NAME, __file__)


def create_app(
    data_root: Path = DEFAULT_DATA_ROOT,
    pws_uploader=upload_pws_observation,
) -> Flask:
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
            service.add_message(auth, org, source, record)
        except ValueError as exc:
            return handle_value_error("invalid add payload", exc, add_missing_registration=True)

        return jsonify({"status": "created"}), 201

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
            org, fields = registration_fields_from_payload(payload)
            created = service.save_registration(auth, org, fields)
        except ValueError as exc:
            return handle_value_error("invalid registration payload", exc)

        if created:
            return jsonify({"status": "created"}), 201
        return jsonify({"status": "updated"}), 200

    @app.get("/nmea/registrations")
    def get_registrations() -> Response:
        try:
            auth = require_bearer_token(request)
        except ValueError as exc:
            return handle_value_error("invalid registrations authorization", exc)

        try:
            registrations = service.get_registrations(auth)
        except ValueError as exc:
            return handle_value_error("invalid registrations request", exc)

        return jsonify(
            [
                registration_response(registration, include_secrets=True)
                for registration in registrations
            ]
        )

    @app.get("/nmea/registrations/<org>")
    def get_registration(org: str) -> Response:
        try:
            registration = service.lookup_registration(org)
        except ValueError as exc:
            return handle_value_error("invalid registration request", exc)

        if registration is None:
            return json_error("registration not found", 404)

        return jsonify(registration_response(registration, include_secrets=False))

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
            deleted_files = service.delete_registration(auth, org)
        except ValueError as exc:
            return handle_value_error("invalid delete registration request", exc)

        return jsonify({"status": "deleted", "files_deleted": deleted_files}), 200

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
            deleted_files = service.purge_stale_data(auth, org, source, what)
        except ValueError as exc:
            return handle_value_error("invalid purge request", exc)

        return jsonify({"status": "deleted", "files_deleted": deleted_files}), 200

    @app.post("/nmea/fix/<org>/<source>")
    @app.post("/nmea/fix/<org>/<source>/<day_text>")
    def fix_nmea_messages(
        org: str,
        source: str,
        day_text: str | None = None,
    ) -> tuple[Response, int]:
        try:
            auth = require_bearer_token(request)
        except ValueError as exc:
            return handle_value_error("invalid fix authorization", exc)

        try:
            target_day = resolve_message_day(day_text)
            result = service.fix_message_file(auth, org, source, target_day)
        except ValueError as exc:
            return handle_value_error("invalid fix request", exc, add_missing_registration=True)

        return (
            jsonify(
                {
                    "status": "fixed",
                    "org": result.org,
                    "source": result.source,
                    "date": result.day.isoformat(),
                    "removed_count": result.removed_count,
                    "corrupt_rows": result.corrupt_rows,
                }
            ),
            200,
        )

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

    @app.get("/nmea/find/<org>/<source>")
    def find_nmea_messages(org: str, source: str) -> Response:
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
            return handle_value_error("invalid find query parameters", exc)

        if not records:
            return plain_text("", 404)
        return plain_text("\n".join(record.sentence_text for record in records))

    @app.get("/nmea/history/<org>/<source>")
    def get_nmea_history(org: str, source: str) -> Response:
        start_text = request.args.get("start")
        end_text = request.args.get("end")
        span_text = request.args.get("span")

        try:
            start, end = apply_date_filters(start_text, end_text, span_text)
            records = service.get_history(org, source, start=start, end=end)
        except ValueError as exc:
            return handle_value_error("invalid history query parameters", exc)

        if not records:
            return jsonify({"error": "no records found"}), 404

        history = build_history_summary(org, source, records, log_exception)
        history["start"] = start_text
        history["end"] = end_text
        history["span"] = span_text
        history["query_start_utc_time"] = format_utc_datetime(start) if start is not None else None
        history["query_end_utc_time"] = format_utc_datetime(end) if end is not None else None
        return jsonify(history)

    @app.get("/nmea/weather/<org>/<source>")
    @app.get("/nmea/weather/<org>/<source>/<int:count>")
    def get_nmea_weather(org: str, source: str, count: int = 1) -> Response:
        if count < 1:
            return json_error("count must be greater than zero", 400)

        start_text = request.args.get("start")
        end_text = request.args.get("end")
        span_text = request.args.get("span")

        try:
            start, end = apply_date_filters(start_text, end_text, span_text)
            if start is None and end is None:
                records = service.find_messages(org, source, count=count)
            else:
                records = service.find_messages(org, source, start=start, end=end)
        except ValueError as exc:
            return handle_value_error("invalid weather request", exc)

        if not records:
            return jsonify({"error": "no records found"}), 404

        return jsonify(
            build_weather_summary(org,source, records, log_exception)
        )

    @app.get("/nmea/pws/<org>/<source>")
    def upload_nmea_pws(org: str, source: str) -> tuple[Response, int]:
        try:
            auth = require_bearer_token(request)
        except ValueError as exc:
            return handle_value_error("invalid pws authorization", exc)

        try:
            registration = service.get_org_registration(auth, org)
            if not registration.pwsid or not registration.pwskey:
                raise ValueError("registration does not include PWS credentials")

            latest_records = service.find_messages(org, source, count=1)
            if not latest_records:
                return jsonify({"error": "no records found"}), 404

            end = latest_records[-1].utc
            start = end - timedelta(minutes=10)
            records = service.find_messages(
                org,
                source,
                start=start,
                end=end + timedelta(microseconds=1),
            )
            samples = weather_samples_from_records(records, log_exception)
            if not samples:
                return jsonify({"error": "no weather samples found"}), 404

            summary = build_weather_window_summary(
                samples,
                start=start,
                end=end,
                segment_count=5,
            )
            fields = build_pws_upload_fields(
                registration.pwsid,
                registration.pwskey,
                summary,
            )
            result = pws_uploader(fields)
        except ValueError as exc:
            return handle_value_error("invalid pws request", exc, add_missing_registration=True)
        except RuntimeError as exc:
            log_exception("pws upload failed", exc)
            return json_error(str(exc), 502)

        return (
            jsonify(
                {
                    "status": "uploaded",
                    "org": org,
                    "source": source,
                    "fields": public_pws_fields(fields),
                    "overall": weather_period_to_dict(summary.overall),
                    "latest_period": weather_period_to_dict(summary.segments[-1]),
                    "response": result.response_text,
                }
            ),
            200,
        )

    return app


def require_bearer_token(req) -> str:
    authorization = req.headers.get("Authorization")
    if not authorization:
        raise ValueError("access denied")

    scheme, _, token = authorization.partition(" ")
    if scheme != "Bearer" or not token:
        raise ValueError("access denied")
    return token


def resolve_message_day(day_text: str | None) -> date:
    if day_text is None:
        return datetime.now(timezone.utc).date()
    return parse_utc_date(day_text)


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


def registration_fields_from_payload(
    payload: dict[str, object],
) -> tuple[str, dict[str, object]]:
    org_value = payload.get("org")
    if not isinstance(org_value, str) or not org_value.strip():
        raise ValueError("org must be a non-empty string")

    fields: dict[str, object] = {}
    if "auth" in payload:
        auth_value = payload.get("auth")
        if not isinstance(auth_value, str) or len(auth_value) < 8:
            raise ValueError("auth must be a string with length 8 or longer")
        fields["auth"] = auth_value

    if "span" in payload:
        span_value = payload.get("span")
        if not isinstance(span_value, str) or not span_value.strip():
            raise ValueError("span must be a non-empty string when provided")
        parse_timespan(span_value)
        fields["span"] = span_value.strip()

    if "limit" in payload:
        limit_value = payload.get("limit")
        if isinstance(limit_value, bool):
            raise ValueError("limit must be an integer when provided")
        if isinstance(limit_value, int):
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
        fields["limit"] = limit

    for field_name in ("gkey", "pwsid", "pwskey"):
        if field_name not in payload:
            continue
        field_value = payload.get(field_name)
        if not isinstance(field_value, str):
            raise ValueError(f"{field_name} must be a string when provided")
        fields[field_name] = field_value.strip()

    return org_value.strip(), fields


def registration_response(
    registration: RegistrationRecord,
    *,
    include_secrets: bool,
) -> dict[str, object]:
    payload = {
        "org": registration.org,
        "span": registration.span,
        "limit": registration.limit,
        "gkey": registration.gkey,
        "pwsid": registration.pwsid,
    }
    if include_secrets:
        payload["pwskey"] = registration.pwskey
    return payload


def handle_value_error(
    message: str,
    exc: ValueError,
    *,
    add_missing_registration: bool = False,
) -> tuple[Response, int]:
    error_text = str(exc)
    log_exception(message, exc)
    if error_text == "registration not found":
        return json_error(
            "access denied" if add_missing_registration else "registration not found",
            401 if add_missing_registration else 404,
        )
    if error_text == "access denied":
        return json_error("access denied", 401)
    return json_error(error_text, 400)


def plain_text(body: str, status: int = 200) -> Response:
    return Response(body, status=status, mimetype="text/plain")


def json_error(message: str, status: int) -> tuple[Response, int]:
    return jsonify({"error": message}), status


app = create_app()


if __name__ == "__main__":
    app.run(host=os.environ.get("NMEA_REPOSITORY_HOST", "127.0.0.1"), port=5000)
