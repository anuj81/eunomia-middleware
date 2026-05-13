"""Settings loader: precedence, env overlay, secret-in-YAML rejection."""

import tempfile
import textwrap
from pathlib import Path

import pytest

from src.config import (
    SecretInYamlError,
    load_settings,
    reset_settings_cache,
)


def _write_yaml(text: str) -> Path:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    tmp.write(textwrap.dedent(text))
    tmp.close()
    return Path(tmp.name)


# --------------------------------------------------------------------------- #
# defaults                                                                    #
# --------------------------------------------------------------------------- #


def test_defaults_when_yaml_missing(tmp_path):
    reset_settings_cache()
    s = load_settings(config_path=tmp_path / "nonexistent.yaml")
    assert s.logging.level == "INFO"
    assert s.openmetadata.mock is True
    assert s.database.driver == "mysql"
    assert s.llm.model == "gemini-2.5-flash"
    # Phase A YAML default would be enabled=false; with no YAML we get the
    # built-in default which is also enabled=False (config/settings.py).
    assert s.rag.enabled is False


def test_real_yaml_loads_committed_defaults():
    reset_settings_cache()
    s = load_settings()  # default config/eunomia.yaml
    assert s.openmetadata.url == "http://localhost:8585/api/v1"
    # Phase C flipped this on in the committed YAML
    assert s.rag.enabled is True
    assert s.rag.mock is True


# --------------------------------------------------------------------------- #
# precedence                                                                  #
# --------------------------------------------------------------------------- #


def test_env_overrides_yaml(monkeypatch):
    monkeypatch.setenv("EUNOMIA_LOGGING__LEVEL", "DEBUG")
    monkeypatch.setenv("EUNOMIA_OPENMETADATA__MOCK", "false")
    reset_settings_cache()
    s = load_settings()
    assert s.logging.level == "DEBUG"
    assert s.openmetadata.mock is False


def test_cli_overrides_beat_env(monkeypatch):
    monkeypatch.setenv("EUNOMIA_LOGGING__LEVEL", "WARN")
    reset_settings_cache()
    s = load_settings(cli_overrides={"logging": {"level": "ERROR"}})
    assert s.logging.level == "ERROR"


def test_well_known_secret_env_vars(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test-123")
    monkeypatch.setenv("DB_PASSWORD", "hunter2")
    monkeypatch.setenv("OPENMETADATA_PASSWORD", "om-pass")
    monkeypatch.setenv("RAG_API_KEY", "rag-token")
    reset_settings_cache()
    s = load_settings()
    assert s.llm.api_key == "sk-test-123"
    assert s.database.password == "hunter2"
    assert s.openmetadata.password == "om-pass"
    assert s.rag.api_key == "rag-token"


# --------------------------------------------------------------------------- #
# secret-in-YAML rejection                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field_path,value", [
    ("llm:\n  api_key: leaked\n", "llm.api_key"),
    ("database:\n  password: hunter2\n", "database.password"),
    ("openmetadata:\n  password: admin\n", "openmetadata.password"),
    ("rag:\n  api_key: tok\n", "rag.api_key"),
])
def test_secrets_in_yaml_rejected(field_path, value):
    bad = _write_yaml(field_path)
    try:
        with pytest.raises(SecretInYamlError) as exc:
            load_settings(config_path=bad)
        assert value in str(exc.value)
    finally:
        bad.unlink()


# --------------------------------------------------------------------------- #
# validation                                                                  #
# --------------------------------------------------------------------------- #


def test_invalid_log_level_rejected():
    with pytest.raises(Exception):  # pydantic ValidationError
        load_settings(cli_overrides={"logging": {"level": "TRACE"}})


def test_log_level_case_insensitive():
    s = load_settings(cli_overrides={"logging": {"level": "debug"}})
    assert s.logging.level == "DEBUG"
