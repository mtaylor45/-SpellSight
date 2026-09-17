"""Configuration: YAML file, overridable by WAND_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class CameraConfig:
    source: Any = 0              # index (0) or device path ("/dev/video0")
    width: int = 640
    height: int = 480
    fps: int = 30
    fourcc: str = "MJPG"         # "" to leave the driver default
    flip_horizontal: bool = False
    flip_vertical: bool = False
    rotate: int = 0              # 0, 90, 180, 270
    # A missing or re-enumerating camera is expected, not exceptional: the
    # capture thread keeps retrying instead of letting the process die.
    reopen_delay: float = 2.0            # first retry wait, seconds
    reopen_max_delay: float = 30.0       # backoff ceiling
    reopen_after_failures: int = 60      # consecutive bad reads before reopening


@dataclass
class TrackerConfig:
    threshold: int = 220         # grayscale cutoff for the IR reflector
    blur: int = 3                # gaussian kernel, odd, 0 disables
    min_area: float = 2.0        # px^2
    max_area: float = 500.0
    max_jump: float = 120.0      # px between frames before we reject the point
    lost_frames: int = 8         # frames without a blob before a gesture ends
    min_points: int = 12
    min_path_length: float = 60.0
    max_duration: float = 4.0    # seconds
    cooldown: float = 1.5        # seconds after a cast before we listen again
    smoothing: float = 0.35      # 0 = raw, 0.9 = very smooth


@dataclass
class RecognizerConfig:
    resample_points: int = 64
    rotation_invariant: bool = False
    min_confidence: float = 0.82
    min_margin: float = 0.03     # gap required between best and runner-up
    templates_path: str = "data/templates.json"


@dataclass
class MqttConfig:
    enabled: bool = True
    host: str = "homeassistant.local"
    port: int = 1883
    username: str = ""
    password: str = ""
    client_id: str = "wand-portal"
    base_topic: str = "wand"
    discovery_prefix: str = "homeassistant"
    node_id: str = "wand"
    device_name: str = "Wand Portal"
    pulse_seconds: float = 3.0
    retain_last_spell: bool = True


@dataclass
class ServerConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    stream_fps: int = 15
    stream_quality: int = 70


@dataclass
class Config:
    camera: CameraConfig = field(default_factory=CameraConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    recognizer: RecognizerConfig = field(default_factory=RecognizerConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    spells: list[str] = field(default_factory=lambda: [
        "lumos", "nox", "alohomora", "colloportus",
        "incendio", "accio", "silencio", "revelio",
    ])
    config_path: str = ""


def _coerce(value: str, target_type: Any) -> Any:
    if target_type is bool:
        return value.strip().lower() in ("1", "true", "yes", "on")
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    if target_type is list:
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def _apply_env(section_name: str, section: Any) -> None:
    """WAND_MQTT_HOST=... overrides config.mqtt.host"""
    for f in fields(section):
        env_key = f"WAND_{section_name}_{f.name}".upper()
        raw = os.environ.get(env_key)
        if raw is None:
            continue
        current = getattr(section, f.name)
        setattr(section, f.name, _coerce(raw, type(current)))


def load(path: str | None = None) -> Config:
    path = path or os.environ.get("WAND_CONFIG", "config.yaml")
    cfg = Config(config_path=str(Path(path).resolve()))

    p = Path(path)
    if p.is_file():
        data = yaml.safe_load(p.read_text()) or {}
        for key, value in data.items():
            if not hasattr(cfg, key):
                continue
            current = getattr(cfg, key)
            if is_dataclass(current) and isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if hasattr(current, sub_key):
                        setattr(current, sub_key, sub_value)
            else:
                setattr(cfg, key, value)

    for name in ("camera", "tracker", "recognizer", "mqtt", "server"):
        _apply_env(name, getattr(cfg, name))

    if os.environ.get("WAND_SPELLS"):
        cfg.spells = _coerce(os.environ["WAND_SPELLS"], list)

    return cfg
