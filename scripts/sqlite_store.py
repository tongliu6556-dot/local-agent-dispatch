#!/usr/bin/env python3
"""Small, stdlib-only transactional store for local-agent-dispatch.

This module deliberately has no dependency on the JSON controller.  It is a
storage seam that can be adopted by the controller incrementally: callers put
JSON domain payloads in the rows, while queue transitions, attempts, events,
and controller leases are committed by SQLite transactions.

The public API is intentionally conservative.  A controller must hold a
controller lease and pass its ``owner_id``/``fence_token`` to claim and
complete operations.  A stale process therefore cannot mutate a queue after a
new controller has taken over, even when the old process is still alive.
"""

from __future__ import annotations

import contextlib
import datetime as _datetime
import hashlib
import json
import os
import pathlib
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterator, Mapping, Sequence


SCHEMA_VERSION = 3
DEFAULT_LEASE_SCOPE = "controller"
DEFAULT_LEASE_TTL_SECONDS = 90


class StoreError(RuntimeError):
    """Base class for durable store errors."""


class MigrationError(StoreError):
    """Raised when a database schema cannot be migrated safely."""


class LeaseConflict(StoreError):
    """Raised when another live controller owns a lease scope."""


class FencingError(StoreError):
    """Raised when an owner/fence pair is absent, expired, or stale."""


class JobConflict(StoreError):
    """Raised when an idempotent job insert conflicts with another payload."""


class JobTransitionError(StoreError):
    """Raised when a job or attempt transition is not valid."""


class ReservationConflict(StoreError):
    """Raised when a job already has an incompatible active reservation."""


class ReservationAdmissionError(StoreError):
    """Raised when a resource admission report rejects a reservation."""


def utc_now() -> str:
    """Return a sortable, timezone-aware UTC timestamp."""

    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _parse_time(value: str | None) -> _datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = _datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
    return parsed.astimezone(_datetime.timezone.utc)


def _is_expired(value: str | None, *, now: str | None = None) -> bool:
    parsed = _parse_time(value)
    if parsed is None:
        return True
    current = _parse_time(now) or _datetime.datetime.now(_datetime.timezone.utc)
    return parsed <= current


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        # A malformed payload is evidence of corruption.  Keep the raw value
        # rather than silently dropping it; callers can fail closed explicitly.
        return value


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _migration_checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


# Keep migration text immutable.  Future migrations must be appended, never
# edited in place; the checksum in schema_migrations detects accidental edits.
_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 1,
    run_id TEXT,
    task_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    claimed_by TEXT,
    claim_fence INTEGER,
    lease_expires_at_utc TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    completed_at_utc TEXT,
    error_class TEXT,
    error_json TEXT,
    state_revision INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX jobs_runnable_idx
    ON jobs(status, priority DESC, created_at_utc, job_id);
CREATE INDEX jobs_lease_idx ON jobs(status, lease_expires_at_utc);

CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 1,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    owner_id TEXT,
    fence_token INTEGER,
    lease_expires_at_utc TEXT,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    artifact_manifest_json TEXT,
    validation_json TEXT,
    error_class TEXT,
    error_json TEXT,
    UNIQUE(job_id, attempt_no)
);
CREATE INDEX attempts_job_idx ON attempts(job_id, attempt_no);
CREATE INDEX attempts_lease_idx ON attempts(status, lease_expires_at_utc);

CREATE TABLE events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL DEFAULT 1,
    job_id TEXT REFERENCES jobs(job_id) ON DELETE CASCADE,
    attempt_id TEXT REFERENCES attempts(attempt_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    at_utc TEXT NOT NULL,
    owner_id TEXT,
    fence_token INTEGER,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX events_job_idx ON events(job_id, event_seq);

CREATE TABLE leases (
    scope TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 1,
    owner_id TEXT,
    fence_token INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'released',
    acquired_at_utc TEXT,
    heartbeat_at_utc TEXT,
    lease_expires_at_utc TEXT,
    released_at_utc TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX leases_status_idx ON leases(status, lease_expires_at_utc);
""",
    ),
    (
        2,
        """
ALTER TABLE jobs ADD COLUMN retry_at_utc TEXT;
CREATE INDEX jobs_retry_idx
    ON jobs(status, retry_at_utc, priority DESC, created_at_utc, job_id);
""",
    ),
    (
        3,
        """
CREATE TABLE reservations (
    reservation_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 1,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    scope TEXT NOT NULL DEFAULT 'controller',
    status TEXT NOT NULL DEFAULT 'active',
    owner_id TEXT NOT NULL,
    fence_token INTEGER NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    lease_expires_at_utc TEXT NOT NULL,
    resource_json TEXT NOT NULL DEFAULT '{}',
    admission_json TEXT NOT NULL DEFAULT '{}',
    release_reason TEXT
);
CREATE INDEX reservations_job_idx ON reservations(job_id, status, scope);
CREATE INDEX reservations_lease_idx ON reservations(status, lease_expires_at_utc);
CREATE UNIQUE INDEX reservations_active_job_scope_idx
    ON reservations(job_id, scope) WHERE status = 'active';
""",
    ),
)


def _split_migration(sql: str) -> list[str]:
    """Split our controlled DDL into statements without a SQL dependency.

    Migration strings are source-controlled DDL with no quoted semicolons.  A
    tiny splitter keeps the module stdlib-only and lets us wrap each migration
    in one explicit transaction (``executescript`` would implicitly commit).
    """

    return [part.strip() for part in sql.split(";") if part.strip()]


class SQLiteStore:
    """A process-safe SQLite WAL store for one local dispatch database.

    The connection is safe for calls from one thread at a time.  Separate
    ``SQLiteStore`` instances (including separate processes) coordinate via
    SQLite's ``BEGIN IMMEDIATE`` transactions.  Long-running provider work is
    intentionally outside the transaction; only claim/complete state changes
    are short, atomic transactions.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        timeout_seconds: float = 30.0,
        busy_timeout_ms: int | None = None,
    ) -> None:
        self.path = str(path)
        if self.path not in {":memory:", ""} and not self.path.startswith("file:"):
            pathlib.Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            timeout=float(timeout_seconds),
            isolation_level=None,
            check_same_thread=False,
            uri=self.path.startswith("file:"),
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=%d" % int(busy_timeout_ms or max(1000, timeout_seconds * 1000)))
        # WAL is persistent for file databases.  In-memory SQLite reports
        # ``memory``; that is the only expected non-WAL exception.
        journal = str(self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        if self.path not in {":memory:", ""} and journal != "wal":
            self.close()
            raise StoreError(f"SQLite WAL could not be enabled for {self.path!r}: {journal}")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._migrate()

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the connection for read-only diagnostics and migrations."""

        return self._conn

    @property
    def schema_version(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
        return int(row[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at_utc TEXT NOT NULL,
                    checksum TEXT NOT NULL
                )"""
            )
            rows = {
                int(row[0]): str(row[1])
                for row in self._conn.execute(
                    "SELECT version, checksum FROM schema_migrations"
                ).fetchall()
            }
            known = {version: _migration_checksum(sql) for version, sql in _MIGRATIONS}
            if any(version > SCHEMA_VERSION for version in rows):
                self.close()
                raise MigrationError("database schema is newer than this local-agent-dispatch build")
            for version, sql in _MIGRATIONS:
                if version in rows:
                    if rows[version] != known[version]:
                        raise MigrationError(f"migration {version} checksum mismatch")
                    continue
                try:
                    self._conn.execute("BEGIN EXCLUSIVE")
                    for statement in _split_migration(sql):
                        self._conn.execute(statement)
                    self._conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at_utc, checksum) VALUES (?, ?, ?)",
                        (version, utc_now(), known[version]),
                    )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            cursor = self._conn.cursor()
            try:
                yield cursor
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    @staticmethod
    def _lease_expiry(ttl_seconds: int | float, *, now: str | None = None) -> str:
        base = _parse_time(now) or _datetime.datetime.now(_datetime.timezone.utc)
        return (base + _datetime.timedelta(seconds=max(1.0, float(ttl_seconds)))).isoformat()

    @staticmethod
    def _row_dict(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return dict(row)

    @classmethod
    def _decode_job_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        result = cls._row_dict(row)
        if result is None:
            return None
        result["payload"] = _decode(result.pop("payload_json", None), {})
        result["error"] = _decode(result.pop("error_json", None), None)
        return result

    @classmethod
    def _decode_attempt_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        result = cls._row_dict(row)
        if result is None:
            return None
        for column, key, default in (
            ("payload_json", "payload", {}),
            ("result_json", "result", None),
            ("artifact_manifest_json", "artifact_manifest", None),
            ("validation_json", "validation", None),
            ("error_json", "error", None),
        ):
            result[key] = _decode(result.pop(column, None), default)
        return result

    @classmethod
    def _decode_event_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        result = cls._row_dict(row)
        if result is None:
            return None
        result["payload"] = _decode(result.pop("payload_json", None), {})
        return result

    @staticmethod
    def _decode_lease_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = _decode(result.pop("metadata_json", None), {})
        # Keep the common spelling used by controller JSON packets.
        result["expires_at_utc"] = result.get("lease_expires_at_utc")
        result["lease_token"] = result.get("fence_token")
        return result

    @staticmethod
    def _decode_reservation_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["resource_request"] = _decode(result.pop("resource_json", None), {})
        result["admission"] = _decode(result.pop("admission_json", None), {})
        result["expires_at_utc"] = result.get("lease_expires_at_utc")
        result["reservation_token"] = result.get("reservation_id")
        return result

    def _assert_lease_tx(
        self,
        cursor: sqlite3.Cursor,
        scope: str,
        owner_id: str,
        fence_token: int,
        *,
        now: str | None = None,
    ) -> sqlite3.Row:
        row = cursor.execute("SELECT * FROM leases WHERE scope = ?", (scope,)).fetchone()
        current = now or utc_now()
        if (
            row is None
            or str(row["owner_id"] or "") != str(owner_id)
            or int(row["fence_token"] or 0) != int(fence_token)
            or str(row["status"]) != "active"
            or _is_expired(row["lease_expires_at_utc"], now=current)
        ):
            raise FencingError(f"stale or missing controller lease for scope {scope!r}")
        return row

    # ------------------------------------------------------------------
    # Controller leases and fencing
    # ------------------------------------------------------------------
    def acquire_controller_lease(
        self,
        owner_id: str,
        *,
        scope: str = DEFAULT_LEASE_SCOPE,
        ttl_seconds: int | float = DEFAULT_LEASE_TTL_SECONDS,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not str(owner_id).strip():
            raise ValueError("owner_id is required")
        scope = str(scope)
        current = utc_now()
        expiry = self._lease_expiry(ttl_seconds, now=current)
        with self._transaction() as cursor:
            row = cursor.execute("SELECT * FROM leases WHERE scope = ?", (scope,)).fetchone()
            if row is not None and str(row["status"]) == "active" and not _is_expired(
                row["lease_expires_at_utc"], now=current
            ):
                if str(row["owner_id"] or "") != str(owner_id):
                    raise LeaseConflict(f"lease scope {scope!r} is held by another controller")
                # Re-entrant acquisition by the same durable owner renews the
                # existing fencing epoch; a second process should use a
                # distinct owner id and cannot accidentally share it.
                cursor.execute(
                    """UPDATE leases
                       SET heartbeat_at_utc = ?, lease_expires_at_utc = ?, metadata_json = ?
                       WHERE scope = ? AND owner_id = ? AND status = 'active'""",
                    (current, expiry, _json(dict(metadata or _decode(row["metadata_json"], {}))), scope, owner_id),
                )
                updated = cursor.execute("SELECT * FROM leases WHERE scope = ?", (scope,)).fetchone()
                assert updated is not None
                return self._decode_lease_row(updated) or {}

            previous_fence = int(row["fence_token"] or 0) if row is not None else 0
            next_fence = previous_fence + 1
            if row is None:
                cursor.execute(
                    """INSERT INTO leases
                       (scope, owner_id, fence_token, status, acquired_at_utc,
                        heartbeat_at_utc, lease_expires_at_utc, released_at_utc, metadata_json)
                       VALUES (?, ?, ?, 'active', ?, ?, ?, NULL, ?)""",
                    (scope, owner_id, next_fence, current, current, expiry, _json(dict(metadata or {}))),
                )
            else:
                cursor.execute(
                    """UPDATE leases
                       SET owner_id = ?, fence_token = ?, status = 'active',
                           acquired_at_utc = ?, heartbeat_at_utc = ?,
                           lease_expires_at_utc = ?, released_at_utc = NULL, metadata_json = ?
                       WHERE scope = ?""",
                    (owner_id, next_fence, current, current, expiry, _json(dict(metadata or {})), scope),
                )
            updated = cursor.execute("SELECT * FROM leases WHERE scope = ?", (scope,)).fetchone()
            assert updated is not None
            return self._decode_lease_row(updated) or {}

    # Short alias useful to integrations that use ``lease`` terminology.
    acquire_lease = acquire_controller_lease

    def heartbeat_controller_lease(
        self,
        owner_id: str,
        fence_token: int,
        *,
        scope: str = DEFAULT_LEASE_SCOPE,
        ttl_seconds: int | float = DEFAULT_LEASE_TTL_SECONDS,
    ) -> dict[str, Any]:
        current = utc_now()
        expiry = self._lease_expiry(ttl_seconds, now=current)
        with self._transaction() as cursor:
            self._assert_lease_tx(cursor, scope, owner_id, fence_token, now=current)
            cursor.execute(
                """UPDATE leases SET heartbeat_at_utc = ?, lease_expires_at_utc = ?
                   WHERE scope = ? AND owner_id = ? AND fence_token = ? AND status = 'active'""",
                (current, expiry, scope, owner_id, int(fence_token)),
            )
            row = cursor.execute("SELECT * FROM leases WHERE scope = ?", (scope,)).fetchone()
            assert row is not None
            return self._decode_lease_row(row) or {}

    heartbeat_lease = heartbeat_controller_lease

    def heartbeat_job_lease(
        self,
        job_id: str,
        attempt_id: str,
        owner_id: str,
        fence_token: int,
        *,
        ttl_seconds: int | float = DEFAULT_LEASE_TTL_SECONDS,
        scope: str = DEFAULT_LEASE_SCOPE,
    ) -> dict[str, Any]:
        """Renew one claimed job and its running attempt atomically.

        Controller fencing alone is insufficient while provider work runs
        outside a transaction: a long attempt could outlive the row-level
        lease and be reclaimed by a second controller.  Both rows are
        renewed in one transaction and an expired/stale owner is rejected,
        so an old worker cannot revive a job after takeover.
        """
        current = utc_now()
        expiry = self._lease_expiry(ttl_seconds, now=current)
        with self._transaction() as cursor:
            self._assert_lease_tx(cursor, scope, owner_id, int(fence_token), now=current)
            job = cursor.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            attempt = cursor.execute(
                "SELECT * FROM attempts WHERE attempt_id = ? AND job_id = ?",
                (attempt_id, job_id),
            ).fetchone()
            if (
                job is None
                or attempt is None
                or str(job["status"]) != "running"
                or str(job["claimed_by"] or "") != str(owner_id)
                or int(job["claim_fence"] or 0) != int(fence_token)
                or str(attempt["status"]) != "running"
                or str(attempt["owner_id"] or "") != str(owner_id)
                or int(attempt["fence_token"] or 0) != int(fence_token)
                or _is_expired(job["lease_expires_at_utc"], now=current)
                or _is_expired(attempt["lease_expires_at_utc"], now=current)
            ):
                raise FencingError(f"stale or missing job lease for {job_id!r}")
            cursor.execute(
                """UPDATE jobs SET lease_expires_at_utc = ?, updated_at_utc = ?
                   WHERE job_id = ? AND status = 'running' AND claimed_by = ? AND claim_fence = ?""",
                (expiry, current, job_id, owner_id, int(fence_token)),
            )
            cursor.execute(
                """UPDATE attempts SET lease_expires_at_utc = ?
                   WHERE attempt_id = ? AND status = 'running' AND owner_id = ? AND fence_token = ?""",
                (expiry, attempt_id, owner_id, int(fence_token)),
            )
            cursor.execute(
                """UPDATE reservations SET updated_at_utc = ?, lease_expires_at_utc = ?
                   WHERE job_id = ? AND scope = ? AND status = 'active'
                     AND owner_id = ? AND fence_token = ?""",
                (current, expiry, job_id, scope, owner_id, int(fence_token)),
            )
            return {
                "job": self._decode_job_row(
                    cursor.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
                ),
                "attempt": self._decode_attempt_row(
                    cursor.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
                ),
            }

    @contextlib.contextmanager
    def job_lease_heartbeat(
        self,
        job_id: str,
        attempt_id: str,
        owner_id: str,
        fence_token: int,
        *,
        ttl_seconds: int | float = DEFAULT_LEASE_TTL_SECONDS,
        heartbeat_interval_seconds: float | None = None,
        scope: str = DEFAULT_LEASE_SCOPE,
    ) -> Iterator[dict[str, Any]]:
        """Keep a running job lease alive while provider work is executing."""
        interval = heartbeat_interval_seconds
        if interval is None:
            interval = max(1.0, min(float(ttl_seconds) / 3.0, 30.0))
        if float(interval) <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        stop = threading.Event()
        lost = threading.Event()

        def beat() -> None:
            while not stop.wait(float(interval)):
                try:
                    self.heartbeat_job_lease(
                        job_id,
                        attempt_id,
                        owner_id,
                        int(fence_token),
                        ttl_seconds=ttl_seconds,
                        scope=scope,
                    )
                except (FencingError, sqlite3.Error):
                    # The foreground completion call will enforce the same
                    # fence.  Stop renewing rather than reviving a lease that
                    # a newer owner has legitimately taken over.
                    lost.set()
                    return

        thread = threading.Thread(target=beat, name="lad-sqlite-job-heartbeat", daemon=True)
        thread.start()
        try:
            yield {"lost": lost}
        finally:
            stop.set()
            thread.join(timeout=max(1.0, float(interval) + 0.5))

    def release_controller_lease(
        self,
        owner_id: str,
        fence_token: int,
        *,
        scope: str = DEFAULT_LEASE_SCOPE,
    ) -> dict[str, Any]:
        current = utc_now()
        with self._transaction() as cursor:
            row = cursor.execute("SELECT * FROM leases WHERE scope = ?", (scope,)).fetchone()
            if (
                row is None
                or str(row["owner_id"] or "") != str(owner_id)
                or int(row["fence_token"] or 0) != int(fence_token)
            ):
                raise FencingError(f"cannot release stale controller lease for scope {scope!r}")
            cursor.execute(
                """UPDATE leases SET status = 'released', released_at_utc = ?,
                   lease_expires_at_utc = ? WHERE scope = ? AND owner_id = ? AND fence_token = ?""",
                (current, current, scope, owner_id, int(fence_token)),
            )
            updated = cursor.execute("SELECT * FROM leases WHERE scope = ?", (scope,)).fetchone()
            assert updated is not None
            return self._decode_lease_row(updated) or {}

    release_lease = release_controller_lease

    @contextlib.contextmanager
    def controller_lease(
        self,
        owner_id: str,
        *,
        scope: str = DEFAULT_LEASE_SCOPE,
        ttl_seconds: int | float = DEFAULT_LEASE_TTL_SECONDS,
        heartbeat_interval_seconds: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        lease = self.acquire_controller_lease(
            owner_id, scope=scope, ttl_seconds=ttl_seconds, metadata=metadata
        )
        stop = threading.Event()
        interval = heartbeat_interval_seconds
        if interval is None:
            interval = max(1.0, min(float(ttl_seconds) / 3.0, 30.0))

        def beat() -> None:
            while not stop.wait(float(interval)):
                try:
                    self.heartbeat_controller_lease(
                        owner_id,
                        int(lease["fence_token"]),
                        scope=scope,
                        ttl_seconds=ttl_seconds,
                    )
                except (FencingError, sqlite3.Error):
                    # The foreground operation observes the lease token on its
                    # next mutation.  Never let a daemon heartbeat hide the
                    # original provider exception.
                    return

        thread = threading.Thread(target=beat, name="lad-sqlite-lease-heartbeat", daemon=True)
        thread.start()
        try:
            yield lease
        finally:
            stop.set()
            thread.join(timeout=max(1.0, float(interval) + 0.5))
            try:
                self.release_controller_lease(
                    owner_id, int(lease["fence_token"]), scope=scope
                )
            except FencingError:
                # A newer owner may have fenced this context after a timeout;
                # preserving the newer lease is the safe outcome.
                pass

    # ------------------------------------------------------------------
    # Resource reservations
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_resource_request(request: Mapping[str, Any] | None) -> dict[str, Any]:
        """Keep reservation inputs numeric, bounded, and JSON serializable."""
        source = dict(request or {})
        result: dict[str, Any] = {}
        numeric = (
            "cpu_cores", "ram_gib", "gpu_count", "vram_gib_per_gpu",
            "new_disk_gib", "compute_minutes", "network_gib",
        )
        for key, value in source.items():
            if key not in numeric:
                # Preserve small placement identifiers, but never persist
                # arbitrary command/prompt material in the reservation row.
                if key in {"host_id", "pool_id", "mount_id", "write_scope"}:
                    result[key] = str(value)
                continue
            if value is None:
                continue
            try:
                parsed = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"resource request {key!r} must be numeric") from exc
            if parsed < 0:
                raise ValueError(f"resource request {key!r} must be non-negative")
            result[key] = int(parsed) if parsed.is_integer() else parsed
        return result

    @staticmethod
    def _job_requires_reservation_payload(payload: Mapping[str, Any]) -> bool:
        # Legacy packets without an estimate remain runnable during migration.
        # Planner/bridge packets carry resource_request and therefore enter the
        # strict reservation path automatically.
        return bool(payload.get("resource_reservation_required") or payload.get("resource_request"))

    def _release_reservation_tx(
        self,
        cursor: sqlite3.Cursor,
        job_id: str,
        *,
        owner_id: str | None = None,
        fence_token: int | None = None,
        reason: str,
        current: str,
    ) -> int:
        where = "job_id = ? AND status = 'active'"
        params: list[Any] = [job_id]
        if owner_id is not None:
            where += " AND owner_id = ?"
            params.append(owner_id)
        if fence_token is not None:
            where += " AND fence_token = ?"
            params.append(int(fence_token))
        cursor.execute(
            f"""UPDATE reservations
                SET status = 'released', updated_at_utc = ?,
                    lease_expires_at_utc = ?, release_reason = ?
                WHERE {where}""",
            [current, current, reason, *params],
        )
        return int(cursor.rowcount or 0)

    def reserve_resources(
        self,
        job_id: str,
        owner_id: str,
        fence_token: int,
        resource_request: Mapping[str, Any] | None,
        *,
        admission: Mapping[str, Any] | None = None,
        ttl_seconds: int | float = DEFAULT_LEASE_TTL_SECONDS,
        scope: str = DEFAULT_LEASE_SCOPE,
        reservation_id: str | None = None,
    ) -> dict[str, Any]:
        """Create one fenced reservation before a job is claimed.

        Capacity arithmetic is deliberately supplied by the Resource Governor
        or a remote host probe.  The store owns the atomic identity/fence and
        lifecycle; it never guesses host capacity from an absent observation.
        """
        request = self._normalize_resource_request(resource_request)
        admission_obj = dict(admission or {})
        if admission_obj.get("allowed") is False:
            raise ReservationAdmissionError(
                str(admission_obj.get("reason") or "resource admission rejected")
            )
        current = utc_now()
        expiry = self._lease_expiry(ttl_seconds, now=current)
        rid = str(reservation_id or _new_id("reservation"))
        with self._transaction() as cursor:
            self._assert_lease_tx(cursor, scope, owner_id, int(fence_token), now=current)
            job = cursor.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if job is None:
                raise JobTransitionError(f"job {job_id!r} does not exist")
            cursor.execute(
                """UPDATE reservations SET status = 'expired', updated_at_utc = ?,
                   lease_expires_at_utc = ?, release_reason = 'lease_expired'
                   WHERE status = 'active' AND lease_expires_at_utc <= ?""",
                (current, current, current),
            )
            active = cursor.execute(
                "SELECT * FROM reservations WHERE job_id = ? AND scope = ? AND status = 'active'",
                (job_id, scope),
            ).fetchone()
            if active is not None:
                existing = self._decode_reservation_row(active) or {}
                if (
                    str(active["owner_id"] or "") == str(owner_id)
                    and int(active["fence_token"] or 0) == int(fence_token)
                    and existing.get("resource_request") == request
                ):
                    return existing
                raise ReservationConflict(f"job {job_id!r} already has an active reservation")
            cursor.execute(
                """INSERT INTO reservations
                   (reservation_id, schema_version, job_id, scope, status, owner_id,
                    fence_token, created_at_utc, updated_at_utc, lease_expires_at_utc,
                    resource_json, admission_json)
                   VALUES (?, 1, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rid, job_id, scope, owner_id, int(fence_token), current, current,
                    expiry, _json(request), _json(admission_obj),
                ),
            )
            self._append_event_tx(
                cursor,
                "reservation_created",
                job_id=job_id,
                owner_id=owner_id,
                fence_token=fence_token,
                payload={"reservation_id": rid, "resource_request": request, "admission": admission_obj},
            )
            return self._decode_reservation_row(
                cursor.execute("SELECT * FROM reservations WHERE reservation_id = ?", (rid,)).fetchone()
            ) or {}

    create_reservation = reserve_resources

    def heartbeat_reservation(
        self,
        job_id: str,
        owner_id: str,
        fence_token: int,
        *,
        ttl_seconds: int | float = DEFAULT_LEASE_TTL_SECONDS,
        scope: str = DEFAULT_LEASE_SCOPE,
    ) -> dict[str, Any]:
        current = utc_now()
        expiry = self._lease_expiry(ttl_seconds, now=current)
        with self._transaction() as cursor:
            self._assert_lease_tx(cursor, scope, owner_id, int(fence_token), now=current)
            row = cursor.execute(
                """SELECT * FROM reservations
                   WHERE job_id = ? AND scope = ? AND status = 'active'""",
                (job_id, scope),
            ).fetchone()
            if (
                row is None
                or str(row["owner_id"] or "") != str(owner_id)
                or int(row["fence_token"] or 0) != int(fence_token)
                or _is_expired(row["lease_expires_at_utc"], now=current)
            ):
                raise FencingError(f"stale or missing reservation for job {job_id!r}")
            cursor.execute(
                """UPDATE reservations SET updated_at_utc = ?, lease_expires_at_utc = ?
                   WHERE reservation_id = ? AND status = 'active'""",
                (current, expiry, row["reservation_id"]),
            )
            return self._decode_reservation_row(
                cursor.execute("SELECT * FROM reservations WHERE reservation_id = ?", (row["reservation_id"],)).fetchone()
            ) or {}

    def release_reservation(
        self,
        job_id: str,
        owner_id: str,
        fence_token: int,
        *,
        reason: str = "released",
        scope: str = DEFAULT_LEASE_SCOPE,
    ) -> int:
        current = utc_now()
        with self._transaction() as cursor:
            self._assert_lease_tx(cursor, scope, owner_id, int(fence_token), now=current)
            count = self._release_reservation_tx(
                cursor, job_id, owner_id=owner_id, fence_token=int(fence_token),
                reason=str(reason), current=current,
            )
            if count:
                self._append_event_tx(
                    cursor, "reservation_released", job_id=job_id,
                    owner_id=owner_id, fence_token=fence_token,
                    payload={"reason": str(reason)},
                )
            return count

    def list_reservations(
        self,
        job_id: str | None = None,
        *,
        statuses: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(str(job_id))
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(str(item) for item in statuses)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM reservations{where} ORDER BY created_at_utc, reservation_id",
            tuple(params),
        ).fetchall()
        return [self._decode_reservation_row(row) or {} for row in rows]

    def active_resource_totals(self, *, scope: str = DEFAULT_LEASE_SCOPE) -> dict[str, float]:
        """Return conservative sums for active, non-expired reservations."""
        now = utc_now()
        totals: dict[str, float] = {}
        for row in self.list_reservations(statuses=("active",)):
            if str(row.get("scope")) != str(scope) or _is_expired(row.get("lease_expires_at_utc"), now=now):
                continue
            for key, value in dict(row.get("resource_request") or {}).items():
                if isinstance(value, (int, float)):
                    totals[key] = totals.get(key, 0.0) + float(value)
        return totals

    # ------------------------------------------------------------------
    # Jobs and atomic claim/complete
    # ------------------------------------------------------------------
    def create_job(
        self,
        job_id: str,
        payload: Mapping[str, Any] | None = None,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        priority: int = 0,
        status: str = "queued",
        owner_id: str | None = None,
        fence_token: int | None = None,
        scope: str = DEFAULT_LEASE_SCOPE,
    ) -> dict[str, Any]:
        """Insert one job idempotently.

        Existing identical payloads return the existing row.  A different
        payload for the same id is a hard conflict, preventing accidental
        queue replacement after a controller restart.
        """

        if not str(job_id).strip():
            raise ValueError("job_id is required")
        if owner_id is not None or fence_token is not None:
            if owner_id is None or fence_token is None:
                raise FencingError("owner_id and fence_token must be supplied together")
        payload_obj = dict(payload or {})
        current = utc_now()
        with self._transaction() as cursor:
            if owner_id is not None:
                self._assert_lease_tx(cursor, scope, owner_id, int(fence_token), now=current)
            existing = cursor.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if existing is not None:
                if _decode(existing["payload_json"], {}) != payload_obj:
                    raise JobConflict(f"job {job_id!r} already exists with a different payload")
                return self._decode_job_row(existing) or {}
            cursor.execute(
                """INSERT INTO jobs
                   (job_id, schema_version, run_id, task_id, status, priority, payload_json,
                    created_at_utc, updated_at_utc, state_revision)
                   VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (job_id, run_id, task_id, status, int(priority), _json(payload_obj), current, current),
            )
            self._append_event_tx(
                cursor,
                "job_enqueued",
                job_id=job_id,
                owner_id=owner_id,
                fence_token=fence_token,
                payload={"status": status},
            )
            row = cursor.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._decode_job_row(row) or {}

    def requeue_job(
        self,
        job_id: str,
        owner_id: str,
        fence_token: int,
        *,
        payload: Mapping[str, Any] | None = None,
        priority: int | None = None,
        reason: str = "replan",
        scope: str = DEFAULT_LEASE_SCOPE,
    ) -> dict[str, Any]:
        """Fencedly put a terminal job back on the queue.

        A monitor/replan cycle deliberately produces a new packet for the
        same logical job.  Treating that packet as an ordinary insert makes a
        failed row either idempotently stay failed or raise ``JobConflict``.
        This explicit transition keeps the old attempts/events for audit,
        replaces only the approved payload, and preserves the attempt counter
        so the next claim receives a new monotonic attempt number.  Completed or running jobs are never requeued by
        this method.
        """

        if not str(job_id).strip():
            raise ValueError("job_id is required")
        if not str(reason).strip():
            raise ValueError("requeue reason is required")
        payload_obj = dict(payload) if payload is not None else None
        current = utc_now()
        with self._transaction() as cursor:
            self._assert_lease_tx(cursor, scope, owner_id, int(fence_token), now=current)
            existing = cursor.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if existing is None:
                raise JobTransitionError(f"job {job_id!r} does not exist")
            old_status = str(existing["status"])
            if old_status not in {"failed", "blocked", "pending", "retry"}:
                raise JobTransitionError(
                    f"job {job_id!r} with status {old_status!r} is not requeueable"
                )
            next_payload = (
                payload_obj if payload_obj is not None else _decode(existing["payload_json"], {})
            )
            if isinstance(next_payload, dict):
                # Attempt numbers remain globally monotonic for audit and the
                # UNIQUE(job_id, attempt_no) constraint.  The controller uses
                # this private marker to start the newly approved packet at
                # its first attempt instead of accidentally skipping to the
                # second fallback from the previous packet.
                next_payload["_lad_replan_base_attempt_count"] = int(
                    existing["attempt_count"] or 0
                )
            next_priority = int(existing["priority"] if priority is None else priority)
            cursor.execute(
                """UPDATE jobs SET status = 'queued', priority = ?, payload_json = ?,
                   updated_at_utc = ?, completed_at_utc = NULL, claimed_by = NULL,
                   claim_fence = NULL, lease_expires_at_utc = NULL, retry_at_utc = NULL,
                   error_class = NULL, error_json = NULL, state_revision = state_revision + 1
                   WHERE job_id = ? AND status IN ('failed', 'blocked', 'pending', 'retry')""",
                (next_priority, _json(next_payload), current, job_id),
            )
            if cursor.rowcount != 1:
                raise JobTransitionError(f"job {job_id!r} changed before requeue")
            self._release_reservation_tx(
                cursor,
                job_id,
                reason="job_requeued",
                current=current,
            )
            self._append_event_tx(
                cursor,
                "job_requeued",
                job_id=job_id,
                owner_id=owner_id,
                fence_token=fence_token,
                payload={"from_status": old_status, "reason": str(reason)},
            )
            row = cursor.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._decode_job_row(row) or {}

    enqueue_job = create_job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._decode_job_row(row)

    def list_jobs(self, *, statuses: Sequence[str] | None = None) -> list[dict[str, Any]]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = self._conn.execute(
                f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY priority DESC, created_at_utc, job_id",
                tuple(str(item) for item in statuses),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY priority DESC, created_at_utc, job_id"
            ).fetchall()
        return [self._decode_job_row(row) or {} for row in rows]

    def expired_running_jobs(self, *, now: str | None = None) -> list[dict[str, Any]]:
        """Return running jobs whose durable lease has expired.

        This is a read-only handoff used by the controller to collect explicit
        process-liveness evidence before recovery.  The store deliberately
        does not inspect PIDs or execute host commands itself.
        """

        current = now or utc_now()
        rows = self._conn.execute(
            """SELECT * FROM jobs WHERE status = 'running'
               AND (lease_expires_at_utc IS NULL OR lease_expires_at_utc <= ?)
               ORDER BY priority DESC, created_at_utc, job_id""",
            (current,),
        ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            job = self._decode_job_row(row) or {}
            attempts = self.list_attempts(str(row["job_id"]))
            running = [item for item in attempts if str(item.get("status")) == "running"]
            records.append({"job": job, "attempt": running[-1] if running else None})
        return records

    def _claim_row_tx(
        self,
        cursor: sqlite3.Cursor,
        row: sqlite3.Row,
        *,
        owner_id: str,
        fence_token: int,
        lease_ttl_seconds: int | float,
        scope: str,
        current: str,
    ) -> dict[str, Any]:
        job_id = str(row["job_id"])
        old_status = str(row["status"])
        attempt_no = int(row["attempt_count"] or 0) + 1
        attempt_id = _new_id("attempt")
        expiry = self._lease_expiry(lease_ttl_seconds, now=current)
        if old_status == "running":
            cursor.execute(
                """UPDATE attempts SET status = 'abandoned', finished_at_utc = ?,
                   error_class = COALESCE(error_class, 'controller_restarted')
                   WHERE job_id = ? AND status = 'running'""",
                (current, job_id),
            )
        updated = cursor.execute(
            """UPDATE jobs SET status = 'running', claimed_by = ?, claim_fence = ?,
               lease_expires_at_utc = ?, attempt_count = ?, updated_at_utc = ?,
               retry_at_utc = NULL, state_revision = state_revision + 1,
               error_class = NULL, error_json = NULL
               WHERE job_id = ? AND (status = 'queued'
                  OR status = 'retry' AND (retry_at_utc IS NULL OR retry_at_utc <= ?))""",
            (owner_id, int(fence_token), expiry, attempt_no, current, job_id, current),
        )
        if updated.rowcount != 1:
            raise JobTransitionError(f"job {job_id!r} is no longer claimable")
        cursor.execute(
            """INSERT INTO attempts
               (attempt_id, schema_version, job_id, attempt_no, status, owner_id, fence_token,
                lease_expires_at_utc, started_at_utc, payload_json)
               VALUES (?, 1, ?, ?, 'running', ?, ?, ?, ?, ?)""",
            (attempt_id, job_id, attempt_no, owner_id, int(fence_token), expiry, current, row["payload_json"]),
        )
        self._append_event_tx(
            cursor,
            "job_claimed",
            job_id=job_id,
            attempt_id=attempt_id,
            owner_id=owner_id,
            fence_token=fence_token,
            payload={"attempt_no": attempt_no, "reclaimed": old_status == "running", "scope": scope},
        )
        claimed = cursor.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return {
            "job": self._decode_job_row(claimed),
            "attempt": self._decode_attempt_row(
                cursor.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            ),
        }

    def _reservation_is_claimable_tx(
        self,
        cursor: sqlite3.Cursor,
        row: sqlite3.Row,
        *,
        owner_id: str,
        fence_token: int,
        scope: str,
        current: str,
        require_reservation: bool,
    ) -> bool:
        payload = _decode(row["payload_json"], {})
        if not isinstance(payload, Mapping) or not self._job_requires_reservation_payload(payload):
            return True
        if not require_reservation:
            return True
        reservation = cursor.execute(
            """SELECT 1 FROM reservations
               WHERE job_id = ? AND scope = ? AND status = 'active'
                 AND owner_id = ? AND fence_token = ?
                 AND lease_expires_at_utc > ?""",
            (row["job_id"], scope, owner_id, int(fence_token), current),
        ).fetchone()
        return reservation is not None

    def claim_job(
        self,
        job_id: str,
        owner_id: str,
        fence_token: int,
        *,
        lease_ttl_seconds: int | float = DEFAULT_LEASE_TTL_SECONDS,
        scope: str = DEFAULT_LEASE_SCOPE,
        require_reservation: bool = False,
    ) -> dict[str, Any] | None:
        current = utc_now()
        with self._transaction() as cursor:
            self._assert_lease_tx(cursor, scope, owner_id, int(fence_token), now=current)
            row = cursor.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            runnable = str(row["status"]) == "queued" or (
                str(row["status"]) == "retry"
                and (row["retry_at_utc"] is None or _is_expired(
                    row["retry_at_utc"], now=current
                ))
            )
            if not runnable:
                return None
            if not self._reservation_is_claimable_tx(
                cursor, row, owner_id=owner_id, fence_token=int(fence_token),
                scope=scope, current=current, require_reservation=require_reservation,
            ):
                return None
            return self._claim_row_tx(
                cursor,
                row,
                owner_id=owner_id,
                fence_token=int(fence_token),
                lease_ttl_seconds=lease_ttl_seconds,
                scope=scope,
                current=current,
            )

    def claim_next_job(
        self,
        owner_id: str,
        fence_token: int,
        *,
        lease_ttl_seconds: int | float = DEFAULT_LEASE_TTL_SECONDS,
        scope: str = DEFAULT_LEASE_SCOPE,
        require_reservation: bool = False,
    ) -> dict[str, Any] | None:
        claimed = self.claim_jobs(
            owner_id,
            fence_token,
            max_jobs=1,
            lease_ttl_seconds=lease_ttl_seconds,
            scope=scope,
            require_reservation=require_reservation,
        )
        return claimed[0] if claimed else None

    def claim_jobs(
        self,
        owner_id: str,
        fence_token: int,
        *,
        max_jobs: int = 1,
        lease_ttl_seconds: int | float = DEFAULT_LEASE_TTL_SECONDS,
        scope: str = DEFAULT_LEASE_SCOPE,
        require_reservation: bool = False,
    ) -> list[dict[str, Any]]:
        """Atomically claim up to ``max_jobs`` independent lanes.

        The controller may hand each returned claim to a worker lane while
        keeping one controller lease/fence.  Selection and all claim rows are
        committed in one short ``BEGIN IMMEDIATE`` transaction, so a second
        process (or a restarted controller) cannot receive the same job.  The
        legacy ``claim_next_job`` API delegates to this method with
        ``max_jobs=1``.
        """

        if isinstance(max_jobs, bool) or int(max_jobs) < 1:
            raise ValueError("max_jobs must be a positive integer")
        limit = min(int(max_jobs), 1024)
        current = utc_now()
        with self._transaction() as cursor:
            self._assert_lease_tx(cursor, scope, owner_id, int(fence_token), now=current)
            # Read a bounded candidate window and filter reservation-bearing
            # packets inside the same transaction.  Legacy packets without a
            # resource request remain compatible during migration; planner
            # packets cannot bypass the reservation gate.
            rows = cursor.execute(
                """SELECT * FROM jobs
                   WHERE status = 'queued'
                      OR (status = 'retry' AND
                          (retry_at_utc IS NULL OR retry_at_utc <= ?))
                   ORDER BY priority DESC, created_at_utc, job_id LIMIT ?""",
                (current, min(1024, max(limit, limit * 8))),
            ).fetchall()
            selected: list[sqlite3.Row] = []
            for row in rows:
                if not self._reservation_is_claimable_tx(
                    cursor, row, owner_id=owner_id, fence_token=int(fence_token),
                    scope=scope, current=current, require_reservation=require_reservation,
                ):
                    continue
                selected.append(row)
                if len(selected) >= limit:
                    break
            return [
                self._claim_row_tx(
                    cursor,
                    row,
                    owner_id=owner_id,
                    fence_token=int(fence_token),
                    lease_ttl_seconds=lease_ttl_seconds,
                    scope=scope,
                    current=current,
                )
                for row in selected
            ]

    # Alternate spelling used by worker-pool adapters.
    claim_next_jobs = claim_jobs

    def complete_job(
        self,
        job_id: str,
        attempt_id: str,
        owner_id: str,
        fence_token: int,
        *,
        success: bool,
        result: Any = None,
        artifact_manifest: Any = None,
        validation: Any = None,
        error_class: str | None = None,
        error: Any = None,
        retryable: bool = False,
        retry_delay_seconds: float | None = None,
        retry_at_utc: str | None = None,
        scope: str = DEFAULT_LEASE_SCOPE,
    ) -> dict[str, Any]:
        """Atomically complete one attempt and its parent job.

        ``success`` is intentionally supplied by the controller after its
        validation/artifact gates.  This layer stores evidence and enforces
        ownership/fencing; it never guesses whether a provider result is valid.
        """

        current = utc_now()
        with self._transaction() as cursor:
            self._assert_lease_tx(cursor, scope, owner_id, int(fence_token), now=current)
            job = cursor.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            attempt = cursor.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            if job is None or attempt is None or str(attempt["job_id"]) != str(job_id):
                raise JobTransitionError("job or attempt does not exist")
            if str(attempt["status"]) != "running":
                # Same-owner duplicate completion is safe and idempotent.  A
                # stale owner still had to pass _assert_lease_tx above.
                if str(attempt["owner_id"] or "") == str(owner_id) and int(attempt["fence_token"] or 0) == int(fence_token):
                    return self._decode_job_row(job) or {}
                raise JobTransitionError(f"attempt {attempt_id!r} is already terminal")
            if (
                str(job["status"]) != "running"
                or str(job["claimed_by"] or "") != str(owner_id)
                or int(job["claim_fence"] or 0) != int(fence_token)
                or str(attempt["owner_id"] or "") != str(owner_id)
                or int(attempt["fence_token"] or 0) != int(fence_token)
            ):
                raise FencingError("job is owned by a different controller fence")
            terminal_status = "completed" if success else ("retry" if retryable else "failed")
            attempt_status = "completed" if success else ("retry" if retryable else "failed")
            next_retry_at = None
            if retryable:
                if retry_at_utc is not None:
                    next_retry_at = str(retry_at_utc)
                elif retry_delay_seconds is not None:
                    next_retry_at = self._lease_expiry(
                        max(0.0, float(retry_delay_seconds)), now=current
                    )
            cursor.execute(
                """UPDATE attempts SET status = ?, finished_at_utc = ?, result_json = ?,
                   artifact_manifest_json = ?, validation_json = ?, error_class = ?, error_json = ?,
                   lease_expires_at_utc = NULL
                   WHERE attempt_id = ? AND status = 'running' AND owner_id = ? AND fence_token = ?""",
                (
                    attempt_status,
                    current,
                    _json(result) if result is not None else None,
                    _json(artifact_manifest) if artifact_manifest is not None else None,
                    _json(validation) if validation is not None else None,
                    error_class,
                    _json(error) if error is not None else None,
                    attempt_id,
                    owner_id,
                    int(fence_token),
                ),
            )
            cursor.execute(
                """UPDATE jobs SET status = ?, updated_at_utc = ?, completed_at_utc = ?,
                   claimed_by = NULL, claim_fence = NULL, lease_expires_at_utc = NULL,
                   retry_at_utc = ?, error_class = ?, error_json = ?,
                   state_revision = state_revision + 1
                   WHERE job_id = ? AND status = 'running' AND claimed_by = ? AND claim_fence = ?""",
                (
                    terminal_status,
                    current,
                    current if success else None,
                    next_retry_at,
                    error_class,
                    _json(error) if error is not None else None,
                    job_id,
                    owner_id,
                    int(fence_token),
                ),
            )
            if cursor.rowcount != 1:
                raise FencingError("job completion lost its controller fence")
            reservation_released = self._release_reservation_tx(
                cursor,
                job_id,
                owner_id=owner_id,
                fence_token=int(fence_token),
                reason="job_terminal" if not retryable else "job_retry",
                current=current,
            )
            self._append_event_tx(
                cursor,
                "job_completed" if success else ("job_retry" if retryable else "job_failed"),
                job_id=job_id,
                attempt_id=attempt_id,
                owner_id=owner_id,
                fence_token=fence_token,
                payload={
                    "success": bool(success),
                    "retryable": bool(retryable),
                    "error_class": error_class,
                    "retry_at_utc": next_retry_at,
                    "reservation_released": reservation_released,
                },
            )
            updated = cursor.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._decode_job_row(updated) or {}

    def recover_expired_jobs(
        self,
        owner_id: str,
        fence_token: int,
        *,
        scope: str = DEFAULT_LEASE_SCOPE,
        status: str = "retry",
        liveness_by_job: Mapping[str, str] | None = None,
        strict_liveness: bool = True,
    ) -> int:
        """Recover expired rows only after explicit process-liveness evidence.

        ``dead`` is the only evidence that permits a retry/queue transition
        when ``strict_liveness`` is enabled.  ``alive`` and ``unknown`` become
        a fenced ``blocked`` row requiring review; the store never guesses
        that an expired lease means the provider process has stopped.
        """

        if status not in {"retry", "failed", "queued"}:
            raise ValueError("recovery status must be retry, queued, or failed")
        evidence_map = {str(key): str(value) for key, value in (liveness_by_job or {}).items()}
        invalid = set(evidence_map.values()) - {"dead", "alive", "unknown"}
        if invalid:
            raise ValueError(f"invalid process-liveness evidence: {sorted(invalid)}")
        current = utc_now()
        count = 0
        with self._transaction() as cursor:
            self._assert_lease_tx(cursor, scope, owner_id, int(fence_token), now=current)
            rows = cursor.execute(
                """SELECT job_id FROM jobs WHERE status = 'running'
                   AND (lease_expires_at_utc IS NULL OR lease_expires_at_utc <= ?)""",
                (current,),
            ).fetchall()
            for row in rows:
                job_id = str(row["job_id"])
                evidence = evidence_map.get(job_id, "unknown")
                next_status = status
                error_class = "controller_restarted"
                error_payload: dict[str, Any] = {"status": status, "liveness": evidence}
                event_type = "job_recovered"
                if strict_liveness and evidence != "dead":
                    next_status = "blocked"
                    error_class = (
                        "orphan_process_alive" if evidence == "alive"
                        else "recovery_liveness_unknown"
                    )
                    error_payload = {
                        "status": "blocked",
                        "requested_status": status,
                        "liveness": evidence,
                        "reason": error_class,
                    }
                    event_type = "job_recovery_blocked"
                cursor.execute(
                    """UPDATE attempts SET status = 'abandoned', finished_at_utc = ?,
                       error_class = COALESCE(error_class, ?),
                       lease_expires_at_utc = NULL
                       WHERE job_id = ? AND status = 'running'""",
                    (current, error_class, job_id),
                )
                cursor.execute(
                    """UPDATE jobs SET status = ?, updated_at_utc = ?, claimed_by = NULL,
                       claim_fence = NULL, lease_expires_at_utc = NULL, retry_at_utc = NULL,
                       error_class = ?, error_json = ?, state_revision = state_revision + 1
                       WHERE job_id = ? AND status = 'running'""",
                    (next_status, current, error_class, _json(error_payload), job_id),
                )
                if cursor.rowcount:
                    self._release_reservation_tx(
                        cursor,
                        job_id,
                        reason="recovery",
                        current=current,
                    )
                    count += 1
                    self._append_event_tx(
                        cursor,
                        event_type,
                        job_id=job_id,
                        owner_id=owner_id,
                        fence_token=fence_token,
                        payload=error_payload,
                    )
        return count

    # Names used by restart supervisors in early prototypes.  Keep these
    # aliases so an adapter can migrate without changing its recovery logic.
    recover_stale_jobs = recover_expired_jobs

    # ------------------------------------------------------------------
    # Events, attempts, and restart snapshots
    # ------------------------------------------------------------------
    def _append_event_tx(
        self,
        cursor: sqlite3.Cursor,
        event_type: str,
        *,
        event_id: str | None = None,
        job_id: str | None = None,
        attempt_id: str | None = None,
        owner_id: str | None = None,
        fence_token: int | None = None,
        payload: Any = None,
        at_utc: str | None = None,
    ) -> dict[str, Any]:
        event_id = event_id or _new_id("event")
        at_utc = at_utc or utc_now()
        cursor.execute(
            """INSERT OR IGNORE INTO events
               (event_id, schema_version, job_id, attempt_id, event_type, at_utc, owner_id, fence_token, payload_json)
               VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, job_id, attempt_id, event_type, at_utc, owner_id, fence_token, _json(payload)),
        )
        row = cursor.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        assert row is not None
        return self._decode_event_row(row) or {}

    def append_event(
        self,
        event_type: str,
        *,
        event_id: str | None = None,
        job_id: str | None = None,
        attempt_id: str | None = None,
        owner_id: str | None = None,
        fence_token: int | None = None,
        scope: str = DEFAULT_LEASE_SCOPE,
        payload: Any = None,
    ) -> dict[str, Any]:
        with self._transaction() as cursor:
            if owner_id is not None or fence_token is not None:
                if owner_id is None or fence_token is None:
                    raise FencingError("owner_id and fence_token must be supplied together")
                self._assert_lease_tx(cursor, scope, owner_id, int(fence_token))
            return self._append_event_tx(
                cursor,
                event_type,
                event_id=event_id,
                job_id=job_id,
                attempt_id=attempt_id,
                owner_id=owner_id,
                fence_token=fence_token,
                payload=payload,
            )

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        return self._decode_attempt_row(row)

    def list_attempts(self, job_id: str | None = None) -> list[dict[str, Any]]:
        if job_id is None:
            rows = self._conn.execute(
                "SELECT * FROM attempts ORDER BY job_id, attempt_no"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM attempts WHERE job_id = ? ORDER BY attempt_no", (job_id,)
            ).fetchall()
        return [self._decode_attempt_row(row) or {} for row in rows]

    def list_events(self, job_id: str | None = None) -> list[dict[str, Any]]:
        if job_id is None:
            rows = self._conn.execute("SELECT * FROM events ORDER BY event_seq").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE job_id = ? ORDER BY event_seq", (job_id,)
            ).fetchall()
        return [self._decode_event_row(row) or {} for row in rows]

    def get_lease(self, *, scope: str = DEFAULT_LEASE_SCOPE) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM leases WHERE scope = ?", (scope,)).fetchone()
        return self._decode_lease_row(row)

    def snapshot(self) -> dict[str, Any]:
        """Return a restart-safe, JSON-serializable view of durable state."""

        return {
            "schema_version": self.schema_version,
            "jobs": self.list_jobs(),
            "attempts": self.list_attempts(),
            "events": self.list_events(),
            "reservations": self.list_reservations(),
            "leases": [
                self._decode_lease_row(row) or {}
                for row in self._conn.execute("SELECT * FROM leases ORDER BY scope").fetchall()
            ],
        }

    rebuild_state = snapshot


__all__ = [
    "DEFAULT_LEASE_SCOPE",
    "DEFAULT_LEASE_TTL_SECONDS",
    "FencingError",
    "JobConflict",
    "JobTransitionError",
    "LeaseConflict",
    "MigrationError",
    "ReservationAdmissionError",
    "ReservationConflict",
    "SCHEMA_VERSION",
    "SQLiteStore",
    "StoreError",
    "utc_now",
]


if __name__ == "__main__":  # pragma: no cover - diagnostic convenience
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="dispatch.sqlite3")
    args = parser.parse_args()
    with SQLiteStore(args.path) as store:
        print(json.dumps({"path": args.path, **store.snapshot()}, ensure_ascii=False, indent=2))
