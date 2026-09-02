"""Configuration loading.

YAML file, then `WAND_<SECTION>_<KEY>` environment overrides on top. The env
layer exists so a Docker deploy can set a broker address without baking a config
file into the image, and it deliberately wins over the file so a container's
environment is always the last word.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar, get_type_hints

import yaml

log = logging.getLogger(__name__)

ENV_PREFIX = "WAND"


@dataclass
class CameraConfig:
    index: int = 0
    width: int = 640
    height: int = 480
    fps: int = 30
    flip_horizontal: bool = False
    flip_vertical: bool = False
    reopen_delay: float = 2.0


@dataclass
class TrackerConfig:
    # Phase 1 is a fixed threshold, which assumes the retroreflector is trivially
    # the brightest thing in frame. SPEC.md R2.2 replaces this with an ambient
    # adaptive mode; `threshold_mode: fixed` must keep reproducing this behavior.
    threshold_mode: str = "fixed"
    threshold: int = 230
    min_area: float = 2.0
    max_area: float = 400.0
    max_jump: float = 160.0
    smoothing: float = 0.35
    start_frames: int = 2
    lost_frames: int = 6
    min_points: int = 12
    min_path_length: float = 90.0
    max_gesture_seconds: float = 4.0


@dataclass
class RecognizerConfig:
    resample_points: int = 64
    min_confidence: float = 0.85
    min_margin: float = 0.06
    templates_path: str = "data/templates.json"
    max_samples_per_spell: int = 12


@dataclass
class MqttConfig:
    enabled: bool = True
    host: str = "localhost"
    port: int = 1883
    username: str = ""
    password: str = ""
    client_id: str = "wandportal"
    base_topic: str = "wand"
    discovery_prefix: str = "homeassistant"
    device_name: str = "Wand Portal"
    pulse_seconds: float = 3.0
    keepalive: int = 60


@dataclass
class EngineConfig:
    cooldown: float = 1.5
    event_history: int = 50


@dataclass
class ServerConfig:
    enabled: bool = True
    bind: str = "0.0.0.0"
    port: int = 8080
    jpeg_quality: int = 70


@dataclass
class Config:
    camera: CameraConfig = field(default_factory=CameraConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    recognizer: RecognizerConfig = field(default_factory=RecognizerConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    server: ServerConfig = field(default_factory=ServerConfig)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


T = TypeVar("T")

_SECTIONS = ("camera", "tracker", "recognizer", "mqtt", "engine", "server")


def _coerce(value: Any, target_type: Any) -> Any:
    """Coerce a YAML or environment value to the dataclass field's type."""
    if target_type is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if target_type is int:
        return int(float(value))
    if target_type is float:
        return float(value)
    if target_type is str:
        return str(value)
    return value


def _field_types(section: Any) -> dict[str, Any]:
    """Resolve a dataclass's field types.

    `from __future__ import annotations` makes `Field.type` a string, which would
    make every coercion below a silent no-op and leave ints as strings.
    """
    hints = get_type_hints(type(section))
    return {f.name: hints[f.name] for f in dataclasses.fields(section)}


def _apply(section: Any, values: dict[str, Any], source: str) -> None:
    """Apply a mapping onto a dataclass instance, ignoring unknown keys."""
    types = _field_types(section)
    for key, value in values.items():
        if key not in types:
            log.warning("Unknown %s key %r in %s, ignoring", type(section).__name__, key, source)
            continue
        try:
            setattr(section, key, _coerce(value, types[key]))
        except (TypeError, ValueError):
            log.warning(
                "Bad value %r for %s.%s in %s, keeping default",
                value, type(section).__name__, key, source,
            )


def _env_overrides(config: Config) -> None:
    for section_name in _SECTIONS:
        section = getattr(config, section_name)
        types = _field_types(section)
        for name, field_type in types.items():
            env_key = f"{ENV_PREFIX}_{section_name.upper()}_{name.upper()}"
            if env_key in os.environ:
                raw = os.environ[env_key]
                try:
                    setattr(section, name, _coerce(raw, field_type))
                except (TypeError, ValueError):
                    log.warning("Bad env override %s=%r, keeping current value", env_key, raw)
                else:
                    log.info("Config override from %s", env_key)


def load_config(path: str | Path | None = None) -> Config:
    """Load config from YAML (if present) with environment overrides applied."""
    config = Config()
    if path:
        p = Path(path)
        if p.exists():
            raw = yaml.safe_load(p.read_text()) or {}
            if not isinstance(raw, dict):
                raise ValueError(f"{p} must contain a top-level mapping")
            for section_name in _SECTIONS:
                values = raw.get(section_name) or {}
                if values:
                    _apply(getattr(config, section_name), values, str(p))
            for key in raw:
                if key not in _SECTIONS:
                    log.warning("Unknown config section %r in %s, ignoring", key, p)
        else:
            log.warning("Config file %s not found, using defaults", p)
    _env_overrides(config)
    return config
