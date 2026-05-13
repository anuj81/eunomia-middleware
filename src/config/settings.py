"""Layered settings loader for Eunomia Middleware.

Precedence (highest wins):
    1. CLI overrides   — passed via load_settings(cli_overrides={...})
    2. Environment     — EUNOMIA_<SECTION>__<FIELD>, or well-known names
                         (OPENMETADATA_PASSWORD, DB_PASSWORD, GEMINI_API_KEY,
                          RAG_API_KEY)
    3. YAML file       — config/eunomia.yaml by default; override with the
                         EUNOMIA_CONFIG env var or the --config CLI flag
    4. Built-in defaults (the BaseModel field defaults below)

Secrets MUST NOT live in YAML. Any of {openmetadata.password,
database.password, llm.api_key, rag.api_key} appearing in the YAML file
raises SecretInYamlError at startup.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Tuple

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# --------------------------------------------------------------------------- #
# Per-section schemas                                                         #
# --------------------------------------------------------------------------- #

LogLevel = Literal["DEBUG", "INFO", "WARN", "ERROR"]


class LoggingRotationConfig(BaseModel):
    max_bytes: int = 10_485_760  # 10 MiB
    backup_count: int = 5


class LoggingConfig(BaseModel):
    level: LogLevel = "INFO"
    log_dir: Path = Path("./logs")
    console: bool = True
    file: bool = True
    rotation: LoggingRotationConfig = Field(default_factory=LoggingRotationConfig)

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = True


class OpenMetadataConfig(BaseModel):
    mock: bool = True
    url: str = "http://localhost:8585/api/v1"
    username: str = "admin@open-metadata.org"
    password: Optional[str] = None  # env-only


class DatabaseConfig(BaseModel):
    driver: Literal["mysql"] = "mysql"
    host: str = "localhost"
    port: int = 3306
    user: str = "api_user"
    name: str = "zenith_corp_eunomia"
    password: Optional[str] = None  # env-only


class LLMConfig(BaseModel):
    provider: Literal["gemini"] = "gemini"
    model: str = "gemini-2.5-flash"
    max_retries: int = 3
    api_key: Optional[str] = None  # env-only


class RagConfig(BaseModel):
    enabled: bool = False
    mock: bool = True
    url: str = "http://localhost:9000"
    top_k: int = 8
    timeout_seconds: int = 5
    api_key: Optional[str] = None  # env-only


# --------------------------------------------------------------------------- #
# Top-level Settings                                                          #
# --------------------------------------------------------------------------- #


class Settings(BaseSettings):
    """Root settings object — sections are nested BaseModels."""

    model_config = SettingsConfigDict(
        env_prefix="EUNOMIA_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    openmetadata: OpenMetadataConfig = Field(default_factory=OpenMetadataConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    rag: RagConfig = Field(default_factory=RagConfig)


# --------------------------------------------------------------------------- #
# Loader internals                                                            #
# --------------------------------------------------------------------------- #


class SecretInYamlError(ValueError):
    """Raised when the YAML config contains a field that must come from env."""


# Field paths (section, field) that are forbidden in YAML.
_FORBIDDEN_YAML_SECRETS: Tuple[Tuple[str, str], ...] = (
    ("openmetadata", "password"),
    ("database", "password"),
    ("llm", "api_key"),
    ("rag", "api_key"),
)

# Convenience: short env-var names that map to nested settings paths.
# These are layered ON TOP of YAML before pydantic-settings runs, so they
# win over YAML but lose to EUNOMIA_*__* env vars (handled by pydantic-settings).
_WELL_KNOWN_SECRET_ENV: Dict[str, Tuple[str, str]] = {
    "OPENMETADATA_PASSWORD": ("openmetadata", "password"),
    "DB_PASSWORD": ("database", "password"),
    "GEMINI_API_KEY": ("llm", "api_key"),
    "RAG_API_KEY": ("rag", "api_key"),
}


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML config at {path} must be a mapping at the root.")
    return data


def _assert_no_yaml_secrets(yaml_data: Dict[str, Any], path: Path) -> None:
    violations = []
    for section, field in _FORBIDDEN_YAML_SECRETS:
        section_data = yaml_data.get(section)
        if isinstance(section_data, dict) and section_data.get(field) is not None:
            violations.append(f"{section}.{field}")
    if violations:
        raise SecretInYamlError(
            f"Secrets present in YAML config ({path}): {violations}. "
            "These must be set via environment variables only: "
            "OPENMETADATA_PASSWORD, DB_PASSWORD, GEMINI_API_KEY, RAG_API_KEY."
        )


def _overlay_well_known_secrets(yaml_data: Dict[str, Any]) -> None:
    """Mutate yaml_data so well-known secret env vars become nested values."""
    for env_key, (section, field) in _WELL_KNOWN_SECRET_ENV.items():
        val = os.environ.get(env_key)
        if val:
            yaml_data.setdefault(section, {})[field] = val


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Merge overlay into base (overlay wins). Returns a new dict."""
    out: Dict[str, Any] = {**base}
    for key, value in overlay.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _default_config_path() -> Path:
    """Find the default YAML path: $EUNOMIA_CONFIG → config/eunomia.yaml."""
    env_path = os.environ.get("EUNOMIA_CONFIG")
    if env_path:
        return Path(env_path)
    # Resolve relative to the middleware project root (parent of src/).
    project_root = Path(__file__).resolve().parents[2]
    return project_root / "config" / "eunomia.yaml"


# --------------------------------------------------------------------------- #
# Public loader                                                               #
# --------------------------------------------------------------------------- #


def load_settings(
    config_path: Optional[Path] = None,
    cli_overrides: Optional[Dict[str, Any]] = None,
) -> Settings:
    """Load layered settings. Call once at startup.

    Args:
        config_path: Explicit YAML path. Falls back to $EUNOMIA_CONFIG, then
                     `<project_root>/config/eunomia.yaml`.
        cli_overrides: Nested dict of overrides from CLI flags. Highest priority.
    """
    path = Path(config_path) if config_path else _default_config_path()
    yaml_data = _read_yaml(path)
    _assert_no_yaml_secrets(yaml_data, path)
    _overlay_well_known_secrets(yaml_data)
    if cli_overrides:
        yaml_data = _deep_merge(yaml_data, cli_overrides)

    # pydantic-settings will also read EUNOMIA_*__* env vars and apply them
    # ON TOP of these init kwargs — that gives us the precedence we want:
    # CLI overrides > EUNOMIA_*__* env > well-known short env > YAML > defaults.
    #
    # Wait — pydantic-settings v2 default precedence is init > env. We want
    # env to override the YAML init values for the prefixed form, but CLI
    # overrides (passed via init) to win over env. Reconcile by reading env
    # ourselves here for the prefixed form and inserting into the dict, then
    # finally applying cli_overrides AGAIN so they remain top of the stack.
    _overlay_prefixed_env(yaml_data)
    if cli_overrides:
        yaml_data = _deep_merge(yaml_data, cli_overrides)

    return Settings(**yaml_data)


def _overlay_prefixed_env(yaml_data: Dict[str, Any]) -> None:
    """Read EUNOMIA_<SECTION>__<FIELD> env vars and overlay into yaml_data.

    Boolean and integer coercion is left to pydantic on construction.
    """
    prefix = "EUNOMIA_"
    delim = "__"
    for env_key, value in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        remainder = env_key[len(prefix):]
        if delim not in remainder:
            continue
        path_parts = [p.lower() for p in remainder.split(delim)]
        # Walk into yaml_data, creating nested dicts as needed.
        cursor = yaml_data
        for part in path_parts[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[path_parts[-1]] = value


# --------------------------------------------------------------------------- #
# Cached accessor                                                             #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached entrypoint for any module that needs settings.

    First call performs the full layered load using env-resolved paths.
    Subsequent calls return the cached instance. Use reset_settings_cache()
    in tests to force a reload.
    """
    return load_settings()


def reset_settings_cache() -> None:
    """Drop the cached Settings — used by tests and CLI override paths."""
    get_settings.cache_clear()
