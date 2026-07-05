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
        self.geometry("880x600")
        self.minsize(760, 520)

        self.cfg = core.load_config() or dict(core.DEFAULT_CFG)
        self.engine = None
        self.log_queue = queue.Queue()
        self.state_queue = queue.Queue()
        self.status_queue = queue.Queue()
        self.device_status = {}
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

        cols = ("name", "kind", "entity_id", "detalle", "estado")
        self.tree_devices = ttk.Treeview(left, columns=cols, show="headings", height=10)
        headers = {"name": "Nombre", "kind": "Tipo", "entity_id": "entity_id",
                   "detalle": "Detalle", "estado": "Ultima prueba"}
        for c, w in zip(cols, (90, 90, 170, 200, 170)):
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

        ttk.Label(right, text="Nombre (ej: humo, agua, luces, ventilador)").pack(anchor="w")
        ttk.Entry(right, textvariable=self.var_dev_name, width=30).pack(fill="x", pady=(0, 8))

        ttk.Label(right, text="entity_id de Home Assistant").pack(anchor="w")
        entry_entity = ttk.Entry(right, textvariable=self.var_dev_entity, width=30)
        entry_entity.pack(fill="x", pady=(0, 8))
        entry_entity.bind("<FocusOut>", self._suggest_dev_kind)

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

        st_cols = ("state", "service", "data")
        self.tree_states = ttk.Treeview(self.frame_states, columns=st_cols, show="headings", height=5)
        for c, w in zip(st_cols, (70, 130, 110)):
            self.tree_states.heading(c, text=c)
            self.tree_states.column(c, width=w)
        self.tree_states.pack(fill="x")
        self.tree_states.bind("<Double-1>", self._on_state_double_click)

        st_form = ttk.Frame(self.frame_states)
        st_form.pack(fill="x", pady=(6, 0))
        self.var_state_name = tk.StringVar()
        self.var_state_service = tk.StringVar()
        self.var_state_data = tk.StringVar(value="{}")
        ttk.Entry(st_form, textvariable=self.var_state_name, width=8).grid(row=0, column=0, padx=1)
        ttk.Entry(st_form, textvariable=self.var_state_service, width=16).grid(row=0, column=1, padx=1)
        ttk.Entry(st_form, textvariable=self.var_state_data, width=14).grid(row=0, column=2, padx=1)
        ttk.Label(st_form, text="nombre / domain.service / {json datos extra}",
                  foreground="#666", font=("", 8)).grid(row=1, column=0, columnspan=3, sticky="w")
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
        else:
            self.frame_states.pack_forget()

    def _add_state_row(self):
        name = self.var_state_name.get().strip().upper()
        service = self.var_state_service.get().strip()
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
        self.tree_states.insert("", "end", iid=name, values=(name, service, json.dumps(data)))
        self.var_state_name.set("")
        self.var_state_service.set("")
        self.var_state_data.set("{}")

    def _remove_state_row(self):
        sel = self.tree_states.selection()
        if sel:
            self.tree_states.delete(sel[0])

    def _on_state_double_click(self, _event):
        sel = self.tree_states.selection()
        if not sel:
            return
        name, service, data = self.tree_states.item(sel[0], "values")
        self.var_state_name.set(name)
        self.var_state_service.set(service)
        self.var_state_data.set(data)

    def _load_fan_template(self):
        self.tree_states.delete(*self.tree_states.get_children())
        for name, s in core.DEFAULT_FAN_STATES.items():
            self.tree_states.insert("", "end", iid=name,
                                     values=(name, s["service"], json.dumps(s["data"])))

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
        ttk.Label(meta, text="Lead time (s)").grid(row=0, column=2, sticky="w")
        ttk.Entry(meta, textvariable=self.var_sheet_lead, width=6).grid(row=0, column=3, sticky="w", padx=4)
        ttk.Label(meta, text="Archivo").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(meta, textvariable=self.var_sheet_filename, width=28).grid(row=1, column=1, sticky="w", padx=4, pady=(4, 0))
        ttk.Button(meta, text="Guardar cue sheet", command=self._save_sheet).grid(row=1, column=3, sticky="w", pady=(4, 0))

        cols = ("t", "device", "modo", "valor")
        self.tree_cues = ttk.Treeview(right, columns=cols, show="headings", height=16)
        headers = {"t": "Timestamp", "device": "Dispositivo", "modo": "Modo", "valor": "Duracion / Estado"}
        for c, w in zip(cols, (110, 110, 90, 130)):
            self.tree_cues.heading(c, text=headers[c])
            self.tree_cues.column(c, width=w)
        self.tree_cues.pack(fill="both", expand=True)
        self.tree_cues.bind("<Double-1>", self._on_cue_double_click)

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
        if not sel:
            return
        t, device, modo, valor = self.tree_cues.item(sel[0], "values")
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
                detalle = ", ".join(sorted(d.get("states", {}).keys()))
            elif d.get("kind") == "scene":
                detalle = f"activar: {d.get('activate_service', 'scene/turn_on')}"
            else:
                detalle = f"{d.get('on_service', '')} / {d.get('off_service', '')}"
            estado = self.device_status.get(d["name"], "") if hasattr(self, "device_status") else ""
            self.tree_devices.insert("", "end", iid=d["name"],
                                      values=(d["name"], d.get("kind", "binary"), d["entity_id"], detalle, estado))
        self._refresh_test_state_options()
        if hasattr(self, "combo_cue_device"):
            self._refresh_cue_device_options()

    def _refresh_test_state_options(self, device=None):
        if device and device.get("kind") == "multi_state":
            states = sorted(device.get("states", {}).keys())
            self.combo_test_state["values"] = states
            self.var_test_state.set(states[0] if states else "")
        else:
            self.combo_test_state["values"] = []
            self.var_test_state.set("")

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
        self.tree_states.delete(*self.tree_states.get_children())
        if device.get("kind") == "multi_state":
            for name, s in device.get("states", {}).items():
                self.tree_states.insert("", "end", iid=name,
                                         values=(name, s["service"], json.dumps(s.get("data", {}))))
        self._on_dev_kind_change()
        self._refresh_test_state_options(device)

    def add_device(self):
        name = self.var_dev_name.get().strip()
        entity = self.var_dev_entity.get().strip()
        kind = self.var_dev_kind.get()
        if not name or not entity:
            messagebox.showwarning("Falta informacion", "Nombre y entity_id son requeridos.")
            return

        if kind == "multi_state":
            states = {}
            for iid in self.tree_states.get_children():
                st_name, service, data = self.tree_states.item(iid, "values")
                states[st_name] = {"service": service, "data": json.loads(data or "{}")}
            if not states:
                messagebox.showwarning("Sin estados", "Agrega al menos un estado (o usa 'Plantilla ventilador').")
                return
            device = {"name": name, "kind": "multi_state", "entity_id": entity, "states": states}
        elif kind == "scene":
            device = {"name": name, "kind": "scene", "entity_id": entity, "activate_service": "scene/turn_on"}
        else:
            on_srv, off_srv = core.services_for_entity(entity)
            device = {"name": name, "kind": "binary", "entity_id": entity,
                      "on_service": on_srv, "off_service": off_srv}

        original_name = getattr(self, "_editing_original_name", None)
        devices = [d for d in self.cfg.get("devices", [])
                   if d["name"].lower() not in (name.lower(), (original_name or "").lower())]
        devices.append(device)
        self.cfg["devices"] = devices
        core.save_config(self.cfg)
        self._editing_original_name = None
        self._refresh_devices_list()
        self.var_dev_name.set("")
        self.var_dev_entity.set("")
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
            vals[4] = text
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
