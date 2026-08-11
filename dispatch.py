#!/usr/bin/env python3
"""Ralph Dispatch: a bounded, critic-gated, single-box research dispatcher.

The runtime is deliberately dependency-free.  It provides a durable SQLite
state machine, strict model-output validation, a persistent resource budget,
an append-only audit log, deterministic tier escalation, dependency-aware job
claims, and Anthropic/OpenAI-compatible HTTP clients.

Safety boundary: this process calls text models only.  It does not give models
tools, network access, shell access, credentials, or publishing authority.
Research evidence must arrive in a job's payload as a bounded evidence pack.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

if sys.version_info < (3, 11):  # noqa: UP036 - guard exists FOR pre-target interpreters
    raise RuntimeError(
        f"Ralph Dispatch requires Python 3.11+ (CI matrix: 3.11-3.13); "
        f"running under {sys.version.split()[0]}"
    )

try:  # Ralph targets Linux/macOS single-box hosts.
    import fcntl
except ImportError:  # pragma: no cover - guarded explicitly on unsupported hosts.
    fcntl = None


# ---------------------------------------------------------------- configuration

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "dispatch.db"

CONFIDENCE_FLOOR = 0.70
MAX_ATTEMPTS_PER_JOB = 3
MAX_REWORKS_PER_LINEAGE = 3
MAX_CALLS_PER_RUN = 500
MAX_INPUT_CHARS_PER_RUN = 5_000_000
MAX_WALL_SECONDS = 4 * 3600
MAX_CALLS_PER_CAMPAIGN = 5_000
MAX_INPUT_CHARS_PER_CAMPAIGN = 50_000_000
MAX_PAYLOAD_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 2_000_000
# The dispatcher-built critic payload embeds a worker payload plus a worker
# result, so the gate must be able to carry any response the runtime accepts.
MAX_CRITIC_PAYLOAD_BYTES = MAX_PAYLOAD_BYTES + MAX_RESPONSE_BYTES + 65_536
MAX_PROMPT_BYTES = 128_000
MAX_EVENT_DETAIL_BYTES = 8_000
REQUEST_TIMEOUT_SECONDS = 180
LEASE_SECONDS = REQUEST_TIMEOUT_SECONDS + 60
RETRY_BACKOFF_SECONDS = 5
MAX_RETRY_BACKOFF_SECONDS = 60
MIN_CALL_WALL_SECONDS = 10
MIN_SCOUT_CANDIDATES = 12

VALID_STATUSES = {
    "pending",
    "running",
    "awaiting_review",
    "committed",
    "superseded",
    "needs_human",
}
TERMINAL_STATUSES = {"committed", "superseded", "needs_human"}

TIERS = {
    "T1_extract": {
        "model": os.getenv("RALPH_T1_MODEL", "claude-haiku-4-5-20251001"),
        "max_tokens": 4096,
    },
    "T2_synth": {
        "model": os.getenv("RALPH_T2_MODEL", "claude-sonnet-4-6"),
        "max_tokens": 8192,
    },
    "T3_critic": {
        "model": os.getenv("RALPH_T3_MODEL", "claude-opus-4-8"),
        "max_tokens": 8192,
    },
}
TIER_ORDER = tuple(TIERS)

KIND_START_TIER = {
    "scout": "T1_extract",
    "dossier_section": "T1_extract",
    "dossier_assemble": "T2_synth",
    "critic_review": "T3_critic",
}
CRITIC_GATED_KINDS = {"scout", "dossier_assemble"}
PROMPT_FILES = {
    "scout": ROOT / "prompts" / "scout_system.md",
    "dossier_section": ROOT / "prompts" / "dossier_section_system.md",
    "dossier_assemble": ROOT / "prompts" / "dossier_assemble_system.md",
    "critic_review": ROOT / "prompts" / "critic_system.md",
}

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$")
# Deliberately tight token shapes: this is a backstop against pasting live
# credentials into payloads, not DLP. Broad patterns would reject legitimate
# research text.
_SECRET_VALUE_RES = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{32,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\."),
)
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
}


# --------------------------------------------------------------------- errors


class DispatchError(RuntimeError):
    """Base class for operator failures."""


class ValidationError(DispatchError):
    """A job, prompt, or model response violated its contract."""


class BudgetExceeded(DispatchError):
    """A run or persistent campaign resource ceiling was reached."""


class DispatcherAlreadyRunning(DispatchError):
    """Another dispatcher owns this database's process lock."""


class ModelCallTransientError(DispatchError):
    """A retryable transport/provider failure (timeout, 408/429/5xx)."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ModelCallPermanentError(DispatchError):
    """The request itself is unacceptable (400/404/422); retries cannot help."""


class ModelEndpointFatalError(DispatchError):
    """Endpoint auth/config failure (401/403); the whole run must halt."""


# --------------------------------------------------------------------- helpers


def now() -> float:
    return time.time()


def _transient_backoff_seconds(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with full jitter, floored by a provider Retry-After."""
    if not RETRY_BACKOFF_SECONDS:
        return retry_after or 0.0
    ceiling = min(RETRY_BACKOFF_SECONDS * (2 ** max(attempt - 1, 0)), MAX_RETRY_BACKOFF_SECONDS)
    jittered = random.uniform(0, ceiling)
    return max(jittered, retry_after or 0.0)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_json(value: Any, limit: int, label: str) -> str:
    try:
        encoded = canonical_json(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError(f"{label} is not finite JSON: {exc}") from exc
    if len(encoded.encode("utf-8")) > limit:
        raise ValidationError(f"{label} exceeds {limit} bytes")
    return encoded


VERBOSE = False


def _progress(message: str) -> None:
    if VERBOSE:
        print(f"[ralph {time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def _safe_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    return text[:500]


def _contains_sensitive_key(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if (
                normalized in _SENSITIVE_KEYS
                or normalized.endswith("_password")
                or normalized.endswith("_secret")
                or normalized.endswith("_api_key")
                or normalized.endswith("_access_token")
            ):
                return str(key)
            found = _contains_sensitive_key(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _contains_sensitive_key(child)
            if found:
                return found
    return None


def _contains_sensitive_value(value: Any) -> str | None:
    if isinstance(value, str):
        for pattern in _SECRET_VALUE_RES:
            if pattern.search(value):
                return pattern.pattern
    elif isinstance(value, Mapping):
        for child in value.values():
            found = _contains_sensitive_value(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _contains_sensitive_value(child)
            if found:
                return found
    return None


def _is_http_url(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    if any(character.isspace() or ord(character) < 32 for character in value):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port  # Force validation of a malformed port.
    except ValueError:
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def resolve_db_path(path: str | os.PathLike[str] | None = None) -> Path:
    candidate = Path(path) if path is not None else DB_PATH
    return candidate.expanduser().resolve(strict=False)


def stop_file_for(path: str | os.PathLike[str] | None = None) -> Path:
    db_path = resolve_db_path(path)
    return db_path.with_name(f"{db_path.name}.stop")


def stop_requested(path: str | os.PathLike[str] | None = None) -> bool:
    return stop_file_for(path).is_file()


def request_stop(path: str | os.PathLike[str] | None = None) -> Path:
    target = stop_file_for(path)
    target.touch(exist_ok=True)
    return target


def clear_stop(path: str | os.PathLike[str] | None = None) -> Path:
    target = stop_file_for(path)
    target.unlink(missing_ok=True)
    return target


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Use an IMMEDIATE transaction, while allowing intentional nesting."""
    owner = not conn.in_transaction
    if owner:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if owner and conn.in_transaction:
            conn.rollback()
        raise
    else:
        if owner and conn.in_transaction:
            conn.commit()


# --------------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL,
    tier             TEXT NOT NULL,
    payload          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    confidence       REAL,
    result           TEXT,
    result_hash      TEXT,
    review_result    TEXT,
    parent_id        INTEGER REFERENCES jobs(id),
    predecessor_id   INTEGER REFERENCES jobs(id),
    root_id          INTEGER REFERENCES jobs(id),
    rework_count     INTEGER NOT NULL DEFAULT 0,
    idempotency_key  TEXT,
    last_error       TEXT,
    lease_owner      TEXT,
    lease_expires    REAL,
    created          REAL NOT NULL,
    updated          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS job_dependencies (
    job_id         INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    depends_on_id  INTEGER NOT NULL REFERENCES jobs(id),
    PRIMARY KEY (job_id, depends_on_id),
    CHECK (job_id != depends_on_id)
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER REFERENCES jobs(id),
    event      TEXT NOT NULL,
    detail     TEXT NOT NULL,
    created    REAL NOT NULL,
    prev_hash  TEXT,
    row_hash   TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    owner        TEXT NOT NULL,
    started      REAL NOT NULL,
    ended        REAL,
    stop_reason  TEXT,
    calls_used   INTEGER NOT NULL DEFAULT 0,
    input_chars  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS model_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL REFERENCES runs(run_id),
    job_id         INTEGER NOT NULL REFERENCES jobs(id),
    tier           TEXT NOT NULL,
    model          TEXT NOT NULL,
    client         TEXT NOT NULL,
    input_chars    INTEGER NOT NULL,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    outcome        TEXT NOT NULL,
    error          TEXT,
    started        REAL NOT NULL,
    ended          REAL
);

CREATE TABLE IF NOT EXISTS campaign_budget (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    max_calls            INTEGER NOT NULL,
    max_input_chars      INTEGER NOT NULL,
    calls_reserved       INTEGER NOT NULL DEFAULT 0,
    input_chars_reserved INTEGER NOT NULL DEFAULT 0,
    updated              REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_tier ON jobs(status, tier, id);
CREATE INDEX IF NOT EXISTS idx_jobs_parent ON jobs(parent_id);
CREATE INDEX IF NOT EXISTS idx_jobs_root ON jobs(root_id);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);
CREATE INDEX IF NOT EXISTS idx_model_calls_run ON model_calls(run_id, id);
CREATE INDEX IF NOT EXISTS idx_model_calls_job ON model_calls(job_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency
    ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL;
"""

_MIGRATION_COLUMNS = {
    "result_hash": "TEXT",
    "review_result": "TEXT",
    "predecessor_id": "INTEGER REFERENCES jobs(id)",
    "root_id": "INTEGER REFERENCES jobs(id)",
    "rework_count": "INTEGER NOT NULL DEFAULT 0",
    "idempotency_key": "TEXT",
    "last_error": "TEXT",
    "lease_owner": "TEXT",
    "lease_expires": "REAL",
    "created": "REAL",
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Create v2 schema and conservatively migrate the reference v1 table."""
    jobs_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    if jobs_exists:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        for name, declaration in _MIGRATION_COLUMNS.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
    events_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone()
    if events_exists:
        event_columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        for name in ("prev_hash", "row_hash"):
            if name not in event_columns:
                conn.execute(f"ALTER TABLE events ADD COLUMN {name} TEXT")
    conn.executescript(SCHEMA)

    timestamp = now()
    with transaction(conn):
        conn.execute("UPDATE jobs SET created=COALESCE(created, updated, ?)", (timestamp,))
        conn.execute("UPDATE jobs SET updated=COALESCE(updated, created, ?)", (timestamp,))
        conn.execute("UPDATE jobs SET rework_count=COALESCE(rework_count, 0)")
        # V1 used `done` both for an unreviewed worker response and for a
        # critic-approved result, and it did not persist verdicts. There is no
        # sound migration to `committed`; require explicit human revalidation.
        conn.execute(
            "UPDATE jobs SET status='needs_human',last_error="
            "'legacy done row requires v2 revalidation' WHERE status='done'"
        )
        conn.execute("UPDATE jobs SET root_id=id WHERE root_id IS NULL AND kind!='critic_review'")
        conn.execute(
            "UPDATE jobs SET root_id=(SELECT COALESCE(p.root_id,p.id) FROM jobs p "
            "WHERE p.id=jobs.parent_id) WHERE root_id IS NULL AND kind='critic_review'"
        )
        for legacy_result in conn.execute(
            "SELECT id,result FROM jobs WHERE result IS NOT NULL AND result_hash IS NULL"
        ).fetchall():
            conn.execute(
                "UPDATE jobs SET result_hash=? WHERE id=?",
                (sha256_text(legacy_result["result"]), legacy_result["id"]),
            )
        conn.execute(
            "INSERT OR IGNORE INTO campaign_budget "
            "(id,max_calls,max_input_chars,calls_reserved,input_chars_reserved,updated) "
            "VALUES (1,?,?,?,?,?)",
            (MAX_CALLS_PER_CAMPAIGN, MAX_INPUT_CHARS_PER_CAMPAIGN, 0, 0, timestamp),
        )
        unchained = conn.execute("SELECT COUNT(*) FROM events WHERE row_hash IS NULL").fetchone()[0]
        if unchained:
            # Retroactively chain pre-v2.2 rows (and finish a chain interrupted
            # mid-migration) by recomputing the whole chain in id order. The
            # chain attests integrity only from this migration forward; rows
            # rewritten before it cannot be detected retroactively.
            prev_hash = EVENT_CHAIN_GENESIS
            for row in conn.execute(
                "SELECT id,job_id,event,detail,created FROM events ORDER BY id"
            ).fetchall():
                row_hash = _event_row_hash(
                    prev_hash, row["job_id"], row["event"], row["detail"], row["created"]
                )
                conn.execute(
                    "UPDATE events SET prev_hash=?,row_hash=? WHERE id=?",
                    (prev_hash, row_hash, row["id"]),
                )
                prev_hash = row_hash
        conn.execute("PRAGMA user_version=2")


def db(
    path: str | os.PathLike[str] | None = None,
    *,
    recover: bool = False,
) -> sqlite3.Connection:
    db_path = resolve_db_path(path)
    if not db_path.parent.is_dir():
        raise FileNotFoundError(f"database directory does not exist: {db_path.parent}")
    creating = not db_path.exists()
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    if creating:
        # Evidence excerpts and research text live in this file; keep a fresh
        # database owner-only. SQLite creates -wal/-shm with the same mode.
        # An operator who widened permissions deliberately is not overridden
        # because only newly created databases are chmodded.
        with contextlib.suppress(OSError):
            os.chmod(db_path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    _migrate(conn)
    if recover:
        recover_stale_jobs(conn)
    return conn


EVENT_CHAIN_GENESIS = "0" * 64


def _event_row_hash(
    prev_hash: str, job_id: int | None, event: str, detail: str, created: float
) -> str:
    payload = canonical_json(
        {"job_id": job_id, "event": event, "detail": detail, "created": created}
    )
    return sha256_text(prev_hash + payload)


def _event_chain_head(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT row_hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
    if row is None or row["row_hash"] is None:
        return EVENT_CHAIN_GENESIS
    return row["row_hash"]


def log_event(
    conn: sqlite3.Connection,
    job_id: int | None,
    event: str,
    detail: Mapping[str, Any] | None = None,
) -> None:
    if not event or len(event) > 80:
        raise ValueError("event name must contain 1-80 characters")
    encoded = _bounded_json(detail or {}, MAX_EVENT_DETAIL_BYTES, "event detail")
    timestamp = now()
    # Hash-chain each event to its predecessor. Rewriting or deleting any row
    # breaks every later hash, turning silent history edits into an audit
    # violation. This detects tampering; it cannot prevent an administrator
    # from re-chaining the whole log, which still requires the externally
    # archived chain head (see THREAT_MODEL residual risks).
    prev_hash = _event_chain_head(conn)
    conn.execute(
        "INSERT INTO events(job_id,event,detail,created,prev_hash,row_hash) VALUES (?,?,?,?,?,?)",
        (
            job_id,
            event,
            encoded,
            timestamp,
            prev_hash,
            _event_row_hash(prev_hash, job_id, event, encoded, timestamp),
        ),
    )


def _validate_evidence(evidence: Any) -> None:
    if evidence is None:
        return
    if not isinstance(evidence, list):
        raise ValidationError("payload.evidence must be a list")
    seen_ids: set[str] = set()
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise ValidationError(f"evidence[{index}] must be an object")
        source_id = item.get("source_id")
        url = item.get("url")
        excerpt = item.get("excerpt")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValidationError(f"evidence[{index}].source_id is required")
        if source_id in seen_ids:
            raise ValidationError(f"duplicate evidence source_id: {source_id}")
        seen_ids.add(source_id)
        if not _is_http_url(url):
            raise ValidationError(f"evidence[{index}].url must be http(s)")
        if not isinstance(excerpt, str) or not excerpt.strip():
            raise ValidationError(f"evidence[{index}].excerpt is required")
        expected_hash = item.get("sha256")
        if expected_hash is not None and expected_hash != sha256_text(excerpt):
            raise ValidationError(f"evidence[{index}] sha256 does not match excerpt")


def validate_payload(kind: str, payload: Mapping[str, Any]) -> str:
    if kind not in KIND_START_TIER:
        raise ValidationError(f"unknown job kind: {kind!r}")
    if not isinstance(payload, Mapping):
        raise ValidationError("payload must be an object")
    sensitive = _contains_sensitive_key(payload)
    if sensitive:
        raise ValidationError(f"payload contains forbidden secret-bearing key: {sensitive}")
    leaked = _contains_sensitive_value(payload)
    if leaked:
        raise ValidationError(f"payload contains a credential-shaped value (pattern: {leaked})")
    if kind == "critic_review":
        for field in ("original_task", "work_product", "worker_result_hash"):
            if not isinstance(payload.get(field), str) or not payload[field].strip():
                raise ValidationError(f"critic payload requires {field}")
    else:
        task = payload.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValidationError("worker payload requires a non-empty task")
    _validate_evidence(payload.get("evidence"))
    limit = MAX_CRITIC_PAYLOAD_BYTES if kind == "critic_review" else MAX_PAYLOAD_BYTES
    return _bounded_json(dict(payload), limit, "payload")


def _validate_idempotency_key(key: str | None, *, internal: bool = False) -> None:
    if key is not None and not _IDEMPOTENCY_RE.fullmatch(key):
        raise ValidationError(
            "idempotency_key must be 1-200 safe characters (letters, digits, ._:/-)"
        )
    if key is not None and not internal and key.startswith(("critic:", "rework:")):
        raise ValidationError("idempotency_key uses a dispatcher-reserved prefix")


def _insert_job(
    conn: sqlite3.Connection,
    kind: str,
    payload: Mapping[str, Any],
    *,
    parent_id: int | None = None,
    predecessor_id: int | None = None,
    root_id: int | None = None,
    rework_count: int = 0,
    idempotency_key: str | None = None,
    depends_on: Iterable[int] = (),
    internal: bool = False,
) -> int:
    if kind == "critic_review" and not internal:
        raise ValidationError("critic_review jobs may only be created by the dispatcher")
    _validate_idempotency_key(idempotency_key, internal=internal)
    payload_json = validate_payload(kind, payload)
    dependency_ids = tuple(dict.fromkeys(int(value) for value in depends_on))
    if rework_count < 0 or rework_count > MAX_REWORKS_PER_LINEAGE:
        raise ValidationError("invalid internal rework_count")

    for dependency_id in dependency_ids:
        if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (dependency_id,)).fetchone():
            raise ValidationError(f"dependency job does not exist: {dependency_id}")
    if (
        parent_id is not None
        and not conn.execute("SELECT 1 FROM jobs WHERE id=?", (parent_id,)).fetchone()
    ):
        raise ValidationError(f"parent job does not exist: {parent_id}")

    timestamp = now()
    try:
        cursor = conn.execute(
            "INSERT INTO jobs "
            "(kind,tier,payload,status,parent_id,predecessor_id,root_id,rework_count,"
            "idempotency_key,created,updated) VALUES (?,?,?,'pending',?,?,?,?,?,?,?)",
            (
                kind,
                KIND_START_TIER[kind],
                payload_json,
                parent_id,
                predecessor_id,
                root_id,
                rework_count,
                idempotency_key,
                timestamp,
                timestamp,
            ),
        )
    except sqlite3.IntegrityError as exc:
        if idempotency_key is None:
            raise
        existing = conn.execute(
            "SELECT id,kind,payload FROM jobs WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if existing and existing["kind"] == kind and existing["payload"] == payload_json:
            existing_dependencies = tuple(
                row[0]
                for row in conn.execute(
                    "SELECT depends_on_id FROM job_dependencies WHERE job_id=? "
                    "ORDER BY depends_on_id",
                    (existing["id"],),
                )
            )
            if existing_dependencies == tuple(sorted(dependency_ids)):
                return int(existing["id"])
        raise ValidationError(f"idempotency key collision: {idempotency_key}") from exc

    job_id = int(cursor.lastrowid)
    effective_root = root_id or job_id
    conn.execute("UPDATE jobs SET root_id=? WHERE id=?", (effective_root, job_id))
    for dependency_id in dependency_ids:
        if dependency_id >= job_id:
            raise ValidationError("dependencies must reference earlier jobs (DAG invariant)")
        conn.execute(
            "INSERT INTO job_dependencies(job_id,depends_on_id) VALUES (?,?)",
            (job_id, dependency_id),
        )
    log_event(
        conn,
        job_id,
        "enqueued",
        {
            "kind": kind,
            "tier": KIND_START_TIER[kind],
            "root_id": effective_root,
            "rework_count": rework_count,
            "dependencies": list(dependency_ids),
        },
    )
    return job_id


def enqueue(
    conn: sqlite3.Connection,
    kind: str,
    payload: Mapping[str, Any],
    *,
    idempotency_key: str | None = None,
    depends_on: Iterable[int] = (),
) -> int:
    """Enqueue a validated worker job. Duplicate idempotent seeds return one id."""
    with transaction(conn):
        return _insert_job(
            conn,
            kind,
            payload,
            idempotency_key=idempotency_key,
            depends_on=depends_on,
        )


def recover_stale_jobs(conn: sqlite3.Connection, *, at: float | None = None) -> int:
    """Recover only expired leases; jobs at their attempt cap are parked."""
    timestamp = now() if at is None else at
    with transaction(conn):
        rows = conn.execute(
            "SELECT id,kind,parent_id,attempts FROM jobs WHERE status='running' "
            "AND (lease_expires IS NULL OR lease_expires<=?)",
            (timestamp,),
        ).fetchall()
        for row in rows:
            status = "needs_human" if row["attempts"] >= MAX_ATTEMPTS_PER_JOB else "pending"
            conn.execute(
                "UPDATE jobs SET status=?,lease_owner=NULL,lease_expires=NULL,"
                "last_error='stale lease recovered',updated=? WHERE id=?",
                (status, timestamp, row["id"]),
            )
            log_event(conn, row["id"], "stale_lease_recovered", {"status": status})
            if status == "needs_human" and row["kind"] == "critic_review":
                _park_parent(conn, row["parent_id"], "critic exhausted during recovery")
        return len(rows)


def _reconcile_failed_dependencies(conn: sqlite3.Connection) -> int:
    with transaction(conn):
        rows = conn.execute(
            "SELECT DISTINCT j.id FROM jobs j JOIN job_dependencies d ON d.job_id=j.id "
            "JOIN jobs upstream ON upstream.id=d.depends_on_id "
            "WHERE j.status='pending' AND NOT EXISTS (SELECT 1 FROM jobs candidate "
            "WHERE candidate.root_id=COALESCE(upstream.root_id,upstream.id) "
            "AND candidate.kind=upstream.kind AND candidate.status IN "
            "('pending','running','awaiting_review','committed'))"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE jobs SET status='needs_human',last_error=?,updated=? WHERE id=?",
                ("upstream dependency did not commit", now(), row["id"]),
            )
            log_event(conn, row["id"], "dependency_failed", {})
        return len(rows)


# ----------------------------------------------------------------- model layer


@dataclasses.dataclass(frozen=True)
class ModelResponse:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class ModelClient:
    """Backend interface. Implementations must not expose tools to the model."""

    def preflight(self) -> None:
        """Validate local configuration before reserving budget or claiming work."""

    def complete(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        timeout_seconds: float,
    ) -> ModelResponse:
        raise NotImplementedError


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent API credentials or prompt data from following redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


def _build_http_opener() -> urllib.request.OpenerDirector:
    """Build an opener that never follows redirects and ignores ambient proxies.

    Python's default opener silently honors HTTP(S)_PROXY/ALL_PROXY environment
    variables, routing API-key-bearing requests through whatever proxy the
    environment names. Ralph only uses a proxy when RALPH_HTTPS_PROXY is set
    deliberately, so proxy transit is an explicit operator decision.
    """
    proxy = os.getenv("RALPH_HTTPS_PROXY")
    proxies = {"https": proxy, "http": proxy} if proxy else {}
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies),
        _NoRedirectHandler,
    )


_HTTP_OPENER = _build_http_opener()

_FATAL_HTTP_STATUSES = {401, 403}
_PERMANENT_HTTP_STATUSES = {400, 404, 405, 413, 422}
_MAX_RETRY_AFTER_SECONDS = 60.0


def _retry_after_seconds(headers: Any) -> float | None:
    try:
        raw = headers.get("retry-after") if headers is not None else None
        if raw is None:
            return None
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


def _classify_http_error(exc: urllib.error.HTTPError) -> DispatchError:
    """Map an HTTP status to fatal/permanent/transient without echoing the body.

    Provider error bodies can echo prompt data; only bounded transport metadata
    is reported. 3xx arrives here because redirects are refused by policy.
    """
    if exc.code in _FATAL_HTTP_STATUSES:
        return ModelEndpointFatalError(f"model endpoint rejected credentials: HTTP {exc.code}")
    if exc.code in _PERMANENT_HTTP_STATUSES:
        return ModelCallPermanentError(f"model endpoint rejected the request: HTTP {exc.code}")
    return ModelCallTransientError(
        f"model endpoint returned HTTP {exc.code}",
        retry_after=_retry_after_seconds(exc.headers),
    )


def _http_open(request: urllib.request.Request, timeout: float):
    return _HTTP_OPENER.open(request, timeout=timeout)


@dataclasses.dataclass
class HttpModelClient(ModelClient):
    """Minimal Anthropic or OpenAI-compatible Messages client."""

    provider: str
    base_url: str
    api_key_env: str
    request_timeout: float = REQUEST_TIMEOUT_SECONDS

    def _api_key(self) -> str | None:
        return os.getenv(self.api_key_env) if self.api_key_env else None

    def _require_key_transport_security(self) -> None:
        """Never send an API key in cleartext to another machine.

        Plain http is acceptable only for loopback endpoints (a local
        inference server). A remote http base_url with a configured key would
        transmit the credential unencrypted; refuse before any request.
        """
        parsed = urllib.parse.urlsplit(self.base_url)
        local_hosts = {"127.0.0.1", "::1", "localhost"}
        if (
            parsed.scheme.lower() == "http"
            and parsed.hostname not in local_hosts
            and self._api_key()
        ):
            raise DispatchError(
                "refusing to send an API key over cleartext http to a non-local host; "
                "use https or an empty --api-key-env for an unauthenticated endpoint"
            )

    def preflight(self) -> None:
        provider = self.provider.lower()
        if provider not in {"anthropic", "openai"}:
            raise DispatchError("provider must be 'anthropic' or 'openai'")
        if not _is_http_url(self.base_url):
            raise DispatchError("base_url must be an HTTP(S) URL without credentials")
        if self.request_timeout <= 0:
            raise DispatchError("request timeout must be positive")
        if provider == "anthropic" and not self._api_key():
            raise DispatchError(f"missing API key in {self.api_key_env}")
        self._require_key_transport_security()
        hostname = urllib.parse.urlsplit(self.base_url).hostname
        local_hosts = {"127.0.0.1", "::1", "localhost"}
        if (
            provider == "openai"
            and hostname not in local_hosts
            and self.api_key_env
            and not self._api_key()
        ):
            raise DispatchError(
                f"missing API key in {self.api_key_env}; use an empty key-env only "
                "for an intentionally unauthenticated endpoint"
            )

    def complete(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        timeout_seconds: float,
    ) -> ModelResponse:
        provider = self.provider.lower()
        base = self.base_url.rstrip("/")
        headers = {"content-type": "application/json", "user-agent": "ralph-dispatch/2"}
        self._require_key_transport_security()  # Defense in depth beyond preflight.
        api_key = self._api_key()
        timeout = max(0.1, min(self.request_timeout, timeout_seconds))

        if provider == "anthropic":
            if not api_key:
                raise DispatchError(f"missing API key in {self.api_key_env}")
            endpoint = f"{base}/v1/messages"
            headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
            body = {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": 0,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
        elif provider == "openai":
            endpoint = f"{base}/v1/chat/completions"
            if api_key:
                headers["authorization"] = f"Bearer {api_key}"
            body = {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        else:
            raise DispatchError("provider must be 'anthropic' or 'openai'")

        request = urllib.request.Request(
            endpoint,
            data=canonical_json(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with _http_open(request, timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise _classify_http_error(exc) from exc
        except TimeoutError as exc:
            raise ModelCallTransientError("model endpoint timed out") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise ModelCallTransientError("model endpoint timed out") from exc
            raise ModelCallTransientError(f"model endpoint unavailable: {exc.reason}") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValidationError("model endpoint response exceeds byte limit")
        try:
            decoded = json.loads(raw)
            if provider == "anthropic":
                text = "".join(
                    block.get("text", "")
                    for block in decoded["content"]
                    if block.get("type") == "text"
                )
                finish = decoded.get("stop_reason")
                truncated = finish == "max_tokens"
                usage = decoded.get("usage", {})
                input_tokens = usage.get("input_tokens")
                output_tokens = usage.get("output_tokens")
            else:
                choice = decoded["choices"][0]
                text = choice["message"]["content"]
                finish = choice.get("finish_reason")
                truncated = finish == "length"
                usage = decoded.get("usage", {})
                input_tokens = usage.get("prompt_tokens")
                output_tokens = usage.get("completion_tokens")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValidationError("model endpoint returned an invalid response schema") from exc
        if truncated:
            # A truncated body is invalid JSON at best and a silent partial at
            # worst; name the cause so escalation/parking events are precise.
            raise ValidationError(f"model output truncated by token limit (finish: {finish})")
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("model endpoint returned empty text")
        return ModelResponse(text, _optional_int(input_tokens), _optional_int(output_tokens))


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    integer = int(value)
    return integer if integer >= 0 else None


def _normalize_model_response(value: Any) -> ModelResponse:
    if isinstance(value, ModelResponse):
        response = value
    elif isinstance(value, Mapping):  # Compatibility for simple test/local clients.
        response = ModelResponse(
            text=value.get("text"),
            input_tokens=_optional_int(value.get("input_tokens")),
            output_tokens=_optional_int(value.get("output_tokens")),
        )
    else:
        raise ValidationError("model client must return ModelResponse")
    if not isinstance(response.text, str) or not response.text.strip():
        raise ValidationError("model response text is empty")
    if len(response.text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValidationError("model response exceeds byte limit")
    return response


# --------------------------------------------------------------- prompt loader


def _read_prompt(kind: str) -> str:
    path = PROMPT_FILES.get(kind)
    if path is None:
        raise ValidationError(f"no prompt registered for kind: {kind!r}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read registered prompt {path.name}: {exc}") from exc
    if len(raw) > MAX_PROMPT_BYTES:
        raise ValidationError(f"registered prompt exceeds {MAX_PROMPT_BYTES} bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"registered prompt is not UTF-8: {path.name}") from exc


def load_prompt(
    kind: str,
    payload: Mapping[str, Any],
    *,
    dependency_results: Sequence[Mapping[str, Any]] = (),
) -> tuple[str, str]:
    """Build prompts from allowlisted, repository-relative files.

    The critic receives a JSON data object containing the task, work product,
    evidence, confidence, and hash.  It never receives worker system prompts,
    hidden reasoning, credentials, or tools.  JSON encoding prevents delimiter
    breakout; the critic prompt still treats every user-supplied value as
    untrusted data because prompt-instruction separation is not airtight.
    """
    if kind not in KIND_START_TIER:
        raise ValidationError(f"unknown job kind: {kind!r}")
    validate_payload(kind, payload)
    system = _read_prompt(kind)
    user_data = dict(payload)
    if kind == "dossier_assemble":
        user_data["dependency_results"] = list(dependency_results)
    user = _bounded_json(user_data, MAX_CRITIC_PAYLOAD_BYTES + MAX_RESPONSE_BYTES, "user prompt")
    return system, user


def _dependency_results(conn: sqlite3.Connection, job_id: int) -> list[dict[str, Any]]:
    upstream_rows = conn.execute(
        "SELECT upstream.id,upstream.kind,COALESCE(upstream.root_id,upstream.id) AS root_id "
        "FROM job_dependencies d JOIN jobs upstream ON upstream.id=d.depends_on_id "
        "WHERE d.job_id=? ORDER BY upstream.id",
        (job_id,),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for upstream in upstream_rows:
        row = conn.execute(
            "SELECT id,kind,result,result_hash FROM jobs WHERE root_id=? AND kind=? "
            "AND status='committed' ORDER BY rework_count DESC,id DESC LIMIT 1",
            (upstream["root_id"], upstream["kind"]),
        ).fetchone()
        if row is None:
            raise ValidationError(f"dependency lineage is not committed: {upstream['id']}")
        results.append(
            {
                "job_id": row["id"],
                "kind": row["kind"],
                "result": json.loads(row["result"]),
                "result_hash": row["result_hash"],
            }
        )
    return results


def _validate_critic_link(
    conn: sqlite3.Connection,
    parent_id: int | None,
    payload: Mapping[str, Any],
    *,
    require_awaiting: bool = True,
) -> None:
    if parent_id is None:
        raise ValidationError("critic has no parent")
    parent = conn.execute("SELECT * FROM jobs WHERE id=?", (parent_id,)).fetchone()
    if parent is None or parent["kind"] not in CRITIC_GATED_KINDS:
        raise ValidationError("critic parent is missing or not critic-gated")
    if require_awaiting and parent["status"] != "awaiting_review":
        raise ValidationError("critic parent is not awaiting review")
    try:
        parent_payload = json.loads(parent["payload"])
        parent_envelope = json.loads(parent["result"])
        expected_work_product = canonical_json(parent_envelope["RESULT"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValidationError("critic parent has invalid stored data") from exc
    comparisons = {
        "original_task": parent_payload.get("task"),
        "work_product": expected_work_product,
        "worker_result_hash": parent["result_hash"],
        "evidence": parent_payload.get("evidence", []),
    }
    for field, expected in comparisons.items():
        if payload.get(field) != expected:
            raise ValidationError(f"critic payload does not match parent {field}")


# ------------------------------------------------------------ output validation


def _parse_json_object(text: str, label: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValidationError(f"{label} contains non-finite JSON constant: {value}")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValidationError(f"{label} contains duplicate key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except RecursionError as exc:
        # Deeply nested arrays/objects within the response byte cap can exhaust
        # the interpreter stack; fail closed as a named validation error rather
        # than letting RecursionError escape the dispatch loop.
        raise ValidationError(f"{label} is nested too deeply") from exc
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValidationError(f"{label} must be one JSON object") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be one JSON object")
    return value


def _finite_number(value: Any, label: str, low: float, high: float) -> float:
    if isinstance(value, bool):
        raise ValidationError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        # A JSON integer literal is unbounded, so float() can raise OverflowError
        # ("int too large to convert to float") rather than ValueError. Treat it
        # as invalid numeric input instead of letting it escape as an
        # unhandled arithmetic error.
        raise ValidationError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise ValidationError(f"{label} must be finite and between {low} and {high}")
    return number


def _evidence_urls(payload: Mapping[str, Any]) -> set[str]:
    return {
        item["url"]
        for item in payload.get("evidence", [])
        if isinstance(item, dict) and isinstance(item.get("url"), str)
    }


def _validate_claims(
    claims: Any,
    label: str,
    *,
    allow_empty: bool = False,
    allowed_urls: set[str] | None = None,
) -> None:
    if not isinstance(claims, list) or (not claims and not allow_empty):
        raise ValidationError(
            f"{label} must be a{' possibly empty' if allow_empty else ' non-empty'} list"
        )
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict) or set(claim) != {
            "statement",
            "source_url",
            "confidence",
            "source_type",
        }:
            raise ValidationError(f"{label}[{index}] must be an object")
        for field in ("statement", "source_url", "confidence", "source_type"):
            if not isinstance(claim.get(field), str) or not claim[field].strip():
                raise ValidationError(f"{label}[{index}].{field} is required")
        if not _is_http_url(claim["source_url"]):
            raise ValidationError(f"{label}[{index}].source_url must be http(s)")
        if allowed_urls is not None and claim["source_url"] not in allowed_urls:
            raise ValidationError(f"{label}[{index}].source_url is absent from evidence")
        if claim["confidence"].upper() not in {"HIGH", "MEDIUM", "LOW"}:
            raise ValidationError(f"{label}[{index}].confidence is invalid")


def _validate_scout_result(result: Any, payload: Mapping[str, Any]) -> None:
    if (
        not isinstance(result, dict)
        or set(result) != {"candidates"}
        or not isinstance(result.get("candidates"), list)
    ):
        raise ValidationError("scout RESULT.candidates must be a list")
    candidates = result["candidates"]
    if len(candidates) < MIN_SCOUT_CANDIDATES:
        raise ValidationError(
            f"scout returned {len(candidates)} candidates; minimum is {MIN_SCOUT_CANDIDATES}"
        )
    exclusions = {str(name).strip().casefold() for name in payload.get("exclusion_list", [])}
    names: set[str] = set()
    allowed_urls = _evidence_urls(payload)
    required = {
        "name",
        "url",
        "thesis_fit",
        "funding_status",
        "revenue_evidence",
        "why_missed",
        "sources",
    }
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict) or set(candidate) != required:
            raise ValidationError(f"candidate[{index}] is missing required fields")
        name = candidate["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(f"candidate[{index}].name is required")
        normalized = name.strip().casefold()
        if normalized in exclusions:
            raise ValidationError(f"candidate[{index}] is on the exclusion list: {name}")
        if normalized in names:
            raise ValidationError(f"duplicate candidate name: {name}")
        names.add(normalized)
        if not _is_http_url(candidate["url"]):
            raise ValidationError(f"candidate[{index}].url must be http(s)")
        for field in ("thesis_fit", "revenue_evidence", "why_missed"):
            if not isinstance(candidate[field], str) or not candidate[field].strip():
                raise ValidationError(f"candidate[{index}].{field} is required")
        if candidate["funding_status"] not in {"funded", "bootstrapped", "unknown"}:
            raise ValidationError(f"candidate[{index}].funding_status is invalid")
        if not isinstance(candidate["sources"], list) or not candidate["sources"]:
            raise ValidationError(f"candidate[{index}].sources must be non-empty")
        for source in candidate["sources"]:
            if not _is_http_url(source):
                raise ValidationError(f"candidate[{index}] source must be http(s)")
            if source not in allowed_urls:
                raise ValidationError(f"candidate[{index}] source is absent from evidence")


def _validate_dossier_section_result(result: Any, payload: Mapping[str, Any]) -> None:
    if not isinstance(result, dict) or set(result) != {"section", "claims", "not_disclosed"}:
        raise ValidationError("dossier section RESULT must be an object")
    if not isinstance(result.get("section"), str) or not result["section"].strip():
        raise ValidationError("dossier section RESULT.section is required")
    not_disclosed = result.get("not_disclosed", [])
    if not isinstance(not_disclosed, list) or not all(
        isinstance(item, str) and item.strip() for item in not_disclosed
    ):
        raise ValidationError("dossier section RESULT.not_disclosed must be strings")
    _validate_claims(
        result.get("claims"),
        "RESULT.claims",
        allow_empty=bool(not_disclosed),
        allowed_urls=_evidence_urls(payload),
    )


def _validate_dossier_assemble_result(result: Any, payload: Mapping[str, Any]) -> None:
    if not isinstance(result, dict) or set(result) != {"markdown", "claims", "viability_score"}:
        raise ValidationError("dossier assembly RESULT must be an object")
    if not isinstance(result.get("markdown"), str) or not result["markdown"].strip():
        raise ValidationError("dossier assembly RESULT.markdown is required")
    _finite_number(result.get("viability_score"), "viability_score", 0, 100)
    _validate_claims(
        result.get("claims"),
        "RESULT.claims",
        allowed_urls=_evidence_urls(payload),
    )


def parse_worker_response(
    kind: str,
    text: str,
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], float]:
    envelope = _parse_json_object(text, "worker response")
    if set(envelope) != {"RESULT", "CONFIDENCE"}:
        raise ValidationError("worker response must contain exactly RESULT and CONFIDENCE")
    confidence = _finite_number(envelope["CONFIDENCE"], "CONFIDENCE", 0, 1)
    result = envelope["RESULT"]
    if kind == "scout":
        _validate_scout_result(result, payload)
    elif kind == "dossier_section":
        _validate_dossier_section_result(result, payload)
    elif kind == "dossier_assemble":
        _validate_dossier_assemble_result(result, payload)
    else:
        raise ValidationError(f"not a worker kind: {kind}")
    return envelope, confidence


def parse_critic_response(text: str) -> dict[str, Any]:
    verdict = _parse_json_object(text, "critic response")
    required = {"VERDICT", "RISK_SCORE", "CRITICAL_ISSUES", "MINOR_ISSUES"}
    if set(verdict) != required:
        raise ValidationError("critic response has missing or unexpected fields")
    if verdict["VERDICT"] not in {"PASS", "CONDITIONAL_PASS", "FAIL"}:
        raise ValidationError("critic VERDICT is invalid")
    verdict["RISK_SCORE"] = _finite_number(verdict["RISK_SCORE"], "RISK_SCORE", 0, 1)
    for field in ("CRITICAL_ISSUES", "MINOR_ISSUES"):
        issues = verdict[field]
        if not isinstance(issues, list) or not all(
            isinstance(issue, str) and issue.strip() and len(issue) <= 2000 for issue in issues
        ):
            raise ValidationError(f"critic {field} must be a list of bounded strings")
    if verdict["VERDICT"] == "PASS" and verdict["CRITICAL_ISSUES"]:
        raise ValidationError("PASS cannot contain critical issues")
    if verdict["VERDICT"] == "CONDITIONAL_PASS" and not (
        verdict["CRITICAL_ISSUES"] or verdict["MINOR_ISSUES"]
    ):
        raise ValidationError("CONDITIONAL_PASS must explain at least one issue")
    if verdict["VERDICT"] == "FAIL" and not verdict["CRITICAL_ISSUES"]:
        raise ValidationError("FAIL must explain at least one critical issue")
    return verdict


# ------------------------------------------------------------------- routing


def next_tier(current: str) -> str | None:
    if current == "T1_extract":
        return "T2_synth"
    if current in {"T2_synth", "T3_critic"}:
        return None
    raise ValidationError(f"unknown tier: {current!r}")


def _park_parent(conn: sqlite3.Connection, parent_id: int | None, reason: str) -> None:
    if parent_id is None:
        return
    conn.execute(
        "UPDATE jobs SET status='needs_human',last_error=?,updated=? "
        "WHERE id=? AND status='awaiting_review'",
        (reason[:500], now(), parent_id),
    )
    log_event(conn, parent_id, "parked_with_critic", {"reason": reason[:500]})


def route_critic_verdict(
    conn: sqlite3.Connection,
    job_id: int,
    verdict: Mapping[str, Any],
) -> int | None:
    """Persist and route a strict verdict. Return a replacement worker id, if any."""
    try:
        strict_verdict = parse_critic_response(canonical_json(dict(verdict)))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"critic verdict is not finite JSON: {exc}") from exc
    verdict_json = canonical_json(strict_verdict)
    timestamp = now()
    with transaction(conn):
        critic = conn.execute(
            "SELECT * FROM jobs WHERE id=? AND kind='critic_review'", (job_id,)
        ).fetchone()
        if critic is None or critic["parent_id"] is None:
            raise ValidationError("critic job or parent is missing")
        parent = conn.execute("SELECT * FROM jobs WHERE id=?", (critic["parent_id"],)).fetchone()
        if parent is None or parent["kind"] not in CRITIC_GATED_KINDS:
            raise ValidationError("critic parent is missing or is not critic-gated")
        if parent["status"] != "awaiting_review":
            raise ValidationError(f"critic parent is not awaiting review: {parent['status']}")

        risk = strict_verdict["RISK_SCORE"]
        passed = (strict_verdict["VERDICT"] == "PASS" and risk < 0.3) or (
            strict_verdict["VERDICT"] == "CONDITIONAL_PASS" and risk < 0.5
        )
        conn.execute(
            "UPDATE jobs SET result=?,result_hash=?,review_result=?,updated=? WHERE id=?",
            (verdict_json, sha256_text(verdict_json), verdict_json, timestamp, job_id),
        )
        conn.execute(
            "UPDATE jobs SET review_result=?,updated=? WHERE id=?",
            (verdict_json, timestamp, parent["id"]),
        )

        if passed:
            conn.execute(
                "UPDATE jobs SET status='committed',lease_owner=NULL,lease_expires=NULL,updated=? "
                "WHERE id IN (?,?)",
                (timestamp, job_id, parent["id"]),
            )
            log_event(
                conn,
                job_id,
                "critic_accepted",
                {"verdict": strict_verdict["VERDICT"], "risk": risk},
            )
            log_event(conn, parent["id"], "committed", {"critic_job_id": job_id, "risk": risk})
            return None

        rework_count = int(parent["rework_count"])
        if rework_count >= MAX_REWORKS_PER_LINEAGE:
            conn.execute(
                "UPDATE jobs SET status='needs_human',lease_owner=NULL,"
                "lease_expires=NULL,updated=? "
                "WHERE id IN (?,?)",
                (timestamp, job_id, parent["id"]),
            )
            log_event(conn, job_id, "rework_cap_reached", {"rework_count": rework_count})
            log_event(conn, parent["id"], "needs_human", {"reason": "critic rework cap"})
            return None

        parent_payload = json.loads(parent["payload"])
        parent_payload["retry_brief"] = {
            "critic_job_id": job_id,
            "critical_issues": strict_verdict["CRITICAL_ISSUES"],
        }
        replacement_id = _insert_job(
            conn,
            parent["kind"],
            parent_payload,
            predecessor_id=parent["id"],
            root_id=parent["root_id"],
            rework_count=rework_count + 1,
            idempotency_key=f"rework:{parent['root_id']}:{rework_count + 1}",
            internal=True,
        )
        conn.execute(
            "UPDATE jobs SET status='superseded',lease_owner=NULL,lease_expires=NULL,updated=? "
            "WHERE id IN (?,?)",
            (timestamp, job_id, parent["id"]),
        )
        log_event(conn, job_id, "critic_rejected", {"risk": risk, "replacement_id": replacement_id})
        log_event(conn, parent["id"], "superseded", {"replacement_id": replacement_id})
        return replacement_id


# --------------------------------------------------------------------- budget


@dataclasses.dataclass
class Budget:
    max_calls: int = MAX_CALLS_PER_RUN
    max_input_chars: int = MAX_INPUT_CHARS_PER_RUN
    max_wall: float = MAX_WALL_SECONDS
    calls: int = 0
    input_chars: int = 0
    _started: float = dataclasses.field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if self.max_calls < 0 or self.max_input_chars < 0 or self.max_wall <= 0:
            raise ValueError("budget ceilings must be non-negative; wall time must be positive")

    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def remaining_wall(self) -> float:
        return max(0.0, self.max_wall - self.elapsed())

    def exhausted(self) -> str | None:
        if self.calls >= self.max_calls:
            return f"run call budget reached ({self.calls}/{self.max_calls})"
        if self.input_chars >= self.max_input_chars:
            return "run input-character budget reached"
        if self.elapsed() >= self.max_wall:
            return "run wall-clock budget reached"
        return None

    def check_next(self, input_chars: int) -> None:
        reason = self.exhausted()
        if reason:
            raise BudgetExceeded(reason)
        if self.calls + 1 > self.max_calls:
            raise BudgetExceeded("run call budget would be exceeded")
        if self.input_chars + input_chars > self.max_input_chars:
            raise BudgetExceeded("run input-character budget would be exceeded")

    def spend(self, input_chars: int) -> None:
        self.check_next(input_chars)
        self.calls += 1
        self.input_chars += input_chars


def _reserve_model_call(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    job_id: int,
    tier: str,
    model: str,
    client_name: str,
    input_chars: int,
) -> int:
    with transaction(conn):
        row = conn.execute("SELECT * FROM campaign_budget WHERE id=1").fetchone()
        if row["calls_reserved"] + 1 > row["max_calls"]:
            raise BudgetExceeded("persistent campaign call budget reached")
        if row["input_chars_reserved"] + input_chars > row["max_input_chars"]:
            raise BudgetExceeded("persistent campaign input-character budget reached")
        conn.execute(
            "UPDATE campaign_budget SET calls_reserved=calls_reserved+1,"
            "input_chars_reserved=input_chars_reserved+?,updated=? WHERE id=1",
            (input_chars, now()),
        )
        cursor = conn.execute(
            "INSERT INTO model_calls "
            "(run_id,job_id,tier,model,client,input_chars,outcome,started) "
            "VALUES (?,?,?,?,?,?,'reserved',?)",
            (run_id, job_id, tier, model, client_name, input_chars, now()),
        )
        return int(cursor.lastrowid)


# ---------------------------------------------------------------- process lock


class DispatcherLock:
    def __init__(self, db_path: str | os.PathLike[str], owner: str):
        self.path = resolve_db_path(db_path).with_suffix(resolve_db_path(db_path).suffix + ".lock")
        self.owner = owner
        self._handle: Any = None

    def __enter__(self) -> DispatcherLock:
        if fcntl is None:
            raise DispatchError("single-dispatcher locking requires fcntl on this host")
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self._handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise DispatcherAlreadyRunning(f"dispatcher lock is held: {self.path}") from exc
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(canonical_json({"owner": self.owner, "pid": os.getpid(), "time": now()}))
        self._handle.flush()
        return self

    def __exit__(self, *_: Any) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


# ------------------------------------------------------------------- dispatch


@dataclasses.dataclass(frozen=True)
class ClaimedJob:
    id: int
    kind: str
    tier: str
    payload: dict[str, Any]
    attempts: int
    parent_id: int | None
    root_id: int
    rework_count: int


def _claim_next(
    conn: sqlite3.Connection,
    tier: str,
    owner: str,
) -> ClaimedJob | None:
    if tier not in TIERS:
        raise ValidationError(f"unknown tier: {tier}")
    with transaction(conn):
        while True:
            row = conn.execute(
                "SELECT j.* FROM jobs j WHERE j.status='pending' AND j.tier=? "
                "AND NOT EXISTS (SELECT 1 FROM job_dependencies d JOIN jobs upstream "
                "ON upstream.id=d.depends_on_id WHERE d.job_id=j.id AND NOT EXISTS "
                "(SELECT 1 FROM jobs candidate WHERE candidate.root_id="
                "COALESCE(upstream.root_id,upstream.id) AND candidate.kind=upstream.kind "
                "AND candidate.status='committed')) ORDER BY j.id LIMIT 1",
                (tier,),
            ).fetchone()
            if row is None:
                return None
            if row["attempts"] >= MAX_ATTEMPTS_PER_JOB:
                conn.execute(
                    "UPDATE jobs SET status='needs_human',last_error=?,updated=? WHERE id=?",
                    ("attempt cap reached before claim", now(), row["id"]),
                )
                log_event(conn, row["id"], "attempt_cap_reached", {})
                if row["kind"] == "critic_review":
                    _park_parent(conn, row["parent_id"], "critic attempt cap reached")
                continue
            lease_expires = now() + LEASE_SECONDS
            cursor = conn.execute(
                "UPDATE jobs SET status='running',attempts=attempts+1,lease_owner=?,"
                "lease_expires=?,last_error=NULL,updated=? WHERE id=? AND status='pending'",
                (owner, lease_expires, now(), row["id"]),
            )
            if cursor.rowcount != 1:
                continue
            attempts = int(row["attempts"]) + 1
            log_event(conn, row["id"], "claimed", {"attempt": attempts, "owner": owner})
            try:
                payload = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError) as exc:
                conn.execute(
                    "UPDATE jobs SET status='needs_human',last_error=?,lease_owner=NULL,"
                    "lease_expires=NULL,updated=? WHERE id=?",
                    (_safe_error(exc), now(), row["id"]),
                )
                log_event(conn, row["id"], "invalid_stored_payload", {})
                continue
            return ClaimedJob(
                int(row["id"]),
                row["kind"],
                row["tier"],
                payload,
                attempts,
                row["parent_id"],
                int(row["root_id"]),
                int(row["rework_count"]),
            )


def _unclaim_for_budget(conn: sqlite3.Connection, job: ClaimedJob, reason: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE jobs SET status='pending',attempts=MAX(attempts-1,0),"
            "lease_owner=NULL,lease_expires=NULL,last_error=?,updated=? WHERE id=?",
            (reason[:500], now(), job.id),
        )
        log_event(conn, job.id, "budget_deferred", {"reason": reason[:500]})


def _park_job(
    conn: sqlite3.Connection,
    job: ClaimedJob,
    reason: str,
    event: str,
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE jobs SET status='needs_human',last_error=?,lease_owner=NULL,"
            "lease_expires=NULL,updated=? WHERE id=?",
            (reason[:500], now(), job.id),
        )
        log_event(conn, job.id, event, {"reason": reason[:500]})
        if job.kind == "critic_review":
            _park_parent(conn, job.parent_id, reason)


def _retry_or_park(
    conn: sqlite3.Connection,
    job: ClaimedJob,
    reason: str,
    event: str,
) -> None:
    with transaction(conn):
        status = "needs_human" if job.attempts >= MAX_ATTEMPTS_PER_JOB else "pending"
        conn.execute(
            "UPDATE jobs SET status=?,last_error=?,lease_owner=NULL,lease_expires=NULL,"
            "updated=? WHERE id=?",
            (status, reason[:500], now(), job.id),
        )
        log_event(conn, job.id, event, {"status": status, "attempt": job.attempts})
        if status == "needs_human" and job.kind == "critic_review":
            _park_parent(conn, job.parent_id, reason)


def _mark_call_rejected(conn: sqlite3.Connection, call_id: int, exc: Exception) -> None:
    """Downgrade a transport-successful call whose output failed validation.

    'succeeded' in the ledger previously meant only that HTTP returned, so
    schema-invalid responses inflated every calibration metric derived from
    it (worker schema-valid rate, false-pass denominators). A distinct
    'rejected_output' outcome keeps transport success and usable output
    separate; the calibration protocol reads the latter.
    """
    with transaction(conn):
        conn.execute(
            "UPDATE model_calls SET outcome='rejected_output',error=? WHERE id=?",
            (_safe_error(exc), call_id),
        )


def _handle_worker_success(
    conn: sqlite3.Connection,
    job: ClaimedJob,
    envelope: dict[str, Any],
    confidence: float,
) -> None:
    result_json = canonical_json(envelope)
    result_hash = sha256_text(result_json)
    timestamp = now()
    with transaction(conn):
        if confidence < CONFIDENCE_FLOOR and (promoted := next_tier(job.tier)):
            conn.execute(
                "UPDATE jobs SET tier=?,status='pending',confidence=?,result=?,result_hash=?,"
                "lease_owner=NULL,lease_expires=NULL,updated=? WHERE id=?",
                (promoted, confidence, result_json, result_hash, timestamp, job.id),
            )
            log_event(
                conn,
                job.id,
                "escalated",
                {"from": job.tier, "to": promoted, "confidence": confidence},
            )
            return

        if confidence < CONFIDENCE_FLOOR and job.kind not in CRITIC_GATED_KINDS:
            conn.execute(
                "UPDATE jobs SET status='needs_human',confidence=?,result=?,result_hash=?,"
                "last_error='terminal-tier confidence below floor',lease_owner=NULL,"
                "lease_expires=NULL,updated=? WHERE id=?",
                (confidence, result_json, result_hash, timestamp, job.id),
            )
            log_event(conn, job.id, "low_confidence_parked", {"confidence": confidence})
            return

        conn.execute(
            "UPDATE jobs SET confidence=?,result=?,result_hash=?,last_error=NULL,"
            "lease_owner=NULL,lease_expires=NULL,updated=? WHERE id=?",
            (confidence, result_json, result_hash, timestamp, job.id),
        )
        if job.kind in CRITIC_GATED_KINDS:
            critic_payload = {
                "original_task": job.payload["task"],
                "work_product": canonical_json(envelope["RESULT"]),
                "worker_confidence": confidence,
                "worker_result_hash": result_hash,
                "evidence": job.payload.get("evidence", []),
            }
            critic_id = _insert_job(
                conn,
                "critic_review",
                critic_payload,
                parent_id=job.id,
                root_id=job.root_id,
                rework_count=job.rework_count,
                idempotency_key=f"critic:{job.id}",
                internal=True,
            )
            conn.execute(
                "UPDATE jobs SET status='awaiting_review',updated=? WHERE id=?", (timestamp, job.id)
            )
            log_event(conn, job.id, "awaiting_review", {"critic_job_id": critic_id})
        else:
            conn.execute(
                "UPDATE jobs SET status='committed',updated=? WHERE id=?", (timestamp, job.id)
            )
            log_event(conn, job.id, "committed", {"confidence": confidence})


def drain_tier(
    conn: sqlite3.Connection,
    client: ModelClient,
    tier: str,
    budget: Budget,
    *,
    owner: str,
    db_path: str | os.PathLike[str],
    run_id: str,
) -> tuple[int, str | None]:
    """Drain one tier sequentially; return (calls_made, halt_reason)."""
    calls_made = 0
    while True:
        if stop_requested(db_path):
            return calls_made, "stop file present"
        if reason := budget.exhausted():
            return calls_made, reason
        if budget.remaining_wall() < MIN_CALL_WALL_SECONDS:
            # A call started now would run with a near-zero timeout, fail, and
            # burn an attempt. Stop cleanly instead of dispatching doomed work.
            return calls_made, "run wall-clock budget nearly exhausted"
        job = _claim_next(conn, tier, owner)
        if job is None:
            return calls_made, None
        try:
            dependencies = _dependency_results(conn, job.id)
            if job.kind == "critic_review":
                _validate_critic_link(conn, job.parent_id, job.payload)
            system, user = load_prompt(job.kind, job.payload, dependency_results=dependencies)
        except (ValidationError, OSError, KeyError, TypeError) as exc:
            _park_job(conn, job, _safe_error(exc), "permanent_prompt_error")
            continue

        input_chars = len(system) + len(user)
        try:
            budget.check_next(input_chars)
            call_id = _reserve_model_call(
                conn,
                run_id=run_id,
                job_id=job.id,
                tier=tier,
                model=TIERS[tier]["model"],
                client_name=type(client).__name__,
                input_chars=input_chars,
            )
            budget.spend(input_chars)
        except BudgetExceeded as exc:
            _unclaim_for_budget(conn, job, str(exc))
            return calls_made, str(exc)

        with transaction(conn):
            conn.execute(
                "UPDATE runs SET calls_used=?,input_chars=? WHERE run_id=?",
                (budget.calls, budget.input_chars, run_id),
            )
            log_event(
                conn,
                job.id,
                "model_call_reserved",
                {"call_id": call_id, "tier": tier, "input_chars": input_chars},
            )
        calls_made += 1
        try:
            raw_response = client.complete(
                TIERS[tier]["model"],
                system,
                user,
                TIERS[tier]["max_tokens"],
                budget.remaining_wall(),
            )
            response = _normalize_model_response(raw_response)
        except Exception as exc:  # Model/network failures are bounded, recorded, and routed.
            with transaction(conn):
                conn.execute(
                    "UPDATE model_calls SET outcome='failed',error=?,ended=? WHERE id=?",
                    (_safe_error(exc), now(), call_id),
                )
            _progress(
                f"call {budget.calls}/{budget.max_calls} job={job.id} kind={job.kind} "
                f"tier={tier} attempt={job.attempts} failed: {_safe_error(exc)[:120]}"
            )
            if isinstance(exc, ModelEndpointFatalError):
                # A credential/config rejection dooms every retry queue-wide.
                # Halt the run after one wasted call instead of attempts*jobs;
                # the claimed job is returned to pending without penalty.
                _unclaim_for_budget(conn, job, _safe_error(exc))
                return calls_made, f"halted: {_safe_error(exc)}"
            if isinstance(exc, ModelCallPermanentError):
                _park_job(conn, job, _safe_error(exc), "model_call_rejected")
                continue
            _retry_or_park(conn, job, _safe_error(exc), "model_call_failed")
            retry_after = getattr(exc, "retry_after", None)
            delay = _transient_backoff_seconds(job.attempts, retry_after)
            if delay and not budget.exhausted():
                time.sleep(min(delay, budget.remaining_wall()))
            continue

        with transaction(conn):
            conn.execute(
                "UPDATE model_calls SET outcome='succeeded',input_tokens=?,"
                "output_tokens=?,ended=? WHERE id=?",
                (response.input_tokens, response.output_tokens, now(), call_id),
            )
        _progress(
            f"call {budget.calls}/{budget.max_calls} job={job.id} kind={job.kind} "
            f"tier={tier} attempt={job.attempts} ok"
        )

        if job.kind == "critic_review":
            try:
                verdict = parse_critic_response(response.text)
                route_critic_verdict(conn, job.id, verdict)
            except ValidationError as exc:
                _mark_call_rejected(conn, call_id, exc)
                _retry_or_park(conn, job, _safe_error(exc), "critic_output_rejected")
        else:
            try:
                envelope, confidence = parse_worker_response(job.kind, response.text, job.payload)
                _handle_worker_success(conn, job, envelope, confidence)
            except ValidationError as exc:
                _mark_call_rejected(conn, call_id, exc)
                promoted = next_tier(job.tier)
                if promoted is not None:
                    with transaction(conn):
                        conn.execute(
                            "UPDATE jobs SET tier=?,status='pending',last_error=?,"
                            "lease_owner=NULL,lease_expires=NULL,updated=? WHERE id=?",
                            (promoted, _safe_error(exc), now(), job.id),
                        )
                        log_event(conn, job.id, "invalid_output_escalated", {"to": promoted})
                else:
                    _retry_or_park(conn, job, _safe_error(exc), "worker_output_rejected")


@dataclasses.dataclass(frozen=True)
class RunSummary:
    run_id: str
    stop_reason: str
    calls_used: int
    input_chars: int
    status_counts: dict[str, int]


def run(
    client: ModelClient,
    budget: Budget | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> RunSummary:
    """Run deterministic tier batches until drained, stopped, or budget-bounded."""
    path = resolve_db_path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"database does not exist; initialize it first: {path}")
    client.preflight()
    budget = budget or Budget()
    run_id = uuid.uuid4().hex
    owner = f"{os.getpid()}:{run_id[:12]}"
    stop_reason = "unknown"
    with DispatcherLock(path, owner):
        conn = db(path, recover=True)
        try:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO runs(run_id,owner,started) VALUES (?,?,?)",
                    (run_id, owner, now()),
                )
            while True:
                _reconcile_failed_dependencies(conn)
                if stop_requested(path):
                    stop_reason = "stop file present"
                    break
                if reason := budget.exhausted():
                    stop_reason = reason
                    break
                calls_this_pass = 0
                halt_reason = None
                for tier in TIER_ORDER:
                    used, halt_reason = drain_tier(
                        conn,
                        client,
                        tier,
                        budget,
                        owner=owner,
                        db_path=path,
                        run_id=run_id,
                    )
                    calls_this_pass += used
                    if halt_reason:
                        break
                if halt_reason:
                    stop_reason = halt_reason
                    break
                pending = conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status='pending'"
                ).fetchone()[0]
                running = conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status='running'"
                ).fetchone()[0]
                if pending == 0 and running == 0:
                    parked = conn.execute(
                        "SELECT COUNT(*) FROM jobs WHERE status='needs_human'"
                    ).fetchone()[0]
                    stop_reason = (
                        f"queue drained ({parked} jobs need human review)"
                        if parked
                        else "queue drained"
                    )
                    break
                if calls_this_pass == 0:
                    # Remaining jobs are dependency-blocked; surface this as a
                    # human-visible stop rather than sleeping forever.
                    stop_reason = "no runnable jobs; inspect dependencies"
                    break
            counts = {
                row["status"]: row["count"]
                for row in conn.execute("SELECT status,COUNT(*) AS count FROM jobs GROUP BY status")
            }
            with transaction(conn):
                conn.execute(
                    "UPDATE runs SET ended=?,stop_reason=?,calls_used=?,input_chars=? "
                    "WHERE run_id=?",
                    (now(), stop_reason, budget.calls, budget.input_chars, run_id),
                )
            return RunSummary(run_id, stop_reason, budget.calls, budget.input_chars, counts)
        finally:
            conn.close()


# ------------------------------------------------------------ audit/inspection


def status_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    jobs = [
        dict(row)
        for row in conn.execute(
            "SELECT status,tier,kind,COUNT(*) AS count FROM jobs "
            "GROUP BY status,tier,kind ORDER BY status,tier,kind"
        )
    ]
    campaign = dict(conn.execute("SELECT * FROM campaign_budget WHERE id=1").fetchone())
    usage = dict(
        conn.execute(
            "SELECT COUNT(*) AS call_records,"
            "COALESCE(SUM(input_tokens),0) AS input_tokens,"
            "COALESCE(SUM(output_tokens),0) AS output_tokens,"
            "COALESCE(SUM(CASE WHEN outcome='failed' THEN 1 ELSE 0 END),0) "
            "AS failed_calls FROM model_calls"
        ).fetchone()
    )
    # Crash-orphaned reservations never resolve to succeeded/failed and are
    # never reclaimed (the conservative-budget contract), so an unattended
    # crashy deployment silently loses campaign headroom. Surface the drift
    # so the operator sees it coming instead of hitting a mystery ceiling.
    orphaned = dict(
        conn.execute(
            "SELECT COUNT(*) AS calls,COALESCE(SUM(input_chars),0) AS input_chars "
            "FROM model_calls WHERE outcome='reserved'"
        ).fetchone()
    )
    events_summary = {
        "count": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "chain_head": _event_chain_head(conn),
    }
    return {
        "jobs": jobs,
        "campaign_budget": campaign,
        "model_usage": usage,
        "orphaned_reservations": orphaned,
        "event_log": events_summary,
    }


def metrics_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compute the calibration KPIs the validation protocol reads from the ledger.

    schema_valid_rate divides usable responses by all responses received
    (succeeded + rejected_output); transport failures and crash-orphaned
    reservations are excluded from that denominator but reported alongside.
    """
    per_tier: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT tier,outcome,COUNT(*) AS count,"
        "COALESCE(SUM(input_tokens),0) AS input_tokens,"
        "COALESCE(SUM(output_tokens),0) AS output_tokens "
        "FROM model_calls GROUP BY tier,outcome"
    ):
        tier = per_tier.setdefault(
            row["tier"],
            {
                "calls": 0,
                "outcomes": {},
                "input_tokens": 0,
                "output_tokens": 0,
                "schema_valid_rate": None,
            },
        )
        tier["calls"] += row["count"]
        tier["outcomes"][row["outcome"]] = row["count"]
        tier["input_tokens"] += row["input_tokens"]
        tier["output_tokens"] += row["output_tokens"]
    for tier in per_tier.values():
        succeeded = tier["outcomes"].get("succeeded", 0)
        rejected = tier["outcomes"].get("rejected_output", 0)
        if succeeded + rejected:
            tier["schema_valid_rate"] = succeeded / (succeeded + rejected)

    committed_workers = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='committed' AND kind!='critic_review'"
    ).fetchone()[0]
    total_calls = conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
    escalations = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event IN ('escalated','invalid_output_escalated')"
    ).fetchone()[0]
    rework = dict(
        conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN status='committed' THEN 1 ELSE 0 END),0) "
            "AS committed,"
            "COALESCE(SUM(CASE WHEN status='needs_human' THEN 1 ELSE 0 END),0) "
            "AS needs_human FROM jobs WHERE rework_count>0 AND kind!='critic_review'"
        ).fetchone()
    )
    statuses = {
        row["status"]: row["count"]
        for row in conn.execute("SELECT status,COUNT(*) AS count FROM jobs GROUP BY status")
    }
    return {
        "per_tier": per_tier,
        "job_statuses": statuses,
        "committed_workers": committed_workers,
        "calls_per_committed_worker": (
            total_calls / committed_workers if committed_workers else None
        ),
        "escalations": escalations,
        "rework_lineages": rework,
    }


def backup_database(
    conn: sqlite3.Connection,
    out_path: str | os.PathLike[str],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Copy the database with VACUUM INTO and verify the copy before trusting it.

    Verification: integrity_check, matching user_version, matching jobs/events
    row counts, and a matching event-chain head. A backup that cannot be read
    back faithfully is worse than none, so any mismatch raises.
    """
    target = Path(out_path).expanduser().resolve(strict=False)
    source = resolve_db_path(conn.execute("PRAGMA database_list").fetchone()[2])
    if target == source:
        raise DispatchError("backup target must differ from the live database path")
    if target.exists():
        if not force:
            raise DispatchError(f"backup target exists; pass --force to replace: {target}")
        target.unlink()
    if conn.in_transaction:
        raise DispatchError("cannot back up inside an open transaction")
    conn.execute("VACUUM INTO ?", (str(target),))
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)
    copy = sqlite3.connect(str(target))
    copy.row_factory = sqlite3.Row
    try:
        integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
        checks = {
            "integrity_check": integrity,
            "user_version_match": (
                copy.execute("PRAGMA user_version").fetchone()[0]
                == conn.execute("PRAGMA user_version").fetchone()[0]
            ),
            "jobs_count_match": (
                copy.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                == conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            ),
            "events_count_match": (
                copy.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                == conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            ),
            "chain_head_match": _event_chain_head(copy) == _event_chain_head(conn),
        }
    finally:
        copy.close()
    if integrity != "ok" or not all(
        value is True for key, value in checks.items() if key != "integrity_check"
    ):
        with contextlib.suppress(OSError):
            target.unlink()
        raise DispatchError(f"backup verification failed: {checks}")
    return {"backup": str(target), "checks": checks, "chain_head": _event_chain_head(conn)}


def audit_database(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return invariant violations. An empty list is a clean audit."""
    violations: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM jobs ORDER BY id"):
        job_id = row["id"]
        if row["kind"] not in KIND_START_TIER:
            violations.append({"job_id": job_id, "rule": "known_kind"})
        if row["tier"] not in TIERS:
            violations.append({"job_id": job_id, "rule": "known_tier"})
        if row["status"] not in VALID_STATUSES:
            violations.append({"job_id": job_id, "rule": "known_status"})
        if row["attempts"] < 0 or row["attempts"] > MAX_ATTEMPTS_PER_JOB:
            violations.append({"job_id": job_id, "rule": "attempt_bounds"})
        if row["result"] is not None and row["result_hash"] != sha256_text(row["result"]):
            violations.append({"job_id": job_id, "rule": "result_hash"})
        if row["status"] == "committed" and row["kind"] != "critic_review":
            try:
                payload = json.loads(row["payload"])
                _envelope, parsed_confidence = parse_worker_response(
                    row["kind"], row["result"], payload
                )
                stored_confidence = _finite_number(row["confidence"], "stored confidence", 0, 1)
                if stored_confidence != parsed_confidence:
                    raise ValidationError("stored confidence does not match result")
                if row["kind"] not in CRITIC_GATED_KINDS and parsed_confidence < CONFIDENCE_FLOOR:
                    raise ValidationError("ungated committed confidence below floor")
            except (
                ValidationError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ):
                violations.append({"job_id": job_id, "rule": "committed_worker_schema"})
        if row["kind"] == "critic_review":
            parent = conn.execute("SELECT * FROM jobs WHERE id=?", (row["parent_id"],)).fetchone()
            if parent is None or parent["kind"] not in CRITIC_GATED_KINDS:
                violations.append({"job_id": job_id, "rule": "critic_parent"})
            if row["tier"] != "T3_critic":
                violations.append({"job_id": job_id, "rule": "critic_tier"})
            try:
                _validate_critic_link(
                    conn,
                    row["parent_id"],
                    json.loads(row["payload"]),
                    require_awaiting=False,
                )
            except (ValidationError, json.JSONDecodeError, TypeError):
                violations.append({"job_id": job_id, "rule": "critic_payload_binding"})
            if row["status"] == "committed":
                try:
                    parse_critic_response(row["result"])
                except (ValidationError, TypeError):
                    violations.append({"job_id": job_id, "rule": "committed_critic_schema"})
        if row["status"] == "committed" and row["kind"] in CRITIC_GATED_KINDS:
            try:
                review = parse_critic_response(row["review_result"])
                risk = review["RISK_SCORE"]
                if not (
                    (review["VERDICT"] == "PASS" and risk < 0.3)
                    or (review["VERDICT"] == "CONDITIONAL_PASS" and risk < 0.5)
                ):
                    raise ValidationError("non-passing review")
            except (ValidationError, TypeError):
                violations.append({"job_id": job_id, "rule": "critic_gate_before_commit"})
            matching_critic = conn.execute(
                "SELECT 1 FROM jobs WHERE parent_id=? AND kind='critic_review' "
                "AND status='committed' AND result=?",
                (job_id, row["review_result"]),
            ).fetchone()
            if not matching_critic:
                violations.append({"job_id": job_id, "rule": "committed_critic_required"})
        if row["status"] == "awaiting_review":
            active = conn.execute(
                "SELECT 1 FROM jobs WHERE parent_id=? AND kind='critic_review' "
                "AND status IN ('pending','running')",
                (job_id,),
            ).fetchone()
            if not active:
                violations.append({"job_id": job_id, "rule": "active_critic_required"})
        if row["status"] == "running" and (not row["lease_owner"] or row["lease_expires"] is None):
            violations.append({"job_id": job_id, "rule": "running_requires_lease"})
    for row in conn.execute(
        "SELECT d.job_id,d.depends_on_id FROM job_dependencies d "
        "LEFT JOIN jobs j ON j.id=d.job_id LEFT JOIN jobs u ON u.id=d.depends_on_id "
        "WHERE j.id IS NULL OR u.id IS NULL OR d.depends_on_id>=d.job_id"
    ):
        violations.append({"job_id": row["job_id"], "rule": "dependency_dag"})
    campaign = conn.execute("SELECT * FROM campaign_budget WHERE id=1").fetchone()
    if (
        campaign["calls_reserved"] < 0
        or campaign["input_chars_reserved"] < 0
        or campaign["calls_reserved"] > campaign["max_calls"]
        or campaign["input_chars_reserved"] > campaign["max_input_chars"]
    ):
        violations.append({"job_id": None, "rule": "campaign_budget_bounds"})
    call_records = conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
    if call_records > campaign["calls_reserved"]:
        violations.append({"job_id": None, "rule": "model_call_ledger_bounds"})
    for call in conn.execute("SELECT * FROM model_calls"):
        if call["outcome"] not in {"reserved", "succeeded", "failed", "rejected_output"}:
            violations.append({"job_id": call["job_id"], "rule": "model_call_outcome"})
        if (
            call["input_chars"] < 0
            or (call["input_tokens"] is not None and call["input_tokens"] < 0)
            or (call["output_tokens"] is not None and call["output_tokens"] < 0)
        ):
            violations.append({"job_id": call["job_id"], "rule": "model_call_usage"})
    expected_prev = EVENT_CHAIN_GENESIS
    for event_row in conn.execute(
        "SELECT id,job_id,event,detail,created,prev_hash,row_hash FROM events ORDER BY id"
    ):
        recomputed = _event_row_hash(
            expected_prev,
            event_row["job_id"],
            event_row["event"],
            event_row["detail"],
            event_row["created"],
        )
        if event_row["prev_hash"] != expected_prev or event_row["row_hash"] != recomputed:
            violations.append({"job_id": event_row["job_id"], "rule": "event_chain"})
            # Resynchronize on the stored hash so one edit yields one
            # violation instead of cascading over every later row.
            expected_prev = event_row["row_hash"] or recomputed
        else:
            expected_prev = recomputed
    return violations


def export_committed(
    conn: sqlite3.Connection,
    *,
    allow_dirty: bool = False,
) -> list[dict[str, Any]]:
    """Return committed worker results as export records.

    Publishing is the highest-consequence boundary, so the export path
    re-audits the database first and refuses to emit from a database that
    violates its own invariants unless explicitly overridden.
    """
    violations = audit_database(conn)
    if violations and not allow_dirty:
        raise ValidationError(
            f"refusing to export from a database with {len(violations)} audit violations"
        )
    records: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT * FROM jobs WHERE status='committed' AND kind!='critic_review' ORDER BY id"
    ):
        payload = json.loads(row["payload"])
        review = json.loads(row["review_result"]) if row["review_result"] else None
        try:
            result = json.loads(row["result"])["RESULT"]
        except (TypeError, ValueError, KeyError) as error:
            if not allow_dirty:  # pragma: no cover - audit refuses first
                raise ValidationError(
                    f"committed job {row['id']} has a malformed result"
                ) from error
            result = None
        records.append(
            {
                "job_id": row["id"],
                "kind": row["kind"],
                "root_id": row["root_id"],
                "rework_count": row["rework_count"],
                "task": payload.get("task"),
                "confidence": row["confidence"],
                "result": result,
                "result_hash": row["result_hash"],
                "critic_review": review,
                "audit_clean": not violations,
            }
        )
    return records


# ----------------------------------------------------------------------- CLI


def _json_print(value: Any) -> None:
    if dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def _payload_from_args(args: argparse.Namespace) -> dict[str, Any]:
    raw = Path(args.payload_file).read_text(encoding="utf-8") if args.payload_file else args.payload
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValidationError("payload must be a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="initialize/migrate the database")
    enqueue_parser = sub.add_parser("enqueue", help="enqueue one validated worker job")
    enqueue_parser.add_argument(
        "--kind", choices=sorted(KIND_START_TIER.keys() - {"critic_review"}), required=True
    )
    source = enqueue_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--payload", help="payload JSON object")
    source.add_argument("--payload-file", help="UTF-8 JSON payload file")
    enqueue_parser.add_argument("--idempotency-key")
    enqueue_parser.add_argument("--depends-on", type=int, action="append", default=[])

    run_parser = sub.add_parser("run", help="run until bounded stop")
    run_parser.add_argument("--provider", choices=("anthropic", "openai"), required=True)
    run_parser.add_argument("--base-url")
    run_parser.add_argument("--api-key-env")
    run_parser.add_argument("--timeout", type=float, default=REQUEST_TIMEOUT_SECONDS)
    run_parser.add_argument("--max-calls", type=int, default=MAX_CALLS_PER_RUN)
    run_parser.add_argument("--max-input-chars", type=int, default=MAX_INPUT_CHARS_PER_RUN)
    run_parser.add_argument("--max-wall-seconds", type=float, default=MAX_WALL_SECONDS)
    run_parser.add_argument(
        "--verbose", action="store_true", help="print per-call progress to stderr"
    )

    sub.add_parser("status", help="show queue and persistent budget state")
    sub.add_parser("audit", help="check database invariants")
    sub.add_parser("metrics", help="show calibration KPIs from the call ledger")
    backup_parser = sub.add_parser("backup", help="copy the database and verify the copy")
    backup_parser.add_argument("--out", required=True, help="backup file path")
    backup_parser.add_argument(
        "--force", action="store_true", help="replace an existing backup file"
    )
    export_parser = sub.add_parser(
        "export", help="emit committed worker results as JSON lines after a clean audit"
    )
    export_parser.add_argument("--out", help="write JSONL here instead of stdout")
    export_parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="export despite audit violations (records are marked audit_clean=false)",
    )
    sub.add_parser("stop", help="request a graceful stop at the next call boundary")
    sub.add_parser("resume", help="remove the stop request; does not run work")
    events_parser = sub.add_parser("events", help="show append-only transition events")
    events_parser.add_argument("--job-id", type=int)
    events_parser.add_argument("--limit", type=int, default=100)
    budget_parser = sub.add_parser("budget", help="show or deliberately raise campaign ceilings")
    budget_parser.add_argument("--max-calls", type=int)
    budget_parser.add_argument("--max-input-chars", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    path = resolve_db_path(args.db)
    try:
        existing_database_commands = {
            "run",
            "status",
            "audit",
            "metrics",
            "backup",
            "stop",
            "resume",
            "events",
            "budget",
            "export",
        }
        if args.command in existing_database_commands and not path.is_file():
            raise FileNotFoundError(f"database does not exist; initialize it first: {path}")
        if args.command == "stop":
            print(request_stop(path))
            return 0
        if args.command == "resume":
            print(clear_stop(path))
            return 0

        conn = db(path)
        try:
            if args.command == "init":
                _json_print(status_snapshot(conn))
            elif args.command == "enqueue":
                job_id = enqueue(
                    conn,
                    args.kind,
                    _payload_from_args(args),
                    idempotency_key=args.idempotency_key,
                    depends_on=args.depends_on,
                )
                _json_print({"job_id": job_id})
            elif args.command == "status":
                _json_print(status_snapshot(conn))
            elif args.command == "metrics":
                _json_print(metrics_snapshot(conn))
            elif args.command == "backup":
                _json_print(backup_database(conn, args.out, force=args.force))
            elif args.command == "audit":
                violations = audit_database(conn)
                _json_print({"clean": not violations, "violations": violations})
                return 0 if not violations else 1
            elif args.command == "export":
                records = export_committed(conn, allow_dirty=args.allow_dirty)
                lines = "".join(canonical_json(record) + "\n" for record in records)
                if args.out:
                    Path(args.out).write_text(lines, encoding="utf-8")
                    print(json.dumps({"exported": len(records), "out": args.out}), file=sys.stderr)
                else:
                    sys.stdout.write(lines)
            elif args.command == "events":
                limit = max(1, min(args.limit, 10_000))
                if args.job_id is None:
                    rows = conn.execute(
                        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM events WHERE job_id=? ORDER BY id DESC LIMIT ?",
                        (args.job_id, limit),
                    ).fetchall()
                _json_print([dict(row) for row in rows])
            elif args.command == "budget":
                with transaction(conn):
                    current = conn.execute("SELECT * FROM campaign_budget WHERE id=1").fetchone()
                    max_calls = current["max_calls"] if args.max_calls is None else args.max_calls
                    max_chars = (
                        current["max_input_chars"]
                        if args.max_input_chars is None
                        else args.max_input_chars
                    )
                    if (
                        max_calls < current["calls_reserved"]
                        or max_chars < current["input_chars_reserved"]
                    ):
                        raise ValidationError("new ceilings cannot be below already-reserved usage")
                    conn.execute(
                        "UPDATE campaign_budget SET max_calls=?,max_input_chars=?,"
                        "updated=? WHERE id=1",
                        (max_calls, max_chars, now()),
                    )
                _json_print(
                    dict(conn.execute("SELECT * FROM campaign_budget WHERE id=1").fetchone())
                )
            elif args.command == "run":
                conn.close()
                global VERBOSE
                VERBOSE = bool(args.verbose)
                base_url = args.base_url or (
                    "https://api.anthropic.com"
                    if args.provider == "anthropic"
                    else "http://127.0.0.1:8000"
                )
                key_env = args.api_key_env
                if key_env is None:
                    key_env = (
                        "ANTHROPIC_API_KEY" if args.provider == "anthropic" else "OPENAI_API_KEY"
                    )
                client = HttpModelClient(args.provider, base_url, key_env, args.timeout)
                summary = run(
                    client,
                    Budget(args.max_calls, args.max_input_chars, args.max_wall_seconds),
                    path,
                )
                _json_print(summary)
            return 0
        finally:
            with contextlib.suppress(Exception):
                conn.close()
    except (DispatchError, FileNotFoundError, json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
