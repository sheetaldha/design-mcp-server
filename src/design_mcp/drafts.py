"""In-memory draft store for the return-prompts pattern.

A DraftRecord tracks the lifecycle of a single design from the moment
`design_landing_page` (or `design_survey_funnel`, when Agent 2 adds it)
hands the caller a brief, through the caller's Claude generating the
HTML, until `submit_design` validates + commits, or `cancel_design`
voids it.

Module-level dict + a single threading.Lock. Process-local — fine for
stdio MCP and adequate for the Day 5 HTTP transport on a single PM2
worker. If we ever shard, swap this for Redis.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

VALID_STATUSES = {"drafted", "submitted", "published", "cancelled", "expired"}
DEFAULT_TTL = timedelta(hours=24)


@dataclass
class DraftRecord:
    design_id: str
    family: str
    brief: str
    created_at: datetime
    expires_at: datetime
    status: str  # one of VALID_STATUSES
    slug_hint: Optional[str] = None
    slug: Optional[str] = None
    html: Optional[str] = None
    manifest: Optional[dict] = None
    chat_summary: Optional[str] = None
    commit_sha: Optional[str] = None
    design_dir: Optional[str] = None
    last_error: Optional[str] = None
    history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["expires_at"] = self.expires_at.isoformat()
        return d


_drafts: dict[str, DraftRecord] = {}
_lock = threading.Lock()


def create(
    family: str,
    brief: str,
    slug_hint: Optional[str] = None,
    ttl: timedelta = DEFAULT_TTL,
) -> DraftRecord:
    """Allocate a new design_id and persist a fresh DraftRecord."""
    now = datetime.now(timezone.utc)
    record = DraftRecord(
        design_id=str(uuid.uuid4()),
        family=family,
        brief=brief,
        created_at=now,
        expires_at=now + ttl,
        status="drafted",
        slug_hint=slug_hint,
    )
    record.history.append({"at": now.isoformat(), "event": "created", "status": "drafted"})
    with _lock:
        _drafts[record.design_id] = record
    return record


def get(design_id: str) -> DraftRecord:
    """Return the DraftRecord or raise KeyError."""
    with _lock:
        record = _drafts.get(design_id)
    if record is None:
        raise KeyError(f"design_id {design_id!r} not found")
    return record


def update(design_id: str, **changes: Any) -> DraftRecord:
    """Mutate fields on an existing record. Validates status if provided."""
    with _lock:
        record = _drafts.get(design_id)
        if record is None:
            raise KeyError(f"design_id {design_id!r} not found")
        if "status" in changes and changes["status"] not in VALID_STATUSES:
            raise ValueError(
                f"invalid status {changes['status']!r}; must be one of {sorted(VALID_STATUSES)}"
            )
        for key, value in changes.items():
            if not hasattr(record, key):
                raise AttributeError(f"DraftRecord has no field {key!r}")
            setattr(record, key, value)
        record.history.append({
            "at": datetime.now(timezone.utc).isoformat(),
            "event": "updated",
            "fields": sorted(changes.keys()),
            "status": record.status,
        })
        return record


def set_status(design_id: str, status: str) -> None:
    """Shortcut for `update(design_id, status=status)`."""
    if status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status {status!r}; must be one of {sorted(VALID_STATUSES)}"
        )
    update(design_id, status=status)


def cleanup_expired() -> int:
    """Mark records past expires_at as expired. Returns count flipped.

    Intentionally not auto-invoked — call from a separate cron / background
    task so we don't sprinkle wall-clock side effects through every request.
    """
    now = datetime.now(timezone.utc)
    flipped = 0
    with _lock:
        for record in _drafts.values():
            if record.status in {"drafted", "submitted"} and record.expires_at <= now:
                record.status = "expired"
                record.history.append({
                    "at": now.isoformat(),
                    "event": "auto-expired",
                    "status": "expired",
                })
                flipped += 1
    return flipped


def _reset_for_tests() -> None:
    """Test-only: wipe the in-memory store."""
    with _lock:
        _drafts.clear()
