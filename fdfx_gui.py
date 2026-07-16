#!/usr/bin/env python3
"""
4DFX GUI - Interfaz grafica standalone (macOS / Windows / Raspberry Pi 4)

Monitorea la reproduccion en un Zidoo X20 Pro y dispara dispositivos
(maquina de humo, agua, luces, estrobos, ...) via Home Assistant en los
timestamps definidos en los cue sheets.

Requisitos: Python 3 con tkinter (incluido de fabrica en la mayoria de
instalaciones) + `pip install requests`.

Ejecutar:  python3 fdfx_gui.py
"""

import colorsys
import json
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

import fdfx_core as core


class _PickOneDialog(tk.Toplevel):
    """Dialogo modal simple para elegir una opcion de una lista corta."""

    def __init__(self, parent, title, options):
        super().__init__(parent)
        self.title(title)
        self.result = None
        self.var = tk.StringVar(value=options[0])
        ttk.Label(self, text=title, padding=10).pack()
        for opt in options:
            ttk.Radiobutton(self, text=opt, variable=self.var, value=opt).pack(anchor="w", padx=20)
        btns = ttk.Frame(self, padding=10)
        btns.pack()
        ttk.Button(btns, text="Aceptar", command=self._accept).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side="left", padx=4)
        self.grab_set()

    def _accept(self):
        self.result = self.var.get()
        self.destroy()


class DFXGui(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("4DFX - Control de efectos")
        self.geometry("1920x1080")
        self.minsize(760, 520)

        self.cfg = core.load_config() or dict(core.DEFAULT_CFG)
        self.engine = None
        self.log_queue = queue.Queue()
        self.state_queue = queue.Queue()
        self.status_queue = queue.Queue()
        self.device_status = {}
        self.shortcut_map = {}
        self.last_pos = None
        self.last_title = None
        self.capture_on = False
        self._capture_held = {}
        self._capture_release_jobs = {}
        self._monitor_stop = threading.Event()
        self.var_player_type = tk.StringVar(value=self.cfg.get("player_type", "zidoo"))

        self._build_ui()
        self._refresh_devices_list()
        self._poll_log_queue()
        self._poll_state_queue()
        self._poll_status_queue()
        self._start_monitor()

    # ---------------------------------------------------------------- UI --
    def _build_ui(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        self.tab_status = ttk.Frame(nb)
        self.tab_devices = ttk.Frame(nb)
        self.tab_cues = ttk.Frame(nb)
        self.tab_config = ttk.Frame(nb)
        nb.add(self.tab_status, text="Estado / Control")
        nb.add(self.tab_devices, text="Dispositivos")
        nb.add(self.tab_cues, text="Editor de Cues")
        nb.add(self.tab_config, text="Configuracion")

        self._build_status_tab()
        self._build_devices_tab()
        self._build_cues_tab()
        self._build_config_tab()

    # -- Tab: Estado / Control -------------------------------------------
    def _build_status_tab(self):
        f = self.tab_status
        top = ttk.Frame(f, padding=10)
        top.pack(fill="x")

        self.lbl_zidoo = ttk.Label(top, text="Zidoo: -", font=("", 11, "bold"))
        self.lbl_zidoo.grid(row=0, column=0, sticky="w", padx=(0, 20))
        self.lbl_ha = ttk.Label(top, text="Home Assistant: -", font=("", 11, "bold"))
        self.lbl_ha.grid(row=0, column=1, sticky="w")

        self.lbl_title = ttk.Label(top, text="Titulo: -", font=("", 13))
        self.lbl_title.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.lbl_pos = ttk.Label(top, text="Posicion: -")
        self.lbl_pos.grid(row=2, column=0, sticky="w")
        self.lbl_sheet = ttk.Label(top, text="Cue sheet: (ninguna)")
        self.lbl_sheet.grid(row=2, column=1, sticky="w")

        btns = ttk.Frame(f, padding=(10, 0))
        btns.pack(fill="x")
        self.btn_start = ttk.Button(btns, text="Iniciar sincronizacion", command=self.start_sync)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(btns, text="Detener", command=self.stop_sync, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(btns, text="Probar conexiones", command=self.test_connections).pack(side="left", padx=6)

        log_frame = ttk.LabelFrame(f, text="Registro", padding=6)
        log_frame.pack(fill="both", expand=True, padx=10, pady=10)
        self.txt_log = tk.Text(log_frame, height=18, state="disabled", wrap="word")
        self.txt_log.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(log_frame, command=self.txt_log.yview)
        sb.pack(side="right", fill="y")
        self.txt_log["yscrollcommand"] = sb.set

    # -- Tab: Dispositivos -------------------------------------------------
    def _build_devices_tab(self):
        f = self.tab_devices
        left = ttk.Frame(f, padding=10)
        left.pack(side="left", fill="both", expand=True)

        cols = ("name", "kind", "atajo", "entity_id", "detalle", "estado")
        self.tree_devices = ttk.Treeview(left, columns=cols, show="headings", height=10)
        headers = {"name": "Nombre", "kind": "Tipo", "atajo": "Atajo", "entity_id": "entity_id",
                   "detalle": "Detalle", "estado": "Ultima prueba"}
        for c, w in zip(cols, (90, 90, 50, 160, 190, 160)):
            self.tree_devices.heading(c, text=headers[c])
            self.tree_devices.column(c, width=w)
        self.tree_devices.pack(fill="both", expand=True)
        self.tree_devices.bind("<Double-1>", self._on_device_double_click)
        self.tree_devices.bind("<<TreeviewSelect>>", self._on_device_select)

        ttk.Label(left, text="Doble clic en una fila para cargarla y editarla.",
                  foreground="#666").pack(anchor="w", pady=(6, 0))

        right = ttk.LabelFrame(f, text="Agregar / editar dispositivo", padding=10)
        right.pack(side="left", fill="y", padx=10, pady=10)

        self.var_dev_name = tk.StringVar()
        self.var_dev_entity = tk.StringVar()
        self.var_dev_kind = tk.StringVar(value="binary")
        self.var_dev_shortcut = tk.StringVar()
        self.var_dev_lead_time = tk.StringVar()

        ttk.Label(right, text="Nombre (ej: humo, agua, luces, ventilador)").pack(anchor="w")
        ttk.Entry(right, textvariable=self.var_dev_name, width=30).pack(fill="x", pady=(0, 8))

        ttk.Label(right, text="entity_id de Home Assistant").pack(anchor="w")
        entry_entity = ttk.Entry(right, textvariable=self.var_dev_entity, width=30)
        entry_entity.pack(fill="x", pady=(0, 8))
        entry_entity.bind("<FocusOut>", self._suggest_dev_kind)

        self.frame_dev_shortcut = ttk.Frame(right)
        self.frame_dev_shortcut.pack(fill="x", pady=(0, 8))
        ttk.Label(self.frame_dev_shortcut, text="Atajo de teclado (1 caracter, para captura en vivo / Stream Deck)").pack(anchor="w")
        ttk.Entry(self.frame_dev_shortcut, textvariable=self.var_dev_shortcut, width=4).pack(anchor="w")

        ttk.Label(right, text="Lead time (s) de este dispositivo (opcional, vacio = usar el de la cue sheet)").pack(anchor="w")
        ttk.Entry(right, textvariable=self.var_dev_lead_time, width=6).pack(anchor="w", pady=(0, 8))

        # -- Avanzado: para dispositivos que no son una entidad HA nativa de
        # on/off, sino un script/servicio generico (ej remote.send_command de
        # un Broadlink, con 'device'/'command' propios en el payload).
        self.var_dev_advanced = tk.BooleanVar(value=False)
        self.frame_dev_advanced_toggle = ttk.Frame(right)
        self.frame_dev_advanced_toggle.pack(fill="x", pady=(0, 4))
        ttk.Checkbutton(self.frame_dev_advanced_toggle,
                        text="Avanzado (servicio/datos JSON personalizados, ej. Broadlink/scripts)",
                        variable=self.var_dev_advanced, command=self._on_dev_advanced_toggle).pack(anchor="w")

        self.frame_dev_advanced = ttk.LabelFrame(right, text="ON / OFF personalizados", padding=6)
        self.var_dev_on_service = tk.StringVar()
        self.var_dev_on_data = tk.StringVar(value="{}")
        self.var_dev_off_service = tk.StringVar()
        self.var_dev_off_data = tk.StringVar(value="{}")
        ttk.Label(self.frame_dev_advanced, text="Servicio ON (domain/service)").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.frame_dev_advanced, textvariable=self.var_dev_on_service, width=22).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(self.frame_dev_advanced, text="Datos ON (JSON)").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(self.frame_dev_advanced, textvariable=self.var_dev_on_data, width=30).grid(row=1, column=1, sticky="w", padx=4, pady=(4, 0))
        ttk.Label(self.frame_dev_advanced, text="Servicio OFF (domain/service)").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(self.frame_dev_advanced, textvariable=self.var_dev_off_service, width=22).grid(row=2, column=1, sticky="w", padx=4, pady=(6, 0))
        ttk.Label(self.frame_dev_advanced, text="Datos OFF (JSON)").grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(self.frame_dev_advanced, textvariable=self.var_dev_off_data, width=30).grid(row=3, column=1, sticky="w", padx=4, pady=(4, 0))
        ttk.Label(self.frame_dev_advanced,
                  text="Ej: servicio 'remote/send_command', datos\n"
                       '{"entity_id":"remote.broadlink","device":"maquina_humo","command":"humo_abierto"}',
                  foreground="#666", font=("", 8), wraplength=260).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # -- MQTT directo (sin Home Assistant): publica On/Off al broker
        # configurado en la pestaña Configuracion.
        self.frame_dev_mqtt = ttk.LabelFrame(right, text="MQTT (broker en pestaña Configuracion)", padding=6)
        self.var_dev_mqtt_on_topic = tk.StringVar()
        self.var_dev_mqtt_on_payload = tk.StringVar(value="ON")
        self.var_dev_mqtt_off_topic = tk.StringVar()
        self.var_dev_mqtt_off_payload = tk.StringVar(value="OFF")
        ttk.Label(self.frame_dev_mqtt, text="Topic ON").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.frame_dev_mqtt, textvariable=self.var_dev_mqtt_on_topic, width=24).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(self.frame_dev_mqtt, text="Payload ON").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(self.frame_dev_mqtt, textvariable=self.var_dev_mqtt_on_payload, width=24).grid(row=1, column=1, sticky="w", padx=4, pady=(4, 0))
        ttk.Label(self.frame_dev_mqtt, text="Topic OFF").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(self.frame_dev_mqtt, textvariable=self.var_dev_mqtt_off_topic, width=24).grid(row=2, column=1, sticky="w", padx=4, pady=(6, 0))
        ttk.Label(self.frame_dev_mqtt, text="Payload OFF").grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(self.frame_dev_mqtt, textvariable=self.var_dev_mqtt_off_payload, width=24).grid(row=3, column=1, sticky="w", padx=4, pady=(4, 0))
        ttk.Label(self.frame_dev_mqtt, text='Ej: topic "shellyplug/relay/0/command",\npayload "on" / "off"',
                  foreground="#666", font=("", 8)).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        ttk.Label(right, text="Tipo de dispositivo").pack(anchor="w")
        kind_frame = ttk.Frame(right)
        kind_frame.pack(fill="x", pady=(0, 4))
        ttk.Radiobutton(kind_frame, text="Binario (on/off)", variable=self.var_dev_kind,
                         value="binary", command=self._on_dev_kind_change).pack(anchor="w")
        ttk.Radiobutton(kind_frame, text="Multi-estado (velocidades, efectos)", variable=self.var_dev_kind,
                         value="multi_state", command=self._on_dev_kind_change).pack(anchor="w")
        ttk.Radiobutton(kind_frame, text="Escena (solo activar, sin apagado)", variable=self.var_dev_kind,
                         value="scene", command=self._on_dev_kind_change).pack(anchor="w")
        ttk.Radiobutton(kind_frame, text="MQTT directo (sin Home Assistant)", variable=self.var_dev_kind,
                         value="mqtt", command=self._on_dev_kind_change).pack(anchor="w")
        ttk.Label(right, text="Binario: se detecta on/off segun el dominio\n"
                              "(switch, light, cover, fan, input_boolean, valve).\n"
                              "Escena (scene.xxx): HA solo permite activarla,\n"
                              "no tiene 'apagado' - se detecta automatico.\n"
                              "MQTT: publica directo al broker (configuralo en\n"
                              "la pestaña Configuracion), sin pasar por HA.",
                  foreground="#666").pack(anchor="w", pady=(0, 8))

        # -- Editor de estados (solo visible/relevante si kind == multi_state)
        self.frame_states = ttk.LabelFrame(right, text="Estados (nombre -> servicio HA)", padding=6)
        self.frame_states.pack(fill="x", pady=(0, 8))

        st_cols = ("state", "service", "data", "shortcut")
        self.tree_states = ttk.Treeview(self.frame_states, columns=st_cols, show="headings", height=5)
        headers_st = {"state": "state", "service": "service", "data": "data", "shortcut": "atajo"}
        for c, w in zip(st_cols, (60, 120, 100, 50)):
            self.tree_states.heading(c, text=headers_st[c])
            self.tree_states.column(c, width=w)
        self.tree_states.pack(fill="x")
        self.tree_states.bind("<Double-1>", self._on_state_double_click)

        st_form = ttk.Frame(self.frame_states)
        st_form.pack(fill="x", pady=(6, 0))
        self.var_state_name = tk.StringVar()
        self.var_state_service = tk.StringVar()
        self.var_state_data = tk.StringVar(value="{}")
        self.var_state_shortcut = tk.StringVar()
        ttk.Entry(st_form, textvariable=self.var_state_name, width=7).grid(row=0, column=0, padx=1)
        ttk.Entry(st_form, textvariable=self.var_state_service, width=14).grid(row=0, column=1, padx=1)
        ttk.Entry(st_form, textvariable=self.var_state_data, width=12).grid(row=0, column=2, padx=1)
        ttk.Entry(st_form, textvariable=self.var_state_shortcut, width=3).grid(row=0, column=3, padx=1)
        ttk.Label(st_form, text="nombre / domain.service / {json} / atajo",
                  foreground="#666", font=("", 8)).grid(row=1, column=0, columnspan=4, sticky="w")
        btn_row = ttk.Frame(self.frame_states)
        btn_row.pack(fill="x", pady=(4, 0))
        ttk.Button(btn_row, text="Agregar/editar estado", command=self._add_state_row).pack(side="left")
        ttk.Button(btn_row, text="Quitar estado", command=self._remove_state_row).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Plantilla ventilador", command=self._load_fan_template).pack(side="left")

        ttk.Button(right, text="Agregar / actualizar dispositivo", command=self.add_device).pack(fill="x", pady=(4, 2))
        ttk.Button(right, text="Eliminar seleccionado", command=self.remove_device).pack(fill="x", pady=2)

        test_row = ttk.Frame(right)
        test_row.pack(fill="x", pady=(10, 2))
        self.var_test_state = tk.StringVar()
        self.combo_test_state = ttk.Combobox(test_row, textvariable=self.var_test_state, width=10, state="readonly")
        self.combo_test_state.pack(side="left")
        ttk.Button(test_row, text="Probar", command=self.test_fire_selected).pack(side="left", padx=4)
        self.btn_test_on = ttk.Button(test_row, text="Probar ON", command=lambda: self._test_fire_side("on"))
        self.btn_test_on.pack(side="left", padx=2)
        self.btn_test_off = ttk.Button(test_row, text="Probar OFF", command=lambda: self._test_fire_side("off"))
        self.btn_test_off.pack(side="left", padx=2)
        self.lbl_test_result = ttk.Label(right, text="", foreground="#666", wraplength=220)
        self.lbl_test_result.pack(anchor="w", pady=(4, 0))

        self._on_dev_kind_change()

    def _suggest_dev_kind(self, _event=None):
        # Solo forzamos el cambio quando el dominio es 'scene' (nunca tiene
        # apagado); para el resto respetamos lo que el usuario haya elegido
        # (para no pisar una eleccion manual de 'multi_state').
        entity = self.var_dev_entity.get().strip()
        if entity and core.suggested_kind_for_entity(entity) == "scene":
            self.var_dev_kind.set("scene")
            self._on_dev_kind_change()

    def _on_dev_kind_change(self):
        kind = self.var_dev_kind.get()
        self.frame_states.pack_forget()
        self.frame_dev_shortcut.pack_forget()
        self.frame_dev_advanced_toggle.pack_forget()
        self.frame_dev_advanced.pack_forget()
        self.frame_dev_mqtt.pack_forget()

        if kind == "multi_state":
            self.frame_states.pack(fill="x", pady=(0, 8))
            return

        self.frame_dev_shortcut.pack(fill="x", pady=(0, 8))
        if kind == "binary":
            self.frame_dev_advanced_toggle.pack(fill="x", pady=(0, 4))
            self._on_dev_advanced_toggle()
        elif kind == "mqtt":
            self.frame_dev_mqtt.pack(fill="x", pady=(0, 8))

    def _on_dev_advanced_toggle(self):
        if self.var_dev_advanced.get():
            self.frame_dev_advanced.pack(fill="x", pady=(0, 8))
        else:
            self.frame_dev_advanced.pack_forget()

    def _add_state_row(self):
        name = self.var_state_name.get().strip().upper()
        service = self.var_state_service.get().strip()
        shortcut = self.var_state_shortcut.get().strip()
        if not name or not service:
            messagebox.showwarning("Falta informacion", "Nombre de estado y servicio son requeridos.")
            return
        try:
            data = json.loads(self.var_state_data.get().strip() or "{}")
        except Exception as e:
            messagebox.showwarning("JSON invalido", f"El campo de datos extra no es JSON valido: {e}")
            return
        if self.tree_states.exists(name):
            self.tree_states.delete(name)
        self.tree_states.insert("", "end", iid=name, values=(name, service, json.dumps(data), shortcut))
        self.var_state_name.set("")
        self.var_state_service.set("")
        self.var_state_data.set("{}")
        self.var_state_shortcut.set("")

    def _remove_state_row(self):
        sel = self.tree_states.selection()
        if sel:
            self.tree_states.delete(sel[0])

    def _on_state_double_click(self, _event):
        sel = self.tree_states.selection()
        if not sel:
            return
        name, service, data, shortcut = self.tree_states.item(sel[0], "values")
        self.var_state_name.set(name)
        self.var_state_service.set(service)
        self.var_state_data.set(data)
        self.var_state_shortcut.set(shortcut)

    def _load_fan_template(self):
        self.tree_states.delete(*self.tree_states.get_children())
        for name, s in core.DEFAULT_FAN_STATES.items():
            self.tree_states.insert("", "end", iid=name,
                                     values=(name, s["service"], json.dumps(s["data"]), s.get("shortcut", "")))

    # -- Tab: Editor de Cues --------------------------------------------------
    def _build_cues_tab(self):
        f = self.tab_cues
        self.current_sheet_path = None
        self._prev_sheet_selection = None
        self._editing_cue_iid = None

        paned = ttk.Panedwindow(f, orient="horizontal")
        paned.pack(fill="both", expand=True)

        self.frame_sheet_list = ttk.Frame(f, padding=10)
        left = self.frame_sheet_list
        ttk.Label(left, text="Cue sheets (.json)", font=("", 10, "bold")).pack(anchor="w")
        self.list_sheets = tk.Listbox(left, width=32, height=20, exportselection=False)
        self.list_sheets.pack(fill="both", expand=True)
        self.list_sheets.bind("<<ListboxSelect>>", self._on_sheet_select)
        sheet_btns = ttk.Frame(left)
        sheet_btns.pack(fill="x", pady=6)
        ttk.Button(sheet_btns, text="Nuevo", command=self._new_sheet).pack(side="left")
        ttk.Button(sheet_btns, text="Eliminar", command=self._delete_sheet).pack(side="left", padx=4)
        ttk.Button(left, text="Importar .txt (AVS Forum)...", command=self._import_state_txt).pack(fill="x", pady=(10, 2))
        paned.add(left, weight=0)

        right = ttk.Frame(f, padding=10)
        paned.add(right, weight=1)

        toggle_row = ttk.Frame(right)
        toggle_row.pack(fill="x")
        self._sheet_list_visible = True
        self.btn_toggle_sheet_list = ttk.Button(toggle_row, text="« Ocultar lista de cue sheets",
                                                 command=lambda: self._toggle_sheet_list(paned))
        self.btn_toggle_sheet_list.pack(side="left")
        self._paned_cues = paned

        src_row = ttk.Frame(right)
        src_row.pack(fill="x", pady=(0, 6))
        ttk.Label(src_row, text="Fuente de reproduccion:").pack(side="left")
        combo_cues_player = ttk.Combobox(src_row, textvariable=self.var_player_type, state="readonly",
                                          values=["zidoo", "vlc", "jriver"], width=10)
        combo_cues_player.pack(side="left", padx=6)
        combo_cues_player.bind("<<ComboboxSelected>>", lambda e: self._on_player_type_selected())

        self.btn_cues_start_sync = ttk.Button(src_row, text="Iniciar sincronizacion", command=self.start_sync)
        self.btn_cues_start_sync.pack(side="left", padx=(16, 2))
        self.btn_cues_stop_sync = ttk.Button(src_row, text="Detener", command=self.stop_sync, state="disabled")
        self.btn_cues_stop_sync.pack(side="left")
        ttk.Label(src_row, text="  (dispara los cues en vivo mientras editas/scrubeas)",
                  foreground="#666", font=("", 8)).pack(side="left")

        meta = ttk.Frame(right)
        meta.pack(fill="x", pady=(0, 2))
        self.var_sheet_match = tk.StringVar()
        self.var_sheet_lead = tk.StringVar()
        self.var_sheet_filename = tk.StringVar()
        ttk.Label(meta, text="Match (substring del titulo)").grid(row=0, column=0, sticky="w")
        ttk.Entry(meta, textvariable=self.var_sheet_match, width=28).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Button(meta, text="Usar titulo actual", command=self._use_current_title).grid(row=0, column=2, sticky="w", padx=4)
        ttk.Label(meta, text="Lead time (s)").grid(row=0, column=3, sticky="w")
        ttk.Entry(meta, textvariable=self.var_sheet_lead, width=6).grid(row=0, column=4, sticky="w", padx=4)
        ttk.Label(meta, text="Archivo").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(meta, textvariable=self.var_sheet_filename, width=28).grid(row=1, column=1, sticky="w", padx=4, pady=(4, 0))
        ttk.Button(meta, text="Guardar cue sheet", command=self._save_sheet).grid(row=1, column=4, sticky="w", pady=(4, 0))
        ttk.Label(meta, text="Lead time: segundos de anticipacion con que se dispara cada cue antes\n"
                             "de su timestamp exacto (para compensar lo que tarda en reaccionar el\n"
                             "efecto fisico, ej. que el humo empiece a salir).",
                  foreground="#666", font=("", 8)).grid(row=2, column=0, columnspan=5, sticky="w", pady=(4, 0))

        self._build_video_preview(right)

        # -- Captura en vivo: mientras el Zidoo reproduce (o el video local en
        # vista previa), presiona el atajo de un dispositivo (o el boton de un
        # Stream Deck configurado para enviar esa tecla) y se inserta un cue
        # en la posicion actual.
        capture = ttk.LabelFrame(right, text="Captura en vivo (atajos de teclado / Stream Deck)", padding=6)
        capture.pack(fill="x", pady=(0, 8))
        self.var_capture_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(capture, text="Activar captura", variable=self.var_capture_on,
                        command=self._toggle_capture).pack(side="left")
        self.lbl_capture_pos = ttk.Label(capture, text="Posicion: -")
        self.lbl_capture_pos.pack(side="left", padx=10)

        cols = ("t", "device", "modo", "valor")
        self.tree_cues = ttk.Treeview(right, columns=cols, show="headings", height=10, selectmode="extended")
        headers = {"t": "Timestamp", "device": "Dispositivo", "modo": "Modo", "valor": "Duracion / Estado"}
        for c, w in zip(cols, (110, 110, 90, 130)):
            self.tree_cues.heading(c, text=headers[c])
            self.tree_cues.column(c, width=w)
        self.tree_cues.pack(fill="both", expand=True)
        self.tree_cues.bind("<Double-1>", self._on_cue_double_click)
        self.tree_cues.bind("<<TreeviewSelect>>", self._on_cue_tree_select)

        # -- Timeline visual (multi-pista, una fila por dispositivo) --------
        timeline_frame = ttk.LabelFrame(right, text="Timeline", padding=6)
        timeline_frame.pack(fill="x", pady=(8, 0))
        zoom_row = ttk.Frame(timeline_frame)
        zoom_row.pack(fill="x")
        ttk.Label(zoom_row, text="Zoom:").pack(side="left")
        ttk.Button(zoom_row, text="-", width=2, command=lambda: self._zoom_timeline(0.7)).pack(side="left")
        ttk.Button(zoom_row, text="+", width=2, command=lambda: self._zoom_timeline(1.4)).pack(side="left")
        ttk.Label(zoom_row, text="  Clic: seleccionar/arrastrar para mover en el tiempo. Supr: eliminar.",
                  foreground="#666").pack(side="left", padx=8)

        self.pixels_per_sec = 4.0
        self.timeline_row_h = 26
        self.timeline_ruler_h = 22
        canvas_area = ttk.Frame(timeline_frame)
        canvas_area.pack(fill="both", expand=True, pady=(4, 0))

        self.timeline_labels = tk.Canvas(canvas_area, width=100, height=160, background="#2a2a2e",
                                          highlightthickness=0)
        self.timeline_labels.pack(side="left", fill="y")

        canvas_wrap = ttk.Frame(canvas_area)
        canvas_wrap.pack(side="left", fill="both", expand=True)
        self.timeline_canvas = tk.Canvas(canvas_wrap, height=160, background="#1e1e22", highlightthickness=0)
        hscroll = ttk.Scrollbar(canvas_wrap, orient="horizontal", command=self.timeline_canvas.xview)
        self.timeline_canvas.configure(xscrollcommand=hscroll.set)
        self.timeline_canvas.pack(fill="both", expand=True, side="top")
        hscroll.pack(fill="x", side="top")
        self.timeline_canvas.bind("<ButtonPress-1>", self._on_timeline_press)
        self.timeline_canvas.bind("<B1-Motion>", self._on_timeline_drag)
        self.timeline_canvas.bind("<ButtonRelease-1>", self._on_timeline_release)
        self.bind("<Delete>", self._on_delete_key)
        self.bind("<BackSpace>", self._on_delete_key)
        self._timeline_drag = None
        self._timeline_selected = None

        form = ttk.LabelFrame(right, text="Agregar / editar cue", padding=8)
        form.pack(fill="x", pady=8)

        self.var_cue_t = tk.StringVar()
        self.var_cue_device = tk.StringVar()
        self.var_cue_mode = tk.StringVar(value="burst")
        self.var_cue_value = tk.StringVar()

        ttk.Label(form, text="Timestamp (HH:MM:SS)").grid(row=0, column=0, sticky="w")
        entry_cue_t = self._add_time_stepper(form, self.var_cue_t, self._on_cue_duration_change,
                                              width=12, is_duration=False)
        entry_cue_t.grid(row=0, column=1, sticky="w", padx=4)

        ttk.Label(form, text="Dispositivo").grid(row=0, column=2, sticky="w")
        self.combo_cue_device = ttk.Combobox(form, textvariable=self.var_cue_device, width=14, state="readonly")
        self.combo_cue_device.grid(row=0, column=3, sticky="w", padx=4)
        self.combo_cue_device.bind("<<ComboboxSelected>>", self._on_cue_device_change)

        ttk.Radiobutton(form, text="Rafaga (duracion_s)", variable=self.var_cue_mode,
                         value="burst", command=self._on_cue_mode_change).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Radiobutton(form, text="Estado", variable=self.var_cue_mode,
                         value="state", command=self._on_cue_mode_change).grid(row=1, column=2, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Radiobutton(form, text="Activar escena", variable=self.var_cue_mode,
                         value="scene", command=self._on_cue_mode_change).grid(row=1, column=4, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Label(form, text="Duracion (s) / Estado").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.var_cue_duration = tk.StringVar(value="3")
        self.entry_cue_duration = self._add_time_stepper(form, self.var_cue_duration, self._on_cue_duration_change,
                                                           width=10, is_duration=True, step=0.5)
        self.combo_cue_state = ttk.Combobox(form, textvariable=self.var_cue_value, width=10, state="readonly")
        self.combo_cue_state.bind("<<ComboboxSelected>>", self._on_cue_state_change)

        self.lbl_cue_end = ttk.Label(form, text="Fin (HH:MM:SS)")
        self.var_cue_end = tk.StringVar()
        self.entry_cue_end = self._add_time_stepper(form, self.var_cue_end, self._on_cue_end_change,
                                                      width=12, is_duration=False)

        btns2 = ttk.Frame(form)
        btns2.grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Button(btns2, text="Agregar / actualizar cue", command=self._add_cue).pack(side="left")
        ttk.Button(btns2, text="Eliminar cue seleccionado", command=self._delete_cue).pack(side="left", padx=4)

        self._refresh_cue_device_options()
        self._new_sheet(confirm=False)
        self._refresh_sheet_list()

    # ---- Vista previa de video: VLC en su propia ventana, controlado ----
    # ---- por su interfaz HTTP (embeber libVLC via NSView/CAOpenGLLayer --
    # ---- resulto inestable en macOS reciente: segfault confirmado en   --
    # ---- libcaopengllayer_plugin.dylib incluso tras actualizar VLC y   --
    # ---- forzar --vout=macosx; VLC en ventana propia es 100% estable). --
    def _build_video_preview(self, parent):
        self.preview_active = False
        self._preview_seek_dragging = False
        self._preview_duration_s = 0.0
        self._auto_started_sync = False

        box = ttk.LabelFrame(parent, text="Vista previa de video (VLC, no requiere el Zidoo)", padding=6)
        box.pack(fill="x", pady=(0, 8))

        toolbar = ttk.Frame(box)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Abrir video en VLC...", command=self._open_preview_video).pack(side="left")
        self.btn_preview_play = ttk.Button(toolbar, text="Pausar/Reproducir", command=self._toggle_preview_play, state="disabled")
        self.btn_preview_play.pack(side="left", padx=4)
        ttk.Button(toolbar, text="Cerrar", command=self._close_preview_video).pack(side="left")
        self.lbl_preview_file = ttk.Label(toolbar, text="(sin video cargado)", foreground="#666")
        self.lbl_preview_file.pack(side="left", padx=10)

        seek_row = ttk.Frame(box)
        seek_row.pack(fill="x", pady=(6, 0))
        self.var_preview_seek = tk.DoubleVar(value=0.0)
        self.scale_preview = ttk.Scale(seek_row, from_=0, to=1000, variable=self.var_preview_seek,
                                        orient="horizontal", command=self._on_preview_seek_drag)
        self.scale_preview.pack(side="left", fill="x", expand=True)
        self.scale_preview.bind("<ButtonPress-1>", lambda e: setattr(self, "_preview_seek_dragging", True))
        self.scale_preview.bind("<ButtonRelease-1>", self._on_preview_seek_release)
        self.lbl_preview_time = ttk.Label(seek_row, text="00:00 / 00:00", width=14)
        self.lbl_preview_time.pack(side="left", padx=6)
        ttk.Label(box, text="VLC se abre en su propia ventana (junto a esta app) y se controla via\n"
                            "su interfaz HTTP. Con el video en reproduccion, los atajos de captura\n"
                            "en vivo usan esa posicion (igual que si fuera el Zidoo real).",
                  foreground="#666", font=("", 8)).pack(anchor="w", pady=(4, 0))

    def _open_preview_video(self):
        path = filedialog.askopenfilename(
            title="Selecciona un video",
            filetypes=[("Video", "*.mp4 *.mkv *.mov *.avi *.m4v *.ts"), ("Todos", "*.*")])
        if not path:
            return
        ok, err = core.launch_vlc_with_file(self.cfg, path)
        if not ok:
            messagebox.showerror("No se pudo abrir VLC", err)
            return
        self.preview_active = True
        self.lbl_preview_file.config(text=Path(path).name)
        self.btn_preview_play.config(state="normal")
        self.after(1500, self._poll_preview_player)

        # Siempre trabajamos con la cue sheet del video que se esta editando:
        # si ya existe una que haga match con el titulo, se carga sola; si
        # no, se crea una nueva con ese nombre para evitar confundirse con
        # otras cue sheets de la lista.
        self._load_or_create_sheet_for_title(Path(path).stem)

        # Al abrir el video de edicion, activamos captura y sincronizacion
        # solas: la idea es abrir VLC y ya poder ver/crear efectos en vivo
        # sin pasos manuales extra.
        if not self.capture_on:
            self.var_capture_on.set(True)
            self._toggle_capture()
        self._auto_started_sync = self.start_sync(auto=True)

    def _load_or_create_sheet_for_title(self, title):
        """Busca una cue sheet existente que aplique a 'title'; si no hay
        ninguna, crea una nueva con ese nombre/match de una vez (asi el
        archivo ya existe y coincide exactamente con lo que se esta viendo,
        sin confundirse con otras cue sheets de la lista)."""
        title = core.strip_video_ext(title)
        sheets = core.load_cue_sheets(self.cfg, log=lambda *_: None)
        found = core.match_sheet(sheets, title)
        if found:
            fname = found["_file"]
            for i in range(self.list_sheets.size()):
                if self.list_sheets.get(i) == fname:
                    self.list_sheets.selection_clear(0, "end")
                    self.list_sheets.selection_set(i)
                    self._on_sheet_select(None)
                    break
            self.log(f"Cue sheet existente cargada para '{title}': {fname}")
        else:
            self._new_sheet(confirm=False)
            self.var_sheet_match.set(title)
            self.var_sheet_filename.set(f"{core.slug_for_title(title)}.json")
            self._autosave_sheet()
            self.log(f"Cue sheet nueva creada para '{title}'.")

    def _toggle_preview_play(self):
        if not self.preview_active:
            return
        core.vlc_control(self.cfg, "pl_pause")

    def _close_preview_video(self):
        if self.preview_active:
            core.vlc_control(self.cfg, "pl_stop")
        self.preview_active = False
        self.lbl_preview_file.config(text="(sin video cargado)")
        self.btn_preview_play.config(state="disabled")
        self.lbl_preview_time.config(text="00:00 / 00:00")
        if self.capture_on:
            self.var_capture_on.set(False)
            self._toggle_capture()
        if getattr(self, "_auto_started_sync", False):
            self.stop_sync()
            self._auto_started_sync = False

    def _on_preview_seek_drag(self, _value):
        if self._preview_seek_dragging and self._preview_duration_s:
            frac = self.var_preview_seek.get() / 1000.0
            self.lbl_preview_time.config(
                text=f"{core.fmt_time(frac * self._preview_duration_s)} / {core.fmt_time(self._preview_duration_s)}")

    def _on_preview_seek_release(self, _event):
        self._preview_seek_dragging = False
        if self.preview_active and self._preview_duration_s:
            frac = self.var_preview_seek.get() / 1000.0
            core.vlc_control(self.cfg, "seek", val=int(frac * self._preview_duration_s))

    def seek_preview_to(self, seconds):
        """Mueve la vista previa a un timestamp (usado al hacer clic en la regla del timeline)."""
        if self.preview_active:
            core.vlc_control(self.cfg, "seek", val=int(max(0, seconds)))

    def _poll_preview_player(self):
        if self.preview_active:
            title, pos_s, playing, err = core.vlc_playback(self.cfg)
            if err:
                if err != getattr(self, "_last_preview_err", None):
                    self.log(f"Vista previa: sin conexion con VLC aun ({err}). Reintentando...")
                    self._last_preview_err = err
                self.lbl_preview_time.config(text="(esperando VLC...)")
            elif not err and pos_s is not None:
                self._last_preview_err = None
                # vlc_playback no expone la duracion total; la leemos aparte
                # via el mismo status.json a traves de vlc_control no aplica,
                # asi que usamos requests directo solo para 'length'.
                try:
                    import requests
                    r = requests.get(f"{self.cfg['vlc_url'].rstrip('/')}/requests/status.json",
                                      auth=("", self.cfg.get("vlc_password", "")), timeout=2)
                    length_s = float(r.json().get("length") or 0)
                except Exception:
                    length_s = self._preview_duration_s
                if length_s > 0:
                    self._preview_duration_s = length_s
                    if not self._preview_seek_dragging:
                        self.var_preview_seek.set((pos_s / length_s) * 1000.0)
                self.lbl_preview_time.config(
                    text=f"{core.fmt_time(pos_s)} / {core.fmt_time(self._preview_duration_s)}")
                self.last_pos = pos_s
                self.last_title = title or self.lbl_preview_file.cget("text")
                self._draw_playhead()
                if hasattr(self, "lbl_capture_pos"):
                    self._update_capture_pos_label()
            self.after(400, self._poll_preview_player)

    # ---- helpers de la pestana de cues --------------------------------
    def _refresh_cue_device_options(self):
        names = [d["name"] for d in self.cfg.get("devices", [])]
        self.combo_cue_device["values"] = names
        if names and not self.var_cue_device.get():
            self.var_cue_device.set(names[0])
        self._on_cue_device_change()

    def _on_cue_device_change(self, *_):
        device = core.get_device(self.cfg, self.var_cue_device.get())
        if device and device.get("kind") == "multi_state":
            states = sorted(device.get("states", {}).keys())
            self.combo_cue_state["values"] = states
            if states:
                self.var_cue_value.set(states[0])
            self.var_cue_mode.set("state")
        elif device and device.get("kind") == "scene":
            self.var_cue_mode.set("scene")
        else:
            self.var_cue_mode.set("burst")
        self._on_cue_mode_change()
        self._commit_form_to_tree()

    def _commit_form_to_tree(self):
        """Aplica en vivo lo que hay en el formulario (Timestamp/Duracion/
        Fin/Estado/Dispositivo) al cue que se esta editando en la tabla y la
        timeline, sin esperar a que se pulse 'Agregar / actualizar cue'. Asi
        las flechitas +/- y teclear un valor se ven reflejados de inmediato."""
        iid = self._editing_cue_iid
        if not iid or not self.tree_cues.exists(iid):
            return
        t = self.var_cue_t.get().strip()
        device = self.var_cue_device.get().strip()
        if not t or not device:
            return
        mode = self.var_cue_mode.get()
        if mode == "state":
            modo, valor = "state", self.var_cue_value.get()
        elif mode == "scene":
            modo, valor = "scene", ""
        else:
            try:
                dur = float(self.var_cue_duration.get() or 0)
            except ValueError:
                return
            modo, valor = "burst", f"{dur}s"
        self.tree_cues.item(iid, values=(t, device, modo, valor))
        self._refresh_timeline()
        self._autosave_sheet()

    def _autosave_sheet(self):
        """Escribe la cue sheet actual a disco de inmediato, sin dialogo ni
        boton: el archivo siempre debe reflejar lo que se ve en pantalla,
        para que la sincronizacion en vivo dispare exactamente eso sin tener
        que acordarse de guardar."""
        match = self.var_sheet_match.get().strip()
        fname = self.var_sheet_filename.get().strip()
        if not match or not fname:
            return
        if not fname.endswith(".json"):
            fname += ".json"
        d = Path(self.cfg["cues_dir"])
        d.mkdir(parents=True, exist_ok=True)
        target = d / fname
        is_new_file = not target.exists()
        data = {
            "match": match,
            "lead_time_s": float(self.var_sheet_lead.get() or self.cfg.get("lead_time_s", 4)),
            "cues": self._tree_cues_to_list(),
        }
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self.current_sheet_path = target
        if self.engine and self.engine.running:
            self.engine.refresh_sheets()
        if is_new_file:
            self._refresh_sheet_list()
            for i in range(self.list_sheets.size()):
                if self.list_sheets.get(i) == fname:
                    self.list_sheets.selection_clear(0, "end")
                    self.list_sheets.selection_set(i)
                    self._prev_sheet_selection = i
                    break

    def _add_time_stepper(self, parent, var, on_change, width=12, is_duration=False, step=1.0):
        """Crea un Entry con botones +/- al lado para ajustar el valor sin
        teclear ni arrastrar en la timeline. 'is_duration' controla si el
        valor es un numero de segundos plano (duracion) o un timestamp
        HH:MM:SS (t / fin)."""
        wrap = ttk.Frame(parent)
        entry = ttk.Entry(wrap, textvariable=var, width=width)
        entry.pack(side="left")
        if on_change:
            entry.bind("<FocusOut>", on_change)

        def _step(delta):
            if is_duration:
                try:
                    val = float(var.get() or 0)
                except ValueError:
                    val = 0.0
                var.set(str(max(0.1, round(val + delta, 2))))
            else:
                try:
                    val = core.parse_time(var.get())
                except Exception:
                    val = 0.0
                var.set(core.fmt_time_ms(max(0.0, round(val + delta, 2))))
            if on_change:
                on_change()

        btns = ttk.Frame(wrap)
        btns.pack(side="left", padx=(2, 0))
        btn_opts = dict(width=1, padx=0, pady=0, font=("", 7), highlightthickness=0, bd=1)
        tk.Button(btns, text="▲", command=lambda: _step(step), **btn_opts).pack()
        tk.Button(btns, text="▼", command=lambda: _step(-step), **btn_opts).pack()
        return wrap

    def _on_cue_mode_change(self):
        mode = self.var_cue_mode.get()
        self.entry_cue_duration.grid_remove()
        self.combo_cue_state.grid_remove()
        self.lbl_cue_end.grid_remove()
        self.entry_cue_end.grid_remove()
        if mode == "state":
            self.combo_cue_state.grid(row=2, column=1, sticky="w", padx=4, pady=(6, 0))
        elif mode == "burst":
            self.entry_cue_duration.grid(row=2, column=1, sticky="w", padx=4, pady=(6, 0))
            self.lbl_cue_end.grid(row=2, column=2, sticky="w", padx=(12, 0), pady=(6, 0))
            self.entry_cue_end.grid(row=2, column=3, sticky="w", padx=4, pady=(6, 0))
            self._sync_cue_end_from_duration()
        # 'scene': ni duracion ni estado, solo dispara al llegar el timestamp
        self._commit_form_to_tree()

    def _sync_cue_end_from_duration(self):
        try:
            start = core.parse_time(self.var_cue_t.get())
            dur = float(self.var_cue_duration.get())
        except (ValueError, Exception):
            return
        self.var_cue_end.set(core.fmt_time_ms(start + dur))

    def _on_cue_duration_change(self, _event=None):
        self._sync_cue_end_from_duration()
        self._commit_form_to_tree()

    def _on_cue_end_change(self, _event=None):
        try:
            start = core.parse_time(self.var_cue_t.get())
            end = core.parse_time(self.var_cue_end.get())
        except Exception:
            return
        dur = max(0.2, end - start)
        self.var_cue_duration.set(str(round(dur, 2)))
        self._commit_form_to_tree()

    def _on_cue_state_change(self, _event=None):
        self._commit_form_to_tree()

    def _toggle_sheet_list(self, paned):
        if self._sheet_list_visible:
            paned.forget(self.frame_sheet_list)
            self.btn_toggle_sheet_list.config(text="» Mostrar lista de cue sheets")
        else:
            paned.insert(0, self.frame_sheet_list, weight=0)
            self.btn_toggle_sheet_list.config(text="« Ocultar lista de cue sheets")
        self._sheet_list_visible = not self._sheet_list_visible

    def _refresh_sheet_list(self):
        self.list_sheets.delete(0, "end")
        d = Path(self.cfg.get("cues_dir", core.DEFAULT_CFG["cues_dir"]))
        d.mkdir(parents=True, exist_ok=True)
        for fpath in sorted(d.glob("*.json")):
            self.list_sheets.insert("end", fpath.name)

    def _warn_if_title_mismatch(self, match):
        """Si hay un titulo actual del reproductor y no coincide con el
        'match' de la cue sheet, avisa (no bloquea, solo informa)."""
        if not match or not self.last_title:
            return
        if match.strip().lower() not in self.last_title.strip().lower():
            messagebox.showwarning(
                "Titulo distinto",
                f"Esta cue sheet es para '{match}', pero el reproductor "
                f"muestra ahora mismo:\n'{self.last_title}'.\n\n"
                "Vas a seguir editando/guardando sobre la cue sheet equivocada?")

    def _on_sheet_select(self, _event):
        sel = self.list_sheets.curselection()
        if not sel:
            return
        fname = self.list_sheets.get(sel[0])
        path = Path(self.cfg["cues_dir"]) / fname
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            messagebox.showerror("Error", f"No pude leer {fname}: {e}")
            return
        self.current_sheet_path = path
        self._editing_cue_iid = None
        self.var_sheet_match.set(data.get("match", ""))
        self.var_sheet_lead.set(str(data.get("lead_time_s", self.cfg.get("lead_time_s", 4))))
        self.var_sheet_filename.set(fname)
        self._load_cues_into_tree(data.get("cues", []))
        self._prev_sheet_selection = sel[0]
        self._warn_if_title_mismatch(data.get("match", ""))

    def _load_cues_into_tree(self, cues):
        self.tree_cues.delete(*self.tree_cues.get_children())
        for i, cue in enumerate(cues):
            if "state" in cue:
                modo, valor = "state", cue["state"]
            elif "duration_s" in cue:
                modo, valor = "burst", f'{cue.get("duration_s", "")}s'
            else:
                modo, valor = "scene", ""
            self.tree_cues.insert("", "end", iid=str(i),
                                   values=(cue["t"], cue.get("device", ""), modo, valor))
        self._refresh_timeline()

    def _tree_cues_to_list(self):
        cues = []
        for iid in self.tree_cues.get_children():
            t, device, modo, valor = self.tree_cues.item(iid, "values")
            cue = {"t": t, "device": device}
            if modo == "state":
                cue["state"] = valor
            elif modo == "burst":
                cue["duration_s"] = float(str(valor).rstrip("s") or 0)
            # 'scene': cue queda solo con t/device, sin campos extra
            cues.append(cue)
        cues.sort(key=lambda c: core.parse_time(c["t"]))
        return cues

    def _new_sheet(self, confirm=True):
        self.current_sheet_path = None
        self._editing_cue_iid = None
        self.var_sheet_match.set("")
        self.var_sheet_lead.set(str(self.cfg.get("lead_time_s", 4)))
        self.var_sheet_filename.set("nuevo_cue_sheet.json")
        self.tree_cues.delete(*self.tree_cues.get_children())
        self._refresh_timeline()
        self.list_sheets.selection_clear(0, "end")
        self._prev_sheet_selection = None

    def _delete_sheet(self):
        sel = self.list_sheets.curselection()
        if not sel:
            return
        fname = self.list_sheets.get(sel[0])
        if not messagebox.askyesno("Confirmar", f"Eliminar '{fname}'?"):
            return
        (Path(self.cfg["cues_dir"]) / fname).unlink(missing_ok=True)
        self._refresh_sheet_list()
        self._new_sheet(confirm=False)

    def _add_cue(self):
        t = self.var_cue_t.get().strip()
        device = self.var_cue_device.get().strip()
        if not t or not device:
            messagebox.showwarning("Falta informacion", "Timestamp y dispositivo son requeridos.")
            return
        try:
            core.parse_time(t)
        except Exception:
            messagebox.showwarning("Timestamp invalido", "Usa formato HH:MM:SS, MM:SS o segundos.")
            return

        # Si ya se esta editando un cue existente (seleccionado en la tabla),
        # los cambios ya se aplicaron en vivo con cada ajuste - este boton
        # solo confirma, no debe crear un duplicado.
        if self._editing_cue_iid and self.tree_cues.exists(self._editing_cue_iid):
            self._commit_form_to_tree()
            return

        cues = self._tree_cues_to_list()
        cues = [c for c in cues if not (c["t"] == t and c["device"] == device)]
        mode = self.var_cue_mode.get()
        if mode == "state":
            cues.append({"t": t, "device": device, "state": self.var_cue_value.get()})
        elif mode == "scene":
            cues.append({"t": t, "device": device})
        else:
            dur = float(self.var_cue_duration.get() or self.cfg.get("default_duration_s", 3))
            cues.append({"t": t, "device": device, "duration_s": dur})
        self._load_cues_into_tree(cues)

        # Seleccionar el cue recien creado para poder seguir ajustandolo en
        # vivo con las flechitas sin tener que volver a buscarlo en la tabla.
        for iid in self.tree_cues.get_children():
            vals = self.tree_cues.item(iid, "values")
            if vals[0] == t and vals[1] == device:
                self.tree_cues.selection_set(iid)
                self._editing_cue_iid = iid
                break
        self._autosave_sheet()

    def _on_cue_double_click(self, _event):
        sel = self.tree_cues.selection()
        if sel:
            self._load_cue_into_form(sel[0])

    def _load_cue_into_form(self, iid):
        # Mientras se puebla el formulario, ningun cambio de variable debe
        # disparar un commit en vivo (se pisaria la fila con datos a medio
        # cargar) - por eso se desactiva la edicion hasta terminar.
        self._editing_cue_iid = None
        t, device, modo, valor = self.tree_cues.item(iid, "values")
        self.var_cue_t.set(t)
        self.var_cue_device.set(device)
        self._on_cue_device_change()
        self.var_cue_mode.set(modo)
        if modo == "state":
            self.var_cue_value.set(valor)
        elif modo == "burst":
            self.var_cue_duration.set(str(valor).rstrip("s"))
        self._on_cue_mode_change()
        self._editing_cue_iid = iid

    def _delete_cue(self):
        sel = list(self.tree_cues.selection())
        if not sel:
            return
        order = list(self.tree_cues.get_children())
        sel_idx = [order.index(i) for i in sel if i in order]
        if not sel_idx:
            return
        max_idx = max(sel_idx)
        # Auto-seleccionar el siguiente cue tras el ultimo eliminado; si no
        # hay siguiente, el anterior a la seleccion.
        next_iid = next((i for i in order[max_idx + 1:] if i not in sel), None)
        if next_iid is None:
            before = [i for i in order[:max_idx] if i not in sel]
            next_iid = before[-1] if before else None
        for iid in sel:
            self.tree_cues.delete(iid)
        if next_iid and self.tree_cues.exists(next_iid):
            self.tree_cues.selection_set(next_iid)
            self._load_cue_into_form(next_iid)
        else:
            self._editing_cue_iid = None
        self._refresh_timeline()
        self._autosave_sheet()

    def _on_delete_key(self, event):
        # No interferir con Backspace/Supr mientras se edita texto en un
        # campo (Entry/Combobox/Text); solo actuar si el foco esta en la
        # tabla o el canvas de la timeline.
        if isinstance(event.widget, (tk.Entry, ttk.Entry, tk.Text, ttk.Combobox)):
            return
        self._delete_cue()

    def _use_current_title(self):
        title, _pos, _playing, error = core.get_playback(self.cfg)
        if error:
            messagebox.showwarning("Sin conexion", f"No pude leer el reproductor: {error}")
            return
        if not title:
            messagebox.showinfo("Sin titulo", "El reproductor no reporta un titulo en este momento.")
            return
        title = core.strip_video_ext(title)
        self.var_sheet_match.set(title)
        if not self.var_sheet_filename.get() or self.var_sheet_filename.get() == "nuevo_cue_sheet.json":
            self.var_sheet_filename.set(f"{core.slug_for_title(title)}.json")
        self.log(f"Titulo actual ({self.cfg.get('player_type', 'zidoo')}): '{title}'")

    # ---- Captura en vivo (atajos de teclado / Stream Deck) ----------------
    # Cuanto esperar tras un KeyRelease antes de darlo por bueno: si en ese
    # lapso llega un KeyPress de la misma tecla, era autorepeat del SO
    # (muchos sistemas mandan Release+Press sinteticos en cada repeticion
    # mientras la tecla sigue fisicamente sostenida), no una soltada real.
    CAPTURE_REPEAT_GUARD_MS = 60

    def _toggle_capture(self):
        self.capture_on = self.var_capture_on.get()
        if self.capture_on:
            self.bind_all("<KeyPress>", self._on_capture_keypress)
            self.bind_all("<KeyRelease>", self._on_capture_keyrelease)
            self._capture_held = {}
            self._capture_release_jobs = {}
            self.log("Captura en vivo activada. Rafagas: toque corto = duracion por "
                     "defecto, mantener presionada = duracion exacta sostenida.")
        else:
            self.unbind_all("<KeyPress>")
            self.unbind_all("<KeyRelease>")
            for job in self._capture_release_jobs.values():
                self.after_cancel(job)
            self._capture_held = {}
            self._capture_release_jobs = {}
            self.log("Captura en vivo desactivada.")

    def _update_capture_pos_label(self):
        if self.last_pos is None:
            self.lbl_capture_pos.config(text="Posicion: -")
        else:
            self.lbl_capture_pos.config(text=f"Posicion: {core.fmt_time_ms(self.last_pos)}")

    @staticmethod
    def _is_text_entry(widget):
        return isinstance(widget, (tk.Entry, ttk.Entry, tk.Text, ttk.Combobox))

    def _on_capture_keypress(self, event):
        if not self.capture_on or self._is_text_entry(event.widget):
            return
        key = (event.char or event.keysym).strip().upper()
        match = self.shortcut_map.get(key)
        if not match:
            return

        # Si habia un "release" pendiente de confirmar para esta tecla, este
        # KeyPress llego dentro de la ventana de gracia: era autorepeat, no
        # una soltada real. Cancelamos el release y seguimos sosteniendo.
        pending_job = self._capture_release_jobs.pop(key, None)
        if pending_job:
            self.after_cancel(pending_job)
            return

        if key in self._capture_held:
            return  # ya esta registrada (no deberia pasar, por seguridad)

        if self.last_pos is None:
            self.log("[captura] sin posicion del reproductor todavia (¿esta reproduciendo?).")
            return

        device_name, mode, extra = match
        self._capture_held[key] = {"device": device_name, "mode": mode, "extra": extra,
                                    "press_pos": self.last_pos}

        if mode != "burst":
            # Estado/escena: instantaneo al presionar, no tiene duracion que
            # sostener (el "held" solo sirve aqui para ignorar el autorepeat).
            t = core.fmt_time_ms(self.last_pos)
            cues = self._tree_cues_to_list()
            if mode == "state":
                cues.append({"t": t, "device": device_name, "state": extra})
                desc = f"estado {extra}"
            else:
                cues.append({"t": t, "device": device_name})
                desc = "activar escena"
            self._load_cues_into_tree(cues)
            self.log(f"[captura] {t} -> {device_name} ({desc})")

    def _on_capture_keyrelease(self, event):
        if not self.capture_on or self._is_text_entry(event.widget):
            return
        key = (event.char or event.keysym).strip().upper()
        if key not in self._capture_held:
            return
        job = self.after(self.CAPTURE_REPEAT_GUARD_MS, self._finalize_capture_release, key)
        self._capture_release_jobs[key] = job

    def _finalize_capture_release(self, key):
        self._capture_release_jobs.pop(key, None)
        held = self._capture_held.pop(key, None)
        if not held or held["mode"] != "burst" or self.last_pos is None:
            return
        press_pos = held["press_pos"]
        device_name = held["device"]
        held_dur = self.last_pos - press_pos
        # Toque corto (menos de ~0.3s de reproduccion sostenida): usar la
        # duracion por defecto en vez de un valor casi cero.
        dur = held_dur if held_dur >= 0.3 else self.cfg.get("default_duration_s", 3)
        t = core.fmt_time_ms(press_pos)
        cues = self._tree_cues_to_list()
        cues.append({"t": t, "device": device_name, "duration_s": round(dur, 2)})
        self._load_cues_into_tree(cues)
        origen = "sostenida" if held_dur >= 0.3 else "toque corto, duracion por defecto"
        self.log(f"[captura] {t} -> {device_name} (rafaga {round(dur, 2)}s, {origen})")

    # ---- Timeline visual ---------------------------------------------------
    def _device_color(self, device_name, device_index, modo, valor):
        """Color estable por dispositivo (para identificarlo de un vistazo en
        el timeline); los cues 'state' varian el brillo segun el estado para
        distinguir velocidades/efectos dentro del mismo dispositivo."""
        idx = device_index.get(device_name, 0)
        hue = (idx * 0.61803398875) % 1.0  # angulo dorado: colores bien separados
        if modo == "state":
            v = 0.55 + 0.35 * ((hash(valor) % 100) / 100.0)
        else:
            v = 0.85
        r, g, b = colorsys.hsv_to_rgb(hue, 0.70, v)
        return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

    @staticmethod
    def _text_color_for(hex_color):
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
        return "#000000" if luminance > 0.55 else "#ffffff"

    def _zoom_timeline(self, factor):
        self.pixels_per_sec = max(0.5, min(40.0, self.pixels_per_sec * factor))
        self._refresh_timeline()
        self._scroll_timeline_to_playhead()

    def _scroll_timeline_to_playhead(self):
        if self.last_pos is None:
            return
        bbox = self.timeline_canvas.bbox("all")
        if not bbox or bbox[2] <= 0:
            return
        total_w = bbox[2]
        x = self.last_pos * self.pixels_per_sec
        frac = max(0.0, min(1.0, (x - 100) / total_w))
        self.timeline_canvas.xview_moveto(frac)

    def _nice_tick_interval(self):
        target_px = 80
        for interval in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
            if interval * self.pixels_per_sec >= target_px:
                return interval
        return 3600

    def _refresh_timeline(self):
        if not hasattr(self, "timeline_canvas"):
            return
        self.timeline_canvas.delete("all")
        self.timeline_labels.delete("all")

        devices = self.cfg.get("devices", [])
        row_h = self.timeline_row_h
        ruler_h = self.timeline_ruler_h
        device_index = {d["name"]: i for i, d in enumerate(devices)}
        height = ruler_h + max(row_h * max(len(devices), 1), 60)
        self.timeline_labels.configure(height=height)
        self.timeline_canvas.configure(height=height)

        self.timeline_labels.create_rectangle(0, 0, 100, ruler_h, fill="#141416", outline="")
        label_bg = ["#2a2a2e", "#333338"]
        for i, d in enumerate(devices):
            y = ruler_h + i * row_h
            swatch_color = self._device_color(d["name"], device_index, "burst", "")
            suffix = f" ({d['shortcut']})" if d.get("kind") != "multi_state" and d.get("shortcut") else ""
            self.timeline_labels.create_rectangle(0, y, 100, y + row_h,
                                                   fill=label_bg[i % 2], outline="")
            self.timeline_labels.create_rectangle(4, y + 5, 12, y + row_h - 5,
                                                   fill=swatch_color, outline="")
            self.timeline_labels.create_text(18, y + row_h / 2, anchor="w", text=d["name"] + suffix,
                                              fill="#ffffff", font=("", 9, "bold"))

        items = list(self.tree_cues.get_children())
        max_t = 60.0
        parsed = []
        for iid in items:
            t, device, modo, valor = self.tree_cues.item(iid, "values")
            try:
                t_s = core.parse_time(t)
            except Exception:
                continue
            parsed.append((iid, t_s, device, modo, valor))
            dur = float(str(valor).rstrip("s") or 0) if modo == "burst" else 0
            max_t = max(max_t, t_s + dur)

        total_w = max(700, int((max_t + 30) * self.pixels_per_sec))
        self.timeline_canvas.configure(scrollregion=(0, 0, total_w, height))

        # Lineas de fondo por fila (para leer mejor a que dispositivo pertenece)
        row_bg = ["#1e1e22", "#26262a"]
        for i in range(len(devices)):
            y = ruler_h + i * row_h
            self.timeline_canvas.create_rectangle(0, y, total_w, y + row_h,
                                                    fill=row_bg[i % 2], outline="")

        selected_iids = set(self.tree_cues.selection())

        parsed.sort(key=lambda p: p[1])
        by_device = {}
        for iid, t_s, device, modo, valor in parsed:
            by_device.setdefault(device, []).append((iid, t_s, modo, valor))

        self._cue_geom = {}
        for iid, t_s, device, modo, valor in parsed:
            row = device_index.get(device)
            if row is None:
                continue
            y0 = ruler_h + row * row_h + 2
            y1 = y0 + row_h - 4
            x0 = t_s * self.pixels_per_sec
            if modo == "burst":
                dur = float(str(valor).rstrip("s") or self.cfg.get("default_duration_s", 3))
                x1 = x0 + max(dur, 0.3) * self.pixels_per_sec
                label = f"{valor}"
            elif modo == "state":
                siblings = sorted(by_device.get(device, []), key=lambda s: s[1])
                nxt = next((s[1] for s in siblings if s[1] > t_s), None)
                x1 = (nxt if nxt is not None else t_s + 20) * self.pixels_per_sec
                label = valor
            else:  # scene
                x1 = x0 + 6
                label = ""
            color = self._device_color(device, device_index, modo, valor)
            outline = "#4fd1ff" if iid in selected_iids else "#000000"
            outline_w = 2 if iid in selected_iids else 1
            rect_id = self.timeline_canvas.create_rectangle(
                x0, y0, max(x1, x0 + 4), y1, fill=color, outline=outline, width=outline_w,
                tags=("cue", f"iid_{iid}"))
            text_id = None
            if label and x1 - x0 > 20:
                text_id = self.timeline_canvas.create_text(
                    (x0 + x1) / 2, (y0 + y1) / 2, text=label, font=("", 7, "bold"),
                    fill=self._text_color_for(color), tags=("cue", f"iid_{iid}"))
            self._cue_geom[iid] = {"x0": x0, "x1": x1, "y0": y0, "y1": y1, "modo": modo,
                                    "device": device, "t_s": t_s, "rect_id": rect_id, "text_id": text_id}

        # Marcador de seleccion: donde quedo asignada cada accion seleccionada
        for sel_iid in selected_iids:
            g = self._cue_geom.get(sel_iid)
            if not g:
                continue
            self.timeline_canvas.create_line(g["x0"], 0, g["x0"], height, fill="#4fd1ff",
                                              width=1, dash=(4, 2), tags=("selection_marker",))
            if g["modo"] == "burst" and g["x1"] > g["x0"] + 1:
                self.timeline_canvas.create_line(g["x1"], 0, g["x1"], height, fill="#4fd1ff",
                                                  width=1, dash=(4, 2), tags=("selection_marker",))

        self._draw_ruler(total_w, height)
        self._draw_playhead(height)

    def _draw_ruler(self, total_w, height):
        ruler_h = self.timeline_ruler_h
        self.timeline_canvas.create_rectangle(0, 0, total_w, ruler_h, fill="#141416", outline="")
        self.timeline_canvas.create_line(0, ruler_h, total_w, ruler_h, fill="#555555")
        interval = self._nice_tick_interval()
        t = 0
        while t * self.pixels_per_sec <= total_w:
            x = t * self.pixels_per_sec
            self.timeline_canvas.create_line(x, ruler_h - 6, x, ruler_h, fill="#999999", tags=("ruler",))
            self.timeline_canvas.create_text(x + 3, ruler_h - 14, anchor="w", text=core.fmt_time(t),
                                              fill="#cccccc", font=("", 7), tags=("ruler",))
            t += interval

    def _draw_playhead(self, height=None):
        self.timeline_canvas.delete("playhead")
        if self.last_pos is None:
            return
        if height is None:
            height = int(self.timeline_canvas.cget("height"))
        x = self.last_pos * self.pixels_per_sec
        self.timeline_canvas.create_line(x, 0, x, height, fill="#ffee58", width=2, tags=("playhead",))
        self.timeline_canvas.create_polygon(x - 5, 0, x + 5, 0, x, 8, fill="#ffee58", tags=("playhead",))

    def _timeline_tooltip(self, x, y, text):
        self.timeline_canvas.delete("tooltip")
        pad = 4
        text_id = self.timeline_canvas.create_text(x + 10, y - 10, anchor="w", text=text,
                                                     fill="#000000", font=("", 8, "bold"), tags=("tooltip",))
        bbox = self.timeline_canvas.bbox(text_id)
        if bbox:
            rect_id = self.timeline_canvas.create_rectangle(
                bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad,
                fill="#ffee58", outline="#000000", tags=("tooltip",))
            self.timeline_canvas.tag_raise(text_id, rect_id)

    EDGE_PX = 6

    def _find_timeline_iid(self, event):
        x = self.timeline_canvas.canvasx(event.x)
        y = self.timeline_canvas.canvasy(event.y)
        for item in self.timeline_canvas.find_overlapping(x, y, x, y):
            for tag in self.timeline_canvas.gettags(item):
                if tag.startswith("iid_"):
                    return tag[4:]
        return None

    # Bits de event.state para modificadores (Tk): Shift=0x0001,
    # Control=0x0004, Command/Mod1 en macOS suele llegar como 0x0008.
    SHIFT_MASK = 0x0001
    CTRL_MASK = 0x0004 | 0x0008

    def _on_timeline_press(self, event):
        iid = self._find_timeline_iid(event)
        shift = bool(event.state & self.SHIFT_MASK)
        ctrl = bool(event.state & self.CTRL_MASK)

        y = self.timeline_canvas.canvasy(event.y)
        if y < self.timeline_ruler_h and self.preview_active:
            x = self.timeline_canvas.canvasx(event.x)
            self.seek_preview_to(x / self.pixels_per_sec)

        if not iid:
            if not shift and not ctrl:
                self.tree_cues.selection_set(())
                self._refresh_timeline()
            self._timeline_drag = None
            return

        order = list(self.tree_cues.get_children())
        last_click = getattr(self, "_last_timeline_click_iid", None)

        if shift and last_click in order:
            i0, i1 = order.index(last_click), order.index(iid)
            lo, hi = sorted((i0, i1))
            self.tree_cues.selection_set(order[lo:hi + 1])
            self._timeline_drag = None
        elif ctrl:
            current = set(self.tree_cues.selection())
            current.symmetric_difference_update({iid})
            self.tree_cues.selection_set(tuple(current))
            self._timeline_drag = None
        else:
            self.tree_cues.selection_set(iid)
            geom = self._cue_geom.get(iid, {})
            t, device, modo, valor = self.tree_cues.item(iid, "values")
            x = self.timeline_canvas.canvasx(event.x)
            drag_mode = "move"
            if modo == "burst" and geom:
                if abs(x - geom["x0"]) <= self.EDGE_PX:
                    drag_mode = "resize-left"
                elif abs(x - geom["x1"]) <= self.EDGE_PX:
                    drag_mode = "resize-right"
            dur = float(str(valor).rstrip("s")) if modo == "burst" else 0.0
            self._timeline_drag = {
                "iid": iid, "mode": drag_mode, "start_x": x, "press_x": x, "moved": False,
                "orig_t": core.parse_time(t), "orig_dur": dur,
            }
            cursor = "sb_h_double_arrow" if drag_mode != "move" else "fleur"
            self.timeline_canvas.configure(cursor=cursor)

        self._last_timeline_click_iid = iid
        self.tree_cues.see(iid)
        self._refresh_timeline()

    def _on_timeline_drag(self, event):
        d = self._timeline_drag
        if not d:
            return
        x = self.timeline_canvas.canvasx(event.x)
        dx = x - d["start_x"]
        if abs(x - d["press_x"]) > 3:
            d["moved"] = True
        d["start_x"] = x
        dt = dx / self.pixels_per_sec

        geom = self._cue_geom.get(d["iid"])
        if d["mode"] == "move":
            self.timeline_canvas.move(f"iid_{d['iid']}", dx, 0)
            d["orig_t"] = max(0.0, d["orig_t"] + dt)
            hint = core.fmt_time_ms(d["orig_t"])
            if d["orig_dur"]:
                hint += f"  ->  {core.fmt_time_ms(d['orig_t'] + d['orig_dur'])}"
        elif d["mode"] == "resize-right":
            d["orig_dur"] = max(0.2, d["orig_dur"] + dt)
            new_x1 = geom["x0"] + d["orig_dur"] * self.pixels_per_sec
            self.timeline_canvas.coords(geom["rect_id"], geom["x0"], geom["y0"], new_x1, geom["y1"])
            if geom["text_id"]:
                self.timeline_canvas.coords(geom["text_id"], (geom["x0"] + new_x1) / 2, (geom["y0"] + geom["y1"]) / 2)
            hint = f"{core.fmt_time_ms(d['orig_t'])}  ->  {core.fmt_time_ms(d['orig_t'] + d['orig_dur'])}"
        else:  # resize-left
            new_t = max(0.0, d["orig_t"] + dt)
            new_dur = max(0.2, d["orig_dur"] - dt)
            d["orig_t"], d["orig_dur"] = new_t, new_dur
            new_x0 = new_t * self.pixels_per_sec
            self.timeline_canvas.coords(geom["rect_id"], new_x0, geom["y0"], geom["x1"], geom["y1"])
            if geom["text_id"]:
                self.timeline_canvas.coords(geom["text_id"], (new_x0 + geom["x1"]) / 2, (geom["y0"] + geom["y1"]) / 2)
            hint = f"{core.fmt_time_ms(new_t)}  ->  {core.fmt_time_ms(new_t + new_dur)}"

        self._timeline_tooltip(x, geom["y0"] if geom else event.y, hint)

    def _on_timeline_release(self, event):
        d = self._timeline_drag
        if not d:
            return
        self.timeline_canvas.configure(cursor="")
        self.timeline_canvas.delete("tooltip")
        iid = d["iid"]
        if d["moved"]:
            vals = list(self.tree_cues.item(iid, "values"))
            new_t = max(0.0, d["orig_t"])
            vals[0] = core.fmt_time_ms(new_t)
            if d["mode"] != "move":
                vals[3] = f"{round(d['orig_dur'], 2)}s"
            self.tree_cues.item(iid, values=vals)
            self._load_cues_into_tree(self._tree_cues_to_list())
            new_iid = self._iid_for_time_device(vals[0], vals[1])
            if new_iid:
                self.tree_cues.selection_set(new_iid)
                self._load_cue_into_form(new_iid)
            self._autosave_sheet()
        else:
            # Fue un clic simple (sin arrastre real): solo seleccionar y
            # cargar en el formulario para editar, sin reordenar la lista.
            self._load_cue_into_form(iid)
        self._timeline_drag = None

    def _iid_for_time_device(self, t, device):
        for iid in self.tree_cues.get_children():
            vals = self.tree_cues.item(iid, "values")
            if vals[0] == t and vals[1] == device:
                return iid
        return None

    def _on_cue_tree_select(self, _event):
        self._refresh_timeline()
        sel = self.tree_cues.selection()
        if len(sel) == 1:
            self._load_cue_into_form(sel[0])
        else:
            self._editing_cue_iid = None

    def _save_sheet(self):
        fname = self.var_sheet_filename.get().strip()
        if not fname:
            messagebox.showwarning("Falta nombre", "Dale un nombre de archivo a la cue sheet.")
            return
        if not fname.endswith(".json"):
            fname += ".json"
        match = self.var_sheet_match.get().strip()
        if not match:
            messagebox.showwarning("Falta 'match'", "Escribe un substring del titulo para asociar esta cue sheet.")
            return

        d = Path(self.cfg["cues_dir"])
        d.mkdir(parents=True, exist_ok=True)
        target = d / fname
        if target.exists() and target != self.current_sheet_path:
            if not messagebox.askyesno(
                    "Sobrescribir archivo existente",
                    f"Ya existe '{fname}' y no es el archivo que tenias cargado.\n"
                    "¿Sobrescribirlo con los cues que estas editando ahora?"):
                return

        self._warn_if_title_mismatch(match)

        data = {
            "match": match,
            "lead_time_s": float(self.var_sheet_lead.get() or self.cfg.get("lead_time_s", 4)),
            "cues": self._tree_cues_to_list(),
        }
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self.current_sheet_path = target
        self._refresh_sheet_list()
        if self.engine and self.engine.running:
            self.engine.refresh_sheets()
        self.log(f"Cue sheet guardada: {fname} ({len(data['cues'])} cues)")
        messagebox.showinfo("4DFX", f"Guardado: {fname}")

    def _import_state_txt(self):
        multi_state_devices = [d["name"] for d in self.cfg.get("devices", []) if d.get("kind") == "multi_state"]
        if not multi_state_devices:
            messagebox.showwarning("Sin dispositivos multi-estado",
                                    "Crea primero un dispositivo 'Multi-estado' (ej. ventilador) en la pestana Dispositivos.")
            return
        path = filedialog.askopenfilename(title="Selecciona el archivo de efectos (.txt)",
                                           filetypes=[("Texto", "*.txt"), ("Todos", "*.*")])
        if not path:
            return

        device_name = multi_state_devices[0]
        if len(multi_state_devices) > 1:
            dlg = _PickOneDialog(self, "Elige el dispositivo destino", multi_state_devices)
            self.wait_window(dlg)
            if not dlg.result:
                return
            device_name = dlg.result

        try:
            imported = core.import_state_txt(path, device_name)
        except Exception as e:
            messagebox.showerror("Error al importar", str(e))
            return

        existing = self._tree_cues_to_list()
        existing = [c for c in existing if c.get("device") != device_name]
        merged = sorted(existing + imported, key=lambda c: core.parse_time(c["t"]))
        self._load_cues_into_tree(merged)
        if not self.var_sheet_filename.get() or self.var_sheet_filename.get() == "nuevo_cue_sheet.json":
            self.var_sheet_filename.set(Path(path).stem + ".json")
        if not self.var_sheet_match.get():
            self.var_sheet_match.set(Path(path).stem)
        self.log(f"Importados {len(imported)} cues de '{Path(path).name}' para '{device_name}'. Revisa 'match' y guarda.")

    # -- Tab: Configuracion --------------------------------------------------
    def _build_config_tab(self):
        outer = ttk.Frame(self.tab_config)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        vscroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        vscroll.pack(side="right", fill="y")
        f = ttk.Frame(canvas, padding=14)
        canvas.create_window((0, 0), window=f, anchor="nw")
        f.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        self.vars = {}

        def row(parent, label, key, r, width=28):
            ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=4)
            v = tk.StringVar(value=str(self.cfg.get(key, "")))
            ttk.Entry(parent, textvariable=v, width=width).grid(row=r, column=1, sticky="w", pady=4, padx=4)
            self.vars[key] = v
            return v

        # -- Reproductor (fuente de titulo/posicion) -------------------------
        player_frame = ttk.LabelFrame(f, text="Reproductor (fuente de titulo/posicion)", padding=10)
        player_frame.pack(fill="x", pady=(0, 10))
        type_row = ttk.Frame(player_frame)
        type_row.pack(fill="x", pady=(0, 8))
        ttk.Label(type_row, text="Tipo:").pack(side="left")
        self.vars["player_type"] = self.var_player_type
        combo_player = ttk.Combobox(type_row, textvariable=self.var_player_type, state="readonly",
                                     values=["zidoo", "vlc", "jriver"], width=10)
        combo_player.pack(side="left", padx=6)
        combo_player.bind("<<ComboboxSelected>>", lambda e: self._on_player_type_selected())

        ttk.Label(player_frame, text=f"IP de esta maquina en tu red: {core.local_ip()}",
                  foreground="#666", font=("", 8)).pack(anchor="w", pady=(0, 6))

        self.frame_player_zidoo = ttk.Frame(player_frame)
        row(self.frame_player_zidoo, "IP", "zidoo_ip", 0)
        row(self.frame_player_zidoo, "Puerto API", "zidoo_port", 1)

        self.frame_player_vlc = ttk.Frame(player_frame)
        row(self.frame_player_vlc, "URL (ej http://127.0.0.1:8080)", "vlc_url", 0, width=32)
        row(self.frame_player_vlc, "Password (Preferencias > Interfaz > Web)", "vlc_password", 1, width=32)
        ttk.Label(self.frame_player_vlc,
                  text="Activa 'Interfaz web' en VLC (Preferencias > Interfaz >\n"
                       "Principales > Web) y define un password ahi mismo.",
                  foreground="#666", font=("", 8)).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.frame_player_jriver = ttk.Frame(player_frame)
        row(self.frame_player_jriver, "URL MCWS (ej http://127.0.0.1:52199)", "jriver_url", 0, width=32)
        row(self.frame_player_jriver, "Usuario (si tiene Access Control)", "jriver_user", 1, width=32)
        row(self.frame_player_jriver, "Password", "jriver_password", 2, width=32)

        self._on_player_type_change()

        # -- Home Assistant ---------------------------------------------------
        ha_frame = ttk.LabelFrame(f, text="Home Assistant", padding=10)
        ha_frame.pack(fill="x", pady=(0, 10))
        row(ha_frame, "URL (ej http://192.168.1.50:8123)", "ha_url", 0, width=32)
        row(ha_frame, "Long-Lived Access Token", "ha_token", 1, width=32)

        # -- MQTT (dispositivos sin pasar por HA) ------------------------------
        mqtt_frame = ttk.LabelFrame(f, text="MQTT (opcional - dispositivos directos, sin Home Assistant)", padding=10)
        mqtt_frame.pack(fill="x", pady=(0, 10))
        row(mqtt_frame, "Host del broker", "mqtt_host", 0, width=28)
        row(mqtt_frame, "Puerto", "mqtt_port", 1, width=10)
        row(mqtt_frame, "Usuario (opcional)", "mqtt_user", 2, width=28)
        row(mqtt_frame, "Password (opcional)", "mqtt_password", 3, width=28)
        ttk.Label(mqtt_frame, text="Requiere 'pip install paho-mqtt'. Util para probar\n"
                                   "estrobos/Shelly directo, sin depender de HA.",
                  foreground="#666", font=("", 8)).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # -- Sincronizacion -----------------------------------------------------
        sync_frame = ttk.LabelFrame(f, text="Sincronizacion", padding=10)
        sync_frame.pack(fill="x", pady=(0, 10))
        row(sync_frame, "Lead time por defecto (s)", "lead_time_s", 0, width=10)
        row(sync_frame, "Duracion por defecto (s)", "default_duration_s", 1, width=10)
        row(sync_frame, "Intervalo de sondeo (s)", "poll_interval_s", 2, width=10)
        row(sync_frame, "Ventana max. de retraso (s)", "max_late_s", 3, width=10)

        # -- Cue sheets -----------------------------------------------------
        cues_frame = ttk.LabelFrame(f, text="Cue sheets", padding=10)
        cues_frame.pack(fill="x", pady=(0, 10))
        row(cues_frame, "Carpeta de cue sheets", "cues_dir", 0, width=40)
        ttk.Button(cues_frame, text="Elegir carpeta...", command=self._pick_cues_dir).grid(row=0, column=2, padx=6)

        ttk.Button(f, text="Guardar configuracion", command=self.save_settings).pack(anchor="w", pady=(6, 0))

    def _on_player_type_change(self):
        ptype = self.var_player_type.get()
        self.frame_player_zidoo.pack_forget()
        self.frame_player_vlc.pack_forget()
        self.frame_player_jriver.pack_forget()
        if ptype == "vlc":
            self.frame_player_vlc.pack(fill="x")
        elif ptype == "jriver":
            self.frame_player_jriver.pack(fill="x")
        else:
            self.frame_player_zidoo.pack(fill="x")

    def _on_player_type_selected(self):
        """Cambia la fuente de reproduccion de inmediato (no hace falta ir a
        Configuracion ni pulsar 'Guardar'), para poder conmutar Zidoo/VLC/
        JRiver mientras se edita una cue sheet."""
        self.cfg["player_type"] = self.var_player_type.get()
        core.save_config(self.cfg)
        self._on_player_type_change()
        self.log(f"Fuente de reproduccion: {self.var_player_type.get()}")

    def _pick_cues_dir(self):
        d = filedialog.askdirectory()
        if d:
            self.vars["cues_dir"].set(d)

    # ------------------------------------------------------------- logic --
    def log(self, msg):
        self.log_queue.put(str(msg))

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.txt_log["state"] = "normal"
                self.txt_log.insert("end", msg + "\n")
                self.txt_log.see("end")
                self.txt_log["state"] = "disabled"
        except queue.Empty:
            pass
        self.after(150, self._poll_log_queue)

    def _poll_state_queue(self):
        try:
            while True:
                title, pos, playing, sheet, error = self.state_queue.get_nowait()
                ptype = self.cfg.get("player_type", "zidoo").upper()
                if error:
                    self.lbl_zidoo.config(text=f"{ptype}: ERROR ({error})", foreground="red")
                    continue
                self.lbl_title.config(text=f"Titulo: {title or '-'}")
                self.lbl_pos.config(text=f"Posicion: {core.fmt_time(pos or 0)}  (reproduciendo: {playing})")
                self.lbl_sheet.config(text=f"Cue sheet: {sheet['_file'] if sheet else '(ninguna)'}")
                self.lbl_zidoo.config(text=f"{ptype}: OK", foreground="green")
                self.last_title = title
                self.last_pos = pos
                # Si ya hay una cue sheet que coincide con lo que se esta
                # reproduciendo y la sincronizacion no esta activa, la
                # arrancamos sola (una sola vez por titulo, para no
                # reintentar en cada tick si la config esta incompleta).
                if sheet and not (self.engine and self.engine.running):
                    if getattr(self, "_last_auto_sync_title", None) != title:
                        self._last_auto_sync_title = title
                        self.start_sync(auto=True)
                if hasattr(self, "lbl_capture_pos"):
                    self._update_capture_pos_label()
                if hasattr(self, "timeline_canvas") and not self._timeline_drag:
                    self._draw_playhead()
        except queue.Empty:
            pass
        self.after(500, self._poll_state_queue)

    def _poll_status_queue(self):
        try:
            while True:
                name, text = self.status_queue.get_nowait()
                self._set_device_status(name, text)
        except queue.Empty:
            pass
        self.after(200, self._poll_status_queue)

    def _player_configured(self):
        ptype = self.cfg.get("player_type", "zidoo")
        if ptype == "vlc":
            return bool(self.cfg.get("vlc_url"))
        if ptype == "jriver":
            return bool(self.cfg.get("jriver_url"))
        return bool(self.cfg.get("zidoo_ip"))

    def _start_monitor(self):
        """Sondea el reproductor continuamente para que la posicion avance en
        pantalla aunque la sincronizacion (disparo de cues) no este activa."""
        def _run():
            while not self._monitor_stop.is_set():
                if self.engine and self.engine.running:
                    time.sleep(1)
                    continue
                if getattr(self, "preview_active", False):
                    # La vista previa local (libVLC embebido) ya reporta su
                    # propia posicion; no pelear con el sondeo remoto.
                    time.sleep(1)
                    continue
                if not self._player_configured():
                    time.sleep(1)
                    continue
                title, pos, playing, error = core.get_playback(self.cfg)
                if error:
                    self.state_queue.put((None, None, False, None, error))
                else:
                    sheets = core.load_cue_sheets(self.cfg, log=lambda *_: None)
                    sheet = core.match_sheet(sheets, title) if title else None
                    self.state_queue.put((title, pos, playing, sheet, None))
                time.sleep(1)
        threading.Thread(target=_run, daemon=True).start()

    def _refresh_devices_list(self):
        self.tree_devices.delete(*self.tree_devices.get_children())
        for d in self.cfg.get("devices", []):
            if d.get("kind") == "multi_state":
                keys = ",".join(f"{n}:{s.get('shortcut','')}" for n, s in d.get("states", {}).items() if s.get("shortcut"))
                detalle = ", ".join(sorted(d.get("states", {}).keys()))
                atajo = keys
            else:
                atajo = d.get("shortcut", "")
                if d.get("kind") == "scene":
                    detalle = f"activar: {d.get('activate_service', 'scene/turn_on')}"
                elif d.get("kind") == "mqtt":
                    detalle = f"mqtt: {d.get('on_topic', '')} / {d.get('off_topic', '')}"
                else:
                    detalle = f"{d.get('on_service', '')} / {d.get('off_service', '')}"
            estado = self.device_status.get(d["name"], "") if hasattr(self, "device_status") else ""
            self.tree_devices.insert("", "end", iid=d["name"],
                                      values=(d["name"], d.get("kind", "binary"), atajo, d["entity_id"], detalle, estado))
        self._refresh_test_state_options()
        if hasattr(self, "combo_cue_device"):
            self._refresh_cue_device_options()
        self._refresh_shortcut_map()

    def _refresh_test_state_options(self, device=None):
        if device and device.get("kind") == "multi_state":
            states = sorted(device.get("states", {}).keys())
            self.combo_test_state["values"] = states
            self.var_test_state.set(states[0] if states else "")
        else:
            self.combo_test_state["values"] = []
            self.var_test_state.set("")

    def _refresh_shortcut_map(self):
        """Construye char -> (device_name, modo, extra) para la captura en
        vivo (teclado / Stream Deck configurado para enviar teclas)."""
        mapping = {}
        for d in self.cfg.get("devices", []):
            if d.get("kind") == "multi_state":
                for st_name, s in d.get("states", {}).items():
                    key = (s.get("shortcut") or "").strip().upper()
                    if key:
                        mapping[key] = (d["name"], "state", st_name)
            else:
                key = (d.get("shortcut") or "").strip().upper()
                if key:
                    mode = "scene" if d.get("kind") == "scene" else "burst"
                    mapping[key] = (d["name"], mode, None)
        self.shortcut_map = mapping
        if hasattr(self, "timeline_canvas"):
            self._refresh_timeline()

    def _on_device_select(self, _event):
        sel = self.tree_devices.selection()
        device = core.get_device(self.cfg, sel[0]) if sel else None
        self._refresh_test_state_options(device)

    def _on_device_double_click(self, _event):
        sel = self.tree_devices.selection()
        if not sel:
            return
        device = core.get_device(self.cfg, sel[0])
        if not device:
            return
        self._editing_original_name = device["name"]
        self.var_dev_name.set(device["name"])
        self.var_dev_entity.set(device["entity_id"])
        self.var_dev_kind.set(device.get("kind", "binary"))
        self.var_dev_shortcut.set(device.get("shortcut", ""))
        self.var_dev_lead_time.set("" if device.get("lead_time_s") in (None, "") else str(device["lead_time_s"]))
        self.tree_states.delete(*self.tree_states.get_children())
        if device.get("kind") == "multi_state":
            for name, s in device.get("states", {}).items():
                self.tree_states.insert("", "end", iid=name,
                                         values=(name, s["service"], json.dumps(s.get("data", {})),
                                                 s.get("shortcut", "")))
        if device.get("kind") == "binary":
            simple_data = {"entity_id": device.get("entity_id")}
            is_advanced = (device.get("on_data") not in (None, simple_data)
                           or device.get("off_data") not in (None, simple_data))
            self.var_dev_advanced.set(is_advanced)
            self.var_dev_on_service.set(device.get("on_service", ""))
            self.var_dev_off_service.set(device.get("off_service", ""))
            self.var_dev_on_data.set(json.dumps(device.get("on_data") or simple_data))
            self.var_dev_off_data.set(json.dumps(device.get("off_data") or simple_data))
        if device.get("kind") == "mqtt":
            self.var_dev_mqtt_on_topic.set(device.get("on_topic", ""))
            self.var_dev_mqtt_on_payload.set(device.get("on_payload", "ON"))
            self.var_dev_mqtt_off_topic.set(device.get("off_topic", ""))
            self.var_dev_mqtt_off_payload.set(device.get("off_payload", "OFF"))
        self._on_dev_kind_change()
        self._refresh_test_state_options(device)

    def add_device(self):
        name = self.var_dev_name.get().strip()
        entity = self.var_dev_entity.get().strip()
        kind = self.var_dev_kind.get()
        shortcut = self.var_dev_shortcut.get().strip()
        if not name or (kind != "mqtt" and not entity):
            messagebox.showwarning("Falta informacion", "Nombre y entity_id son requeridos.")
            return

        if kind == "mqtt":
            on_topic = self.var_dev_mqtt_on_topic.get().strip()
            off_topic = self.var_dev_mqtt_off_topic.get().strip()
            if not on_topic or not off_topic:
                messagebox.showwarning("Falta informacion", "Topic ON y Topic OFF son requeridos.")
                return
            device = {"name": name, "kind": "mqtt", "entity_id": entity,
                      "on_topic": on_topic, "on_payload": self.var_dev_mqtt_on_payload.get(),
                      "off_topic": off_topic, "off_payload": self.var_dev_mqtt_off_payload.get(),
                      "shortcut": shortcut}
        elif kind == "multi_state":
            states = {}
            for iid in self.tree_states.get_children():
                st_name, service, data, st_shortcut = self.tree_states.item(iid, "values")
                states[st_name] = {"service": service, "data": json.loads(data or "{}"),
                                    "shortcut": st_shortcut}
            if not states:
                messagebox.showwarning("Sin estados", "Agrega al menos un estado (o usa 'Plantilla ventilador').")
                return
            device = {"name": name, "kind": "multi_state", "entity_id": entity, "states": states}
        elif kind == "scene":
            device = {"name": name, "kind": "scene", "entity_id": entity,
                      "activate_service": "scene/turn_on", "shortcut": shortcut}
        elif kind == "binary" and self.var_dev_advanced.get():
            try:
                on_data = json.loads(self.var_dev_on_data.get().strip() or "{}")
                off_data = json.loads(self.var_dev_off_data.get().strip() or "{}")
            except Exception as e:
                messagebox.showwarning("JSON invalido", f"Datos ON/OFF invalidos: {e}")
                return
            on_srv = self.var_dev_on_service.get().strip()
            off_srv = self.var_dev_off_service.get().strip()
            if not on_srv or not off_srv:
                messagebox.showwarning("Falta informacion", "Servicio ON y OFF son requeridos en modo avanzado.")
                return
            device = {"name": name, "kind": "binary", "entity_id": entity,
                      "on_service": on_srv, "off_service": off_srv,
                      "on_data": on_data, "off_data": off_data, "shortcut": shortcut}
        else:
            on_srv, off_srv = core.services_for_entity(entity)
            device = {"name": name, "kind": "binary", "entity_id": entity,
                      "on_service": on_srv, "off_service": off_srv,
                      "on_data": {"entity_id": entity}, "off_data": {"entity_id": entity},
                      "shortcut": shortcut}

        lead_str = self.var_dev_lead_time.get().strip()
        if lead_str:
            try:
                device["lead_time_s"] = float(lead_str)
            except ValueError:
                messagebox.showwarning("Lead time invalido", "El lead time del dispositivo debe ser un numero.")
                return

        original_name = getattr(self, "_editing_original_name", None)
        devices = [d for d in self.cfg.get("devices", [])
                   if d["name"].lower() not in (name.lower(), (original_name or "").lower())]
        devices.append(device)
        self.cfg["devices"] = devices
        core.save_config(self.cfg)
        self._editing_original_name = None
        self._refresh_devices_list()
        self._refresh_shortcut_map()
        self.var_dev_name.set("")
        self.var_dev_entity.set("")
        self.var_dev_shortcut.set("")
        self.var_dev_lead_time.set("")
        self.var_dev_advanced.set(False)
        self._on_dev_advanced_toggle()
        self.tree_states.delete(*self.tree_states.get_children())
        self.log(f"Dispositivo '{name}' guardado ({entity}).")

    def remove_device(self):
        sel = self.tree_devices.selection()
        if not sel:
            return
        name = sel[0]
        self.cfg["devices"] = [d for d in self.cfg.get("devices", []) if d["name"] != name]
        core.save_config(self.cfg)
        self._refresh_devices_list()
        self.log(f"Dispositivo '{name}' eliminado.")

    def test_fire_selected(self):
        sel = self.tree_devices.selection()
        if not sel:
            messagebox.showinfo("Selecciona un dispositivo", "Elige un dispositivo de la lista primero.")
            return
        device = core.get_device(self.cfg, sel[0])
        if not device:
            return

        self._set_device_status(device["name"], "probando...")

        def on_done(ok, detail):
            ts = time.strftime("%H:%M:%S")
            text = f"{'OK' if ok else 'ERROR'} {ts} - {detail}"
            self.status_queue.put((device["name"], text))

        if device.get("kind") == "multi_state":
            state = self.var_test_state.get()
            if not state:
                messagebox.showinfo("Elige un estado", "Selecciona el estado a probar.")
                return
            self.log(f"Probando '{device['name']}' -> estado {state}...")
            core.set_device_state_async(self.cfg, device, state, log=self.log, on_done=on_done)
        elif device.get("kind") == "scene":
            self.log(f"Probando '{device['name']}' -> activar escena...")
            core.activate_scene_async(self.cfg, device, log=self.log, on_done=on_done)
        elif device.get("kind") == "mqtt":
            self.log(f"Probando '{device['name']}' (MQTT {device['on_topic']})...")
            core.fire_mqtt_device_async(self.cfg, device, 2.0, log=self.log, on_done=on_done)
        else:
            self.log(f"Probando '{device['name']}' ({device['entity_id']})...")
            core.fire_device_async(self.cfg, device, 2.0, log=self.log, on_done=on_done)

    def _test_fire_side(self, side):
        """Dispara solo ON o solo OFF de un dispositivo binary/mqtt, sin
        esperar ni disparar el otro lado despues - util para confirmar que
        el payload de apagado realmente apaga (ej. un estrobo que quedo
        prendido tras un 'Probar' con burst)."""
        sel = self.tree_devices.selection()
        if not sel:
            messagebox.showinfo("Selecciona un dispositivo", "Elige un dispositivo de la lista primero.")
            return
        device = core.get_device(self.cfg, sel[0])
        if not device:
            return
        kind = device.get("kind", "binary")
        if kind not in ("binary", "mqtt"):
            messagebox.showinfo("No aplica",
                                 "'Probar ON/OFF' solo aplica a dispositivos binary o mqtt.\n"
                                 "Para multi_state usa el combo de estados; para scene usa 'Probar'.")
            return

        self._set_device_status(device["name"], f"probando {side}...")

        def on_done(ok, detail):
            ts = time.strftime("%H:%M:%S")
            text = f"{'OK' if ok else 'ERROR'} {ts} - {detail}"
            self.status_queue.put((device["name"], text))

        self.log(f"Probando '{device['name']}' -> {side}...")
        if kind == "mqtt":
            core.fire_mqtt_side_async(self.cfg, device, side, log=self.log, on_done=on_done)
        else:
            core.fire_binary_side_async(self.cfg, device, side, log=self.log, on_done=on_done)

    def _set_device_status(self, name, text):
        self.device_status[name] = text
        if self.tree_devices.exists(name):
            vals = list(self.tree_devices.item(name, "values"))
            vals[5] = text
            self.tree_devices.item(name, values=vals)
        self.lbl_test_result.config(text=f"{name}: {text}")

    def save_settings(self):
        for key, var in self.vars.items():
            val = var.get().strip()
            if key in ("zidoo_port", "mqtt_port"):
                val = int(val) if val else core.DEFAULT_CFG[key]
            elif key in ("lead_time_s", "default_duration_s", "poll_interval_s", "max_late_s"):
                val = float(val) if val else core.DEFAULT_CFG[key]
            self.cfg[key] = val
        core.save_config(self.cfg)
        self.log("Configuracion guardada.")
        messagebox.showinfo("4DFX", "Configuracion guardada.")

    def test_connections(self):
        title, pos, playing, error = core.get_playback(self.cfg)
        ptype = self.cfg.get("player_type", "zidoo").upper()
        if error:
            self.lbl_zidoo.config(text=f"{ptype}: ERROR ({error})", foreground="red")
        else:
            self.lbl_zidoo.config(text=f"{ptype}: OK", foreground="green")
            if title:
                self.lbl_title.config(text=f"Titulo: {title}")
                self.lbl_pos.config(text=f"Posicion: {core.fmt_time(pos or 0)}  (reproduciendo: {playing})")

        ok, detail = core.ha_ping(self.cfg)
        self.lbl_ha.config(text=f"Home Assistant: {'OK' if ok else 'ERROR'}",
                            foreground="green" if ok else "red")
        if not ok:
            self.log(f"[HA] {detail}")
        else:
            self.log("[HA] Conexion OK.")

    def start_sync(self, auto=False):
        """Arranca el motor de sincronizacion. Con auto=True (llamado desde
        la vista previa de video o la deteccion automatica de cue sheet en
        Estado/Control) las validaciones que fallan solo se registran en el
        log, sin interrumpir con un dialogo modal. Devuelve True si arranco."""
        def _fail(msg):
            if auto:
                self.log(f"[auto-sync] no arranco: {msg}")
            else:
                messagebox.showwarning("Configuracion incompleta", msg)
            return False

        if self.engine and self.engine.running:
            return True
        if not self.cfg.get("devices"):
            return _fail("Agrega al menos un dispositivo en la pestana 'Dispositivos'.")
        ptype = self.cfg.get("player_type", "zidoo")
        if ptype == "zidoo" and not self.cfg.get("zidoo_ip"):
            return _fail("Completa la IP del Zidoo en 'Configuracion'.")
        if ptype == "vlc" and not self.cfg.get("vlc_url"):
            return _fail("Completa la URL de VLC en 'Configuracion'.")
        if ptype == "jriver" and not self.cfg.get("jriver_url"):
            return _fail("Completa la URL de JRiver en 'Configuracion'.")
        needs_ha = any(d.get("kind") != "mqtt" for d in self.cfg.get("devices", []))
        if needs_ha and not self.cfg.get("ha_url"):
            return _fail("Completa Home Assistant en 'Configuracion' (o usa solo dispositivos MQTT).")

        def on_state(title=None, pos=None, playing=None, sheet=None):
            self.state_queue.put((title, pos, playing, sheet, None))

        self.engine = core.SyncEngine(self.cfg, log=self.log, on_state=on_state)
        self.engine.start()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        if hasattr(self, "btn_cues_start_sync"):
            self.btn_cues_start_sync.config(state="disabled")
            self.btn_cues_stop_sync.config(state="normal")
        self.log("Sincronizacion iniciada" + (" (automatica)" if auto else "") + ".")
        return True

    def stop_sync(self):
        if self.engine:
            self.engine.stop()
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        if hasattr(self, "btn_cues_start_sync"):
            self.btn_cues_start_sync.config(state="normal")
            self.btn_cues_stop_sync.config(state="disabled")

    def on_close(self):
        self._monitor_stop.set()
        if self.engine:
            self.engine.stop()
        self.destroy()


def main():
    try:
        app = DFXGui()
    except tk.TclError as e:
        print("No se pudo iniciar la interfaz grafica (tkinter no disponible).")
        print(f"Detalle: {e}")
        print("En Raspberry Pi / Linux instala con:  sudo apt install python3-tk")
        sys.exit(1)
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
