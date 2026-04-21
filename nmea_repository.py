#!/usr/bin/env python3
"""REST repository for JSON batches emitted by nmea_filter."""

from __future__ import annotations

import os
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request
from sqlalchemy import DateTime, String, Text, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from common_helpers import (
    log_exception as write_log_exception,
    parse_query_time_range,
    parse_utc_datetime,
)


PROGRAM_NAME = os.path.splitext(os.path.basename(__file__))[0]
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATABASE_PATH = Path(SCRIPT_DIR) / "data" / "nmea_repository.db"
DATABASE_URL = os.environ.get(
    "NMEA_REPOSITORY_DATABASE_URL",
    f"sqlite:///{DEFAULT_DATABASE_PATH.as_posix()}",
)
log_exception = partial(write_log_exception, PROGRAM_NAME, __file__)


class Base(DeclarativeBase):
    pass


class NmeaMessage(Base):
    __tablename__ = "NmeaMessages"

    org: Mapped[str] = mapped_column(String(collation="NOCASE"), nullable=False, primary_key=True)
    source: Mapped[str] = mapped_column(String(collation="NOCASE"), nullable=False, primary_key=True)
    utc: Mapped[datetime] = mapped_column(DateTime, nullable=False, primary_key=True)
    sentences: Mapped[str] = mapped_column(Text, nullable=False)


def create_app(database_url: str = DATABASE_URL) -> Flask:
    app = Flask(__name__)
    if database_url == DATABASE_URL:
        DEFAULT_DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, future=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    with engine.begin() as connection:
        Base.metadata.create_all(connection)
        # sneak in manual database schema and data changes here
        # connection.exec_driver_sql("ALTER TABLE Messages ADD COLUMN org TEXT NOT NULL DEFAULT 'WSC'")

    @app.put("/nmea/add/<org>/<source>")
    def add_nmea_message(org: str, source: str) -> tuple[Response, int]:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return json_error("request body must be a JSON object", 400)

        try:
            start_time, sentence_text = validate_payload(payload)
        except ValueError as exc:
            log_exception("invalid add payload", exc)
            return json_error(str(exc), 400)

        with session_factory.begin() as session:
            record = session.scalars(
                select(NmeaMessage)
                .where(NmeaMessage.org == org)
                .where(NmeaMessage.source == source)
                .where(NmeaMessage.utc == start_time)
                .limit(1)
            ).first()
            if record is None:
                session.add(
                    NmeaMessage(
                        org=org,
                        source=source,
                        utc=start_time,
                        sentences=sentence_text,
                    )
                )
                return jsonify({"status": "created"}), 201
            else:
                record.sentences = sentence_text
                return jsonify({"status": "updated"}), 200
    

    @app.get("/nmea/last/<org>/<source>")
    @app.get("/nmea/last/<org>/<source>/<int:count>")
    def get_nmea_latest_messages(org: str, source: str, count: int = 1) -> Response:
        if count < 1:
            return json_error("count must be greater than zero", 400)

        with session_factory() as session:
            records = get_latest_messages(session, org, source, count)

        if not records:
            return plain_text("", 404)
        return plain_text("\n".join(record.sentences for record in records))
    

    @app.get("/nmea/count/<org>/<source>")
    def count_nmea_messages(org: str, source: str) -> Response:
        start_text = request.args.get("start")
        end_text = request.args.get("end")

        statement = (
            select(func.count())
            .select_from(NmeaMessage)
            .where(NmeaMessage.org == org)
            .where(NmeaMessage.source == source)
        )
        try:
            statement = apply_date_filters(statement, start_text, end_text)
        except ValueError as exc:
            log_exception("invalid count query parameters", exc)
            return json_error(str(exc), 400)

        with session_factory() as session:
            record_count = session.scalar(statement)

        return plain_text(str(record_count or 0))
    

    @app.get("/nmea/search/<org>/<source>")
    def search_nmea_messages(org: str, source: str) -> Response:
        start_text = request.args.get("start")
        end_text = request.args.get("end")

        with session_factory() as session:
            if start_text is None and end_text is None:
                records = get_latest_messages(session, org, source)
                if not records:
                    return plain_text("", 404)
                return plain_text(records[0].sentences)

            statement = (
                select(NmeaMessage)
                .where(NmeaMessage.org == org)
                .where(NmeaMessage.source == source)
                .order_by(NmeaMessage.utc.asc())
            )
            try:
                statement = apply_date_filters(statement, start_text, end_text)
            except ValueError as exc:
                log_exception("invalid search query parameters", exc)
                return json_error(str(exc), 400)

            records = session.scalars(statement).all()

        if not records:
            return plain_text("", 404)
        return plain_text("\n".join(record.sentences for record in records))

    return app


def validate_payload(payload: dict[str, Any]) -> tuple[datetime, str]:
    start_value = payload.get("start")
    if not isinstance(start_value, str) or not start_value.strip():
        raise ValueError("JSON object must contain a non-empty string start")

    sentences = payload.get("sentences")
    if not isinstance(sentences, list) or not all(
        isinstance(sentence, str) for sentence in sentences
    ):
        raise ValueError("JSON object must contain a sentences list of strings")

    start_time = parse_utc_datetime(start_value)
    sentence_text = "\n".join(
        sentence for sentence in sentences if isinstance(sentence, str)
    )
    return start_time, sentence_text


def apply_date_filters(statement, start_text: str | None, end_text: str | None):
    if start_text is not None:
        start, _ = parse_query_time_range(start_text)
        statement = statement.where(NmeaMessage.utc >= start)
    if end_text is not None:
        _, end = parse_query_time_range(end_text)
        statement = statement.where(NmeaMessage.utc < end)
    return statement


def get_latest_messages(
    session: Session,
    org: str,
    source: str,
    count: int = 1,
) -> list[NmeaMessage]:
    records = session.scalars(
        select(NmeaMessage)
        .where(NmeaMessage.org == org)
        .where(NmeaMessage.source == source)
        .order_by(NmeaMessage.utc.desc())
        .limit(count)
    ).all()
    records.reverse()
    return records


def plain_text(body: str, status: int = 200) -> Response:
    return Response(body, status=status, mimetype="text/plain")


def json_error(message: str, status: int) -> tuple[Response, int]:
    return jsonify({"error": message}), status


app = create_app()


if __name__ == "__main__":
    app.run(host=os.environ.get("NMEA_REPOSITORY_HOST", "127.0.0.1"), port=5000)
