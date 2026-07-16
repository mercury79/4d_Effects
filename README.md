# 4DFX — Control de efectos 4D para peliculas y conciertos

Sincroniza efectos fisicos (maquina de humo, ventiladores, luces, escenas,
agua, estrobos...) controlados via **Home Assistant**, con los timestamps
de una pelicula o concierto reproducido en un **Zidoo X20 Pro**.

Standalone: corre igual en macOS, Windows y Raspberry Pi 4. No depende de
que Home Assistant este en la misma maquina, solo que sea alcanzable por
red (REST API).

## Arquitectura

- **`fdfx_core.py`** — logica compartida (sin GUI): conexion al Zidoo,
  llamadas a Home Assistant, modelo de dispositivos y cues, motor de
  sincronizacion (`SyncEngine`). Tanto la CLI como la GUI importan este
  modulo; no deberia haber logica duplicada entre ambas.
- **`fdfx_gui.py`** — interfaz grafica (Tkinter, incluido con Python).
  Pestañas: Estado/Control, Dispositivos, Editor de Cues, Configuracion.
- **`4dfx.py`** — CLI (`setup`, `probe`, `test-fire`, `run`, `gui`).
  Utilna para depurar sin abrir ventanas, o para correr `run` como
  servicio/systemd en un Raspberry Pi headless.
- **`Abrir 4DFX.command`** (macOS) / **`Abrir 4DFX.bat`**
  (Windows) — lanzadores de doble clic que abren la GUI.

## Configuracion

Se guarda en `~/.4dfx/config.json` (fuera de esta carpeta, por lo que
nunca se sube al repositorio ni se comparte por accidente — ahi vive
tambien el token de Home Assistant).

Campos principales:
- `zidoo_ip` / `zidoo_port` — direccion del Zidoo (puerto API por defecto `9529`).
- `ha_url` / `ha_token` — URL base de Home Assistant (con `http://` o
  `https://` explicito) y un *Long-Lived Access Token* (Perfil de usuario
  en HA -> Seguridad -> Long-Lived Access Tokens).
- `lead_time_s` — segundos de anticipacion por defecto antes de un cue
  (para que el efecto "suba" justo a tiempo).
- `default_duration_s` — duracion por defecto de una rafaga.
- `poll_interval_s` — cada cuanto se consulta al Zidoo (default 0.5s).
- `max_late_s` — ventana maxima para considerar un cue "a tiempo" si el
  loop se atrasa un poco.
- `cues_dir` — carpeta donde viven los archivos `.json` de cue sheets.
- `devices` — lista de dispositivos (ver abajo).

### Tipos de dispositivo (`kind`)

| kind          | Uso                                      | Como se dispara                              |
|---------------|-------------------------------------------|-----------------------------------------------|
| `binary`      | switch/light/cover/fan simple on-off     | Rafaga: enciende, espera `duration_s`, apaga  |
| `multi_state` | Ventiladores multi-velocidad, efectos de luz | Cue de `state`: llama el servicio de HA definido para ese estado; se mantiene hasta el siguiente cue |
| `scene`       | Escenas de HA (ej. Philips Hue)          | Solo se activa (`scene.turn_on`); las escenas no tienen "apagado" en HA |

El tipo se detecta/sugiere automaticamente por el dominio del `entity_id`
(ej. `scene.xxx` siempre sugiere `kind: scene`), pero se puede forzar
manualmente en la pestaña Dispositivos.

### Cue sheets

Archivos `.json` en `cues_dir`, uno por pelicula/concierto:

```json
{
  "match": "Roger Waters",
  "lead_time_s": 4,
  "cues": [
    { "t": "00:12:30", "device": "humo", "duration_s": 3 },
    { "t": "00:03:00.430", "device": "ventilador", "state": "HIGH" },
    { "t": "01:20:00", "device": "escena_dorada" }
  ]
}
```

- `match`: substring (case-insensitive) del titulo que reporta el Zidoo.
- Cada cue necesita `t` (timestamp: segundos, `MM:SS` o `HH:MM:SS[.mmm]`) y
  `device` (nombre configurado en la pestaña Dispositivos).
- `duration_s` -> dispositivo `binary` (rafaga).
- `state` -> dispositivo `multi_state`.
- Ni uno ni otro -> dispositivo `scene` (solo activa).

Se editan visualmente desde la pestaña **Editor de Cues** (no hace falta
tocar el JSON a mano). Tambien se pueden **importar** archivos de la
comunidad AVS Forum ("4D Theater Wind Effect", formato
`HH:MM:SS.mmm,ESTADO`) para convertirlos automaticamente a cues de estado.

## Uso rapido

1. `python3 fdfx_gui.py` (o doble clic en el lanzador).
2. Pestaña **Configuracion**: IP del Zidoo, URL/token de HA, guardar.
3. Pestaña **Dispositivos**: agregar cada salida (humo, ventilador,
   escena de luces, etc).
4. Pestaña **Editor de Cues**: crear o importar la cue sheet de la
   pelicula/concierto.
5. Pestaña **Estado/Control**: "Iniciar sincronizacion" mientras el Zidoo
   reproduce.

## Notas de diseño (por si algo parece raro)

- El motor solo trata un cambio de titulo del Zidoo como "cambio real" si
  el titulo es valido y distinto — un error de red transitorio no debe
  resetear que cues ya se dispararon.
- Los cues de tipo `state` se "alcanzan" (catch-up) si el usuario adelanta
  rapido el video mas alla de la ventana normal, para no dejar un
  ventilador/luz en un estado viejo. Solo se aplica el ultimo estado
  saltado por dispositivo, no todos, para no saturar Home Assistant.
- Cualquier callback que se ejecuta en un hilo de fondo (motor de sync,
  pruebas de dispositivo) reporta resultados via `queue.Queue` y no llama
  directamente a metodos de Tkinter — Tkinter no es thread-safe y llamar
  `self.after(...)` desde un hilo que no sea el principal puede lanzar
  `RuntimeError: main thread is not in main loop`.
