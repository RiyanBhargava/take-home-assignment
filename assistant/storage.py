import csv
import logging
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import uuid4

from assistant.models import BookingReceipt, ChatResponse, Exchange, Profile, SessionState

logger = logging.getLogger(__name__)
CSV_FIELDS = (
    "lead_id",
    "user_id",
    "name",
    "budget_min",
    "budget_max",
    "currency",
    "make",
    "model",
    "trim",
    "body",
    "color",
    "features",
    "min_year",
    "max_year",
    "max_mileage_km",
    "needs",
    "listing_id",
    "booking_id",
    "viewing_start",
    "captured_at",
)


class SessionMissing(Exception):
    pass


class ConcurrentTurn(Exception):
    pass


class SlotTaken(Exception):
    pass


class StoredResponse(ChatResponse):
    vehicle_inquiry: bool | None = None


@dataclass
class Snapshot:
    user_id: str
    version: int
    profile_version: int
    profile: Profile
    state: SessionState
    history: list[Exchange]


class Storage:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.db = directory / "assistant.sqlite3"
        self.leads_csv = directory / "leads.csv"
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS profiles (
                    user_id TEXT PRIMARY KEY, data TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES profiles(user_id),
                    state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
                    is_returning INTEGER NOT NULL, welcome TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exchanges (
                    session_id TEXT NOT NULL REFERENCES sessions(id), seq INTEGER NOT NULL,
                    message TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY (session_id, seq)
                );
                CREATE TABLE IF NOT EXISTS bookings (
                    id TEXT PRIMARY KEY, listing_id TEXT NOT NULL, starts_at TEXT NOT NULL,
                    user_id TEXT NOT NULL REFERENCES profiles(user_id),
                    UNIQUE (listing_id, starts_at)
                );
                CREATE TABLE IF NOT EXISTS leads (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES profiles(user_id),
                    profile TEXT NOT NULL, listing_id TEXT,
                    booking_id TEXT REFERENCES bookings(id), viewing_start TEXT,
                    captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                );
            """)
        self.refresh_csv()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create_session(self, user_id: str, name: str | None) -> tuple[str, bool, Profile]:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT data FROM profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
            profile = Profile.model_validate_json(existing["data"]) if existing else Profile()
            if name:
                profile.name = name
            if not existing:
                conn.execute(
                    "INSERT INTO profiles (user_id, data) VALUES (?, ?)",
                    (
                        user_id,
                        profile.model_dump_json(),
                    ),
                )
            elif name:
                conn.execute(
                    "UPDATE profiles SET data = ?, version = version + 1 WHERE user_id = ?",
                    (
                        profile.model_dump_json(),
                        user_id,
                    ),
                )
            session_id = uuid4().hex
            conn.execute(
                "INSERT INTO sessions (id, user_id, state, is_returning, welcome) VALUES (?, ?, ?, ?, '')",
                (session_id, user_id, SessionState().model_dump_json(), bool(existing)),
            )
        return session_id, bool(existing), profile

    def set_welcome(self, session_id: str, welcome: str) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE sessions SET welcome = ? WHERE id = ?", (welcome, session_id))

    def load(self, session_id: str, history_limit: int = 8) -> tuple[Snapshot, bool, str]:
        with self.connection() as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                """
                SELECT s.user_id, s.version, s.state, s.is_returning, s.welcome,
                       p.data AS profile, p.version AS profile_version
                FROM sessions s JOIN profiles p USING (user_id) WHERE s.id = ?
            """,
                (session_id,),
            ).fetchone()
            if row is None:
                raise SessionMissing
            exchanges = conn.execute(
                """
                SELECT message, response FROM exchanges
                WHERE session_id = ? ORDER BY seq DESC LIMIT ?
            """,
                (session_id, history_limit),
            ).fetchall()
        return (
            Snapshot(
                row["user_id"],
                row["version"],
                row["profile_version"],
                Profile.model_validate_json(row["profile"]),
                SessionState.model_validate_json(row["state"]),
                [
                    Exchange(
                        message=item["message"],
                        response=ChatResponse.model_validate_json(item["response"]),
                    )
                    for item in reversed(exchanges)
                ],
            ),
            bool(row["is_returning"]),
            row["welcome"],
        )

    def taken_slots(self, listing_id: str, day: date) -> set[str]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT starts_at FROM bookings WHERE listing_id = ? AND starts_at LIKE ?",
                (listing_id, f"{day.isoformat()}T%"),
            ).fetchall()
        return {row["starts_at"] for row in rows}

    def latest_vehicle_inquiry(
        self,
        user_id: str,
        session_id: str,
        *,
        prior_sessions_only: bool,
    ) -> tuple[Exchange, bool] | None:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT e.message, e.response, e.session_id = ? AS current_session
                FROM exchanges e JOIN sessions s ON s.id = e.session_id
                WHERE s.user_id = ?
                  AND (NOT ? OR e.session_id <> ?)
                  AND json_array_length(e.response, '$.listings') > 0
                  AND COALESCE(json_extract(e.response, '$.vehicle_inquiry'), 1)
                ORDER BY current_session DESC, e.rowid DESC
            """,
                (session_id, user_id, prior_sessions_only, session_id),
            )
            for row in rows:
                response = StoredResponse.model_validate_json(row["response"])
                # Older replies have no marker. Recognize the original inventory reply formats.
                if response.vehicle_inquiry is None and not response.reply.startswith(
                    ("I found ", f"{response.listings[0].label}."),
                ):
                    continue
                return Exchange(message=row["message"], response=response), bool(
                    row["current_session"]
                )
        return None

    def save_turn(
        self,
        session_id: str,
        snapshot: Snapshot,
        message: str,
        response: ChatResponse,
        lead_listing_id: str | None = None,
        *,
        vehicle_inquiry: bool = False,
    ) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Recheck both versions after interpretation to prevent stale state from being saved.
            updated = conn.execute(
                "UPDATE profiles SET data = ?, version = version + 1 WHERE user_id = ? AND version = ?",
                (snapshot.profile.model_dump_json(), snapshot.user_id, snapshot.profile_version),
            )
            session = conn.execute(
                """
                UPDATE sessions SET state = ?, version = version + 1 WHERE id = ? AND version = ?
            """,
                (snapshot.state.model_dump_json(), session_id, snapshot.version),
            )
            if not (updated.rowcount and session.rowcount):
                raise ConcurrentTurn
            if response.booking:
                self._insert_booking(conn, snapshot.user_id, response.booking)
            if response.lead_saved:
                conn.execute(
                    """
                    INSERT INTO leads (id, user_id, profile, listing_id, booking_id, viewing_start)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (
                        uuid4().hex,
                        snapshot.user_id,
                        snapshot.profile.model_dump_json(),
                        lead_listing_id,
                        response.booking.id if response.booking else None,
                        response.booking.starts_at if response.booking else None,
                    ),
                )
            stored_response = StoredResponse(
                **response.model_dump(),
                vehicle_inquiry=vehicle_inquiry,
            )
            conn.execute(
                "INSERT INTO exchanges VALUES (?, ?, ?, ?)",
                (
                    session_id,
                    snapshot.version + 1,
                    message,
                    stored_response.model_dump_json(),
                ),
            )

    @staticmethod
    def _insert_booking(conn: sqlite3.Connection, user_id: str, booking: BookingReceipt) -> None:
        result = conn.execute(
            """
            INSERT INTO bookings (id, user_id, listing_id, starts_at) VALUES (?, ?, ?, ?)
            ON CONFLICT (listing_id, starts_at) DO NOTHING
        """,
            (booking.id, user_id, booking.listing_id, booking.starts_at),
        )
        if not result.rowcount:
            raise SlotTaken

    def refresh_csv(self) -> bool:
        """Publish an atomic projection of the lead ledger under a SQLite writer lock."""
        temp_path = None
        try:
            with self.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute("SELECT * FROM leads ORDER BY captured_at, id").fetchall()
                with tempfile.NamedTemporaryFile(
                    dir=self.leads_csv.parent,
                    mode="w",
                    encoding="utf-8",
                    newline="",
                    delete=False,
                ) as handle:
                    temp_path = Path(handle.name)
                    writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
                    writer.writeheader()
                    writer.writerows(_csv_row(row) for row in rows)
                os.replace(temp_path, self.leads_csv)
        except (OSError, sqlite3.Error):
            logger.warning("Lead CSV refresh failed. The SQLite lead ledger is intact.")
            return False
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return True


def _csv_row(row: sqlite3.Row) -> dict:
    profile = Profile.model_validate_json(row["profile"])
    filters = profile.preferences
    values = (
        row["id"],
        row["user_id"],
        profile.name,
        filters.min_price,
        filters.max_price,
        filters.currency
        or ("AED" if filters.min_price is not None or filters.max_price is not None else None),
        filters.make,
        filters.model,
        filters.trim,
        filters.body,
        filters.color,
        ", ".join(filters.features or []),
        filters.min_year,
        filters.max_year,
        filters.max_mileage_km,
        profile.needs,
        row["listing_id"],
        row["booking_id"],
        row["viewing_start"],
        row["captured_at"],
    )
    return {
        column: "'" + value
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
        else value
        for column, value in zip(CSV_FIELDS, values, strict=True)
    }
