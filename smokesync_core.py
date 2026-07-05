"""
SmokeSync - Core

Logica compartida entre la CLI (smokesync.py) y la interfaz grafica
(smokesync_gui.py). No depende de tkinter ni de ningun frontend: solo de
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
import threading
import time
from pathlib import Path

import requests

CONFIG_DIR = Path.home() / ".smokesync"
CONFIG_PATH = CONFIG_DIR / "config.json"

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
    "zidoo_ip": "",
    "zidoo_port": 9529,
    "ha_url": "",
    "ha_token": "",
    "lead_time_s": 4,
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
    on_done(ok, detail) reporta el resultado combinado."""
    def _run():
        ok1 = ha_call(cfg, device["on_service"], device["entity_id"], log)
        time.sleep(duration)
        ok2 = ha_call(cfg, device["off_service"], device["entity_id"], log)
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


def fire_at(sheet, cue, cfg):
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

    def _loop(self):
        cfg = self.cfg
        sheets = load_cue_sheets(cfg, self.log)
        self.log(f"Cargados {len(sheets)} cue sheet(s). Vigilando el Zidoo...")

        active_title = None
        active_sheet = None
        fired = set()
        last_pos = -1

        while not self._stop.is_set():
            try:
                title, pos, playing = parse_playback(zidoo_status(cfg))
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
                            t_fire = fire_at(active_sheet, cue, cfg)
                            self.log(f"     cue {fmt_time(parse_time(cue['t']))} "
                                     f"({cue.get('device', '?')}) -> dispara en ventana "
                                     f"[{fmt_time(t_fire)}, {fmt_time(t_fire + cfg['max_late_s'])})")
                    else:
                        self.log(f">> '{title}' sin cue sheet.")

                if playing and active_sheet and pos is not None:
                    if pos + 1 < last_pos:
                        fired = {i for i in fired
                                 if fire_at(active_sheet, active_sheet["cues"][i], cfg) < pos}
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
                        t_fire = fire_at(active_sheet, cue, cfg)
                        if pos >= t_fire + cfg["max_late_s"]:
                            dev_name = cue.get("device") or (default_device(cfg) or {}).get("name")
                            last_skipped_by_device[dev_name] = i

                    for i, cue in enumerate(cues):
                        if i in fired:
                            continue
                        t_fire = fire_at(active_sheet, cue, cfg)
                        is_state_cue = "state" in cue
                        in_window = t_fire <= pos < t_fire + cfg["max_late_s"]
                        dev_name = cue.get("device") or (default_device(cfg) or {}).get("name")
                        # Los cues de 'state' (ventiladores/luces multi-estado) se
                        # "alcanzan" aunque el usuario haya adelantado rapido y ya
                        # se haya salido de la ventana normal: el estado debe
                        # quedar correcto igual. Los de 'duration_s' (rafaga) no,
                        # una rafaga fuera de tiempo no tiene sentido.
                        catch_up = (is_state_cue and pos >= t_fire + cfg["max_late_s"]
                                    and last_skipped_by_device.get(dev_name) == i)
                        if in_window or catch_up:
                            device = get_device(cfg, dev_name)
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
