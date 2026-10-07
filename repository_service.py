"""Service-layer authorization and orchestration for repository backends."""

from __future__ import annotations

from datetime import timedelta

from nmea_helpers import datesub
from repository_store import MessageRecord, RegistrationRecord, RepairResult, RepositoryStore


class RepositoryService:
    def __init__(self, store: RepositoryStore) -> None:
        self.store = store

    # Message operations
    def add_message(
        self,
        auth: str,
        org: str,
        source: str,
        record: MessageRecord,
    ) -> bool:
        self._require_org_access(org, auth)
        return self.store.add_message(org, source, record)

    def find_messages(
        self,
        org: str,
        source: str,
        start=None,
        end=None,
        count: int | None = None,
    ) -> list[MessageRecord]:
        return self.store.find_messages(org, source, start, end, count)

    def count_messages(
        self,
        org: str | None = None,
        source: str | None = None,
        start=None,
        end=None,
    ) -> int:
        return self.store.count_messages(org, source, start, end)

    def get_latest_messages(
        self,
        org: str,
        source: str,
        what: str | None = None,
    ) -> list[MessageRecord]:
        latest_records = self.store.find_messages(org, source, count=1)
        if what is None or not latest_records:
            return latest_records

        text = what.strip()
        if not text:
            raise ValueError("what must not be empty")

        try:
            count = int(text)
        except ValueError:
            start = datesub(latest_records[-1].utc, text)
            return self.store.find_messages(org, source, start=start)

        if count < 1:
            raise ValueError("count must be greater than zero")
        return self.store.find_messages(org, source, count=count)

    def get_history(
        self,
        org: str,
        source: str,
        start=None,
        end=None,
        span: str | None = None,
    ) -> list[MessageRecord]:
        if span is not None and start is None and end is None:
            latest_records = self.store.find_messages(org, source, count=1)
            if not latest_records:
                return []
            latest_utc = latest_records[-1].utc
            start = datesub(latest_utc, span)
            end = latest_utc + timedelta(microseconds=1)
        return self.store.find_messages(org, source, start=start, end=end)

    def purge_stale_data(
        self,
        auth: str,
        org: str,
        source: str,
        what: str | None = None,
    ) -> int:
        self._require_org_access(org, auth)
        return self.store.purge_stale_data(org, source, what)

    def fix_message_file(
        self,
        auth: str,
        org: str,
        source: str,
        day=None,
    ) -> RepairResult:
        self._require_org_access(org, auth)
        return self.store.fix_message_file(org, source, day)

    # Registration operations
    def add_registration(
        self,
        auth: str,
        registration: RegistrationRecord,
    ) -> bool:
        if not auth:
            raise ValueError("access denied")

        admin_registration = self.store.get_registration("admin")
        if admin_registration is None:
            if registration.org.lower() != "admin":
                raise ValueError("access denied")
            if auth != registration.auth:
                raise ValueError("access denied")
        else:
            self._require_admin_access(auth)
        return self.store.add_registration(registration)

    def save_registration(
        self,
        auth: str,
        org: str,
        fields: dict[str, object],
    ) -> bool:
        if not auth:
            raise ValueError("access denied")

        existing_registration = self.store.get_registration(org)
        admin_registration = self.store.get_registration("admin")
        if existing_registration is None:
            registration = self._registration_from_fields(org, fields)
            if admin_registration is None:
                if registration.org.lower() != "admin":
                    raise ValueError("access denied")
                if auth != registration.auth:
                    raise ValueError("access denied")
            else:
                self._require_admin_access(auth)
            return self.store.add_registration(registration)

        self._require_admin_access(auth)
        self.store.update_registration(org, fields)
        return False

    def delete_registration(self, auth: str, org: str) -> int:
        self._require_admin_access(auth)
        return self.store.delete_registration(org)

    def lookup_registration(self, org: str) -> RegistrationRecord | None:
        return self.store.get_registration(org)

    def get_registration(self, auth: str, org: str) -> RegistrationRecord | None:
        self._require_admin_access(auth)
        return self.store.get_registration(org)

    def get_org_registration(self, auth: str, org: str) -> RegistrationRecord:
        self._require_org_access(org, auth)
        registration = self.store.get_registration(org)
        if registration is None:
            raise ValueError("registration not found")
        return registration

    def get_registrations(
        self,
        auth: str,
        org: str | None = None,
    ) -> list[RegistrationRecord]:
        self._require_admin_access(auth)
        return self.store.get_registrations(org)

    def _registration_from_fields(
        self,
        org: str,
        fields: dict[str, object],
    ) -> RegistrationRecord:
        auth = fields.get("auth")
        if not isinstance(auth, str):
            raise ValueError("auth is required for new registrations")

        return RegistrationRecord(
            org=org,
            auth=auth,
            span=self._string_field(fields, "span", "1 year"),
            limit=self._integer_field(fields, "limit", 366),
            gkey=self._string_field(fields, "gkey", ""),
            pwsid=self._string_field(fields, "pwsid", ""),
            pwskey=self._string_field(fields, "pwskey", ""),
        )

    def _string_field(
        self,
        fields: dict[str, object],
        name: str,
        default: str,
    ) -> str:
        value = fields.get(name, default)
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        return value

    def _integer_field(
        self,
        fields: dict[str, object],
        name: str,
        default: int,
    ) -> int:
        value = fields.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
        return value

    def _require_admin_access(self, auth: str) -> None:
        admin_registration = self.store.get_registration("admin")
        if admin_registration is None:
            raise ValueError("access denied")
        if not self.store.authenticate("admin", auth):
            raise ValueError("access denied")

    def _require_org_access(self, org: str, auth: str) -> None:
        if self.store.get_registration(org) is None:
            raise ValueError("registration not found")
        if not self.store.authenticate(org, auth):
            raise ValueError("access denied")
