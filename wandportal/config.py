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
    # "fixed" is the Phase 1 behaviour and the baseline the tests assert
    # against. "adaptive" (spec R2.2) tracks measured ambient instead and
    # arrives on day 8; the estimator below already runs in both modes,
    # because wand/health needs ambient regardless of what sets the cutoff.
    threshold_mode: str = "fixed"
    threshold: int = 220         # grayscale cutoff for the IR reflector
    ambient_percentile: float = 99.0   # of the scene, not counting the wand
    ambient_interval: int = 15         # frames between recalculations
    ambient_width: int = 160           # downscale before the percentile
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
    # Training is an evening's work and `clear` is one tap away, so keep a short
    # ring of previous saves beside the live file. 0 disables.
    template_backups: int = 5


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


def validate(cfg: Config) -> list[str]:
    """Collect every problem with a config, rather than raising on the first.

    A misconfigured device is found at 11pm by someone holding a wand, not at a
    terminal. Reporting all of it at once beats fixing one typo per restart.
    """
    from .spells import BY_ID                      # local: avoids a config->spells cycle

    problems: list[str] = []

    unknown = [s for s in cfg.spells if s not in BY_ID]
    if unknown:
        problems.append(
            f"unknown spell id(s): {', '.join(sorted(unknown))}. "
            f"Run `python -m wandportal --list-spells` for the {len(BY_ID)} valid ids."
        )
    if not cfg.spells:
        problems.append("spells: is empty — nothing would ever be recognized or discovered")

    t = cfg.tracker
    if t.threshold_mode == "adaptive":
        problems.append(
            "tracker.threshold_mode: 'adaptive' is not implemented yet (spec R2.2, "
            "plan day 8). Use 'fixed'. The ambient estimate it needs is already "
            "published, so you can watch headroom before switching."
        )
    elif t.threshold_mode != "fixed":
        problems.append(
            f"tracker.threshold_mode {t.threshold_mode!r} must be 'fixed' or 'adaptive'"
        )
    if not 1 <= t.ambient_percentile <= 100:
        problems.append(f"tracker.ambient_percentile {t.ambient_percentile} must be in 1..100")
    if t.ambient_interval < 1:
        problems.append(f"tracker.ambient_interval {t.ambient_interval} must be at least 1")
    if t.ambient_width < 16:
        problems.append(f"tracker.ambient_width {t.ambient_width} must be at least 16")
    if not 1 <= t.threshold <= 254:
        problems.append(f"tracker.threshold {t.threshold} is outside 1..254")
    if t.min_area <= 0:
        problems.append(f"tracker.min_area {t.min_area} must be above 0")
    if t.min_area >= t.max_area:
        problems.append(
            f"tracker.min_area ({t.min_area}) must be below tracker.max_area "
            f"({t.max_area}); an inverted area window matches nothing"
        )
    if t.lost_frames < 1:
        problems.append(f"tracker.lost_frames {t.lost_frames} must be at least 1")
    if t.min_points < 2:
        problems.append(f"tracker.min_points {t.min_points} must be at least 2")
    if not 0.0 <= t.smoothing < 1.0:
        problems.append(f"tracker.smoothing {t.smoothing} must be in 0.0..0.99")
    if t.max_duration <= 0:
        problems.append(f"tracker.max_duration {t.max_duration} must be above 0")

    r = cfg.recognizer
    if r.resample_points < 8:
        problems.append(f"recognizer.resample_points {r.resample_points} must be at least 8")
    if not 0.0 <= r.min_confidence <= 1.0:
        problems.append(f"recognizer.min_confidence {r.min_confidence} must be in 0.0..1.0")
    if not 0.0 <= r.min_margin <= 1.0:
        problems.append(f"recognizer.min_margin {r.min_margin} must be in 0.0..1.0")

    c = cfg.camera
    if c.width <= 0 or c.height <= 0:
        problems.append(f"camera resolution {c.width}x{c.height} is not positive")
    if c.rotate not in (0, 90, 180, 270):
        problems.append(f"camera.rotate {c.rotate} must be one of 0, 90, 180, 270")
    if c.reopen_delay <= 0 or c.reopen_max_delay < c.reopen_delay:
        problems.append(
            f"camera.reopen_delay ({c.reopen_delay}) must be above 0 and at most "
            f"camera.reopen_max_delay ({c.reopen_max_delay})"
        )

    if not 1 <= cfg.server.port <= 65535:
        problems.append(f"server.port {cfg.server.port} is outside 1..65535")
    if not 1 <= cfg.mqtt.port <= 65535:
        problems.append(f"mqtt.port {cfg.mqtt.port} is outside 1..65535")
    if cfg.mqtt.pulse_seconds <= 0:
        problems.append(f"mqtt.pulse_seconds {cfg.mqtt.pulse_seconds} must be above 0")

    return problems


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
