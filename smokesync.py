#!/usr/bin/env python3
"""
SmokeSync - Sincroniza dispositivos (maquina de humo, agua, luces, estrobos...)
via Home Assistant con los timestamps de cues de peliculas/conciertos
reproducidos en un Zidoo X20 Pro.

Standalone, multiplataforma (macOS / Windows / Raspberry Pi 4). Sin
dependencia de HA local: solo necesita alcanzar el Zidoo (API HTTP :9529)
y el REST API de HA por red.

Uso:
    python smokesync.py setup     # asistente: pide cada variable y prueba conexion
    python smokesync.py probe     # muestra lo que el Zidoo reporta ahora mismo
    python smokesync.py test-fire # dispara un dispositivo 1 vez (para verificar HA)
    python smokesync.py run       # arranca el loop de sincronizacion (CLI)
    python smokesync.py gui       # abre la interfaz grafica

Requisito: pip install requests   (tkinter viene con Python, salvo en
algunas instalaciones minimas de Linux: sudo apt install python3-tk)
"""

import argparse
import json
import sys
import time

try:
    import requests  # noqa: F401  (verificado aqui para dar un mensaje claro)
except ImportError:
    print("Falta 'requests'. Instala con:  pip install requests")
    sys.exit(1)

import smokesync_core as core


# --- Helpers de entrada -------------------------------------------------------
def ask(prompt, default=None, cast=str):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        if not raw:
            print("  (requerido)")
            continue
        try:
            return cast(raw)
        except (ValueError, TypeError):
            print(f"  Valor invalido, esperaba {cast.__name__}")


# --- Asistente de configuracion ----------------------------------------------
def cmd_setup(_args):
    print("=" * 60)
    print(" SmokeSync  -  Asistente de configuracion")
    print("=" * 60)
    cfg = core.load_config() or dict(core.DEFAULT_CFG)

    print("\n--- Zidoo X20 Pro ---")
    cfg["zidoo_ip"] = ask("IP del Zidoo", cfg.get("zidoo_ip") or None)
    cfg["zidoo_port"] = ask("Puerto API", cfg.get("zidoo_port", 9529), int)

    print("Probando Zidoo...")
    j = core.zidoo_status(cfg)
    if j.get("_error"):
        print(f"  AVISO: no respondio ({j['_error']}). Revisa IP/puerto y que este encendido.")
    else:
        title, pos, playing = core.parse_playback(j)
        print(f"  OK. status={j.get('status')}  reproduciendo={playing}")
        if title:
            print(f"  Ahora suena: '{title}'  pos={pos}s")

    print("\n--- Home Assistant ---")
    cfg["ha_url"] = ask("URL de HA (ej http://192.168.1.50:8123)",
                        cfg.get("ha_url") or None).rstrip("/")
    cfg["ha_token"] = ask("Long-Lived Access Token", cfg.get("ha_token") or None)
    print("Probando HA...")
    ok, detail = core.ha_ping(cfg)
    print("  " + ("OK, token valido." if ok else f"AVISO: {detail}"))

    print("\n--- Dispositivos (entidades HA, control binario) ---")
    print("  Deja el nombre en blanco para terminar de agregar dispositivos.")
    devices = cfg.get("devices", [])
    while True:
        name = input(f"  Nombre del dispositivo (ej humo, agua, luces) [terminar]: ").strip()
        if not name:
            break
        entity = ask("    entity_id (ej switch.maquina_humo)", None)
        on_srv, off_srv = core.services_for_entity(entity)
        devices = [d for d in devices if d["name"] != name]
        devices.append({"name": name, "entity_id": entity, "on_service": on_srv, "off_service": off_srv})
        print(f"    ON  -> {on_srv}   OFF -> {off_srv}")
    if devices:
        cfg["devices"] = devices
    if not cfg.get("devices"):
        print("  AVISO: no se configuro ningun dispositivo. Podras agregarlos luego con la GUI.")

    print("\n--- Parametros de sincronizacion ---")
    cfg["lead_time_s"] = ask("Lead time por defecto (seg antes del cue)",
                             cfg.get("lead_time_s", 4), float)
    cfg["default_duration_s"] = ask("Duracion por defecto de cada rafaga (seg)",
                                    cfg.get("default_duration_s", 3), float)
    cfg["poll_interval_s"] = ask("Intervalo de sondeo (seg)",
                                 cfg.get("poll_interval_s", 0.5), float)
    cfg["max_late_s"] = ask("Ventana max. para disparar un cue atrasado (seg)",
                            cfg.get("max_late_s", 3.0), float)
    default_cues = str(core.CONFIG_DIR / "cues")
    cfg["cues_dir"] = ask("Carpeta de cue sheets", cfg.get("cues_dir", default_cues))

    from pathlib import Path
    Path(cfg["cues_dir"]).mkdir(parents=True, exist_ok=True)

    core.save_config(cfg)
    print(f"\nConfiguracion guardada en: {core.CONFIG_PATH}")
    print("Coloca tus archivos .json de cues en:")
    print(f"  {cfg['cues_dir']}")
    print("Luego corre:  python smokesync.py run   o   python smokesync.py gui")


# --- Probe / test ------------------------------------------------------------
def cmd_probe(_args):
    cfg = _require_cfg()
    j = core.zidoo_status(cfg)
    print(json.dumps(j, indent=2, ensure_ascii=False))
    title, pos, playing = core.parse_playback(j)
    print(f"\nInterpretado -> title='{title}'  pos={pos}s  playing={playing}")


def cmd_test_fire(args):
    cfg = _require_cfg()
    devices = cfg.get("devices", [])
    if not devices:
        print("No hay dispositivos configurados. Corre 'setup' o usa la GUI.")
        sys.exit(1)
    name = args.device or devices[0]["name"]
    device = core.get_device(cfg, name)
    if not device:
        print(f"Dispositivo '{name}' no encontrado. Disponibles: {[d['name'] for d in devices]}")
        sys.exit(1)
    dur = cfg.get("default_duration_s", 3)
    print(f"Disparando '{device['name']}' ({device.get('entity_id', '')}) por {dur}s...")
    on_data = device.get("on_data") or {"entity_id": device.get("entity_id")}
    off_data = device.get("off_data") or {"entity_id": device.get("entity_id")}
    core.ha_call_service(cfg, device["on_service"], on_data)
    time.sleep(dur)
    core.ha_call_service(cfg, device["off_service"], off_data)
    print("Hecho.")


# --- Loop principal (bloqueante, para uso en terminal / servicio) ------------
def cmd_run(_args):
    cfg = _require_cfg()
    engine = core.SyncEngine(cfg, log=print)
    engine.start()
    print("Ctrl+C para salir.")
    try:
        while engine.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        engine.stop()
        time.sleep(0.5)


def cmd_gui(_args):
    import smokesync_gui
    smokesync_gui.main()


def _require_cfg():
    cfg = core.load_config()
    if not cfg or not cfg.get("zidoo_ip"):
        print("No hay configuracion. Corre primero:  python smokesync.py setup")
        sys.exit(1)
    return cfg


# --- CLI ---------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="SmokeSync - efectos 4DX sincronizados con conciertos/peliculas")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="asistente de configuracion")
    sub.add_parser("probe", help="mostrar lo que reporta el Zidoo ahora")
    p_fire = sub.add_parser("test-fire", help="disparar un dispositivo una vez")
    p_fire.add_argument("device", nargs="?", help="nombre del dispositivo (por defecto el primero configurado)")
    sub.add_parser("run", help="arrancar el loop de sincronizacion (CLI)")
    sub.add_parser("gui", help="abrir la interfaz grafica")
    args = p.parse_args()
    {"setup": cmd_setup, "probe": cmd_probe,
     "test-fire": cmd_test_fire, "run": cmd_run, "gui": cmd_gui}[args.cmd](args)


if __name__ == "__main__":
    main()
