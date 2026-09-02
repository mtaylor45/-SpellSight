"""MQTT client and Home Assistant discovery.

The topic shapes, entity object_ids and payload keys here are the frozen
interface in SPEC.md section 4. Home Assistant automations are written against
them, so adding topics under `wand/` is fine and repurposing these is not.

Connection is asynchronous throughout. The device has to reach a working
recognition state with no network at all, so nothing in this module may block
the caller waiting for a broker.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import paho.mqtt.client as mqtt

from .spells import Spell, SPELLS_BY_ID

log = logging.getLogger(__name__)

SW_VERSION = "1.0.0"


def _make_client(client_id: str) -> mqtt.Client:
    """Construct a paho client across the v1/v2 callback API split."""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except AttributeError:  # paho-mqtt 1.x
        return mqtt.Client(client_id=client_id)


class MqttBridge:
    """Publishes spells and Home Assistant discovery configs."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1883,
        username: str = "",
        password: str = "",
        client_id: str = "wandportal",
        base_topic: str = "wand",
        discovery_prefix: str = "homeassistant",
        device_name: str = "Wand Portal",
        pulse_seconds: float = 3.0,
        keepalive: int = 60,
        enabled: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.base_topic = base_topic.rstrip("/")
        self.discovery_prefix = discovery_prefix.rstrip("/")
        self.device_name = device_name
        self.pulse_seconds = pulse_seconds
        self.keepalive = keepalive
        self.enabled = enabled

        self.connected = False
        self.publishes = 0
        self.dropped = 0
        self.last_error: str | None = None
        self.connected_at: float | None = None

        self._timers: dict[str, threading.Timer] = {}
        self._timer_lock = threading.Lock()
        self._client: mqtt.Client | None = None

        if not enabled:
            log.info("MQTT disabled by config; recognition still runs")
            return

        client = _make_client(client_id)
        if username:
            client.username_pw_set(username, password or None)
        client.will_set(self.topic("status"), "offline", qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client = client

    # ---- topics ----------------------------------------------------------

    def topic(self, *parts: str) -> str:
        return "/".join([self.base_topic, *parts])

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Begin connecting in the background. Never blocks, never raises."""
        if not self._client:
            return
        try:
            self._client.connect_async(self.host, self.port, self.keepalive)
            self._client.loop_start()
            log.info("MQTT connecting to %s:%s in the background", self.host, self.port)
        except Exception as exc:  # bad hostname, bad port
            self.last_error = str(exc)
            log.error("MQTT could not start: %s", exc)

    def stop(self) -> None:
        with self._timer_lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
        if not self._client:
            return
        try:
            if self.connected:
                self._client.publish(self.topic("status"), "offline", qos=1, retain=True)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception as exc:
            log.debug("MQTT shutdown: %s", exc)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        code = getattr(reason_code, "value", reason_code)
        if code != 0:
            self.last_error = f"connect refused ({reason_code})"
            log.error("MQTT connection refused: %s", reason_code)
            return
        self.connected = True
        self.connected_at = time.time()
        self.last_error = None
        log.info("MQTT connected to %s:%s", self.host, self.port)
        client.publish(self.topic("status"), "online", qos=1, retain=True)

    def _on_disconnect(self, client, userdata, *args) -> None:
        self.connected = False
        log.warning("MQTT disconnected; paho will retry in the background")

    # ---- publishing ------------------------------------------------------

    def publish(self, topic: str, payload: str, retain: bool = False, qos: int = 0) -> bool:
        """Publish one message, counting drops rather than raising.

        A cast during a broker outage is dropped and counted, not queued. Firing
        a backlog of spells at the house when the network returns would be worse
        than losing them.
        """
        if not self._client or not self.connected:
            self.dropped += 1
            return False
        try:
            self._client.publish(topic, payload, qos=qos, retain=retain)
            self.publishes += 1
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.dropped += 1
            log.warning("MQTT publish to %s failed: %s", topic, exc)
            return False

    def publish_spell(self, spell_id: str, confidence: float, duration: float) -> bool:
        """Publish one recognized cast across all four spell topics."""
        spell = SPELLS_BY_ID.get(spell_id)
        name = spell.name if spell else spell_id
        effect = spell.effect if spell else ""
        now = time.time()

        state_topic = self.topic("spell", spell_id, "state")
        sent = self.publish(state_topic, "ON")
        self.publish(self.topic("last_spell"), name, retain=True)
        self.publish(
            self.topic("last_spell", "attributes"),
            json.dumps(
                {
                    "spell_id": spell_id,
                    "confidence": round(float(confidence), 4),
                    "duration": round(float(duration), 3),
                    "effect": effect,
                    "cast_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
                }
            ),
            retain=True,
        )
        self.publish(
            self.topic("event"),
            json.dumps(
                {"spell": spell_id, "name": name, "confidence": round(float(confidence), 4)}
            ),
        )
        if sent:
            self._schedule_off(spell_id, state_topic)
        return sent

    def _schedule_off(self, spell_id: str, state_topic: str) -> None:
        """Turn the binary_sensor back off after the pulse.

        On its own timer thread: the capture loop must not sleep for three
        seconds waiting to publish OFF.
        """
        with self._timer_lock:
            existing = self._timers.pop(spell_id, None)
            if existing:
                existing.cancel()
            timer = threading.Timer(
                self.pulse_seconds, self._publish_off, args=(spell_id, state_topic)
            )
            timer.daemon = True
            self._timers[spell_id] = timer
            timer.start()

    def _publish_off(self, spell_id: str, state_topic: str) -> None:
        self.publish(state_topic, "OFF")
        with self._timer_lock:
            self._timers.pop(spell_id, None)

    # ---- Home Assistant discovery ---------------------------------------

    def _device(self) -> dict:
        # One device for every entity, so removing the integration in Home
        # Assistant removes all of them together.
        return {
            "identifiers": ["wand"],
            "name": self.device_name,
            "manufacturer": "Wand Portal",
            "model": "IR Wand Gesture Recognition",
            "sw_version": SW_VERSION,
        }

    def _discovery_topic(self, component: str, object_id: str) -> str:
        return f"{self.discovery_prefix}/{component}/wand/{object_id}/config"

    def publish_discovery(self, spells: list[Spell] | list[str]) -> int:
        """Publish discovery configs for the given spells, plus the last-spell sensor."""
        resolved: list[Spell] = []
        for item in spells:
            spell = SPELLS_BY_ID.get(item) if isinstance(item, str) else item
            if spell:
                resolved.append(spell)

        count = 0
        for spell in resolved:
            config = {
                "name": spell.name,
                "object_id": f"wand_{spell.id}",
                "unique_id": f"wand_{spell.id}",
                "state_topic": self.topic("spell", spell.id, "state"),
                "payload_on": "ON",
                "payload_off": "OFF",
                "availability_topic": self.topic("status"),
                "payload_available": "online",
                "payload_not_available": "offline",
                "icon": "mdi:auto-fix",
                "device": self._device(),
            }
            if self.publish(
                self._discovery_topic("binary_sensor", spell.id),
                json.dumps(config),
                retain=True,
                qos=1,
            ):
                count += 1

        last_spell = {
            "name": "Last Spell",
            "object_id": "wand_last_spell",
            "unique_id": "wand_last_spell",
            "state_topic": self.topic("last_spell"),
            "json_attributes_topic": self.topic("last_spell", "attributes"),
            "availability_topic": self.topic("status"),
            "payload_available": "online",
            "payload_not_available": "offline",
            "icon": "mdi:auto-fix",
            "device": self._device(),
        }
        if self.publish(
            self._discovery_topic("sensor", "last_spell"),
            json.dumps(last_spell),
            retain=True,
            qos=1,
        ):
            count += 1
        log.info("Published %d discovery configs", count)
        return count

    def remove_discovery(self, spells: list[Spell] | list[str] | None = None) -> int:
        """Clear retained discovery configs.

        Call this *before* renaming or removing spells. An empty payload on the
        config topic is how Home Assistant is told to forget an entity;
        otherwise the retained config outlives the spell and leaves a permanently
        unavailable entity behind.
        """
        targets = spells if spells is not None else list(SPELLS_BY_ID.values())
        count = 0
        for item in targets:
            spell_id = item if isinstance(item, str) else item.id
            if self.publish(
                self._discovery_topic("binary_sensor", spell_id), "", retain=True, qos=1
            ):
                count += 1
        if self.publish(self._discovery_topic("sensor", "last_spell"), "", retain=True, qos=1):
            count += 1
        return count

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "publishes": self.publishes,
            "dropped": self.dropped,
            "last_error": self.last_error,
        }
