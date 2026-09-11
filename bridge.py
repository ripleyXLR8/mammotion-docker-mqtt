"""Pont Mammotion (cloud) → MQTT Discovery.

Se connecte au cloud Mammotion avec un compte (idéalement un compte dédié auquel
la tondeuse est partagée — le cloud n'autorise qu'une session par compte), publie
l'état et les commandes de chaque tondeuse au format MQTT Discovery (« HA
Discovery »), que le plugin Jeedom MQTT Discovery transforme en équipement.

La bibliothèque PyMammotion fait tout le dialogue (login, MQTT Aliyun, décodage
protobuf, rafraîchissement de jeton). L'état arrive en push ; on le republie
périodiquement. Réécriture 2026-09 : l'ancien pont utilisait une API de
PyMammotion 0.7.31 qui n'existe plus et mourait en boucle sur un jeton IoT périmé.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from typing import Any

import aiomqtt
from pymammotion.client import MammotionClient
from pymammotion.utility.constant.device_constant import device_connection, device_mode

LOGGER = logging.getLogger("mammotion2mqtt")

# --- Configuration (variables d'environnement) -------------------------------
EMAIL = os.environ.get("MAMMOTION_EMAIL", "")
PASSWORD = os.environ.get("MAMMOTION_PASSWORD", "")
MQTT_BROKER = os.environ.get("MQTT_BROKER", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER") or None
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD") or None
DISCOVERY_PREFIX = os.environ.get("MQTT_DISCOVERY_PREFIX", "homeassistant")
TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "mammotion")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
INCLUDE_RTK = os.environ.get("INCLUDE_RTK", "false").lower() in ("1", "true", "yes")
# Doit être un numéro de version type Mammotion-HA : le serveur dérive l'en-tête
# App-Version (« HA,2.<x> ») et REFUSE le login sinon (renvoyé comme « Account or
# password mismatch » — piège vérifié le 2026-09-11, cf. PyMammotion #137).
HA_VERSION = os.environ.get("MAMMOTION_HA_VERSION", "0.6.4")
CHARGING_STATES = (1, 2)

# Commandes exposées : libellé Jeedom → (méthode MammotionCommand, kwargs).
COMMANDS: dict[str, tuple[str, dict[str, Any]]] = {
    "start": ("start_job", {}),
    "pause": ("pause_execute_task", {}),
    "dock": ("return_to_dock", {}),
    "cancel": ("cancel_job", {}),
    "leave_dock": ("leave_dock", {}),
    "blade_on": ("set_blade_control", {"on_off": 1}),
    "blade_off": ("set_blade_control", {"on_off": 0}),
}
COMMAND_LABELS = {
    "start": "Démarrer la tonte",
    "pause": "Pause",
    "dock": "Retour à la base",
    "cancel": "Annuler",
    "leave_dock": "Quitter la base",
    "blade_on": "Lame ON",
    "blade_off": "Lame OFF",
}


def slugify(value: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in value).strip("_").lower() or "device"


def is_mower(name: str) -> bool:
    """Une tondeuse (par opposition à une base RTK) : nom Luba/Yuka/Mammotion."""
    return name.lower().startswith(("luba", "yuka", "mammotion"))


class Bridge:
    def __init__(self) -> None:
        self.client = MammotionClient(ha_version=HA_VERSION)
        self.mqtt: aiomqtt.Client | None = None
        self.devices: list[str] = []
        self._stop = asyncio.Event()

    # ---- pymammotion -------------------------------------------------------
    async def login(self) -> None:
        LOGGER.info("Connexion au cloud Mammotion (compte %s)...", EMAIL)
        await self.client.login_and_initiate_cloud(EMAIL, PASSWORD)
        all_names = [h.device_name for h in self.client.device_registry.all_devices]
        self.devices = [n for n in all_names if is_mower(n) or INCLUDE_RTK]
        LOGGER.info("Appareils : %s (retenus : %s)", all_names, self.devices)
        for name in self.devices:
            with contextlib.suppress(Exception):
                await self.client.request_reports(name, count=1, timeout=3000)

    # ---- état → MQTT -------------------------------------------------------
    def _fields(self, name: str) -> dict[str, Any] | None:
        dev = self.client.get_device_by_name(name)
        if dev is None or getattr(dev, "report_data", None) is None:
            return None
        rd = dev.report_data
        d = getattr(rd, "dev", None)
        loc = getattr(dev, "location", None)
        work = getattr(rd, "work", None)
        errs = getattr(dev, "errors", None)
        err_list = list(getattr(errs, "err_code_list", []) or []) if errs else []
        fields: dict[str, Any] = {
            "online": "online" if getattr(dev, "online", True) else "offline",
            "status": device_mode(getattr(d, "sys_status", 0)) if d else "MODE_OFFLINE",
            "battery": getattr(d, "battery_val", None) if d else None,
            "charging": "ON" if (d and getattr(d, "charge_state", 0) in CHARGING_STATES) else "OFF",
            "blade": "ON" if getattr(dev.mower_state, "blade_status", False) else "OFF",
            "progress": getattr(work, "progress", None) if work else None,
            "network": device_connection(rd.connect) if getattr(rd, "connect", None) else None,
            "error": ", ".join(str(e) for e in err_list) if err_list else "aucune",
        }
        if loc is not None and getattr(loc, "device", None) is not None:
            fields["latitude"] = getattr(loc.device, "latitude", None)
            fields["longitude"] = getattr(loc.device, "longitude", None)
        return fields

    async def publish_state(self) -> None:
        if self.mqtt is None:
            return
        for name in self.devices:
            fields = self._fields(name)
            if fields is None:
                continue
            base = f"{TOPIC_PREFIX}/{name}"
            await self.mqtt.publish(f"{base}/availability", fields["online"], retain=True)
            for key, value in fields.items():
                if key == "online" or value is None:
                    continue
                await self.mqtt.publish(f"{base}/{key}", value, retain=True)

    # ---- découverte MQTT ---------------------------------------------------
    async def publish_discovery(self) -> None:
        if self.mqtt is None:
            return
        sensors = [
            ("status", "État", None, None, "mdi:robot-mower"),
            ("battery", "Batterie", "battery", "%", None),
            ("progress", "Progression", None, "%", "mdi:progress-clock"),
            ("network", "Réseau", None, None, "mdi:wifi"),
            ("error", "Erreur", None, None, "mdi:alert-circle-outline"),
            ("latitude", "Latitude", None, "°", None),
            ("longitude", "Longitude", None, "°", None),
        ]
        binaries = [
            ("charging", "En charge", "battery_charging"),
            ("blade", "Lame", "running"),
        ]
        for name in self.devices:
            slug = f"mammotion_{slugify(name)}"
            base = f"{TOPIC_PREFIX}/{name}"
            device_block = {
                "identifiers": [slug],
                "name": name,
                "manufacturer": "Mammotion",
                "model": "Luba / Yuka",
            }
            avail = {"availability_topic": f"{base}/availability",
                     "payload_available": "online", "payload_not_available": "offline"}
            for key, label, dev_class, unit, icon in sensors:
                cfg = {"name": label, "unique_id": f"{slug}_{key}",
                       "state_topic": f"{base}/{key}", "device": device_block, **avail}
                if dev_class:
                    cfg["device_class"] = dev_class
                if unit:
                    cfg["unit_of_measurement"] = unit
                if icon:
                    cfg["icon"] = icon
                await self.mqtt.publish(
                    f"{DISCOVERY_PREFIX}/sensor/{slug}/{key}/config",
                    json.dumps(cfg, ensure_ascii=False), retain=True)
            for key, label, dev_class in binaries:
                cfg = {"name": label, "unique_id": f"{slug}_{key}",
                       "state_topic": f"{base}/{key}", "payload_on": "ON", "payload_off": "OFF",
                       "device_class": dev_class, "device": device_block, **avail}
                await self.mqtt.publish(
                    f"{DISCOVERY_PREFIX}/binary_sensor/{slug}/{key}/config",
                    json.dumps(cfg, ensure_ascii=False), retain=True)
            if not is_mower(name):
                continue  # pas de commandes sur une base RTK
            for cmd, label in COMMAND_LABELS.items():
                cfg = {"name": label, "unique_id": f"{slug}_{cmd}",
                       "command_topic": f"{base}/cmd/{cmd}/set", "payload_press": "PRESS",
                       "device": device_block, **avail}
                await self.mqtt.publish(
                    f"{DISCOVERY_PREFIX}/button/{slug}/{cmd}/config",
                    json.dumps(cfg, ensure_ascii=False), retain=True)

    # ---- commandes MQTT → tondeuse ----------------------------------------
    async def handle_command(self, topic: str) -> None:
        # topic = mammotion/<dev>/cmd/<name>/set
        parts = topic.split("/")
        if len(parts) < 5 or parts[-3] != "cmd" or parts[-1] != "set":
            return
        name = "/".join(parts[1:-3])
        cmd = parts[-2]
        if name not in self.devices or cmd not in COMMANDS:
            LOGGER.warning("Commande inconnue: %s / %s", name, cmd)
            return
        key, kwargs = COMMANDS[cmd]
        LOGGER.info("Commande %s → %s(%s) sur %s", cmd, key, kwargs, name)
        try:
            await self.client.send_command_with_args(name, key, **kwargs)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Échec de la commande %s sur %s : %r", cmd, name, exc)

    # ---- boucle principale -------------------------------------------------
    async def state_loop(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                await self.publish_state()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        await self.login()
        while not self._stop.is_set():
            try:
                async with aiomqtt.Client(
                    hostname=MQTT_BROKER, port=MQTT_PORT,
                    username=MQTT_USER, password=MQTT_PASSWORD,
                    identifier="mammotion2mqtt",
                ) as mqtt:
                    self.mqtt = mqtt
                    LOGGER.info("Connecté au MQTT %s:%s — (re)publication de la découverte", MQTT_BROKER, MQTT_PORT)
                    await self.publish_discovery()
                    await self.publish_state()
                    await mqtt.subscribe(f"{TOPIC_PREFIX}/+/cmd/+/set")
                    state_task = asyncio.create_task(self.state_loop())
                    try:
                        async for message in mqtt.messages:
                            await self.handle_command(str(message.topic))
                    finally:
                        state_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await state_task
            except aiomqtt.MqttError as exc:
                self.mqtt = None
                LOGGER.warning("MQTT déconnecté (%s) — reconnexion dans 5 s", exc)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=5)
        await self.shutdown()

    async def shutdown(self) -> None:
        LOGGER.info("Arrêt du pont")
        with contextlib.suppress(Exception):
            await self.client.stop()

    def request_stop(self) -> None:
        self._stop.set()


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s: %(message)s",
    )
    if not EMAIL or not PASSWORD:
        raise SystemExit("MAMMOTION_EMAIL et MAMMOTION_PASSWORD sont requis.")
    bridge = Bridge()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, bridge.request_stop)
    await bridge.run()


if __name__ == "__main__":
    asyncio.run(main())
