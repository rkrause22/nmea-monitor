"""File-backed repository store for daily JSONL NMEA logs."""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import re
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from nmea_helpers import datesub, format_utc_datetime, parse_timespan, parse_utc_datetime
from repository_store import MessageRecord, RegistrationRecord, RepairResult, RepositoryStore


NMEA_SUFFIX = ".nmea.jsonl"
GZIP_SUFFIX = ".nmea.jsonl.gz"
AUTH_HASH_ITERATIONS = 200_000
AUTH_SALT_BYTES = 16
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class FileStore(RepositoryStore):
    def __init__(
        self,
        data_root: str | Path,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.messages_root = self.data_root / "messages"
        self.registrations_path = self.data_root / "registrations" / "registrations.json"
        self._now_factory = now_factory or (
            lambda: datetime.now(timezone.utc).replace(tzinfo=None)
        )

    # Message operations
    def add_message(
        self,
        org: str,
        source: str,
        record: MessageRecord,
    ) -> bool:
        org = self._normalize_org_name(org)
        source = self._normalize_source_name(source)
        folder = self._message_folder(org, source)
        folder.mkdir(parents=True, exist_ok=True)

        target_path = self._nmea_path(org, source, record.utc.date())

        if not target_path.exists():
            self._compress_stale_nmea_files(org, source, keep_date=record.utc.date())
            self.purge_stale_data(org, source, None)

        self._append_record(target_path, record)

        gzip_path = self._gzip_path(org, source, record.utc.date())
        if gzip_path.exists():
            gzip_path.unlink()
        return True

    def find_messages(
        self,
        org: str,
        source: str,
        start: datetime | None = None,
        end: datetime | None = None,
        count: int | None = None,
    ) -> list[MessageRecord]:
        org = self._normalize_org_name(org)
        source = self._normalize_source_name(source)
        if count is not None and count < 1:
            return []
        if start is not None and end is not None and start >= end:
            return []

        if start is None and end is None:
            latest_count = 1 if count is None else count
            return self._find_latest_messages(org, source, latest_count)

        matches: list[MessageRecord] = []
        for daily_path in self._iter_candidate_paths(org, source, start, end):
            for record in self._read_records(daily_path):
                if start is not None and record.utc < start:
                    continue
                if end is not None and record.utc >= end:
                    continue
                matches.append(record)

        if count is not None:
            matches = matches[-count:]
        return matches

    def count_messages(
        self,
        org: str | None = None,
        source: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        total = 0
        for path in self._iter_scope_paths(org, source, start, end):
            for record in self._read_records(path):
                if start is not None and record.utc < start:
                    continue
                if end is not None and record.utc >= end:
                    continue
                total += 1
        return total

    def purge_stale_data(
        self,
        org: str,
        source: str,
        what: str | None = None,
    ) -> int:
        org = self._normalize_org_name(org)
        source = self._normalize_source_name(source)
        available_units = self._available_days(org, source)
        if not available_units:
            return 0

        if what is None:
            registration = self.get_registration(org)
            if registration is None:
                raise ValueError("registration not found")
            cutoff_date = self._cutoff_date_for_span(registration.span)
            volume_to_keep = registration.limit
        else:
            text = what.strip()
            if not text:
                raise ValueError("what must not be empty")
            if self._looks_like_integer(text):
                cutoff_date = None
                volume_to_keep = int(text)
            else:
                cutoff_date = self._cutoff_date_for_span(text)
                volume_to_keep = None

        deleted = 0
        keep_units = set(available_units)

        if cutoff_date is not None:
            keep_units = {day for day in keep_units if day >= cutoff_date}

        if volume_to_keep is not None and len(keep_units) > volume_to_keep:
            newest_units = sorted(keep_units)[-volume_to_keep:] if volume_to_keep > 0 else []
            keep_units = set(newest_units)

        for day, path in available_units.items():
            if day in keep_units:
                continue
            path.unlink()
            deleted += 1
        return deleted

    def fix_message_file(
        self,
        org: str,
        source: str,
        day: date | None = None,
    ) -> RepairResult:
        org = self._normalize_org_name(org)
        source = self._normalize_source_name(source)
        target_day = day or self._now_factory().date()
        plain_path = self._nmea_path(org, source, target_day)
        gzip_path = self._gzip_path(org, source, target_day)

        source_path: Path | None = None
        was_gzipped = False
        if plain_path.exists():
            source_path = plain_path
        elif gzip_path.exists():
            source_path = gzip_path
            was_gzipped = True
        if source_path is None:
            raise ValueError("message file not found")

        opener = gzip.open if was_gzipped else open
        valid_lines: list[str] = []
        corrupt_rows: list[str] = []
        with opener(source_path, "rt", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.rstrip("\r\n")
                if not line.strip():
                    corrupt_rows.append(line)
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    corrupt_rows.append(line)
                    continue
                record = self._message_record_from_storage_payload(payload)
                if record is None:
                    corrupt_rows.append(line)
                    continue
                valid_lines.append(self._record_to_jsonl_line(record))

        self._write_fixed_lines(plain_path, valid_lines)

        if target_day == self._now_factory().date():
            if gzip_path.exists():
                gzip_path.unlink()
        else:
            self._compress_plain_file(plain_path, gzip_path)

        return RepairResult(
            org=org,
            source=source,
            day=target_day,
            removed_count=len(corrupt_rows),
            corrupt_rows=corrupt_rows,
        )

    # Registration operations
    def add_registration(
        self,
        registration: RegistrationRecord,
    ) -> bool:
        normalized_org = self._normalize_org_name(registration.org)
        registration = RegistrationRecord(
            org=normalized_org,
            auth=registration.auth,
            span=registration.span,
            limit=registration.limit,
            gkey=registration.gkey,
            pwsid=registration.pwsid,
            pwskey=registration.pwskey,
        )
        if len(registration.auth) < 8:
            raise ValueError("registration auth must be at least 8 characters")
        parse_timespan(registration.span)
        if registration.limit < 1:
            raise ValueError("registration limit must be greater than zero")

        registrations = self._load_registrations()
        registration_payload = self._get_registration_payload(
            registrations, registration.org
        )

        if registration_payload is None:
            registration_payload = self._build_registration_payload(registration)
            registrations[registration.org] = registration_payload
        else:
            return False
        self._save_registrations(registrations)
        return True

    def update_registration(
        self,
        org: str,
        fields: dict[str, object],
    ) -> None:
        org = self._normalize_org_name(org)
        registrations = self._load_registrations()
        registration_payload = self._get_registration_payload(registrations, org)
        if registration_payload is None:
            raise ValueError("registration not found")

        auth = fields.get("auth")
        if auth is not None:
            if not isinstance(auth, str) or len(auth) < 8:
                raise ValueError("registration auth must be at least 8 characters")
            salt = self._new_auth_salt()
            registration_payload["auth_hash"] = self._hash_auth_token(auth, salt)
            registration_payload["auth_salt"] = salt

        span = fields.get("span")
        if span is not None:
            if not isinstance(span, str):
                raise ValueError("registration span must be a string")
            parse_timespan(span)
            registration_payload["span"] = span

        limit = fields.get("limit")
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool):
                raise ValueError("registration limit must be an integer")
            if limit < 1:
                raise ValueError("registration limit must be greater than zero")
            registration_payload["limit"] = limit

        for field_name in ("gkey", "pwsid", "pwskey"):
            field_value = fields.get(field_name)
            if field_value is None:
                continue
            if not isinstance(field_value, str):
                raise ValueError(f"registration {field_name} must be a string")
            registration_payload[field_name] = field_value

        self._save_registrations(registrations)

    def authenticate(self, org: str, auth: str) -> bool:
        org = self._normalize_org_name(org)
        if len(auth) < 1:
            return False
        registrations = self._load_registrations()
        registration_payload = self._get_registration_payload(registrations, org)
        if registration_payload is None:
            return False
        return self._verify_org_auth(registration_payload, auth)

    def delete_registration(
        self,
        org: str,
    ) -> int:
        org = self._normalize_org_name(org)
        registrations = self._load_registrations()
        registration_payload = self._get_registration_payload(registrations, org)
        if registration_payload is None:
            return 0

        deleted_files = self._count_stored_files(self.messages_root / org)
        registrations.pop(org, None)
        self._save_registrations(registrations)
        org_folder = self.messages_root / org
        if org_folder.exists():
            paths = sorted(
                org_folder.rglob("*"),
                key=lambda path: (len(path.parts), str(path).lower()),
                reverse=True,
            )
            for path in paths:
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            if org_folder.exists():
                org_folder.rmdir()
        return deleted_files

    def get_registrations(
        self,
        org: str | None = None,
    ) -> list[RegistrationRecord]:
        if org is not None:
            org = self._normalize_org_name(org)
        registrations = self._load_registrations()
        records: list[RegistrationRecord] = []

        for org_name in sorted(registrations):
            normalized_org_name = org_name.lower() if isinstance(org_name, str) else ""
            if org is not None and not normalized_org_name.startswith(org):
                continue
            record = self.get_registration(normalized_org_name)
            if record is not None:
                records.append(record)
        return records

    def _load_registrations(self) -> dict[str, object]:
        if not self.registrations_path.exists():
            return {}
        with self.registrations_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("registrations file must contain a JSON object")
        return payload

    def _save_registrations(self, registrations: dict[str, object]) -> None:
        self.registrations_path.parent.mkdir(parents=True, exist_ok=True)
        with self.registrations_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(registrations, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")

    def _find_latest_messages(
        self,
        org: str,
        source: str,
        count: int,
    ) -> list[MessageRecord]:
        matches: list[MessageRecord] = []
        for daily_path in self._iter_daily_paths_desc(org, source):
            day_records = list(self._read_records(daily_path))
            if not day_records:
                continue
            for record in reversed(day_records):
                matches.append(record)
                if len(matches) == count:
                    matches.reverse()
                    return matches
        matches.reverse()
        return matches

    def _message_folder(self, org: str, source: str) -> Path:
        return self.messages_root / org / source

    def _nmea_path(self, org: str, source: str, day: date) -> Path:
        filename = f"{org}-{source}-{day.isoformat()}{NMEA_SUFFIX}"
        return self._message_folder(org, source) / filename

    def _gzip_path(self, org: str, source: str, day: date) -> Path:
        filename = f"{org}-{source}-{day.isoformat()}{GZIP_SUFFIX}"
        return self._message_folder(org, source) / filename

    def _compress_stale_nmea_files(
        self,
        org: str,
        source: str,
        keep_date: date,
    ) -> None:
        for nmea_path in self._message_folder(org, source).glob(f"*{NMEA_SUFFIX}"):
            day = self._date_from_path(nmea_path)
            if day is None or day == keep_date:
                continue
            gzip_path = self._gzip_path(org, source, day)
            self._compress_plain_file(nmea_path, gzip_path)

    def _iter_candidate_paths(
        self,
        org: str,
        source: str,
        start: datetime | None,
        end: datetime | None,
    ) -> Iterable[Path]:
        lower_date = start.date() if start is not None else None
        upper_dt = end if end is not None else self._now_factory()
        upper_date = upper_dt.date()

        for day, path in self._available_days(org, source).items():
            if lower_date is not None and day < lower_date:
                continue
            if day > upper_date:
                continue
            yield path

    def _iter_daily_paths_desc(self, org: str, source: str) -> Iterable[Path]:
        for _, path in reversed(list(self._available_days(org, source).items())):
            yield path

    def _iter_scope_paths(
        self,
        org: str | None,
        source: str | None,
        start: datetime | None,
        end: datetime | None,
    ) -> Iterable[Path]:
        if source is not None and org is None:
            raise ValueError("org is required when source is provided")

        normalized_org = self._normalize_org_name(org) if org is not None else None
        normalized_source = (
            self._normalize_source_name(source) if source is not None else None
        )

        if normalized_org is not None and normalized_source is not None:
            yield from self._iter_candidate_paths(
                normalized_org,
                normalized_source,
                start,
                end,
            )
            return

        folders = self._iter_message_folders(normalized_org)
        for folder in folders:
            scope_org = folder.parent.name
            scope_source = folder.name
            yield from self._iter_candidate_paths(scope_org, scope_source, start, end)

    def _iter_message_folders(self, org: str | None = None) -> Iterable[Path]:
        if org is not None:
            org_root = self.messages_root / org
            roots = [org_root] if org_root.exists() else []
        elif self.messages_root.exists():
            roots = [path for path in self.messages_root.iterdir() if path.is_dir()]
        else:
            roots = []

        for root in sorted(roots):
            for path in sorted(root.iterdir()):
                if path.is_dir():
                    yield path

    def _available_days(self, org: str, source: str) -> dict[date, Path]:
        folder = self._message_folder(org, source)
        if not folder.exists():
            return {}

        by_day: dict[date, Path] = {}
        for path in sorted(folder.iterdir()):
            if not path.is_file():
                continue
            if not (path.name.endswith(NMEA_SUFFIX) or path.name.endswith(GZIP_SUFFIX)):
                continue
            day = self._date_from_path(path)
            if day is None:
                continue
            existing = by_day.get(day)
            if existing is None or existing.name.endswith(GZIP_SUFFIX):
                by_day[day] = path
        return dict(sorted(by_day.items()))

    def _count_day_files(self, root: Path) -> int:
        count = 0
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.name.endswith(NMEA_SUFFIX) or path.name.endswith(GZIP_SUFFIX):
                count += 1
        return count

    def _count_stored_files(self, root: Path) -> int:
        if not root.exists():
            return 0
        return self._count_day_files(root)

    def _date_from_path(self, path: Path) -> date | None:
        name = path.name
        suffix = GZIP_SUFFIX if name.endswith(GZIP_SUFFIX) else NMEA_SUFFIX
        if not name.endswith(suffix):
            return None

        day_text = name[: -len(suffix)].rsplit("-", 3)[-3:]
        if len(day_text) != 3:
            return None

        try:
            return date.fromisoformat("-".join(day_text))
        except ValueError:
            return None

    def _read_records(self, path: Path) -> Iterable[MessageRecord]:
        opener = gzip.open if path.name.endswith(GZIP_SUFFIX) else open
        with opener(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                record = self._message_record_from_storage_payload(payload)
                if record is not None:
                    yield record

    def _append_record(
        self,
        path: Path,
        record: MessageRecord,
    ) -> None:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(self._record_to_jsonl_line(record))
            handle.write("\n")

    def _record_to_jsonl_line(self, record: MessageRecord) -> str:
        payload = {
            "utc": format_utc_datetime(record.utc),
            "sentences": record.sentences,
        }
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))

    def _message_record_from_storage_payload(
        self,
        payload: object,
    ) -> MessageRecord | None:
        if not isinstance(payload, dict):
            return None
        utc_value = payload.get("utc")
        sentences_value = payload.get("sentences")
        if not isinstance(utc_value, str):
            return None
        if not isinstance(sentences_value, list):
            return None
        sentences = [item for item in sentences_value if isinstance(item, str)]
        if len(sentences) != len(sentences_value):
            return None
        try:
            utc = parse_utc_datetime(utc_value)
        except ValueError:
            return None
        return MessageRecord(utc=utc, sentences=sentences)

    def _write_fixed_lines(self, path: Path, lines: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                handle.write(line)
                handle.write("\n")

    def _compress_plain_file(self, plain_path: Path, gzip_path: Path) -> None:
        with plain_path.open("rb") as source_handle:
            with gzip.open(gzip_path, "wb") as target_handle:
                target_handle.writelines(source_handle)
        plain_path.unlink()

    def get_registration(self, org: str) -> RegistrationRecord | None:
        registrations = self._load_registrations()
        registration_payload = self._get_registration_payload(registrations, org)
        if registration_payload is None:
            return None
        return self._registration_from_payload(org, registration_payload)

    def _registration_from_payload(
        self,
        org: str,
        payload: object,
    ) -> RegistrationRecord | None:
        if not isinstance(payload, dict):
            return None
        span = payload.get("span")
        limit = payload.get("limit")
        gkey = payload.get("gkey")
        pwsid = payload.get("pwsid")
        pwskey = payload.get("pwskey")
        if not isinstance(span, str):
            return None
        if not isinstance(limit, int) or isinstance(limit, bool):
            return None
        return RegistrationRecord(
            org=org,
            auth="",
            span=span,
            limit=limit,
            gkey=gkey if isinstance(gkey, str) else "",
            pwsid=pwsid if isinstance(pwsid, str) else "",
            pwskey=pwskey if isinstance(pwskey, str) else "",
        )

    def _cutoff_date_for_span(self, span_text: str) -> date:
        cutoff = datesub(self._now_factory(), span_text)
        return cutoff.date()

    def _looks_like_integer(self, text: str) -> bool:
        try:
            value = int(text)
        except ValueError:
            return False
        return value >= 0

    def _build_registration_payload(
        self,
        registration: RegistrationRecord,
    ) -> dict[str, object]:
        salt = self._new_auth_salt()
        return {
            "auth_hash": self._hash_auth_token(registration.auth, salt),
            "auth_salt": salt,
            "span": registration.span,
            "limit": registration.limit,
            "gkey": registration.gkey,
            "pwsid": registration.pwsid,
            "pwskey": registration.pwskey,
        }

    def _get_registration_payload(
        self,
        registrations: dict[str, object],
        org: str,
    ) -> dict[str, object] | None:
        normalized_org = self._normalize_org_name(org)
        raw_payload = registrations.get(normalized_org)
        if raw_payload is None:
            matched_org: str | None = None
            for existing_org, existing_payload in registrations.items():
                if isinstance(existing_org, str) and existing_org.lower() == normalized_org:
                    matched_org = existing_org
                    raw_payload = existing_payload
                    break
            if matched_org is not None and matched_org != normalized_org:
                registrations[normalized_org] = raw_payload
        if raw_payload is None:
            return None
        if not isinstance(raw_payload, dict):
            raise ValueError("registration org entries must be JSON objects")

        if "span" in raw_payload and "limit" in raw_payload:
            return raw_payload

        return self._upgrade_legacy_registration_payload(
            registrations,
            normalized_org,
            raw_payload,
        )

    def _upgrade_legacy_registration_payload(
        self,
        registrations: dict[str, object],
        org: str,
        legacy_payload: dict[str, object],
    ) -> dict[str, object]:
        legacy_auth: str | None = None
        span_value: str | None = None
        limit_value: int | None = None

        for _, payload in legacy_payload.items():
            if not isinstance(payload, dict):
                continue
            auth = payload.get("auth")
            span = payload.get("span")
            limit = payload.get("limit")
            if not isinstance(span, str):
                continue
            if not isinstance(limit, int) or isinstance(limit, bool):
                continue
            if legacy_auth is None and isinstance(auth, str):
                legacy_auth = auth
            if span_value is None:
                span_value = span
            if limit_value is None:
                limit_value = limit

        upgraded: dict[str, object] = {}
        if legacy_auth is not None:
            salt = self._new_auth_salt()
            upgraded["auth_hash"] = self._hash_auth_token(legacy_auth, salt)
            upgraded["auth_salt"] = salt
        if span_value is not None:
            upgraded["span"] = span_value
        if limit_value is not None:
            upgraded["limit"] = limit_value

        registrations[org] = upgraded
        return upgraded

    def _verify_org_auth(
        self,
        registration_payload: dict[str, object],
        auth: str,
    ) -> bool:
        auth_hash = registration_payload.get("auth_hash")
        auth_salt = registration_payload.get("auth_salt")
        if isinstance(auth_hash, str) and isinstance(auth_salt, str):
            expected_hash = self._hash_auth_token(auth, auth_salt)
            return hmac.compare_digest(expected_hash, auth_hash)
        return False

    def _new_auth_salt(self) -> str:
        return base64.b64encode(secrets.token_bytes(AUTH_SALT_BYTES)).decode("ascii")

    def _hash_auth_token(self, auth: str, salt_text: str) -> str:
        salt = base64.b64decode(salt_text.encode("ascii"))
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            auth.encode("utf-8"),
            salt,
            AUTH_HASH_ITERATIONS,
        )
        return base64.b64encode(digest).decode("ascii")

    def _normalize_org_name(self, org: str) -> str:
        return self._normalize_name(org, "org")

    def _normalize_source_name(self, source: str) -> str:
        return self._normalize_name(source, "source")

    def _normalize_name(self, value: str, label: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError(f"{label} must not be empty")
        if text in (".", ".."):
            raise ValueError(f"{label} contains unsupported characters")
        if not NAME_PATTERN.fullmatch(text):
            raise ValueError(
                f"{label} must use only letters, numbers, '.', '_', or '-'"
            )
        return text.lower()
