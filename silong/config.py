"""Hub configuration read from SILONG_* environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

WHISPER_CHOICES = ("small", "base", "off")
DEFAULT_PORT = 8000
DEFAULT_DB = "silong.db"


class ConfigError(ValueError):
    """An environment variable holds a value the hub cannot use."""


@dataclass(frozen=True)
class Config:
    model: Optional[str]  # Ollama model tag; None until inference is configured.
    whisper: str  # One of WHISPER_CHOICES.
    port: int
    db_path: Path


def _get(environ: Mapping[str, str], name: str) -> Optional[str]:
    value = environ.get(name, "").strip()
    return value or None


def load_config(environ: Optional[Mapping[str, str]] = None) -> Config:
    """Build a Config from `environ` (defaults to os.environ).

    Absent or blank variables use their defaults. Invalid values raise
    ConfigError naming the variable and the accepted values.
    """
    if environ is None:
        environ = os.environ

    whisper = (_get(environ, "SILONG_WHISPER") or "off").lower()
    if whisper not in WHISPER_CHOICES:
        raise ConfigError(
            f"SILONG_WHISPER must be one of {', '.join(WHISPER_CHOICES)}; "
            f"got {environ['SILONG_WHISPER']!r}"
        )

    raw_port = _get(environ, "SILONG_PORT")
    if raw_port is None:
        port = DEFAULT_PORT
    else:
        if not (raw_port.isascii() and raw_port.isdigit()):
            raise ConfigError(
                f"SILONG_PORT must be a whole number from 1 to 65535; got {raw_port!r}"
            )
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise ConfigError(f"SILONG_PORT must be from 1 to 65535; got {port}")

    return Config(
        model=_get(environ, "SILONG_MODEL"),
        whisper=whisper,
        port=port,
        db_path=Path(_get(environ, "SILONG_DB") or DEFAULT_DB),
    )
