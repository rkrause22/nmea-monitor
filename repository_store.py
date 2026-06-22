"""Storage-facing interfaces for NMEA repository backends.

These types are intentionally small for the first file-store pass. Some nearby
domain objects now live in the NMEA aggregation and weather helper modules; once
the file-backed path settles down, we can consolidate overlapping models instead
of carrying parallel shapes forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class MessageRecord:
    utc: datetime
    sentences: list[str]

    @property
    def sentence_text(self) -> str:
        return "\n".join(self.sentences)


@dataclass(frozen=True)
class RegistrationRecord:
    org: str
    auth: str
    span: str
    limit: int
    gkey: str = ""


@dataclass(frozen=True)
class RepairResult:
    org: str
    source: str
    day: date
    removed_count: int
    corrupt_rows: list[str]


class RepositoryStore(Protocol):
    # Message operations
    def add_message(
        self,
        org: str,
        source: str,
        record: MessageRecord,
    ) -> bool:
        """Append one message batch and return True when the write succeeds."""

    def find_messages(
        self,
        org: str,
        source: str,
        start: datetime | None = None,
        end: datetime | None = None,
        count: int | None = None,
    ) -> list[MessageRecord]:
        """Return matching messages ordered from oldest to newest."""

    def count_messages(
        self,
        org: str | None = None,
        source: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        """Return the number of matching messages within the requested scope."""

    def purge_stale_data(
        self,
        org: str,
        source: str,
        what: str | None = None,
    ) -> int:
        """Delete stale stored volume for one org/source and return the delete count."""

    def fix_message_file(
        self,
        org: str,
        source: str,
        day: date | None = None,
    ) -> RepairResult:
        """Remove malformed JSONL rows from one day file and report the repair result."""

    # Registration operations
    def add_registration(
        self,
        registration: RegistrationRecord,
    ) -> bool:
        """Add one registration and return True when it did not already exist."""

    def authenticate(self, org: str, auth: str) -> bool:
        """Return True when the supplied org-level bearer token is valid."""

    def get_registration(self, org: str) -> RegistrationRecord | None:
        """Return one org registration or None when it does not exist."""

    def delete_registration(
        self,
        org: str,
    ) -> int:
        """Delete one org registration and return the amount of related volume removed."""

    def get_registrations(
        self,
        org: str | None = None,
    ) -> list[RegistrationRecord]:
        """Return org registrations, optionally filtered by an org-name prefix."""
