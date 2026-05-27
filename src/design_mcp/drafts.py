"""PG-backed draft store for the return-prompts pattern.

A DraftRecord tracks the lifecycle of a single design from the moment
`design_landing_page` / `design_survey_funnel` hands the caller a brief,
through the caller's Claude generating the HTML, until `submit_design`
validates + commits, or `cancel_design` voids it.

State lives in `design_mcp_drafts` on DO PG 17 (table created by
`migrations/003_drafts.sql`). Every row carries `user_email` so cross-user
reads/modifications are impossible at the data layer.

Tests substitute an in-memory backend via `set_backend(...)` so they don't
need a live PG instance — see `_InMemoryBackend` and `_reset_for_tests`.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol

from .db import get_conn

VALID_STATUSES = {"drafted", "submitting", "submitted", "published", "failed", "cancelled", "expired"}
DEFAULT_TTL = timedelta(hours=24)

# Hard cap on persisted last_error length — avoid bloating the row with
# multi-megabyte tracebacks. Truncation happens in set_last_error.
LAST_ERROR_MAX_CHARS = 2000


@dataclass
class DraftRecord:
    design_id: str
    user_email: str
    family: str
    brief: str
    slug_hint: str
    status: str  # one of VALID_STATUSES
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    iteration_log: list[dict] = field(default_factory=list)
    slug: Optional[str] = None
    html: Optional[str] = None
    manifest: Optional[dict] = None
    chat_summary: Optional[str] = None
    published_repo_sha: Optional[str] = None
    commit_sha: Optional[str] = None
    design_dir: Optional[str] = None
    last_error: Optional[str] = None

    # Legacy alias — older code (and tests) called this `history`.
    @property
    def history(self) -> list[dict]:
        return self.iteration_log

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["updated_at"] = self.updated_at.isoformat()
        d["expires_at"] = self.expires_at.isoformat()
        return d


# ---------------------------------------------------------------------------
# Backend protocol — PG by default, in-memory for tests
# ---------------------------------------------------------------------------

class _Backend(Protocol):
    def insert(self, record: DraftRecord) -> None: ...
    def select_by_id(self, design_id: str) -> Optional[DraftRecord]: ...
    def update(self, record: DraftRecord) -> None: ...
    def select_expired_active(self, now: datetime) -> list[DraftRecord]: ...


class _PgBackend:
    """PostgreSQL-backed store. All queries parameterised."""

    _COLUMNS = (
        "design_id, user_email, family, brief, slug_hint, status, "
        "iteration_log, slug, html, manifest, chat_summary, "
        "published_repo_sha, commit_sha, design_dir, last_error, "
        "created_at, updated_at, expires_at"
    )

    def _row_to_record(self, row: dict) -> DraftRecord:
        return DraftRecord(
            design_id=str(row["design_id"]),
            user_email=row["user_email"],
            family=row["family"],
            brief=row["brief"],
            slug_hint=row["slug_hint"] or "",
            status=row["status"],
            iteration_log=row["iteration_log"] or [],
            slug=row.get("slug"),
            html=row.get("html"),
            manifest=row.get("manifest"),
            chat_summary=row.get("chat_summary"),
            published_repo_sha=row.get("published_repo_sha"),
            commit_sha=row.get("commit_sha"),
            design_dir=row.get("design_dir"),
            last_error=row.get("last_error"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )

    def insert(self, record: DraftRecord) -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO design_mcp_drafts
                    (design_id, user_email, family, brief, slug_hint, status,
                     iteration_log, slug, html, manifest, chat_summary,
                     published_repo_sha, commit_sha, design_dir, last_error,
                     created_at, updated_at, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s, %s, %s::jsonb, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s)
                """,
                (
                    record.design_id,
                    record.user_email,
                    record.family,
                    record.brief,
                    record.slug_hint,
                    record.status,
                    json.dumps(record.iteration_log),
                    record.slug,
                    record.html,
                    json.dumps(record.manifest) if record.manifest is not None else None,
                    record.chat_summary,
                    record.published_repo_sha,
                    record.commit_sha,
                    record.design_dir,
                    record.last_error,
                    record.created_at,
                    record.updated_at,
                    record.expires_at,
                ),
            )

    def select_by_id(self, design_id: str) -> Optional[DraftRecord]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._COLUMNS} FROM design_mcp_drafts WHERE design_id = %s",
                (design_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return self._row_to_record(row)

    def update(self, record: DraftRecord) -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE design_mcp_drafts
                   SET status = %s,
                       iteration_log = %s::jsonb,
                       slug = %s,
                       html = %s,
                       manifest = %s::jsonb,
                       chat_summary = %s,
                       published_repo_sha = %s,
                       commit_sha = %s,
                       design_dir = %s,
                       last_error = %s,
                       updated_at = %s,
                       expires_at = %s
                 WHERE design_id = %s AND user_email = %s
                """,
                (
                    record.status,
                    json.dumps(record.iteration_log),
                    record.slug,
                    record.html,
                    json.dumps(record.manifest) if record.manifest is not None else None,
                    record.chat_summary,
                    record.published_repo_sha,
                    record.commit_sha,
                    record.design_dir,
                    record.last_error,
                    record.updated_at,
                    record.expires_at,
                    record.design_id,
                    record.user_email,
                ),
            )

    def select_expired_active(self, now: datetime) -> list[DraftRecord]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {self._COLUMNS}
                  FROM design_mcp_drafts
                 WHERE status IN ('drafted', 'submitted')
                   AND expires_at <= %s
                """,
                (now,),
            )
            rows = cur.fetchall()
        return [self._row_to_record(r) for r in rows]


class _InMemoryBackend:
    """Test backend — same surface as _PgBackend, dict-of-records storage."""

    def __init__(self) -> None:
        self._rows: dict[str, DraftRecord] = {}

    def insert(self, record: DraftRecord) -> None:
        # Store a copy so callers mutating the returned record don't bleed in.
        self._rows[record.design_id] = _clone(record)

    def select_by_id(self, design_id: str) -> Optional[DraftRecord]:
        row = self._rows.get(design_id)
        return _clone(row) if row else None

    def update(self, record: DraftRecord) -> None:
        if record.design_id not in self._rows:
            return
        existing = self._rows[record.design_id]
        if existing.user_email != record.user_email:
            # user mismatch — silently no-op (PG WHERE clause also rejects)
            return
        self._rows[record.design_id] = _clone(record)

    def select_expired_active(self, now: datetime) -> list[DraftRecord]:
        return [
            _clone(r) for r in self._rows.values()
            if r.status in {"drafted", "submitted"} and r.expires_at <= now
        ]

    def _all(self) -> list[DraftRecord]:
        return [_clone(r) for r in self._rows.values()]

    def _clear(self) -> None:
        self._rows.clear()


def _clone(record: DraftRecord) -> DraftRecord:
    return DraftRecord(
        design_id=record.design_id,
        user_email=record.user_email,
        family=record.family,
        brief=record.brief,
        slug_hint=record.slug_hint,
        status=record.status,
        iteration_log=list(record.iteration_log),
        slug=record.slug,
        html=record.html,
        manifest=dict(record.manifest) if record.manifest is not None else None,
        chat_summary=record.chat_summary,
        published_repo_sha=record.published_repo_sha,
        commit_sha=record.commit_sha,
        design_dir=record.design_dir,
        last_error=record.last_error,
        created_at=record.created_at,
        updated_at=record.updated_at,
        expires_at=record.expires_at,
    )


_backend: _Backend = _PgBackend()


def set_backend(backend: _Backend) -> None:
    """Swap the active backend. Used by tests."""
    global _backend
    _backend = backend


def get_backend() -> _Backend:
    return _backend


# ---------------------------------------------------------------------------
# Public API — every mutating call is scoped by user_email
# ---------------------------------------------------------------------------

def create(
    user_email: str,
    family: str,
    brief: str,
    slug_hint: str,
    ttl: timedelta = DEFAULT_TTL,
) -> DraftRecord:
    """Allocate a new design_id and persist a fresh DraftRecord for user_email."""
    if not user_email:
        raise ValueError("user_email is required")
    now = datetime.now(timezone.utc)
    record = DraftRecord(
        design_id=str(uuid.uuid4()),
        user_email=user_email,
        family=family,
        brief=brief,
        slug_hint=slug_hint or "",
        status="drafted",
        iteration_log=[{"at": now.isoformat(), "event": "created", "status": "drafted"}],
        created_at=now,
        updated_at=now,
        expires_at=now + ttl,
    )
    _backend.insert(record)
    return record


def get(design_id: str, user_email: str) -> Optional[DraftRecord]:
    """Return the DraftRecord for (design_id, user_email).

    Returns None if the design_id doesn't exist OR if it exists but is owned
    by a different user — callers can't distinguish these cases (deliberate;
    avoids leaking ownership information).
    """
    record = _backend.select_by_id(design_id)
    if record is None:
        return None
    if record.user_email != user_email:
        return None
    return record


def update(design_id: str, user_email: str, **changes: Any) -> DraftRecord:
    """Mutate fields on an existing record owned by user_email. Validates status."""
    record = get(design_id, user_email)
    if record is None:
        raise KeyError(
            f"design_id {design_id!r} not found or not owned by this user"
        )
    if "status" in changes and changes["status"] not in VALID_STATUSES:
        raise ValueError(
            f"invalid status {changes['status']!r}; "
            f"must be one of {sorted(VALID_STATUSES)}"
        )
    for key, value in changes.items():
        if not hasattr(record, key) or key in {"design_id", "user_email", "created_at"}:
            raise AttributeError(f"DraftRecord field {key!r} is read-only or unknown")
        setattr(record, key, value)
    record.updated_at = datetime.now(timezone.utc)
    record.iteration_log = list(record.iteration_log) + [{
        "at": record.updated_at.isoformat(),
        "event": "updated",
        "fields": sorted(changes.keys()),
        "status": record.status,
    }]
    _backend.update(record)
    return record


def set_status(design_id: str, user_email: str, status: str) -> None:
    """Shortcut for `update(design_id, user_email, status=status)`."""
    if status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status {status!r}; must be one of {sorted(VALID_STATUSES)}"
        )
    update(design_id, user_email, status=status)


def set_last_error(design_id: str, user_email: str, error: Optional[str]) -> None:
    """Persist (or clear, when ``error`` is None) the ``last_error`` field.

    Truncates to LAST_ERROR_MAX_CHARS to keep the row bounded. Idempotent:
    if the stored value already equals the (truncated) incoming value, this
    is a no-op and the iteration_log is not extended.
    """
    record = get(design_id, user_email)
    if record is None:
        raise KeyError(
            f"design_id {design_id!r} not found or not owned by this user"
        )
    truncated: Optional[str]
    if error is None:
        truncated = None
    else:
        truncated = error if len(error) <= LAST_ERROR_MAX_CHARS else error[:LAST_ERROR_MAX_CHARS]
    if record.last_error == truncated:
        return
    update(design_id, user_email, last_error=truncated)


def record_submission(
    design_id: str,
    user_email: str,
    html: str,
    manifest: dict,
    chat_summary: str,
    slug: Optional[str] = None,
) -> DraftRecord:
    """Persist a submit_design payload without publishing (status=submitted)."""
    return update(
        design_id,
        user_email,
        status="submitted",
        slug=slug,
        html=html,
        manifest=manifest,
        chat_summary=chat_summary,
        last_error=None,
    )


def mark_published(
    design_id: str,
    user_email: str,
    repo_sha: str,
    design_dir: Optional[str] = None,
) -> DraftRecord:
    """Flip a submitted draft to published once the git commit lands."""
    return update(
        design_id,
        user_email,
        status="published",
        published_repo_sha=repo_sha,
        commit_sha=repo_sha,
        design_dir=design_dir,
        last_error=None,
    )


def cleanup_expired() -> int:
    """Mark records past expires_at as expired. System call — no user_email."""
    now = datetime.now(timezone.utc)
    flipped = 0
    for record in _backend.select_expired_active(now):
        record.status = "expired"
        record.updated_at = now
        record.iteration_log = list(record.iteration_log) + [{
            "at": now.isoformat(),
            "event": "auto-expired",
            "status": "expired",
        }]
        _backend.update(record)
        flipped += 1
    return flipped


def get_draft_html(design_id: str) -> Optional[tuple[str, str]]:
    """Return ``(html, user_email)`` for a draft, bypassing user-scope checks.

    Intended ONLY for the signed-URL preview route in ``server.py``: the
    signature itself is the authorisation, so the route doesn't have a
    bearer token / context user to compare against. Every other code path
    MUST go through ``get(design_id, user_email)``.

    Returns ``None`` when the design_id is unknown OR when the draft has
    no html column yet (i.e. ``submit_design`` hasn't been called).
    """
    record = _backend.select_by_id(design_id)
    if record is None or not record.html:
        return None
    return record.html, record.user_email


def _reset_for_tests() -> None:
    """Test-only: install a fresh in-memory backend."""
    set_backend(_InMemoryBackend())
