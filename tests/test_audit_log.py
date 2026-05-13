"""Audit logging — record assembly, dual-sink emission, rotation, lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.audit_log import (
    AuditLogger,
    AuditRecord,
    configure_audit_logger,
    get_audit_logger,
    reset_audit_logger,
)
from src.config import load_settings, reset_settings_cache


# --------------------------------------------------------------------------- #
# fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def audit_settings(tmp_path, monkeypatch):
    """Settings with audit pointed at a fresh tmp dir."""
    monkeypatch.setenv("EUNOMIA_AUDIT__LOG_DIR", str(tmp_path))
    reset_settings_cache()
    reset_audit_logger()
    s = load_settings()
    assert Path(str(s.audit.log_dir)) == tmp_path
    return s


# --------------------------------------------------------------------------- #
# AuditRecord                                                                 #
# --------------------------------------------------------------------------- #


def test_audit_record_start_captures_identity():
    identity = {
        "sub": "abc-123", "email": "alice@x.com", "preferred_username": "alice",
        "roles": ["eunomia-finance-user", "eunomia-pii-unmask"],
        "provider": "keycloak",
    }
    rec = AuditRecord.start(identity=identity, query="show revenue")
    assert rec.sub == "abc-123"
    assert rec.email == "alice@x.com"
    assert rec.preferred_username == "alice"
    assert rec.roles == ["eunomia-finance-user", "eunomia-pii-unmask"]
    assert rec.auth_provider == "keycloak"
    assert rec.query == "show revenue"
    assert rec.status == "in_progress"
    # request_id is a UUID-shaped string
    assert len(rec.request_id) == 36 and rec.request_id.count("-") == 4


def test_audit_record_handles_missing_identity_gracefully():
    rec = AuditRecord.start(identity=None, query="anything")
    assert rec.sub is None
    assert rec.email is None
    assert rec.roles == []


def test_audit_record_finish_sets_duration_and_status():
    rec = AuditRecord.start(identity={"sub": "x"}, query="q")
    rec.finish(status="ok")
    assert rec.status == "ok"
    assert rec.ts_completed is not None
    assert rec.duration_ms is not None
    assert rec.duration_ms >= 0


def test_audit_record_finish_preserves_first_error():
    rec = AuditRecord.start(identity={"sub": "x"})
    rec.error_message = "first error"
    rec.finish(status="error", error="second error")
    # First-set error wins
    assert rec.error_message == "first error"


def test_audit_record_to_jsonl_is_valid_json():
    rec = AuditRecord.start(identity={"sub": "u1"}, query="q1")
    rec.allowed_views = ["v1", "v2"]
    rec.pii_columns = {"v1": ["email"]}
    rec.finish(status="ok")
    parsed = json.loads(rec.to_jsonl())
    assert parsed["sub"] == "u1"
    assert parsed["query"] == "q1"
    assert parsed["allowed_views"] == ["v1", "v2"]
    assert parsed["pii_columns"] == {"v1": ["email"]}
    assert parsed["status"] == "ok"
    # Private monotonic field is stripped
    assert "_started_at_monotonic" not in parsed


def test_audit_record_human_format_includes_key_fields():
    rec = AuditRecord.start(
        identity={"sub": "x", "preferred_username": "alice",
                   "roles": ["eunomia-finance-user"], "provider": "keycloak"},
        query="daily revenue",
    )
    rec.allowed_views = ["v1", "v2"]
    rec.relevant_views = ["v1"]
    rec.executed_sql = "SELECT * FROM v1"
    rec.rows_returned = 7
    rec.validation_attempts = 1
    rec.finish(status="ok")
    line = rec.to_human()
    assert "alice" in line
    assert "eunomia-finance-user" in line
    assert "keycloak" in line
    assert "daily revenue" in line
    assert "allowed=2" in line
    assert "prompt_top_k=1" in line
    assert "rows=7" in line
    assert "status=OK" in line


# --------------------------------------------------------------------------- #
# AuditLogger (dual-sink)                                                     #
# --------------------------------------------------------------------------- #


def test_emit_writes_to_both_files(audit_settings, tmp_path):
    configure_audit_logger(audit_settings)
    logger = get_audit_logger()

    rec = AuditRecord.start(
        identity={"sub": "u", "preferred_username": "alice",
                   "roles": ["eunomia-finance-user"], "provider": "keycloak"},
        query="q1",
    )
    rec.allowed_views = ["v1"]
    rec.executed_sql = "SELECT * FROM v1"
    rec.rows_returned = 3
    rec.finish(status="ok")
    logger.emit(rec)

    human_file = tmp_path / audit_settings.audit.human_file
    jsonl_file = tmp_path / audit_settings.audit.jsonl_file
    assert human_file.exists()
    assert jsonl_file.exists()

    human = human_file.read_text().strip().splitlines()
    jsonl = jsonl_file.read_text().strip().splitlines()
    assert len(human) == 1
    assert len(jsonl) == 1
    assert "alice" in human[0]
    parsed = json.loads(jsonl[0])
    assert parsed["preferred_username"] == "alice"
    assert parsed["status"] == "ok"


def test_emit_disabled_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_AUDIT__LOG_DIR", str(tmp_path))
    monkeypatch.setenv("EUNOMIA_AUDIT__ENABLED", "false")
    reset_settings_cache()
    reset_audit_logger()
    s = load_settings()
    assert s.audit.enabled is False
    configure_audit_logger(s)
    rec = AuditRecord.start(identity={"sub": "x"}, query="q")
    rec.finish(status="ok")
    get_audit_logger().emit(rec)
    # No files created.
    assert not (tmp_path / s.audit.human_file).exists()
    assert not (tmp_path / s.audit.jsonl_file).exists()


def test_jsonl_is_one_json_per_line_under_load(audit_settings, tmp_path):
    configure_audit_logger(audit_settings)
    logger = get_audit_logger()
    for i in range(25):
        rec = AuditRecord.start(
            identity={"sub": f"u{i}", "preferred_username": f"user{i}",
                       "roles": ["eunomia-finance-user"], "provider": "keycloak"},
            query=f"query #{i}",
        )
        rec.rows_returned = i
        rec.finish(status="ok")
        logger.emit(rec)

    jsonl_file = tmp_path / audit_settings.audit.jsonl_file
    lines = jsonl_file.read_text().strip().splitlines()
    assert len(lines) == 25
    for i, line in enumerate(lines):
        parsed = json.loads(line)
        assert parsed["preferred_username"] == f"user{i}"
        assert parsed["query"] == f"query #{i}"
        assert parsed["rows_returned"] == i


def test_emit_swallows_sink_exceptions(audit_settings, monkeypatch, tmp_path):
    """A failing sink must NOT propagate into the request flow."""
    configure_audit_logger(audit_settings)
    logger = get_audit_logger()

    # Sabotage the human sink to raise on write.
    def boom(*a, **kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(logger._sinks, "emit", boom)

    rec = AuditRecord.start(identity={"sub": "x"}, query="q")
    rec.finish(status="ok")
    # Should NOT raise.
    logger.emit(rec)


def test_emit_when_enabled_but_unconfigured_falls_back_to_main_logger(caplog):
    """Defensive net: enabled=true but sinks not initialized yet → log a warning
    to the application logger so the audit record isn't silently lost."""
    reset_audit_logger()
    logger = get_audit_logger()
    # Force enabled=True without going through configure() (which would build sinks).
    logger._enabled = True
    rec = AuditRecord.start(identity={"sub": "x"}, query="early")
    rec.finish(status="ok")
    with caplog.at_level("WARNING", logger="eunomia.audit_log"):
        logger.emit(rec)
    assert any("AuditLogger.emit() before configure()" in r.message for r in caplog.records)


def test_emit_when_disabled_is_silent(caplog):
    """If audit is disabled, emit is a no-op (no fallback warnings)."""
    reset_audit_logger()
    rec = AuditRecord.start(identity={"sub": "x"}, query="q")
    rec.finish(status="ok")
    with caplog.at_level("DEBUG", logger="eunomia.audit_log"):
        get_audit_logger().emit(rec)
    assert not any("AuditLogger" in r.message for r in caplog.records)
