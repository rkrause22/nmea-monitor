#!/usr/bin/env python3
"""REST repository for JSON batches emitted by nmea_filter."""

from __future__ import annotations

import os
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request, send_from_directory
from sqlalchemy import DateTime, String, Text, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from nmea_helpers import (
    FrameAggregator,
    NMEAError,
    average_direction_degrees,
    average_geographic_degrees,
    average_value,
    format_utc_datetime,
    log_exception as write_log_exception,
    parse_sentence,
    parse_query_time_range,
    parse_utc_datetime,
    parse_zda_datetime,
)


PROGRAM_NAME = os.path.splitext(os.path.basename(__file__))[0]
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATABASE_PATH = Path(SCRIPT_DIR) / "data" / "nmea_repository.db"
DATABASE_URL = os.environ.get(
    "NMEA_REPOSITORY_DATABASE_URL",
    f"sqlite:///{DEFAULT_DATABASE_PATH.as_posix()}",
)
log_exception = partial(write_log_exception, PROGRAM_NAME, __file__)
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

    @app.get("/<filename>.html")
    def get_html_page(filename: str) -> Response:
        return send_from_directory(SCRIPT_DIR, f"{filename}.html")

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
    

    @app.get("/nmea/count")
    @app.get("/nmea/count/<org>")
    @app.get("/nmea/count/<org>/<source>")
    def count_nmea_messages(
        org: str | None = None,
        source: str | None = None,
    ) -> Response:
        start_text = request.args.get("start")
        end_text = request.args.get("end")

        statement = (
            select(func.count())
            .select_from(NmeaMessage)
        )
        if org is not None:
            statement = statement.where(NmeaMessage.org == org)
        if source is not None:
            statement = statement.where(NmeaMessage.source == source)

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

    @app.get("/nmea/weather/<org>/<source>")
    @app.get("/nmea/weather/<org>/<source>/<int:count>")
    def get_nmea_weather(org: str, source: str, count: int = 1) -> Response:
        if count < 1:
            return json_error("count must be greater than zero", 400)

        with session_factory() as session:
            records = get_latest_messages(session, org, source, count)

        if not records:
            return jsonify({"error": "no records found"}), 404

        return jsonify(build_weather_summary(records))

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


def build_weather_summary(records: list[NmeaMessage]) -> dict[str, object]:
    frame = FrameAggregator()
    utc_time = records[-1].utc

    for record in records:
        for raw_sentence in record.sentences.splitlines():
            if not raw_sentence.strip():
                continue
            try:
                sentence = parse_sentence(raw_sentence)
                if sentence.sentence_type == "ZDA":
                    utc_time = parse_zda_datetime(sentence).replace(tzinfo=None)
                else:
                    frame.add_sentence(sentence)
            except NMEAError as exc:
                log_exception("invalid weather sentence skipped", exc, raw_sentence)

    latitude, longitude = weather_position(frame)
    temperature = weather_temperature(frame)
    wind_direction, wind_direction_units = weather_wind_direction(frame)
    wind_direction_symbol = symbolic_wind_direction(wind_direction)
    wind_speed, wind_speed_units = weather_wind_speed(frame)

    return {
        "org": records[-1].org,
        "source": records[-1].source,
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
    }


def weather_position(frame: FrameAggregator) -> tuple[float | None, float | None]:
    if not frame.gga.has_data():
        return None, None
    return average_geographic_degrees(
        frame.gga.position_x_sum,
        frame.gga.position_y_sum,
        frame.gga.position_z_sum,
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


def plain_text(body: str, status: int = 200) -> Response:
    return Response(body, status=status, mimetype="text/plain")


def json_error(message: str, status: int) -> tuple[Response, int]:
    return jsonify({"error": message}), status


app = create_app()


if __name__ == "__main__":
    app.run(host=os.environ.get("NMEA_REPOSITORY_HOST", "127.0.0.1"), port=5000)
