"""
4DFX - Core

Logica compartida entre la CLI (4dfx.py) y la interfaz grafica
(fdfx_gui.py). No depende de tkinter ni de ningun frontend: solo de
'requests' de la libreria estandar, para que corra igual en macOS, Windows
y Raspberry Pi 4.

Conceptos:
- "device": cualquier salida controlada por Home Assistant (maquina de humo,
  valvula de agua, luces, estrobos, ventiladores, etc). Hay dos tipos (kind):
    * "binary": on/off simple (switch, cover, light basico). Sus cues usan
      'duration_s' -> se enciende, se espera esa duracion, se apaga solo.
    * "multi_state": tiene varios estados con nombre (ej OFF/ECO/LOW/MED/HIGH
      para un ventilador, o efectos de luz a futuro). Cada estado define que
      servicio de HA llamar y con que datos extra. Sus cues usan 'state' ->
      se llama ese estado y se mantiene hasta el siguiente cue del mismo
      dispositivo (no hay apagado automatico).
- "cue": un evento con timestamp 't' dentro de una pelicula/concierto que
  dispara un 'device', ya sea por duracion (burst) o por estado (state).
- "cue sheet": archivo .json con una lista de cues, asociado a un titulo
  via el campo 'match' (substring, case-insensitive).
- Formato de import compatible con los archivos de la comunidad AVS Forum
  ("4D Theater Wind Effect"): lineas 'HH:MM:SS.mmm,ESTADO', con '#' para
  comentarios. Ver import_state_txt().
"""

import json
import shutil
import socket
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

try:
    import paho.mqtt.publish as mqtt_publish_mod
except ImportError:
    mqtt_publish_mod = None

CONFIG_DIR = Path.home() / ".4dfx"
CONFIG_PATH = CONFIG_DIR / "config.json"
_OLD_CONFIG_DIR = Path.home() / ".smokesync"


def _migrate_old_config_dir():
    """Si existe la carpeta de config del nombre anterior (SmokeSync) y la
    nueva (.4dfx) todavia no, la renombramos para no perder la configuracion,
    dispositivos y cue sheets ya guardados."""
    if _OLD_CONFIG_DIR.exists() and not CONFIG_DIR.exists():
        _OLD_CONFIG_DIR.rename(CONFIG_DIR)


_migrate_old_config_dir()

# Mapeo dominio de entidad HA -> servicios on/off (control binario)
DOMAIN_SERVICES = {
    "switch":        ("switch/turn_on",  "switch/turn_off"),
    "input_boolean": ("input_boolean/turn_on", "input_boolean/turn_off"),
    "light":         ("light/turn_on",   "light/turn_off"),
    "cover":         ("cover/open_cover", "cover/close_cover"),
    "fan":           ("fan/turn_on",     "fan/turn_off"),
    "valve":         ("valve/open_valve", "valve/close_valve"),
}

DEFAULT_CFG = {
    "player_type": "zidoo",  # zidoo | vlc | jriver
    "zidoo_ip": "",
    "zidoo_port": 9529,
    "vlc_url": "http://127.0.0.1:8080",
    "vlc_password": "",
    "jriver_url": "http://127.0.0.1:52199",
    "jriver_user": "",
    "jriver_password": "",
    "ha_url": "",
    "ha_token": "",
    "mqtt_host": "",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_password": "",
    "lead_time_s": 0,
    "default_duration_s": 3,
    "poll_interval_s": 0.5,
    "max_late_s": 3.0,
    "cues_dir": str(CONFIG_DIR / "cues"),
    "devices": [],
}


def services_for_entity(entity_id):
    domain = entity_id.split(".")[0]
    return DOMAIN_SERVICES.get(domain, DOMAIN_SERVICES["switch"])


def suggested_kind_for_entity(entity_id):
    """'scene' no tiene apagado (solo se activa); todo lo demas se sugiere
    binario por defecto."""
    return "scene" if entity_id.split(".")[0] == "scene" else "binary"


# --- Configuracion -------------------------------------------------------
def load_config():
    if not CONFIG_PATH.exists():
        return None
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return migrate_config(cfg)


def migrate_config(cfg):
    """Convierte configs viejas (un solo entity_id de humo) al formato con
    lista de 'devices', sin romper compatibilidad."""
    merged = {**DEFAULT_CFG, **cfg}
    if not merged.get("devices") and cfg.get("entity_id"):
        on_srv = cfg.get("on_service") or services_for_entity(cfg["entity_id"])[0]
        off_srv = cfg.get("off_service") or services_for_entity(cfg["entity_id"])[1]
        merged["devices"] = [{
            "name": "humo",
            "kind": "binary",
            "entity_id": cfg["entity_id"],
            "on_service": on_srv,
            "off_service": off_srv,
        }]
    merged.pop("entity_id", None)
    merged.pop("on_service", None)
    merged.pop("off_service", None)
    for d in merged.get("devices", []):
        d.setdefault("kind", "binary")
        if d["kind"] == "binary":
            # 'on_data'/'off_data' son el payload completo que se manda a HA
            # (entity_id + cualquier dato extra). Si el dispositivo viene del
            # formato viejo (solo entity_id + servicios), se completa con el
            # payload simple {"entity_id": ...} para no romper compatibilidad.
            d.setdefault("on_data", {"entity_id": d.get("entity_id")})
            d.setdefault("off_data", {"entity_id": d.get("entity_id")})
    return merged


def normalize_ha_url(url):
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


def save_config(cfg):
    cfg["ha_url"] = normalize_ha_url(cfg.get("ha_url"))
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                            encoding="utf-8")


def get_device(cfg, name):
    if not name:
        return None
    name = name.strip().lower()
    for d in cfg.get("devices", []):
        if d["name"].strip().lower() == name:
            return d
    return None


def default_device(cfg):
    devices = cfg.get("devices", [])
    return devices[0] if devices else None


# Estados sugeridos por defecto para un dispositivo multi_state nuevo tipo
# ventilador (coincide con las etiquetas usadas por los archivos de la
# comunidad AVS Forum: OFF/ECO/LOW/MED/HIGH).
DEFAULT_FAN_STATES = {
    "OFF":  {"service": "fan/turn_off", "data": {}, "shortcut": "0"},
    "ECO":  {"service": "fan/set_percentage", "data": {"percentage": 25}, "shortcut": "9"},
    "LOW":  {"service": "fan/set_percentage", "data": {"percentage": 45}, "shortcut": "1"},
    "MED":  {"service": "fan/set_percentage", "data": {"percentage": 70}, "shortcut": "2"},
    "HIGH": {"service": "fan/set_percentage", "data": {"percentage": 100}, "shortcut": "3"},
}


# --- Zidoo -----------------------------------------------------------------
def zidoo_status(cfg):
    """Devuelve dict crudo de getPlayStatus, o {'_error': ...} si no responde."""
    url = f"http://{cfg['zidoo_ip']}:{cfg['zidoo_port']}/ZidooVideoPlay/getPlayStatus"
    try:
        r = requests.get(url, timeout=3)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"_error": str(e)}


def parse_playback(j):
    """Extrae (title, position_seconds, is_playing) del JSON del Zidoo."""
    if not j or j.get("_error"):
        return None, None, False
    video = j.get("video", {}) or {}
    pos_ms = video.get("currentPosition")
    title = video.get("title") or ""
    path = video.get("path") or ""
    if not title and path:
        title = Path(path).stem
    is_playing = video.get("status") in (1, "1", True)
    pos_s = (pos_ms / 1000.0) if isinstance(pos_ms, (int, float)) else None
    return (title or path), pos_s, is_playing


# --- VLC ---------------------------------------------------------------------
def vlc_playback(cfg):
    """Lee estado via la interfaz HTTP de VLC (Preferencias > Interfaz > Web,
    con un password configurado). Devuelve (title, pos_s, playing, error)."""
    url = f"{cfg['vlc_url'].rstrip('/')}/requests/status.json"
    try:
        r = requests.get(url, auth=("", cfg.get("vlc_password", "")), timeout=3)
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        return None, None, False, str(e)
    meta = (j.get("information", {}) or {}).get("category", {}).get("meta", {}) or {}
    title = meta.get("title") or meta.get("filename") or ""
    pos = j.get("time")
    playing = j.get("state") == "playing"
    return (title or None), (float(pos) if pos is not None else None), playing, None


# --- JRiver Media Center (MCWS) -----------------------------------------------
def jriver_playback(cfg):
    """Lee estado via la Media Center Web Service (MCWS) de JRiver.
    Devuelve (title, pos_s, playing, error)."""
    url = f"{cfg['jriver_url'].rstrip('/')}/MCWS/v1/Playback/Info"
    auth = (cfg.get("jriver_user") or None, cfg.get("jriver_password") or None)
    try:
        r = requests.get(url, auth=auth if auth[0] else None, timeout=3)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        fields = {item.get("Name"): (item.text or "") for item in root.findall("Item")}
    except Exception as e:
        return None, None, False, str(e)
    title = fields.get("Filename") or fields.get("Name") or ""
    pos_ms = fields.get("PositionMS")
    pos = float(pos_ms) / 1000.0 if pos_ms else None
    # MCWS: State 0=detenido, 1=pausado, 2=reproduciendo
    playing = fields.get("State") == "2"
    return (title or None), pos, playing, None


def vlc_control(cfg, command, **params):
    """Envia un comando a la interfaz HTTP de VLC (play/pause/seek/in_play...).
    Ver https://wiki.videolan.org/VLC_HTTP_requests/"""
    url = f"{cfg['vlc_url'].rstrip('/')}/requests/status.json"
    q = {"command": command, **params}
    try:
        r = requests.get(url, params=q, auth=("", cfg.get("vlc_password", "")), timeout=3)
        r.raise_for_status()
        return True, None
    except Exception as e:
        return False, str(e)


_VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".m4v", ".ts", ".m2ts", ".wmv")


def strip_video_ext(title):
    """Quita la extension de video al final del titulo si la trae (algunos
    reproductores reportan el nombre de archivo completo, otros no) - asi
    'Pelicula.mp4' y 'Pelicula' generan siempre el mismo match/slug de cue
    sheet en vez de crear dos archivos distintos para el mismo video."""
    for ext in _VIDEO_EXTENSIONS:
        if title.lower().endswith(ext):
            return title[: -len(ext)]
    return title


def slug_for_title(title):
    clean = strip_video_ext(title)
    return "".join(c if c.isalnum() else "_" for c in clean.lower()).strip("_")


def local_ip():
    """IP de esta maquina en la red local (util para configurar Shelly/HA/
    MQTT apuntando aqui). No abre conexion real, solo usa el truco UDP para
    que el SO resuelva que interfaz usaria."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def find_vlc_binary():
    """Ubica el ejecutable de VLC segun la plataforma. Devuelve None si no se encuentra."""
    if sys.platform == "darwin":
        candidates = ["/Applications/VLC.app/Contents/MacOS/VLC"]
    elif sys.platform == "win32":
        candidates = [
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
        ]
    else:
        candidates = ["/usr/bin/vlc", "/usr/local/bin/vlc"]
    for c in candidates:
        if Path(c).exists():
            return c
    found = shutil.which("vlc")
    return found


def launch_vlc_with_file(cfg, path):
    """Lanza VLC (ventana propia, no embebida) con el archivo dado y su
    interfaz HTTP activada, usando vlc_url/vlc_password de cfg para poder
    monitorear/controlar la reproduccion despues. Devuelve (ok, error_msg)."""
    binary = find_vlc_binary()
    if not binary:
        return False, "No se encontro el ejecutable de VLC en esta maquina."
    try:
        port = cfg["vlc_url"].rsplit(":", 1)[-1]
        int(port)
    except (KeyError, ValueError):
        port = "8080"
    args = [
        binary, path,
        "--extraintf", "http",
        # 0.0.0.0 en vez de 127.0.0.1: si vlc_url usa la IP de la red local
        # (en vez de localhost), VLC debe escuchar ahi tambien, no solo en
        # loopback, o la app no podra conectarse (Connection refused).
        "--http-host", "0.0.0.0",
        "--http-port", str(port),
        "--http-password", cfg.get("vlc_password", "") or "4dfx",
        # Mantiene la ventana de VLC siempre visible aunque la app tenga el
        # foco (son procesos/ventanas separados; sin esto, VLC se va detras
        # al hacer clic en la app).
        "--video-on-top",
    ]
    if not cfg.get("vlc_password"):
        cfg["vlc_password"] = "4dfx"
    try:
        subprocess.Popen(args)
        if sys.platform == "darwin":
            threading.Timer(2.0, _position_vlc_window_macos).start()
        return True, None
    except Exception as e:
        return False, str(e)


def _position_vlc_window_macos(x=760, y=60, w=760, h=480):
    """Mueve/redimensiona la ventana de VLC a un lugar fijo, para no tener
    que acomodarla a mano cada vez que se abre un video para editar cues.
    Usa System Events (requiere dar permiso de Accesibilidad la primera vez
    a la Terminal/app que corre esto, en Ajustes > Privacidad y Seguridad)."""
    script = (
        'tell application "VLC" to activate\n'
        'delay 0.3\n'
        'tell application "System Events"\n'
        '  tell process "VLC"\n'
        f'    set position of front window to {{{x}, {y}}}\n'
        f'    set size of front window to {{{w}, {h}}}\n'
        '  end tell\n'
        'end tell\n'
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        pass


def get_playback(cfg):
    """Punto unico de lectura de reproduccion: despacha al backend elegido en
    cfg['player_type'] (zidoo/vlc/jriver). Devuelve (title, pos_s, playing, error)."""
    ptype = cfg.get("player_type", "zidoo")
    if ptype == "vlc":
        return vlc_playback(cfg)
    if ptype == "jriver":
        return jriver_playback(cfg)
    j = zidoo_status(cfg)
    if j.get("_error"):
        return None, None, False, j["_error"]
    title, pos, playing = parse_playback(j)
    return title, pos, playing, None


# --- Home Assistant ----------------------------------------------------------
def ha_headers(cfg):
    return {"Authorization": f"Bearer {cfg['ha_token']}",
            "Content-Type": "application/json"}


def ha_ping(cfg):
    """Devuelve (ok, detalle). El detalle explica la causa cuando falla."""
    url = f"{cfg['ha_url']}/api/"
    try:
        r = requests.get(url, headers=ha_headers(cfg), timeout=5)
    except requests.exceptions.SSLError as e:
        return False, f"Error de certificado SSL en {url}: {e}"
    except requests.exceptions.ConnectionError as e:
        return False, f"No se pudo conectar a {url}: {e}"
    except requests.exceptions.Timeout:
        return False, f"Timeout conectando a {url} (revisa IP/puerto o firewall)"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    if r.status_code == 401:
        return False, "401 Unauthorized: el token es invalido o expiro"
    if r.status_code == 404:
        return False, "404: la URL no expone /api/ (revisa que sea la URL base de HA)"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    return True, "OK"


def ha_call(cfg, service_path, entity_id, log=print):
    return ha_call_service(cfg, service_path, {"entity_id": entity_id}, log)


def ha_call_service(cfg, service_path, data, log=print):
    """Llama cualquier servicio de HA con un payload arbitrario (entity_id +
    datos extra, ej percentage/color/effect)."""
    url = f"{cfg['ha_url']}/api/services/{service_path}"
    try:
        r = requests.post(url, headers=ha_headers(cfg), json=data, timeout=5)
        r.raise_for_status()
        return True
    except Exception as e:
        log(f"  [HA] fallo al llamar {service_path}: {e}")
        return False


def set_device_state_async(cfg, device, state_name, log=print, on_done=None):
    """Para dispositivos multi_state: llama el servicio definido para ese
    estado. No hay apagado automatico; el estado se mantiene hasta el
    siguiente cue del mismo dispositivo (igual que los archivos de la
    comunidad AVS Forum). on_done(ok, detail) reporta el resultado."""
    state = (device.get("states") or {}).get(state_name)

    def _run():
        if not state:
            detail = f"'{device['name']}' no tiene estado '{state_name}' definido"
            log(f"  [HA] {detail}")
            ok = False
        else:
            data = {"entity_id": device["entity_id"], **state.get("data", {})}
            ok = ha_call_service(cfg, state["service"], data, log)
            detail = f"estado {state_name}" if ok else f"fallo estado {state_name}"
        if on_done:
            on_done(ok, detail)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def fire_device_async(cfg, device, duration, log=print, on_done=None):
    """Para dispositivos binary: enciende, espera 'duration' y apaga.
    on_data/off_data son el payload completo (entity_id + cualquier dato
    extra), lo que permite dispositivos que no son una entidad HA nativa
    sino un script/servicio generico (ej remote.send_command de un
    Broadlink con 'device'/'command' en el payload).
    on_done(ok, detail) reporta el resultado combinado."""
    def _run():
        on_data = device.get("on_data") or {"entity_id": device.get("entity_id")}
        off_data = device.get("off_data") or {"entity_id": device.get("entity_id")}
        ok1 = ha_call_service(cfg, device["on_service"], on_data, log)
        time.sleep(duration)
        ok2 = ha_call_service(cfg, device["off_service"], off_data, log)
        ok = ok1 and ok2
        detail = f"burst {duration}s" if ok else "fallo burst"
        if on_done:
            on_done(ok, detail)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def activate_scene_async(cfg, device, log=print, on_done=None):
    """Para dispositivos kind=scene: solo se activa (scene.turn_on), no hay
    apagado. on_done(ok, detail) reporta el resultado."""
    service = device.get("activate_service", "scene/turn_on")

    def _run():
        ok = ha_call(cfg, service, device["entity_id"], log)
        detail = "escena activada" if ok else "fallo al activar escena"
        if on_done:
            on_done(ok, detail)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# --- MQTT directo (sin pasar por Home Assistant) ------------------------------
def mqtt_publish(cfg, topic, payload, log=print):
    """Publica un mensaje MQTT directo al broker configurado (para probar
    dispositivos - ej. Shelly, estrobos - sin depender de Home Assistant).
    Conexion efimera: conecta, publica, desconecta."""
    if mqtt_publish_mod is None:
        log("  [MQTT] falta el paquete 'paho-mqtt'. Instala con: pip install paho-mqtt")
        return False
    auth = None
    if cfg.get("mqtt_user"):
        auth = {"username": cfg["mqtt_user"], "password": cfg.get("mqtt_password", "")}
    try:
        mqtt_publish_mod.single(topic, payload=payload, hostname=cfg.get("mqtt_host", ""),
                                 port=int(cfg.get("mqtt_port", 1883)), auth=auth)
        return True
    except Exception as e:
        log(f"  [MQTT] fallo al publicar en '{topic}': {e}")
        return False


def fire_binary_side_async(cfg, device, side, log=print, on_done=None):
    """Dispara solo un lado (on/off) de un dispositivo binary, sin esperar ni
    apagar despues. Util para probar el payload de apagado de forma
    independiente (ej. confirmar que 'off' realmente apaga un estrobo)."""
    def _run():
        service = device["on_service"] if side == "on" else device["off_service"]
        data = device.get(f"{side}_data") or {"entity_id": device.get("entity_id")}
        ok = ha_call_service(cfg, service, data, log)
        detail = f"{side} enviado" if ok else f"fallo {side}"
        if on_done:
            on_done(ok, detail)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def fire_mqtt_side_async(cfg, device, side, log=print, on_done=None):
    """Dispara solo un lado (on/off) de un dispositivo mqtt, sin esperar ni
    apagar despues. Util para probar el payload de apagado de forma
    independiente (ej. confirmar que 'off' realmente apaga un estrobo)."""
    def _run():
        topic = device[f"{side}_topic"]
        payload = device.get(f"{side}_payload", side.upper())
        ok = mqtt_publish(cfg, topic, payload, log)
        detail = f"{side} enviado" if ok else f"fallo {side}"
        if on_done:
            on_done(ok, detail)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def fire_mqtt_device_async(cfg, device, duration, log=print, on_done=None):
    """Para dispositivos kind=mqtt: publica el payload ON, espera 'duration'
    y publica el payload OFF. on_done(ok, detail) reporta el resultado."""
    def _run():
        ok1 = mqtt_publish(cfg, device["on_topic"], device.get("on_payload", "ON"), log)
        time.sleep(duration)
        ok2 = mqtt_publish(cfg, device["off_topic"], device.get("off_payload", "OFF"), log)
        ok = ok1 and ok2
        detail = f"burst mqtt {duration}s" if ok else "fallo burst mqtt"
        if on_done:
            on_done(ok, detail)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# --- Cue sheets --------------------------------------------------------------
def parse_time(v):
    """Acepta segundos (num), 'MM:SS' o 'HH:MM:SS'."""
    if isinstance(v, (int, float)):
        return float(v)
    parts = str(v).split(":")
    parts = [float(p) for p in parts]
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def load_cue_sheets(cfg, log=print):
    sheets = []
    d = Path(cfg["cues_dir"])
    d.mkdir(parents=True, exist_ok=True)
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_file"] = f.name
            sheets.append(data)
        except Exception as e:
            log(f"  [cues] no pude leer {f.name}: {e}")
    return sheets


def match_sheet(sheets, title):
    if not title:
        return None
    tl = title.lower()
    for s in sheets:
        m = str(s.get("match", "")).lower()
        if m and m in tl:
            return s
    return None


def import_state_txt(path, device_name):
    """Convierte un archivo de la comunidad AVS Forum ('4D Theater Wind
    Effect'), con lineas 'HH:MM:SS.mmm,ESTADO' y '#' como comentario, a una
    lista de cues 'state' para 'device_name'. No agrupa ni filtra: conserva
    el archivo tal cual, un cue por linea."""
    cues = []
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "," not in line:
            continue
        t_str, state = line.split(",", 1)
        t_str, state = t_str.strip(), state.strip().upper()
        try:
            parse_time(t_str)
        except Exception:
            continue
        cues.append({"t": t_str, "device": device_name, "state": state})
    cues.sort(key=lambda c: parse_time(c["t"]))
    return cues


def fire_at(sheet, cue, cfg, device=None):
    """Momento real en que se dispara el cue, restando el lead time para
    compensar el lag de reaccion del hardware. Prioridad: lead_time_s del
    dispositivo (cada uno puede tener un lag distinto, ej. un estrobo
    reacciona distinto a una maquina de humo) > el de la cue sheet > el
    global de la configuracion."""
    if device is not None and device.get("lead_time_s") not in (None, ""):
        lead = float(device["lead_time_s"])
    else:
        lead = float(sheet.get("lead_time_s", cfg["lead_time_s"]))
    return max(0.0, parse_time(cue["t"]) - lead)


def fmt_time(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def fmt_time_ms(sec):
    """Como fmt_time pero con milisegundos, para capturas en vivo con
    precision de fotograma (ej. '01:04:29.430')."""
    sec = max(0.0, float(sec))
    whole = int(sec)
    ms = round((sec - whole) * 1000)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    prefix = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    return f"{prefix}.{ms:03d}"


# --- Motor de sincronizacion (usado por CLI y GUI) ----------------------------
class SyncEngine:
    """Ejecuta el loop de vigilancia del Zidoo + disparo de cues en un hilo
    propio, para que un frontend (CLI o GUI) pueda arrancarlo/detenerlo sin
    bloquearse."""

    def __init__(self, cfg, log=print, on_state=None):
        self.cfg = cfg
        self.log = log
        self.on_state = on_state or (lambda **kw: None)
        self._stop = threading.Event()
        self._refresh = threading.Event()
        self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def refresh_sheets(self):
        """Fuerza a releer las cue sheets de disco y re-evaluar cual aplica
        al titulo actual, sin esperar a que el titulo cambie. Util cuando el
        usuario guarda/edita la cue sheet del video que ya esta reproduciendo
        (si no, el motor se queda con el 'sin cue sheet' que vio al arrancar)."""
        self._refresh.set()

    def _loop(self):
        cfg = self.cfg
        sheets = load_cue_sheets(cfg, self.log)
        self.log(f"Cargados {len(sheets)} cue sheet(s). Vigilando "
                 f"'{cfg.get('player_type', 'zidoo')}'...")

        active_title = None
        active_sheet = None
        fired = set()
        last_pos = -1

        while not self._stop.is_set():
            try:
                if self._refresh.is_set():
                    self._refresh.clear()
                    sheets = load_cue_sheets(cfg, self.log)
                    active_sheet = match_sheet(sheets, active_title) if active_title else None
                    fired = set()
                    last_pos = -1
                    if active_sheet:
                        self.log(f">> cue sheet recargada: {active_sheet['_file']} "
                                 f"({len(active_sheet.get('cues', []))} cues)")
                    else:
                        self.log(">> cue sheets recargadas, sigue sin haber match para el titulo actual.")

                title, pos, playing, _error = get_playback(cfg)
                self.on_state(title=title, pos=pos, playing=playing,
                              sheet=active_sheet)

                # Solo tratamos esto como "cambio de titulo" real cuando el
                # Zidoo reporta un titulo valido y distinto del actual. Un
                # error transitorio de red o un status vacio no debe borrar
                # los cues ya disparados (si no, un hipo de conexion vuelve
                # a armar y disparar cues ya pasados).
                if title and title != active_title:
                    active_title = title
                    fired = set()
                    last_pos = -1
                    active_sheet = match_sheet(sheets, title)
                    if active_sheet:
                        self.log(f">> '{title}' -> cues: {active_sheet['_file']} "
                                 f"({len(active_sheet.get('cues', []))} cues)")
                        for cue in active_sheet.get("cues", []):
                            dev = get_device(cfg, cue.get("device") or (default_device(cfg) or {}).get("name"))
                            t_fire = fire_at(active_sheet, cue, cfg, dev)
                            self.log(f"     cue {fmt_time(parse_time(cue['t']))} "
                                     f"({cue.get('device', '?')}) -> dispara en ventana "
                                     f"[{fmt_time(t_fire)}, {fmt_time(t_fire + cfg['max_late_s'])})")
                    else:
                        self.log(f">> '{title}' sin cue sheet.")

                if playing and active_sheet and pos is not None:
                    def _dev_for(cue):
                        return get_device(cfg, cue.get("device") or (default_device(cfg) or {}).get("name"))

                    if pos + 1 < last_pos:
                        fired = {i for i in fired
                                 if fire_at(active_sheet, active_sheet["cues"][i], cfg,
                                            _dev_for(active_sheet["cues"][i])) < pos}
                    last_pos = pos

                    cues = active_sheet.get("cues", [])
                    # Si el usuario adelanto rapido (seek), puede haber varios
                    # cues de 'state' ya vencidos y sin disparar. Solo nos
                    # interesa alcanzar el ULTIMO estado por dispositivo (no
                    # bombardear HA con todos los saltados en orden).
                    last_skipped_by_device = {}
                    for i, cue in enumerate(cues):
                        if i in fired or "state" not in cue:
                            continue
                        t_fire = fire_at(active_sheet, cue, cfg, _dev_for(cue))
                        if pos >= t_fire + cfg["max_late_s"]:
                            dev_name = cue.get("device") or (default_device(cfg) or {}).get("name")
                            last_skipped_by_device[dev_name] = i

                    for i, cue in enumerate(cues):
                        if i in fired:
                            continue
                        dev_name = cue.get("device") or (default_device(cfg) or {}).get("name")
                        device = get_device(cfg, dev_name)
                        t_fire = fire_at(active_sheet, cue, cfg, device)
                        is_state_cue = "state" in cue
                        in_window = t_fire <= pos < t_fire + cfg["max_late_s"]
                        # Los cues de 'state' (ventiladores/luces multi-estado) se
                        # "alcanzan" aunque el usuario haya adelantado rapido y ya
                        # se haya salido de la ventana normal: el estado debe
                        # quedar correcto igual. Los de 'duration_s' (rafaga) no,
                        # una rafaga fuera de tiempo no tiene sentido.
                        catch_up = (is_state_cue and pos >= t_fire + cfg["max_late_s"]
                                    and last_skipped_by_device.get(dev_name) == i)
                        if in_window or catch_up:
                            if not device:
                                self.log(f"[{fmt_time(pos)}] cue sin device valido ('{dev_name}')")
                            elif is_state_cue:
                                self.log(f"[{fmt_time(pos)}] {device['name'].upper()} -> "
                                         f"estado {cue['state']} (cue {fmt_time(parse_time(cue['t']))})")
                                set_device_state_async(cfg, device, cue["state"], self.log)
                            elif device.get("kind") == "scene":
                                self.log(f"[{fmt_time(pos)}] {device['name'].upper()} -> "
                                         f"activar escena (cue {fmt_time(parse_time(cue['t']))})")
                                activate_scene_async(cfg, device, self.log)
                            elif device.get("kind") == "mqtt":
                                dur = float(cue.get("duration_s", cfg["default_duration_s"]))
                                self.log(f"[{fmt_time(pos)}] {device['name'].upper()} -> "
                                         f"cue mqtt {fmt_time(parse_time(cue['t']))} ({dur}s)")
                                fire_mqtt_device_async(cfg, device, dur, self.log)
                            else:
                                dur = float(cue.get("duration_s", cfg["default_duration_s"]))
                                self.log(f"[{fmt_time(pos)}] {device['name'].upper()} -> "
                                         f"cue {fmt_time(parse_time(cue['t']))} ({dur}s)")
                                fire_device_async(cfg, device, dur, self.log)
                            fired.add(i)
                        elif pos >= t_fire + cfg["max_late_s"]:
                            fired.add(i)

                time.sleep(cfg["poll_interval_s"])
            except Exception as e:
                self.log(f"[loop] {e}")
                time.sleep(1)

        self.log("Detenido.")
