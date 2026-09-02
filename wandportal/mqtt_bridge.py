"""MQTT publishing with Home Assistant discovery.

Each enabled spell is announced as a binary_sensor that pulses on when
cast. A `last_spell` sensor and a JSON `event` topic are published too,
so you can drive automations off either shape.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import paho.mqtt.client as mqtt

from .config import MqttConfig
from .spells import Spell

log = logging.getLogger(__name__)


class MqttBridge:
    def __init__(self, cfg: MqttConfig, spells: list[Spell]):
        self.cfg = cfg
        self.spells = spells
        self.connected = False
        self._timers: dict[str, threading.Timer] = {}
        self._client: mqtt.Client | None = None

        base = cfg.base_topic.rstrip("/")
        self.t_status = f"{base}/status"
        self.t_last = f"{base}/last_spell"
        self.t_attrs = f"{base}/last_spell/attributes"
        self.t_event = f"{base}/event"
        self.t_spell = lambda sid: f"{base}/spell/{sid}/state"

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if not self.cfg.enabled:
            log.info("MQTT disabled")
            return

        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=self.cfg.client_id,
            )
        except (AttributeError, TypeError):  # paho-mqtt 1.x
            client = mqtt.Client(client_id=self.cfg.client_id)

        if self.cfg.username:
            client.username_pw_set(self.cfg.username, self.cfg.password)
        client.will_set(self.t_status, "offline", retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect

        self._client = client
        try:
            client.connect_async(self.cfg.host, self.cfg.port, keepalive=60)
        except Exception as exc:
            log.error("MQTT connect failed: %s", exc)
        client.loop_start()

    def stop(self) -> None:
        for t in list(self._timers.values()):
            t.cancel()
        if self._client:
            try:
                self._client.publish(self.t_status, "offline", retain=True)
                time.sleep(0.2)
            finally:
                self._client.loop_stop()
                self._client.disconnect()

    # -- callbacks ---------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        code = getattr(reason_code, "value", reason_code)
        if code != 0:
            log.error("MQTT connection refused (code %s)", code)
            return
        self.connected = True
        log.info("MQTT connected to %s:%s", self.cfg.host, self.cfg.port)
        client.publish(self.t_status, "online", retain=True)
        self.publish_discovery()
        for spell in self.spells:
            client.publish(self.t_spell(spell.id), "OFF", retain=False)

    def _on_disconnect(self, client, userdata, *args):
        self.connected = False
        log.warning("MQTT disconnected")

    # -- discovery ---------------------------------------------------------

    def _device(self) -> dict:
        return {
            "identifiers": [self.cfg.node_id],
            "name": self.cfg.device_name,
            "manufacturer": "Ollivanders",
            "model": "IR Wand Portal",
            "sw_version": "1.0",
        }

    def publish_discovery(self) -> None:
        if not self._client:
            return
        prefix = self.cfg.discovery_prefix.rstrip("/")
        node = self.cfg.node_id
        device = self._device()

        for spell in self.spells:
            uid = f"{node}_{spell.id}"
            payload = {
                "name": spell.name,
                "unique_id": uid,
                "object_id": f"wand_{spell.id}",
                "state_topic": self.t_spell(spell.id),
                "payload_on": "ON",
                "payload_off": "OFF",
                "off_delay": int(self.cfg.pulse_seconds),
                "availability_topic": self.t_status,
                "icon": "mdi:wizard-hat",
                "device": device,
            }
            self._client.publish(
                f"{prefix}/binary_sensor/{node}/{spell.id}/config",
                json.dumps(payload),
                retain=True,
            )

        self._client.publish(
            f"{prefix}/sensor/{node}/last_spell/config",
            json.dumps({
                "name": "Last spell",
                "unique_id": f"{node}_last_spell",
                "object_id": "wand_last_spell",
                "state_topic": self.t_last,
                "json_attributes_topic": self.t_attrs,
                "availability_topic": self.t_status,
                "icon": "mdi:auto-fix",
                "device": device,
            }),
            retain=True,
        )
        log.info("Published discovery for %d spells", len(self.spells))

    def remove_discovery(self) -> None:
        """Clear retained discovery configs (use when renaming or pruning spells)."""
        if not self._client:
            return
        prefix = self.cfg.discovery_prefix.rstrip("/")
        node = self.cfg.node_id
        for spell in self.spells:
            self._client.publish(f"{prefix}/binary_sensor/{node}/{spell.id}/config", "", retain=True)
        self._client.publish(f"{prefix}/sensor/{node}/last_spell/config", "", retain=True)

    # -- casting -----------------------------------------------------------

    def cast(self, spell: Spell, confidence: float, duration: float = 0.0) -> None:
        if not self._client or not self.connected:
            log.warning("Cast %s dropped: MQTT not connected", spell.id)
            return

        topic = self.t_spell(spell.id)
        self._client.publish(topic, "ON", retain=False)
        self._client.publish(self.t_last, spell.name, retain=self.cfg.retain_last_spell)
        self._client.publish(
            self.t_attrs,
            json.dumps({
                "spell_id": spell.id,
                "confidence": round(confidence, 3),
                "duration": round(duration, 2),
                "effect": spell.effect,
                "cast_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }),
            retain=self.cfg.retain_last_spell,
        )
        self._client.publish(
            self.t_event,
            json.dumps({"spell": spell.id, "name": spell.name, "confidence": round(confidence, 3)}),
            retain=False,
        )

        # off_delay covers HA, but publish OFF too so the topic is honest.
        old = self._timers.pop(spell.id, None)
        if old:
            old.cancel()
        timer = threading.Timer(
            self.cfg.pulse_seconds,
            lambda: self._client and self._client.publish(topic, "OFF", retain=False),
        )
        timer.daemon = True
        timer.start()
        self._timers[spell.id] = timer
