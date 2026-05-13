"""Per-request audit trail — independent of the main application log.

Two side-by-side files under ``audit.log_dir`` (default = the regular log dir):

    audit.log     human-readable one-line-per-request summary
    audit.jsonl   newline-delimited JSON for SIEM ingestion

Audit emission is decoupled from the main logger:
    • Always-on at info equivalent regardless of ``logging.level``.
    • Different rotation lifetime via ``audit.{max_bytes, backup_count}``.
    • Distinct handlers so an operator can pipe audit alone to a separate
      collector without touching application log routing.

Usage:

    from src.audit_log import AuditRecord, get_audit_logger

    record = AuditRecord.start(identity={"sub": "alice", "roles": [...]},
                               query="show me daily revenue")
    # ... attach fields as the NLQ flows ...
    record.allowed_views = ["finance_daily_revenue_view"]
    record.executed_sql = "SELECT ..."
    record.rows_returned = 10
    record.status = "ok"
    record.finish()
    get_audit_logger().emit(record)
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


# --------------------------------------------------------------------------- #
# AuditRecord                                                                 #
# --------------------------------------------------------------------------- #


@dataclass
class AuditRecord:
    """One NLQ's audit trail. Accumulated through the request lifecycle."""

    request_id:           str
    ts_request:           str          # ISO 8601 UTC at request start
    ts_completed:         Optional[str] = None
    duration_ms:          Optional[int] = None

    # Identity (what the JWT said)
    sub:                  Optional[str] = None
    email:                Optional[str] = None
    preferred_username:   Optional[str] = None
    roles:                List[str] = field(default_factory=list)
    auth_provider:        Optional[str] = None
    unmask_pii:           bool = False
    admin_bypass:         bool = False

    # What was asked
    query:                str = ""

    # What the catalog returned (the trust decision)
    allowed_views:        List[str] = field(default_factory=list)
    relevant_views:       List[str] = field(default_factory=list)
    pii_columns:          Dict[str, List[str]] = field(default_factory=dict)

    # What the LLM produced + validator did
    executed_sql:         Optional[str] = None
    validation_attempts:  int = 0
    validation_errors:    List[str] = field(default_factory=list)

    # What MySQL gave back
    rows_returned:        Optional[int] = None
    pii_columns_masked:   List[str] = field(default_factory=list)

    # Outcome
    status:               str = "in_progress"   # "ok" | "error" | "denied" | "in_progress"
    error_message:        Optional[str] = None

    # private — for duration calculation
    _started_at_monotonic: float = field(default_factory=lambda: time.monotonic(), repr=False)

    # ---------------------------------------------------- factory / lifecycle #

    @classmethod
    def start(cls, *, identity: Optional[Dict[str, Any]] = None, query: str = "") -> "AuditRecord":
        identity = identity or {}
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        return cls(
            request_id=str(uuid.uuid4()),
            ts_request=now,
            sub=identity.get("sub"),
            email=identity.get("email"),
            preferred_username=identity.get("preferred_username"),
            roles=list(identity.get("roles") or []),
            auth_provider=identity.get("provider"),
            query=query,
        )

    def finish(self, status: Optional[str] = None, error: Optional[str] = None) -> None:
        self.ts_completed = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self.duration_ms = int((time.monotonic() - self._started_at_monotonic) * 1000)
        if status:
            self.status = status
        if error and not self.error_message:
            self.error_message = error

    # --------------------------------------------------------- serialization #

    def to_jsonl(self) -> str:
        d = asdict(self)
        d.pop("_started_at_monotonic", None)
        return json.dumps(d, ensure_ascii=False, default=str)

    def to_human(self) -> str:
        """One-line human-readable summary."""
        who = self.preferred_username or self.sub or "(anonymous)"
        roles = ",".join(self.roles) or "-"
        status_part = self.status.upper()
        if self.error_message:
            status_part += f" {self.error_message[:120]!r}"
        return (
            f"{self.ts_request} | id={self.request_id[:8]} "
            f"| user={who} roles=[{roles}] provider={self.auth_provider or '?'} "
            f"| query={self.query!r}"
            f" | allowed={len(self.allowed_views)} prompt_top_k={len(self.relevant_views)}"
            f" | sql_attempts={self.validation_attempts} rows={self.rows_returned}"
            f" | masked_pii={','.join(self.pii_columns_masked) or '-'} unmask={self.unmask_pii}"
            f" | dur_ms={self.duration_ms} status={status_part}"
        )


# --------------------------------------------------------------------------- #
# Sinks                                                                       #
# --------------------------------------------------------------------------- #


class _AuditSinks:
    """Holds the two file handlers. Thread-safe append-and-flush."""

    def __init__(self, human_path: Path, jsonl_path: Path, max_bytes: int, backup_count: int):
        # We deliberately use RotatingFileHandler indirectly: it formats via the
        # stdlib logging stack, which is overkill here. Roll our own minimal
        # rotation to keep the audit lines uncluttered.
        human_path.parent.mkdir(parents=True, exist_ok=True)
        self._human = logging.handlers.RotatingFileHandler(
            filename=human_path, maxBytes=max_bytes, backupCount=backup_count,
            encoding="utf-8",
        )
        self._jsonl = logging.handlers.RotatingFileHandler(
            filename=jsonl_path, maxBytes=max_bytes, backupCount=backup_count,
            encoding="utf-8",
        )
        # No prefix formatter — we already format the line content ourselves.
        plain = logging.Formatter(fmt="%(message)s")
        self._human.setFormatter(plain)
        self._jsonl.setFormatter(plain)
        # Wrap each handler in a dedicated logger so RotatingFileHandler can
        # do its rotation bookkeeping but we still bypass any inherited
        # formatters from root.
        self._human_logger = logging.getLogger(f"_audit.human.{id(self)}")
        self._human_logger.propagate = False
        self._human_logger.setLevel(logging.INFO)
        self._human_logger.addHandler(self._human)

        self._jsonl_logger = logging.getLogger(f"_audit.jsonl.{id(self)}")
        self._jsonl_logger.propagate = False
        self._jsonl_logger.setLevel(logging.INFO)
        self._jsonl_logger.addHandler(self._jsonl)

        self._lock = Lock()

    def emit(self, record: AuditRecord) -> None:
        with self._lock:
            self._human_logger.info(record.to_human())
            self._jsonl_logger.info(record.to_jsonl())


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


class AuditLogger:
    """Process-wide audit logger. ``configure(settings)`` wires the sinks."""

    def __init__(self) -> None:
        self._sinks: Optional[_AuditSinks] = None
        self._enabled: bool = False
        self._fallback_log = logging.getLogger("eunomia.audit_log")

    def configure(self, settings: "Settings") -> None:
        cfg = settings.audit
        self._enabled = bool(cfg.enabled)
        if not self._enabled:
            self._sinks = None
            return

        log_dir = Path(cfg.log_dir) if cfg.log_dir else None
        if log_dir is None:
            # Fall through to the main logger's directory so the operator can
            # ship both via one log shipper if desired.
            main_log_dir = settings.logging.log_dir
            log_dir = Path(main_log_dir)
        # Resolve relative paths against the middleware project root.
        if not log_dir.is_absolute():
            project_root = Path(__file__).resolve().parent.parent
            log_dir = project_root / log_dir

        self._sinks = _AuditSinks(
            human_path=log_dir / cfg.human_file,
            jsonl_path=log_dir / cfg.jsonl_file,
            max_bytes=cfg.max_bytes,
            backup_count=cfg.backup_count,
        )
        self._fallback_log.info(
            "AuditLogger configured: %s/%s + %s",
            log_dir, cfg.human_file, cfg.jsonl_file,
        )

    def emit(self, record: AuditRecord) -> None:
        if not self._enabled:
            return
        if self._sinks is None:
            # If someone calls emit() before configure(), fall through to the
            # main logger at WARNING so the trail is at least preserved.
            self._fallback_log.warning(
                "AuditLogger.emit() before configure(); writing to main log: %s",
                record.to_jsonl(),
            )
            return
        try:
            self._sinks.emit(record)
        except Exception:
            # Never let audit writes fail the request.
            self._fallback_log.exception("Audit emit failed; record=%r", record.request_id)


_audit_logger = AuditLogger()


def configure_audit_logger(settings: "Settings") -> None:
    """Wire the global audit logger. Safe to call multiple times."""
    _audit_logger.configure(settings)


def get_audit_logger() -> AuditLogger:
    return _audit_logger


def reset_audit_logger() -> None:
    """Test helper — drops sinks so the next configure() rebuilds them."""
    global _audit_logger
    _audit_logger = AuditLogger()
