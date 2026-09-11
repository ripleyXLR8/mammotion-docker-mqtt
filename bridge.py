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
import math
import os
import signal
from typing import Any

import aiomqtt
from aiohttp import web
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
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8099"))  # serveur du lecteur caméra FPV
# Sous-chemin de service, ex. "/mammocam" quand le pont est derrière un reverse
# proxy (NPM) qui ne retire PAS le préfixe. Le lecteur utilise des chemins
# relatifs, donc /mammocam/tokens et /mammocam/keepalive sont servis en plus des
# routes racine. Vide = comportement d'origine (racine seule).
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")
AGORA_SDK_URL = os.environ.get("AGORA_SDK_URL", "https://cdn.jsdelivr.net/npm/agora-rtc-sdk-ng/AgoraRTC_N-production.js")
# Doit être un numéro de version type Mammotion-HA : le serveur dérive l'en-tête
# App-Version (« HA,2.<x> ») et REFUSE le login sinon (renvoyé comme « Account or
# password mismatch » — piège vérifié le 2026-09-11, cf. PyMammotion #137).
HA_VERSION = os.environ.get("MAMMOTION_HA_VERSION", "0.6.4")
CHARGING_STATES = (1, 2)


def _parse_latlon(value: str) -> tuple[float, float] | None:
    try:
        a, b = value.split(",")
        return (float(a.strip()), float(b.strip()))
    except Exception:  # noqa: BLE001
        return None


# Ancre géographique absolue (lat,lon) du jardin/base. Les coordonnées de la
# tondeuse sont RELATIVES à la base RTK (offset de quelques mètres, ~1e-5°), pas du
# GPS absolu ; on ajoute l'offset à l'ancre pour obtenir une position affichable sur
# une carte. Sans ancre, /position renvoie l'offset brut (carte centrée sur 0,0).
GARDEN_ANCHOR = _parse_latlon(os.environ.get("GARDEN_ANCHOR", ""))

# Commandes-boutons : clé → (méthode MammotionCommand, kwargs, libellé).
COMMANDS: dict[str, tuple[str, dict[str, Any], str]] = {
    "start": ("start_job", {}, "Démarrer la tonte"),
    "pause": ("pause_execute_task", {}, "Pause"),
    "resume": ("resume_execute_task", {}, "Reprendre"),
    "continue": ("break_point_continue", {}, "Reprendre après charge"),
    "dock": ("return_to_dock", {}, "Retour à la base"),
    "cancel_dock": ("cancel_return_to_dock", {}, "Annuler le retour"),
    "cancel": ("cancel_job", {}, "Annuler la tonte"),
    "leave_dock": ("leave_dock", {}, "Quitter la base"),
    "blade_on": ("set_blade_control", {"on_off": 1}, "Lame ON"),
    "blade_off": ("set_blade_control", {"on_off": 0}, "Lame OFF"),
    "restart": ("remote_restart", {"force_reset": 1}, "Redémarrer la tondeuse"),
    "reset_blade_time": ("reset_blade_time", {}, "Réinitialiser l'usure des lames"),
    # Conduite manuelle (à-coups 0.4 comme l'app ; via cloud → latence). Stop = vitesses nulles.
    "forward": ("move_forward", {"linear": 0.4}, "Avancer"),
    "back": ("move_back", {"linear": 0.4}, "Reculer"),
    "left": ("move_left", {"angular": 0.4}, "Tourner à gauche"),
    "right": ("move_right", {"angular": 0.4}, "Tourner à droite"),
    "stop_move": ("send_movement", {"linear_speed": 0, "angular_speed": 0}, "Stop (conduite)"),
    # Debug de l'appareil
    "debug_on": ("set_debug_enable", {"enable": 1}, "Debug ON"),
    "debug_off": ("set_debug_enable", {"enable": 0}, "Debug OFF"),
}

# Niveau de position RTK (report_data.rtk.pos_level).
RTK_LEVELS = {0: "Aucune position", 1: "RTK fixe", 2: "RTK + vision", 3: "Vision seule"}

# Page lecteur du flux FPV : le navigateur est le pair WebRTC (SDK Agora Web),
# exactement comme l'app et Home Assistant. Le pont ne fournit que les jetons et
# le keep-alive. Sert aussi de corps au widget Jeedom (même JS).
PLAYER_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Caméra tondeuse</title>
<style>html,body{margin:0;background:#000;height:100%}#player{width:100%;height:100%;min-height:200px}
#status{position:absolute;top:6px;left:8px;color:#fff;font:12px sans-serif;background:rgba(0,0,0,.55);padding:2px 6px;border-radius:4px}</style>
</head><body><div id="player"></div><div id="status">connexion…</div>
<script src="{{AGORA_SDK_URL}}"></script><script>
const params=new URLSearchParams(location.search);const device=params.get('device')||'';
const base=(location.pathname.replace(/\/(player)?$/,''));
const s=document.getElementById('status');const set=t=>s.textContent=t;
let client,ka,renew;
async function tokens(){const r=await fetch(base+'/tokens?device='+encodeURIComponent(device));if(!r.ok)throw new Error('tokens '+r.status);return r.json();}
async function start(){
  if(typeof AgoraRTC==='undefined'){set('SDK Agora non chargé');return;}
  let t;try{t=await tokens();}catch(e){set('Erreur jetons: '+e.message);return;}
  if(t.error){set('Flux indisponible: '+t.error);return;}
  client=AgoraRTC.createClient({mode:'rtc',codec:'h264'});
  client.on('user-published',async(u,m)=>{try{await client.subscribe(u,m);if(m==='video'){u.videoTrack.play('player');set('');}}catch(e){set('subscribe: '+e.message);}});
  client.on('user-unpublished',()=>set('flux interrompu, maintien…'));
  try{await client.join(t.appid,t.channelName,t.token,t.uid);set('en attente du flux…');}catch(e){set('join: '+(e.message||e));return;}
  ka=setInterval(()=>fetch(base+'/keepalive?device='+encodeURIComponent(device)).catch(()=>{}),3000);
  renew=setInterval(async()=>{try{const n=await tokens();if(n.token)await client.renewToken(n.token);}catch(e){}},1800000);
}
start();
window.addEventListener('beforeunload',()=>{if(ka)clearInterval(ka);if(renew)clearInterval(renew);if(client)client.leave().catch(()=>{});});
</script></body></html>"""

# Page carte : Leaflet + tuiles OpenStreetMap (aucune clef d'API). Récupère la
# position via /position et place la tondeuse (et la base). Rafraîchit seul.
MAP_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Carte tondeuse</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<style>html,body{margin:0;height:100%}#map{width:100%;height:100%;min-height:180px;background:#aad3df}
#st{position:absolute;z-index:1000;top:6px;left:8px;font:12px sans-serif;background:rgba(0,0,0,.55);color:#fff;padding:2px 6px;border-radius:4px}</style>
</head><body><div id="map"></div><div id="st">chargement…</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script><script>
const base=location.pathname.replace(/\/map$/,'');
const st=document.getElementById('st');const set=t=>{st.textContent=t;st.style.display=t?'':'none';};
let map,mower,dock;
async function getpos(){const r=await fetch(base+'/position');if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}
function ensureMap(lat,lon){if(map)return;map=L.map('map').setView([lat,lon],19);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap'}).addTo(map);}
async function refresh(){let p;try{p=await getpos();}catch(e){set('position indisponible');return;}
  if(!p||!p.mower||p.mower[0]==null){set('pas de position');return;}
  const lat=p.mower[0],lon=p.mower[1];ensureMap(lat,lon);
  if(!mower){mower=L.marker([lat,lon]).addTo(map).bindPopup('Tondeuse');}else{mower.setLatLng([lat,lon]);}
  if(p.base&&p.base[0]!=null){if(!dock){dock=L.circleMarker(p.base,{radius:6,color:'#2e7d32',fillColor:'#2e7d32',fillOpacity:1}).addTo(map).bindPopup('Base RTK');}else{dock.setLatLng(p.base);}}
  set(p.mower_src==='device_gps'?'':(p.mower_src==='base_no_fix'?'tondeuse sans fix (montrée à la base)':'position approximative'));}
refresh();setInterval(refresh,15000);
</script></body></html>"""

# Réglages-curseurs : clé → (méthode, nom d'argument, type, min, max, pas, unité, libellé).
# Plages prudentes pour un LUBA 2 AWD — à affiner si besoin.
NUMBERS: dict[str, tuple[str, str, type, float, float, float, str, str]] = {
    "blade_height": ("set_blade_height", "height", int, 30, 70, 5, "mm", "Hauteur de coupe"),
    "speed": ("set_speed", "speed", float, 0.2, 0.6, 0.1, "m/s", "Vitesse"),
    "volume": ("set_car_volume", "volume", int, 0, 100, 10, "%", "Volume"),
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
        self.iot_ids: dict[str, str] = {}
        self._stop = asyncio.Event()

    # ---- pymammotion -------------------------------------------------------
    async def login(self) -> None:
        LOGGER.info("Connexion au cloud Mammotion (compte %s)...", EMAIL)
        await self.client.login_and_initiate_cloud(EMAIL, PASSWORD)
        all_names = [h.device_name for h in self.client.device_registry.all_devices]
        self.devices = [n for n in all_names if is_mower(n) or INCLUDE_RTK]
        self.iot_ids = {h.device_name: getattr(h, "iot_id", "") for h in self.client.device_registry.all_devices}
        LOGGER.info("Appareils : %s (retenus : %s)", all_names, self.devices)
        for name in self.devices:
            with contextlib.suppress(Exception):
                await self.client.request_reports(name, count=1, timeout=3000)

    # ---- caméra FPV : jetons Agora + serveur du lecteur --------------------
    def _first_mower(self) -> str | None:
        return next((n for n in self.devices if is_mower(n)), None)

    async def http_tokens(self, request: web.Request) -> web.Response:
        name = request.query.get("device") or self._first_mower()
        if not name or name not in self.iot_ids:
            return web.json_response({"error": "device inconnu"}, status=404)
        try:
            sub = await self.client.get_stream_subscription(name, self.iot_ids[name])
            data = getattr(sub, "data", sub)
            payload = data.to_dict() if hasattr(data, "to_dict") else dict(data)
            return web.json_response(payload)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("tokens caméra %s : %r", name, exc)
            return web.json_response({"error": str(exc)}, status=502)

    async def http_keepalive(self, request: web.Request) -> web.Response:
        name = request.query.get("device") or self._first_mower()
        if name and name in self.devices:
            with contextlib.suppress(Exception):
                await self.client.send_command_with_args(name, "refresh_fpv")
        return web.json_response({"ok": True})

    async def http_index(self, request: web.Request) -> web.Response:
        html = PLAYER_HTML.replace("{{AGORA_SDK_URL}}", AGORA_SDK_URL)
        return web.Response(text=html, content_type="text/html")

    # ---- carte : position absolue de la tondeuse --------------------------
    def _raw_location(self, name: str) -> dict[str, Any] | None:
        dev = self.client.get_device_by_name(name)
        loc = getattr(dev, "location", None) if dev else None
        if loc is None:
            return None

        def pt(o: Any) -> dict[str, Any] | None:
            if o is None:
                return None
            return {"lat": getattr(o, "latitude", None),
                    "lon": getattr(o, "longitude", None),
                    "yaw": getattr(o, "yaw", None)}

        return {"device": pt(getattr(loc, "device", None)),
                "RTK": pt(getattr(loc, "RTK", None)),
                "dock": pt(getattr(loc, "dock", None)),
                "position_type": getattr(loc, "position_type", None)}

    @staticmethod
    def _plausible_abs(lat: Any, lon: Any) -> bool:
        """Vraie coordonnée absolue (pas un offset proche de 0 ni du bruit)."""
        try:
            return lat is not None and lon is not None and 1.0 < abs(float(lat)) <= 90.0 and abs(float(lon)) <= 180.0
        except (TypeError, ValueError):
            return False

    async def http_map(self, request: web.Request) -> web.Response:
        return web.Response(text=MAP_HTML, content_type="text/html")

    def _rtk_base_abs(self, rtk: dict[str, Any] | None) -> list[float] | None:
        """Position absolue de la base RTK. ``location.RTK`` est en RADIANS
        (0.855 rad ≈ 49°) ; on convertit en degrés. Sert de repère « base »."""
        if not rtk or rtk.get("lat") is None:
            return None
        lat, lon = rtk["lat"], rtk["lon"]
        try:
            dlat, dlon = math.degrees(float(lat)), math.degrees(float(lon))
        except (TypeError, ValueError):
            return None
        if self._plausible_abs(dlat, dlon):
            return [dlat, dlon]
        if self._plausible_abs(lat, lon):  # déjà en degrés, au cas où
            return [float(lat), float(lon)]
        return None

    async def http_position(self, request: web.Request) -> web.Response:
        name = request.query.get("device") or self._first_mower()
        raw = self._raw_location(name) if name else None
        dev = raw.get("device") if raw else None
        rtk = raw.get("RTK") if raw else None
        base = self._rtk_base_abs(rtk)
        anchor = base or (list(GARDEN_ANCHOR) if GARDEN_ANCHOR else None)
        # ``device`` est en degrés absolus dès qu'il y a un fix RTK ; sans fix il est
        # proche de 0 → on montre alors la tondeuse à la base (ou à l'ancre).
        if dev and self._plausible_abs(dev.get("lat"), dev.get("lon")):
            mower, mower_src = [float(dev["lat"]), float(dev["lon"])], "device_gps"
        elif anchor:
            mower, mower_src = anchor, ("base_no_fix" if base else "anchor_env")
        else:
            mower, mower_src = None, None
        return web.json_response({
            "mower": mower,
            "mower_src": mower_src,
            "base": base,
            "anchor_env": list(GARDEN_ANCHOR) if GARDEN_ANCHOR else None,
            "raw": raw,
        })

    async def start_http(self) -> None:
        app = web.Application()
        prefixes = [""]
        if BASE_PATH and BASE_PATH not in prefixes:
            prefixes.append(BASE_PATH)
        routes = []
        for p in prefixes:
            if p:  # servir aussi le sous-chemin sans slash final
                routes.append(web.get(p, self.http_index))
            routes += [
                web.get(f"{p}/", self.http_index),
                web.get(f"{p}/player", self.http_index),
                web.get(f"{p}/tokens", self.http_tokens),
                web.get(f"{p}/keepalive", self.http_keepalive),
                web.get(f"{p}/map", self.http_map),
                web.get(f"{p}/position", self.http_position),
            ]
        app.add_routes(routes)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
        await site.start()
        LOGGER.info("Lecteur caméra sur http://0.0.0.0:%s%s/ (page) et %s/tokens",
                    HTTP_PORT, BASE_PATH, BASE_PATH or "")

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
        # La progression n'est un pourcentage qu'en cours de tonte ; hors plage
        # (valeurs internes négatives à l'arrêt) → 0.
        raw_progress = getattr(work, "progress", 0) if work else 0
        progress = raw_progress if isinstance(raw_progress, (int, float)) and 0 <= raw_progress <= 100 else 0
        fields: dict[str, Any] = {
            "online": "online" if getattr(dev, "online", True) else "offline",
            "status": device_mode(getattr(d, "sys_status", 0)) if d else "MODE_OFFLINE",
            "battery": getattr(d, "battery_val", None) if d else None,
            "charging": "ON" if (d and getattr(d, "charge_state", 0) in CHARGING_STATES) else "OFF",
            "blade": "ON" if getattr(dev.mower_state, "blade_status", False) else "OFF",
            "progress": progress,
            "network": device_connection(rd.connect) if getattr(rd, "connect", None) else None,
            "error": ", ".join(str(e) for e in err_list) if err_list else "aucune",
        }
        if loc is not None and getattr(loc, "device", None) is not None:
            fields["latitude"] = getattr(loc.device, "latitude", None)
            fields["longitude"] = getattr(loc.device, "longitude", None)
        # Réseau + RTK
        conn = getattr(rd, "connect", None)
        rtk = getattr(rd, "rtk", None)
        ms2 = getattr(dev, "mowing_state", None)
        fields["wifi_rssi"] = getattr(conn, "wifi_rssi", None) if conn else None
        fields["mnet_rssi"] = getattr(conn, "mnet_rssi", None) if conn else None
        fields["satellites"] = getattr(ms2, "satellites_total", None) if ms2 else None
        fields["rtk_age"] = getattr(rtk, "age", None) if rtk else None
        if rtk is not None:
            lvl = getattr(rtk, "pos_level", 0)
            fields["rtk_level"] = RTK_LEVELS.get(lvl, str(lvl))
        return fields

    def _number_values(self, name: str) -> dict[str, Any]:
        """Valeurs courantes des réglages (best-effort ; 0 à l'arrêt pour certains)."""
        dev = self.client.get_device_by_name(name)
        if dev is None:
            return {}
        ms = getattr(dev, "mower_state", None)
        work = getattr(getattr(dev, "report_data", None), "work", None)
        audio = getattr(ms, "audio", None) if ms else None
        return {
            "blade_height": getattr(work, "knife_height", None) if work else None,
            "speed": getattr(ms, "travel_speed", None) if ms else None,
            "volume": getattr(audio, "volume", None) if audio else None,
        }

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
            for key, value in self._number_values(name).items():
                if value is not None:
                    await self.mqtt.publish(f"{base}/num/{key}/state", value, retain=True)

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
            ("wifi_rssi", "WiFi RSSI", "signal_strength", "dBm", None),
            ("mnet_rssi", "4G RSSI", "signal_strength", "dBm", None),
            ("satellites", "Satellites", None, None, "mdi:satellite-variant"),
            ("rtk_level", "Niveau RTK", None, None, "mdi:crosshairs-gps"),
            ("rtk_age", "Âge RTK", None, "s", "mdi:timer-outline"),
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
            for cmd, (_key, _kwargs, label) in COMMANDS.items():
                cfg = {"name": label, "unique_id": f"{slug}_{cmd}",
                       "command_topic": f"{base}/cmd/{cmd}/set", "payload_press": "PRESS",
                       "device": device_block, **avail}
                await self.mqtt.publish(
                    f"{DISCOVERY_PREFIX}/button/{slug}/{cmd}/config",
                    json.dumps(cfg, ensure_ascii=False), retain=True)
            for key, (_m, _a, _c, mn, mx, step, unit, label) in NUMBERS.items():
                cfg = {"name": label, "unique_id": f"{slug}_{key}",
                       "command_topic": f"{base}/num/{key}/set",
                       "state_topic": f"{base}/num/{key}/state",
                       "min": mn, "max": mx, "step": step, "unit_of_measurement": unit,
                       "mode": "slider", "device": device_block, **avail}
                await self.mqtt.publish(
                    f"{DISCOVERY_PREFIX}/number/{slug}/{key}/config",
                    json.dumps(cfg, ensure_ascii=False), retain=True)

    # ---- commandes MQTT → tondeuse ----------------------------------------
    async def handle_command(self, topic: str, payload: str) -> None:
        # topic = mammotion/<dev>/cmd|num/<clé>/set
        parts = topic.split("/")
        if len(parts) < 5 or parts[-1] != "set" or parts[-3] not in ("cmd", "num"):
            return
        kind, key = parts[-3], parts[-2]
        name = "/".join(parts[1:-3])
        if name not in self.devices:
            return
        try:
            if kind == "cmd" and key in COMMANDS:
                method, kwargs, _ = COMMANDS[key]
                LOGGER.info("Bouton %s → %s(%s) sur %s", key, method, kwargs, name)
                await self.client.send_command_with_args(name, method, **kwargs)
            elif kind == "num" and key in NUMBERS:
                method, arg, cast, mn, mx, *_ = NUMBERS[key]
                value = cast(max(mn, min(mx, float(payload))))
                LOGGER.info("Réglage %s → %s(%s=%s) sur %s", key, method, arg, value, name)
                await self.client.send_command_with_args(name, method, **{arg: value})
            else:
                LOGGER.warning("Commande inconnue: %s %s / %s", kind, key, name)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Échec de %s %s sur %s : %r", kind, key, name, exc)

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
        with contextlib.suppress(Exception):
            await self.start_http()
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
                    await mqtt.subscribe(f"{TOPIC_PREFIX}/+/num/+/set")
                    state_task = asyncio.create_task(self.state_loop())
                    try:
                        async for message in mqtt.messages:
                            payload = message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload)
                            await self.handle_command(str(message.topic), payload)
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
