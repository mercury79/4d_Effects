#!/usr/bin/env python3
"""
SmokeSync GUI - Interfaz grafica standalone (macOS / Windows / Raspberry Pi 4)

Monitorea la reproduccion en un Zidoo X20 Pro y dispara dispositivos
(maquina de humo, agua, luces, estrobos, ...) via Home Assistant en los
timestamps definidos en los cue sheets.

Requisitos: Python 3 con tkinter (incluido de fabrica en la mayoria de
instalaciones) + `pip install requests`.

Ejecutar:  python3 smokesync_gui.py
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

import smokesync_core as core


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


class SmokeSyncGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SmokeSync - 4DX Control")
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
        self._monitor_stop = threading.Event()

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

        ttk.Label(right, text="Tipo de dispositivo").pack(anchor="w")
        kind_frame = ttk.Frame(right)
        kind_frame.pack(fill="x", pady=(0, 4))
        ttk.Radiobutton(kind_frame, text="Binario (on/off)", variable=self.var_dev_kind,
                         value="binary", command=self._on_dev_kind_change).pack(anchor="w")
        ttk.Radiobutton(kind_frame, text="Multi-estado (velocidades, efectos)", variable=self.var_dev_kind,
                         value="multi_state", command=self._on_dev_kind_change).pack(anchor="w")
        ttk.Radiobutton(kind_frame, text="Escena (solo activar, sin apagado)", variable=self.var_dev_kind,
                         value="scene", command=self._on_dev_kind_change).pack(anchor="w")
        ttk.Label(right, text="Binario: se detecta on/off segun el dominio\n"
                              "(switch, light, cover, fan, input_boolean, valve).\n"
                              "Escena (scene.xxx): HA solo permite activarla,\n"
                              "no tiene 'apagado' - se detecta automatico.",
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
        if self.var_dev_kind.get() == "multi_state":
            self.frame_states.pack(fill="x", pady=(0, 8))
            self.frame_dev_shortcut.pack_forget()
        else:
            self.frame_states.pack_forget()
            self.frame_dev_shortcut.pack(fill="x", pady=(0, 8))

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

        left = ttk.Frame(f, padding=10)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Cue sheets (.json)", font=("", 10, "bold")).pack(anchor="w")
        self.list_sheets = tk.Listbox(left, width=32, height=20, exportselection=False)
        self.list_sheets.pack(fill="y", expand=True)
        self.list_sheets.bind("<<ListboxSelect>>", self._on_sheet_select)
        sheet_btns = ttk.Frame(left)
        sheet_btns.pack(fill="x", pady=6)
        ttk.Button(sheet_btns, text="Nuevo", command=self._new_sheet).pack(side="left")
        ttk.Button(sheet_btns, text="Eliminar", command=self._delete_sheet).pack(side="left", padx=4)
        ttk.Button(left, text="Importar .txt (AVS Forum)...", command=self._import_state_txt).pack(fill="x", pady=(10, 2))

        right = ttk.Frame(f, padding=10)
        right.pack(side="left", fill="both", expand=True)

        meta = ttk.Frame(right)
        meta.pack(fill="x", pady=(0, 8))
        self.var_sheet_match = tk.StringVar()
        self.var_sheet_lead = tk.StringVar()
        self.var_sheet_filename = tk.StringVar()
        ttk.Label(meta, text="Match (substring del titulo)").grid(row=0, column=0, sticky="w")
        ttk.Entry(meta, textvariable=self.var_sheet_match, width=28).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Button(meta, text="Usar titulo actual (Zidoo)", command=self._use_current_title).grid(row=0, column=2, sticky="w", padx=4)
        ttk.Label(meta, text="Lead time (s)").grid(row=0, column=3, sticky="w")
        ttk.Entry(meta, textvariable=self.var_sheet_lead, width=6).grid(row=0, column=4, sticky="w", padx=4)
        ttk.Label(meta, text="Archivo").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(meta, textvariable=self.var_sheet_filename, width=28).grid(row=1, column=1, sticky="w", padx=4, pady=(4, 0))
        ttk.Button(meta, text="Guardar cue sheet", command=self._save_sheet).grid(row=1, column=4, sticky="w", pady=(4, 0))

        # -- Captura en vivo: mientras el Zidoo reproduce, presiona el atajo
        # de un dispositivo (o el boton de un Stream Deck configurado para
        # enviar esa tecla) y se inserta un cue en la posicion actual.
        capture = ttk.LabelFrame(right, text="Captura en vivo (atajos de teclado / Stream Deck)", padding=6)
        capture.pack(fill="x", pady=(0, 8))
        self.var_capture_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(capture, text="Activar captura", variable=self.var_capture_on,
                        command=self._toggle_capture).pack(side="left")
        self.lbl_capture_pos = ttk.Label(capture, text="Posicion: -")
        self.lbl_capture_pos.pack(side="left", padx=10)

        cols = ("t", "device", "modo", "valor")
        self.tree_cues = ttk.Treeview(right, columns=cols, show="headings", height=10)
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
        ttk.Entry(form, textvariable=self.var_cue_t, width=12).grid(row=0, column=1, sticky="w", padx=4)

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
        self.entry_cue_duration = ttk.Entry(form, textvariable=self.var_cue_duration, width=10)
        self.entry_cue_duration.grid(row=2, column=1, sticky="w", padx=4, pady=(6, 0))
        self.combo_cue_state = ttk.Combobox(form, textvariable=self.var_cue_value, width=10, state="readonly")

        btns2 = ttk.Frame(form)
        btns2.grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Button(btns2, text="Agregar / actualizar cue", command=self._add_cue).pack(side="left")
        ttk.Button(btns2, text="Eliminar cue seleccionado", command=self._delete_cue).pack(side="left", padx=4)

        self._refresh_cue_device_options()
        self._new_sheet()
        self._refresh_sheet_list()

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

    def _on_cue_mode_change(self):
        mode = self.var_cue_mode.get()
        self.entry_cue_duration.grid_remove()
        self.combo_cue_state.grid_remove()
        if mode == "state":
            self.combo_cue_state.grid(row=2, column=1, sticky="w", padx=4, pady=(6, 0))
        elif mode == "burst":
            self.entry_cue_duration.grid(row=2, column=1, sticky="w", padx=4, pady=(6, 0))
        # 'scene': ni duracion ni estado, solo dispara al llegar el timestamp

    def _refresh_sheet_list(self):
        self.list_sheets.delete(0, "end")
        d = Path(self.cfg.get("cues_dir", core.DEFAULT_CFG["cues_dir"]))
        d.mkdir(parents=True, exist_ok=True)
        for fpath in sorted(d.glob("*.json")):
            self.list_sheets.insert("end", fpath.name)

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
        self.var_sheet_match.set(data.get("match", ""))
        self.var_sheet_lead.set(str(data.get("lead_time_s", self.cfg.get("lead_time_s", 4))))
        self.var_sheet_filename.set(fname)
        self._load_cues_into_tree(data.get("cues", []))

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

    def _new_sheet(self):
        self.current_sheet_path = None
        self.var_sheet_match.set("")
        self.var_sheet_lead.set(str(self.cfg.get("lead_time_s", 4)))
        self.var_sheet_filename.set("nuevo_cue_sheet.json")
        self.tree_cues.delete(*self.tree_cues.get_children())
        self._refresh_timeline()

    def _delete_sheet(self):
        sel = self.list_sheets.curselection()
        if not sel:
            return
        fname = self.list_sheets.get(sel[0])
        if not messagebox.askyesno("Confirmar", f"Eliminar '{fname}'?"):
            return
        (Path(self.cfg["cues_dir"]) / fname).unlink(missing_ok=True)
        self._refresh_sheet_list()
        self._new_sheet()

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

    def _on_cue_double_click(self, _event):
        sel = self.tree_cues.selection()
        if sel:
            self._load_cue_into_form(sel[0])

    def _load_cue_into_form(self, iid):
        t, device, modo, valor = self.tree_cues.item(iid, "values")
        self.var_cue_t.set(t)
        self.var_cue_device.set(device)
        self._on_cue_device_change()
        self.var_cue_mode.set(modo)
        self._on_cue_mode_change()
        if modo == "state":
            self.var_cue_value.set(valor)
        elif modo == "burst":
            self.var_cue_duration.set(str(valor).rstrip("s"))

    def _delete_cue(self):
        sel = self.tree_cues.selection()
        if sel:
            self.tree_cues.delete(sel[0])
            self._refresh_timeline()

    def _on_delete_key(self, event):
        # No interferir con Backspace/Supr mientras se edita texto en un
        # campo (Entry/Combobox/Text); solo actuar si el foco esta en la
        # tabla o el canvas de la timeline.
        if isinstance(event.widget, (tk.Entry, ttk.Entry, tk.Text, ttk.Combobox)):
            return
        self._delete_cue()

    def _use_current_title(self):
        j = core.zidoo_status(self.cfg)
        if j.get("_error"):
            messagebox.showwarning("Sin conexion", f"No pude leer el Zidoo: {j['_error']}")
            return
        title, _pos, _playing = core.parse_playback(j)
        if not title:
            messagebox.showinfo("Sin titulo", "El Zidoo no reporta un titulo en este momento.")
            return
        self.var_sheet_match.set(title)
        if not self.var_sheet_filename.get() or self.var_sheet_filename.get() == "nuevo_cue_sheet.json":
            slug = "".join(c if c.isalnum() else "_" for c in title.lower()).strip("_")
            self.var_sheet_filename.set(f"{slug}.json")
        self.log(f"Titulo actual del Zidoo: '{title}'")

    # ---- Captura en vivo (atajos de teclado / Stream Deck) ----------------
    def _toggle_capture(self):
        self.capture_on = self.var_capture_on.get()
        if self.capture_on:
            self._capture_bind_id = self.bind_all("<KeyPress>", self._on_capture_key)
            self.log("Captura en vivo activada.")
        else:
            if getattr(self, "_capture_bind_id", None):
                self.unbind_all("<KeyPress>")
            self.log("Captura en vivo desactivada.")

    def _update_capture_pos_label(self):
        if self.last_pos is None:
            self.lbl_capture_pos.config(text="Posicion: -")
        else:
            self.lbl_capture_pos.config(text=f"Posicion: {core.fmt_time_ms(self.last_pos)}")

    def _on_capture_key(self, event):
        if not self.capture_on:
            return
        # No capturar mientras el usuario esta escribiendo en un campo de texto.
        if isinstance(event.widget, (tk.Entry, ttk.Entry, tk.Text, ttk.Combobox)):
            return
        key = (event.char or event.keysym).strip().upper()
        match = self.shortcut_map.get(key)
        if not match:
            return
        if self.last_pos is None:
            self.log("[captura] sin posicion del Zidoo todavia (¿esta reproduciendo?).")
            return
        device_name, mode, extra = match
        t = core.fmt_time_ms(self.last_pos)
        cues = self._tree_cues_to_list()
        if mode == "state":
            cues.append({"t": t, "device": device_name, "state": extra})
            desc = f"estado {extra}"
        elif mode == "scene":
            cues.append({"t": t, "device": device_name})
            desc = "activar escena"
        else:
            dur = self.cfg.get("default_duration_s", 3)
            cues.append({"t": t, "device": device_name, "duration_s": dur})
            desc = f"rafaga {dur}s"
        self._load_cues_into_tree(cues)
        self.log(f"[captura] {t} -> {device_name} ({desc})")

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

        selected_iid = self.tree_cues.selection()[0] if self.tree_cues.selection() else None

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
            outline = "#4fd1ff" if iid == selected_iid else "#000000"
            outline_w = 2 if iid == selected_iid else 1
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

        # Marcador de seleccion: donde quedo asignada la accion seleccionada
        if selected_iid and selected_iid in self._cue_geom:
            g = self._cue_geom[selected_iid]
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

    def _on_timeline_press(self, event):
        iid = self._find_timeline_iid(event)
        if not iid:
            self._timeline_drag = None
            return
        self.tree_cues.selection_set(iid)
        self.tree_cues.see(iid)
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
        sel = self.tree_cues.selection()
        if sel:
            self._refresh_timeline()

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
        data = {
            "match": match,
            "lead_time_s": float(self.var_sheet_lead.get() or self.cfg.get("lead_time_s", 4)),
            "cues": self._tree_cues_to_list(),
        }
        d = Path(self.cfg["cues_dir"])
        d.mkdir(parents=True, exist_ok=True)
        (d / fname).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self.current_sheet_path = d / fname
        self._refresh_sheet_list()
        self.log(f"Cue sheet guardada: {fname} ({len(data['cues'])} cues)")
        messagebox.showinfo("SmokeSync", f"Guardado: {fname}")

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
        f = ttk.Frame(self.tab_config, padding=14)
        f.pack(fill="both", expand=True)

        self.vars = {}

        def row(label, key, r, width=28):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=4)
            v = tk.StringVar(value=str(self.cfg.get(key, "")))
            ttk.Entry(f, textvariable=v, width=width).grid(row=r, column=1, sticky="w", pady=4)
            self.vars[key] = v

        ttk.Label(f, text="Zidoo X20 Pro", font=("", 11, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 6))
        row("IP", "zidoo_ip", 1)
        row("Puerto API", "zidoo_port", 2)

        ttk.Label(f, text="Home Assistant", font=("", 11, "bold")).grid(row=3, column=0, sticky="w", pady=(14, 6))
        row("URL (ej http://192.168.1.50:8123)", "ha_url", 4)
        row("Long-Lived Access Token", "ha_token", 5)

        ttk.Label(f, text="Sincronizacion", font=("", 11, "bold")).grid(row=6, column=0, sticky="w", pady=(14, 6))
        row("Lead time por defecto (s)", "lead_time_s", 7)
        row("Duracion por defecto (s)", "default_duration_s", 8)
        row("Intervalo de sondeo (s)", "poll_interval_s", 9)
        row("Ventana max. de retraso (s)", "max_late_s", 10)

        ttk.Label(f, text="Cue sheets", font=("", 11, "bold")).grid(row=11, column=0, sticky="w", pady=(14, 6))
        row("Carpeta de cue sheets", "cues_dir", 12, width=40)
        ttk.Button(f, text="Elegir carpeta...", command=self._pick_cues_dir).grid(row=12, column=2, padx=6)

        ttk.Button(f, text="Guardar configuracion", command=self.save_settings).grid(
            row=13, column=0, columnspan=2, sticky="w", pady=(18, 0))

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
                if error:
                    self.lbl_zidoo.config(text=f"Zidoo: ERROR ({error})", foreground="red")
                    continue
                self.lbl_title.config(text=f"Titulo: {title or '-'}")
                self.lbl_pos.config(text=f"Posicion: {core.fmt_time(pos or 0)}  (reproduciendo: {playing})")
                self.lbl_sheet.config(text=f"Cue sheet: {sheet['_file'] if sheet else '(ninguna)'}")
                self.lbl_zidoo.config(text="Zidoo: OK", foreground="green")
                self.last_title = title
                self.last_pos = pos
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

    def _start_monitor(self):
        """Sondea el Zidoo continuamente para que la posicion avance en
        pantalla aunque la sincronizacion (disparo de cues) no este activa."""
        def _run():
            while not self._monitor_stop.is_set():
                if self.engine and self.engine.running:
                    time.sleep(1)
                    continue
                if not self.cfg.get("zidoo_ip"):
                    time.sleep(1)
                    continue
                j = core.zidoo_status(self.cfg)
                if j.get("_error"):
                    self.state_queue.put((None, None, False, None, j["_error"]))
                else:
                    title, pos, playing = core.parse_playback(j)
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
        self.tree_states.delete(*self.tree_states.get_children())
        if device.get("kind") == "multi_state":
            for name, s in device.get("states", {}).items():
                self.tree_states.insert("", "end", iid=name,
                                         values=(name, s["service"], json.dumps(s.get("data", {})),
                                                 s.get("shortcut", "")))
        self._on_dev_kind_change()
        self._refresh_test_state_options(device)

    def add_device(self):
        name = self.var_dev_name.get().strip()
        entity = self.var_dev_entity.get().strip()
        kind = self.var_dev_kind.get()
        shortcut = self.var_dev_shortcut.get().strip()
        if not name or not entity:
            messagebox.showwarning("Falta informacion", "Nombre y entity_id son requeridos.")
            return

        if kind == "multi_state":
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
        else:
            on_srv, off_srv = core.services_for_entity(entity)
            device = {"name": name, "kind": "binary", "entity_id": entity,
                      "on_service": on_srv, "off_service": off_srv, "shortcut": shortcut}

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
        else:
            self.log(f"Probando '{device['name']}' ({device['entity_id']})...")
            core.fire_device_async(self.cfg, device, 2.0, log=self.log, on_done=on_done)

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
            if key in ("zidoo_port",):
                val = int(val) if val else core.DEFAULT_CFG[key]
            elif key in ("lead_time_s", "default_duration_s", "poll_interval_s", "max_late_s"):
                val = float(val) if val else core.DEFAULT_CFG[key]
            self.cfg[key] = val
        core.save_config(self.cfg)
        self.log("Configuracion guardada.")
        messagebox.showinfo("SmokeSync", "Configuracion guardada.")

    def test_connections(self):
        j = core.zidoo_status(self.cfg)
        if j.get("_error"):
            self.lbl_zidoo.config(text=f"Zidoo: ERROR ({j['_error']})", foreground="red")
        else:
            title, pos, playing = core.parse_playback(j)
            self.lbl_zidoo.config(text="Zidoo: OK", foreground="green")
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

    def start_sync(self):
        if not self.cfg.get("devices"):
            messagebox.showwarning("Sin dispositivos", "Agrega al menos un dispositivo en la pestana 'Dispositivos'.")
            return
        if not self.cfg.get("zidoo_ip") or not self.cfg.get("ha_url"):
            messagebox.showwarning("Configuracion incompleta", "Completa Zidoo y Home Assistant en 'Configuracion'.")
            return

        def on_state(title=None, pos=None, playing=None, sheet=None):
            self.state_queue.put((title, pos, playing, sheet, None))

        self.engine = core.SyncEngine(self.cfg, log=self.log, on_state=on_state)
        self.engine.start()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.log("Sincronizacion iniciada.")

    def stop_sync(self):
        if self.engine:
            self.engine.stop()
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")

    def on_close(self):
        self._monitor_stop.set()
        if self.engine:
            self.engine.stop()
        self.destroy()


def main():
    try:
        app = SmokeSyncGUI()
    except tk.TclError as e:
        print("No se pudo iniciar la interfaz grafica (tkinter no disponible).")
        print(f"Detalle: {e}")
        print("En Raspberry Pi / Linux instala con:  sudo apt install python3-tk")
        sys.exit(1)
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
