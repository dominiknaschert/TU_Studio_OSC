"""k
OSC Spatial Sender
- Sources page: X/Y/Z + Gain control per source
- LFO page:     assign sine LFOs to any parameter of any source
- 3D View page: live matplotlib 3D scatter of all active sources
- Real-time OSC sending at ~30 Hz
"""

import json
import math
import os
import time
import threading
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk
from pythonosc import udp_client
import mido

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# ── appearance ────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

DEFAULT_IP   = "riviera.ak.tu-berlin.de"
DEFAULT_PORT = 4455
SEND_HZ      = 30
MAX_SOURCES  = 16

# distinct colours for up to 16 sources
SOURCE_COLORS = [
    "#e74c3c","#3498db","#2ecc71","#f39c12",
    "#9b59b6","#1abc9c","#e67e22","#e91e63",
    "#00bcd4","#8bc34a","#ff5722","#607d8b",
    "#673ab7","#009688","#ffc107","#795548",
]

def clamp(v: float, lo=-1.0, hi=1.0) -> float:
    return max(lo, min(hi, v))


# ═════════════════════════════════════════════════════════════════════════════
# Data model
# ═════════════════════════════════════════════════════════════════════════════

class SourceData:
    def __init__(self, index: int):
        self.index     = index
        self.x         = 0.0
        self.y         = 0.0
        self.z         = 0.0
        self.gain_ambi = 1.0   # renderer 0 = Ambisonics
        self.gain_wfs  = 0.0   # renderer 1 = WFS
        self.gain_lfe  = 0.0   # renderer 2 = LFE


class LFO:
    def __init__(self):
        self.enabled    = False
        self.rate       = 0.5
        self.depth      = 0.5
        self.phase      = 0.0
        self.target_src = 1
        self.target_par = "x"
        self._start     = time.time()

    def value(self, speed_mult: float = 1.0) -> float:
        t = time.time() - self._start
        return math.sin(2 * math.pi * self.rate * speed_mult * t + self.phase) * self.depth


# ═════════════════════════════════════════════════════════════════════════════
# Engine
# ═════════════════════════════════════════════════════════════════════════════

class Engine:
    def __init__(self):
        self.sources:        list[SourceData] = [SourceData(i) for i in range(1, MAX_SOURCES + 1)]
        self.lfos:           list[LFO]        = [LFO() for _ in range(16)]
        self.client:         udp_client.SimpleUDPClient | None = None
        self.num_active      = 4
        self.lfo_speed_mult  = 1.0   # global LFO speed multiplier
        self.running         = False
        # last rendered positions (with LFO applied) for the 3-D view
        self.rendered: list[tuple[float, float, float]] = [(0, 0, 0)] * MAX_SOURCES

    def connect(self, ip: str, port: int):
        self.client = udp_client.SimpleUDPClient(ip, port)

    def start(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.running = False

    def _loop(self):
        interval = 1.0 / SEND_HZ
        while self.running:
            t0 = time.time()
            self._tick()
            sleep_t = interval - (time.time() - t0)
            if sleep_t > 0:
                time.sleep(sleep_t)

    def _tick(self):
        offsets: dict[tuple[int, str], float] = {}
        for lfo in self.lfos:
            if not lfo.enabled:
                continue
            key = (lfo.target_src, lfo.target_par)
            offsets[key] = offsets.get(key, 0.0) + lfo.value(self.lfo_speed_mult)

        rendered = list(self.rendered)
        for src in self.sources[:self.num_active]:
            si         = src.index
            x          = clamp(src.x + offsets.get((si, "x"), 0.0))
            y          = clamp(src.y + offsets.get((si, "y"), 0.0))
            z          = clamp(src.z + offsets.get((si, "z"), 0.0))
            gain_ambi  = clamp(src.gain_ambi + offsets.get((si, "gain_ambi"), 0.0), 0.0, 1.0)
            gain_wfs   = clamp(src.gain_wfs  + offsets.get((si, "gain_wfs"),  0.0), 0.0, 1.0)
            gain_lfe   = clamp(src.gain_lfe  + offsets.get((si, "gain_lfe"),  0.0), 0.0, 1.0)
            rendered[si - 1] = (x, y, z)
            if self.client:
                try:
                    self.client.send_message("/source/pos/xyz", [si, x, y, z])
                    self.client.send_message("/send/gain", [si, 0, gain_ambi])
                    self.client.send_message("/send/gain", [si, 1, gain_wfs])
                    self.client.send_message("/send/gain", [si, 2, gain_lfe])
                except Exception:
                    pass
        self.rendered = rendered


# ═════════════════════════════════════════════════════════════════════════════
# Sources page
# ═════════════════════════════════════════════════════════════════════════════

class SourceStrip(ctk.CTkFrame):
    def __init__(self, parent, src: SourceData, **kw):
        super().__init__(parent, corner_radius=8, **kw)
        self.src = src
        self._build()

    def _build(self):
        color = SOURCE_COLORS[(self.src.index - 1) % len(SOURCE_COLORS)]
        ctk.CTkLabel(self, text=f"Source {self.src.index}",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=color).pack(pady=(8, 4))

        for attr, label in [("x", "X"), ("y", "Y"), ("z", "Z")]:
            self._slider_row(attr, label, -1.0, 1.0)

        ctk.CTkLabel(self, text="── Send ──", font=ctk.CTkFont(size=10),
                     text_color="#555").pack(pady=(6, 0))
        self._slider_row("gain_ambi", "Ambi Gain", 0.0, 1.0)
        self._slider_row("gain_wfs",  "WFS Gain",  0.0, 1.0)
        self._slider_row("gain_lfe",  "LFE Gain",  0.0, 1.0)

    def _slider_row(self, attr, label, lo, hi):
        var = ctk.DoubleVar(value=getattr(self.src, attr))

        def on_change(v):
            setattr(self.src, attr, float(v))
            lbl.configure(text=f"{float(v):+.2f}" if lo < 0 else f"{float(v):.2f}")

        ctk.CTkLabel(self, text=label, font=ctk.CTkFont(size=11)).pack()
        ctk.CTkSlider(self, from_=lo, to=hi, variable=var,
                      command=on_change, width=130).pack(padx=8)
        lbl = ctk.CTkLabel(self, text=f"{getattr(self.src, attr):+.2f}",
                            font=ctk.CTkFont(size=10))
        lbl.pack()



class SourcesPage(ctk.CTkScrollableFrame):
    def __init__(self, parent, engine: Engine, **kw):
        super().__init__(parent, orientation="horizontal", **kw)
        self.engine  = engine
        self._strips: list[SourceStrip] = []
        self._rebuild(engine.num_active)

    def set_num_sources(self, n: int):
        n = max(1, min(MAX_SOURCES, n))
        self.engine.num_active = n
        self._rebuild(n)

    def _rebuild(self, n: int):
        for s in self._strips:
            s.destroy()
        self._strips.clear()
        for i in range(n):
            s = SourceStrip(self, self.engine.sources[i])
            s.pack(side=tk.LEFT, padx=6, pady=6, fill=tk.Y)
            self._strips.append(s)


# ═════════════════════════════════════════════════════════════════════════════
# LFO page
# ═════════════════════════════════════════════════════════════════════════════

class LFOStrip(ctk.CTkFrame):
    def __init__(self, parent, idx: int, lfo: LFO, **kw):
        super().__init__(parent, corner_radius=8, **kw)
        self.lfo = lfo
        self.idx = idx
        self._build()

    def _build(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill=tk.X, padx=10, pady=(8, 4))
        ctk.CTkLabel(header, text=f"LFO {self.idx + 1}",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side=tk.LEFT)
        self.en_var = ctk.BooleanVar(value=self.lfo.enabled)
        ctk.CTkSwitch(header, text="On", variable=self.en_var,
                      command=lambda: setattr(self.lfo, "enabled", self.en_var.get())
                      ).pack(side=tk.RIGHT)

        self._label("Target Source")
        ctk.CTkOptionMenu(
            self,
            values=[str(i) for i in range(1, MAX_SOURCES + 1)],
            variable=ctk.StringVar(value=str(self.lfo.target_src)),
            command=lambda v: setattr(self.lfo, "target_src", int(v)),
            width=100,
        ).pack(padx=10, pady=2)

        self._label("Parameter")
        pm = ctk.CTkOptionMenu(self, values=["x", "y", "z", "gain_ambi", "gain_wfs", "gain_lfe"],
                               command=lambda v: setattr(self.lfo, "target_par", v),
                               width=120)
        pm.set(self.lfo.target_par)
        pm.pack(padx=10, pady=2)

        self._slider_block("Rate (Hz)", "rate",  0.01, 5.0,           "{:.2f} Hz")
        self._slider_block("Depth",     "depth", 0.0,  1.0,           "{:.2f}")
        self._slider_block("Phase",     "phase", 0.0,  2 * math.pi,   "{:.2f} rad")

        self._label("Live")
        self.indicator = tk.Canvas(self, width=100, height=12,
                                   bg="#1c1c1c", highlightthickness=0)
        self.indicator.pack(padx=10, pady=(0, 10))
        self._animate()

    def _label(self, text):
        ctk.CTkLabel(self, text=text, font=ctk.CTkFont(size=11)).pack(anchor="w", padx=10)

    def _slider_block(self, label, attr, lo, hi, fmt):
        self._label(label)
        var = ctk.DoubleVar(value=getattr(self.lfo, attr))
        lbl = ctk.CTkLabel(self, text=fmt.format(getattr(self.lfo, attr)),
                            font=ctk.CTkFont(size=10))

        def on_change(v):
            setattr(self.lfo, attr, float(v))
            lbl.configure(text=fmt.format(float(v)))

        ctk.CTkSlider(self, from_=lo, to=hi, variable=var,
                      command=on_change, width=130).pack(padx=10)
        lbl.pack()

    def _animate(self):
        v     = self.lfo.value()
        depth = max(self.lfo.depth, 0.001)
        norm  = (v / depth + 1) / 2
        x     = int(norm * 96) + 2
        self.indicator.delete("all")
        color = "#4a9eff" if self.lfo.enabled else "#444"
        self.indicator.create_oval(x - 4, 2, x + 4, 10, fill=color, outline="")
        self.after(50, self._animate)


class LFOPage(ctk.CTkScrollableFrame):
    def __init__(self, parent, engine: Engine, **kw):
        super().__init__(parent, orientation="horizontal", **kw)
        for i, lfo in enumerate(engine.lfos):
            LFOStrip(self, i, lfo).pack(side=tk.LEFT, padx=6, pady=6, fill=tk.Y)


# ═════════════════════════════════════════════════════════════════════════════
# 2-D XY View page
# ═════════════════════════════════════════════════════════════════════════════

class View2DPage(ctk.CTkFrame):
    REFRESH_MS = 80   # ~12 fps

    def __init__(self, parent, engine: Engine, **kw):
        super().__init__(parent, fg_color="transparent", **kw)
        self.engine = engine
        self._build()
        self._update()

    def _build(self):
        plt.style.use("dark_background")
        self.fig, self.ax = plt.subplots(figsize=(5, 5), facecolor="#1c1c1c")
        self.ax.set_facecolor("#1c1c1c")
        self.ax.set_xlim(-1.1, 1.1)
        self.ax.set_ylim(-1.1, 1.1)
        self.ax.set_xlabel("X", color="#aaa")
        self.ax.set_ylabel("Y", color="#aaa")
        self.ax.tick_params(colors="#666", labelsize=8)
        self.ax.axhline(0, color="#333", linewidth=0.8)
        self.ax.axvline(0, color="#333", linewidth=0.8)
        self.ax.set_aspect("equal")
        self.ax.grid(True, color="#2a2a2a", linewidth=0.5)

        # unit circle for reference
        theta = [i * 2 * math.pi / 120 for i in range(121)]
        self.ax.plot([math.cos(t) for t in theta],
                     [math.sin(t) for t in theta],
                     color="#333", linewidth=0.8, linestyle="--")

        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # pre-create scatter + labels (all hidden)
        self._scatter = self.ax.scatter(
            [0.0] * MAX_SOURCES, [0.0] * MAX_SOURCES,
            c=SOURCE_COLORS, s=[80] * MAX_SOURCES,
            edgecolors="white", linewidths=0.5, zorder=5, alpha=0.0,
        )
        self._texts = [
            self.ax.text(0, 0, f" S{i+1}",
                         color=SOURCE_COLORS[i % len(SOURCE_COLORS)],
                         fontsize=8, visible=False, zorder=6)
            for i in range(MAX_SOURCES)
        ]

    def _update(self):
        try:
            if not self.canvas.get_tk_widget().winfo_ismapped():
                self.after(self.REFRESH_MS, self._update)
                return
        except Exception:
            pass

        n        = self.engine.num_active
        rendered = self.engine.rendered

        xs = [rendered[i][0] if i < n else 0.0 for i in range(MAX_SOURCES)]
        ys = [rendered[i][1] if i < n else 0.0 for i in range(MAX_SOURCES)]

        self._scatter.set_offsets(list(zip(xs, ys)))
        self._scatter.set_alpha(1.0)

        sizes = [max(40, self.engine.sources[i].gain_ambi * 200) if i < n else 1
                 for i in range(MAX_SOURCES)]
        self._scatter.set_sizes(sizes)

        for i, txt in enumerate(self._texts):
            if i < n:
                txt.set_position((xs[i] + 0.03, ys[i] + 0.03))
                txt.set_visible(True)
            else:
                txt.set_visible(False)

        self.canvas.draw_idle()
        self.after(self.REFRESH_MS, self._update)


# ═════════════════════════════════════════════════════════════════════════════
# MIDI
# ═════════════════════════════════════════════════════════════════════════════

class MidiMapping:
    """Maps a (channel, cc) to a target parameter with a value range."""
    def __init__(self, channel: int, cc: int, target: str,
                 lo: float = 0.0, hi: float = 1.0):
        self.channel = channel
        self.cc      = cc
        self.target  = target   # e.g. "lfo_speed", "src1_x", "src1_gain_ambi", …
        self.lo      = lo
        self.hi      = hi

    def scaled(self, midi_val: int) -> float:
        return self.lo + (midi_val / 127.0) * (self.hi - self.lo)

    def to_dict(self) -> dict:
        return {"channel": self.channel, "cc": self.cc, "target": self.target,
                "lo": self.lo, "hi": self.hi}

    @staticmethod
    def from_dict(d: dict) -> "MidiMapping":
        return MidiMapping(d["channel"], d["cc"], d["target"], d["lo"], d["hi"])


class MidiEngine:
    """Listens on a MIDI port and applies CC mappings to Engine parameters."""

    def __init__(self, engine: Engine):
        self.engine   = engine
        self.mappings: list[MidiMapping] = []
        self._port    = None
        self._thread: threading.Thread | None = None
        self._running = False
        self.learning: str | None = None      # target being learned
        self.last_cc:  tuple[int,int] | None = None   # (channel, cc) of last CC seen
        self._on_learn_cb = None              # called when learn completes

    # ── port management ───────────────────────────────────────────────────────

    def connect(self, port_name: str):
        self.disconnect()
        self._port    = mido.open_input(port_name)
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def disconnect(self):
        self._running = False
        if self._port:
            try:
                self._port.close()
            except Exception:
                pass
            self._port = None

    def _loop(self):
        while self._running and self._port:
            for msg in self._port.iter_pending():
                if msg.type == "control_change":
                    self.last_cc = (msg.channel, msg.control)
                    if self.learning:
                        # auto-add mapping with sensible defaults
                        target = self.learning
                        lo, hi = self._default_range(target)
                        self.mappings.append(
                            MidiMapping(msg.channel, msg.control, target, lo, hi))
                        self.learning = None
                        if self._on_learn_cb:
                            self._on_learn_cb(msg.channel, msg.control)
                    else:
                        self._apply(msg.channel, msg.control, msg.value)
            time.sleep(0.005)

    def _default_range(self, target: str) -> tuple[float, float]:
        if target == "lfo_speed":
            return 0.0, 4.0
        if target.endswith(("_x", "_y", "_z")):
            return -1.0, 1.0
        return 0.0, 1.0

    def _apply(self, channel: int, cc: int, value: int):
        for m in self.mappings:
            if m.channel == channel and m.cc == cc:
                v = m.scaled(value)
                self._set_target(m.target, v)

    def _set_target(self, target: str, value: float):
        eng = self.engine
        if target == "lfo_speed":
            eng.lfo_speed_mult = value
            return
        # "src{i}_{attr}"  e.g. src1_x, src3_gain_ambi
        if target.startswith("src"):
            parts = target.split("_", 1)
            try:
                idx  = int(parts[0][3:]) - 1
                attr = parts[1]
                setattr(eng.sources[idx], attr, value)
            except Exception:
                pass

    # ── mapping persistence ───────────────────────────────────────────────────

    def save_mappings(self, path: str):
        with open(path, "w") as f:
            json.dump([m.to_dict() for m in self.mappings], f, indent=2)

    def load_mappings(self, path: str):
        try:
            with open(path) as f:
                self.mappings = [MidiMapping.from_dict(d) for d in json.load(f)]
        except FileNotFoundError:
            pass


# ── MIDI page UI ──────────────────────────────────────────────────────────────

class MidiPage(ctk.CTkFrame):
    MIDI_MAP_FILE = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "midi_mappings.json")

    def __init__(self, parent, midi_engine: MidiEngine, app_engine: Engine, **kw):
        super().__init__(parent, fg_color="transparent", **kw)
        self.midi   = midi_engine
        self.engine = app_engine
        self.midi.load_mappings(self.MIDI_MAP_FILE)
        self._build()

    def _build(self):
        # ── top: device select ────────────────────────────────────────────────
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill=tk.X, padx=12, pady=(10, 4))

        ctk.CTkLabel(top, text="MIDI Device:").pack(side=tk.LEFT, padx=(0, 6))
        self._dev_var = ctk.StringVar(value="")
        self._dev_menu = ctk.CTkOptionMenu(top, variable=self._dev_var,
                                           values=self._get_ports(), width=220,
                                           command=self._on_device_select)
        self._dev_menu.pack(side=tk.LEFT)
        ctk.CTkButton(top, text="↺ Refresh", width=80,
                      command=self._refresh_ports).pack(side=tk.LEFT, padx=6)
        self._conn_label = ctk.CTkLabel(top, text="", text_color="#888",
                                        font=ctk.CTkFont(size=11))
        self._conn_label.pack(side=tk.LEFT, padx=8)

        # ── learn section ─────────────────────────────────────────────────────
        learn_frame = ctk.CTkFrame(self, corner_radius=8)
        learn_frame.pack(fill=tk.X, padx=12, pady=6)

        ctk.CTkLabel(learn_frame, text="Add Mapping",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=10, pady=(8,4))

        row = ctk.CTkFrame(learn_frame, fg_color="transparent")
        row.pack(fill=tk.X, padx=10, pady=(0, 8))

        ctk.CTkLabel(row, text="Target:").pack(side=tk.LEFT, padx=(0, 4))
        # build target list: lfo_speed + all src params
        targets = ["lfo_speed"]
        for i in range(1, MAX_SOURCES + 1):
            for attr in ["x", "y", "z", "gain_ambi", "gain_wfs", "gain_lfe"]:
                targets.append(f"src{i}_{attr}")
        self._target_var = ctk.StringVar(value="lfo_speed")
        ctk.CTkOptionMenu(row, variable=self._target_var,
                          values=targets, width=180).pack(side=tk.LEFT, padx=4)

        self._learn_btn = ctk.CTkButton(row, text="Learn CC", width=90,
                                        command=self._start_learn)
        self._learn_btn.pack(side=tk.LEFT, padx=6)
        self._learn_status = ctk.CTkLabel(row, text="", text_color="#888",
                                          font=ctk.CTkFont(size=11))
        self._learn_status.pack(side=tk.LEFT, padx=6)

        # ── mapping table ─────────────────────────────────────────────────────
        ctk.CTkLabel(self, text="Active Mappings",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=12, pady=(10, 2))

        self._table_frame = ctk.CTkScrollableFrame(self, height=280)
        self._table_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill=tk.X, padx=12, pady=(0, 8))
        ctk.CTkButton(btn_row, text="Save Mappings", width=120,
                      command=self._save).pack(side=tk.LEFT, padx=(0, 6))
        ctk.CTkButton(btn_row, text="Clear All", width=90,
                      fg_color="#5a1a1a", hover_color="#7a2a2a",
                      command=self._clear_all).pack(side=tk.LEFT)

        self._refresh_table()

    def _get_ports(self) -> list[str]:
        try:
            ports = mido.get_input_names()
            return ports if ports else ["(no devices)"]
        except Exception:
            return ["(no devices)"]

    def _refresh_ports(self):
        ports = self._get_ports()
        self._dev_menu.configure(values=ports)
        if ports:
            self._dev_var.set(ports[0])

    def _on_device_select(self, name: str):
        if name == "(no devices)":
            return
        try:
            self.midi.connect(name)
            self._conn_label.configure(text=f"● {name}", text_color="#4caf50")
        except Exception as e:
            self._conn_label.configure(text=str(e), text_color="#f44336")

    def _start_learn(self):
        target = self._target_var.get()
        self.midi.learning = target
        self._learn_btn.configure(text="Waiting…", fg_color="#e67e22")
        self._learn_status.configure(text="Move a CC on your controller…", text_color="#f39c12")

        def on_learned(ch, cc):
            self.after(0, lambda: self._on_learned(ch, cc))

        self.midi._on_learn_cb = on_learned
        # timeout after 10s
        self.after(10000, self._learn_timeout)

    def _on_learned(self, ch: int, cc: int):
        self._learn_btn.configure(text="Learn CC", fg_color=["#3B8ED0", "#1F6AA5"])
        self._learn_status.configure(
            text=f"✓ Mapped to Ch{ch+1} CC{cc}", text_color="#4caf50")
        self._refresh_table()

    def _learn_timeout(self):
        if self.midi.learning:
            self.midi.learning = None
            self._learn_btn.configure(text="Learn CC", fg_color=["#3B8ED0", "#1F6AA5"])
            self._learn_status.configure(text="Timeout.", text_color="#888")

    def _refresh_table(self):
        for w in self._table_frame.winfo_children():
            w.destroy()

        # header
        hdr = ctk.CTkFrame(self._table_frame, fg_color="#2a2a2a", corner_radius=4)
        hdr.pack(fill=tk.X, pady=(0, 2))
        for text, w in [("Ch", 40), ("CC", 40), ("Target", 180), ("Lo", 55), ("Hi", 55), ("", 60)]:
            ctk.CTkLabel(hdr, text=text, width=w,
                         font=ctk.CTkFont(size=11, weight="bold")).pack(side=tk.LEFT, padx=4)

        for i, m in enumerate(self.midi.mappings):
            self._mapping_row(i, m)

    def _mapping_row(self, idx: int, m: MidiMapping):
        row = ctk.CTkFrame(self._table_frame, fg_color="#1e1e1e", corner_radius=4)
        row.pack(fill=tk.X, pady=1)

        ctk.CTkLabel(row, text=str(m.channel + 1), width=40,
                     font=ctk.CTkFont(size=11)).pack(side=tk.LEFT, padx=4)
        ctk.CTkLabel(row, text=str(m.cc), width=40,
                     font=ctk.CTkFont(size=11)).pack(side=tk.LEFT, padx=4)
        ctk.CTkLabel(row, text=m.target, width=180,
                     font=ctk.CTkFont(size=11), text_color="#4a9eff").pack(side=tk.LEFT, padx=4)

        lo_var = ctk.StringVar(value=str(m.lo))
        hi_var = ctk.StringVar(value=str(m.hi))

        def update_lo(e, mapping=m, var=lo_var):
            try: mapping.lo = float(var.get())
            except ValueError: pass

        def update_hi(e, mapping=m, var=hi_var):
            try: mapping.hi = float(var.get())
            except ValueError: pass

        lo_e = ctk.CTkEntry(row, textvariable=lo_var, width=55, height=22,
                             font=ctk.CTkFont(size=11))
        lo_e.pack(side=tk.LEFT, padx=4)
        lo_e.bind("<Return>", update_lo)
        lo_e.bind("<FocusOut>", update_lo)

        hi_e = ctk.CTkEntry(row, textvariable=hi_var, width=55, height=22,
                             font=ctk.CTkFont(size=11))
        hi_e.pack(side=tk.LEFT, padx=4)
        hi_e.bind("<Return>", update_hi)
        hi_e.bind("<FocusOut>", update_hi)

        ctk.CTkButton(row, text="✕", width=30, height=22,
                      fg_color="#5a1a1a", hover_color="#7a2a2a",
                      command=lambda i=idx: self._delete_mapping(i)).pack(side=tk.LEFT, padx=4)

    def _delete_mapping(self, idx: int):
        del self.midi.mappings[idx]
        self._refresh_table()

    def _save(self):
        self.midi.save_mappings(self.MIDI_MAP_FILE)
        self._learn_status.configure(text="✓ Saved", text_color="#4caf50")

    def _clear_all(self):
        self.midi.mappings.clear()
        self._refresh_table()


# ═════════════════════════════════════════════════════════════════════════════
# Preset manager
# ═════════════════════════════════════════════════════════════════════════════

PRESETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets")


class PresetManager:
    def __init__(self, engine: Engine):
        self.engine = engine
        os.makedirs(PRESETS_DIR, exist_ok=True)

    def save(self, name: str):
        eng  = self.engine
        data = {
            "num_active": eng.num_active,
            "sources": [
                {"x": s.x, "y": s.y, "z": s.z,
                 "gain_ambi": s.gain_ambi, "gain_wfs": s.gain_wfs, "gain_lfe": s.gain_lfe}
                for s in eng.sources
            ],
            "lfos": [
                {"enabled": lfo.enabled, "rate": lfo.rate, "depth": lfo.depth,
                 "phase": lfo.phase, "target_src": lfo.target_src, "target_par": lfo.target_par}
                for lfo in eng.lfos
            ],
        }
        with open(os.path.join(PRESETS_DIR, f"{name}.json"), "w") as f:
            json.dump(data, f, indent=2)

    def load(self, name: str) -> dict:
        with open(os.path.join(PRESETS_DIR, f"{name}.json")) as f:
            return json.load(f)

    def list_presets(self) -> list[str]:
        try:
            return sorted(f[:-5] for f in os.listdir(PRESETS_DIR) if f.endswith(".json"))
        except FileNotFoundError:
            return []


class PresetBar(ctk.CTkFrame):
    def __init__(self, parent, preset_mgr: PresetManager, on_load_cb, **kw):
        super().__init__(parent, height=28, corner_radius=0,
                         fg_color="#1a1a1a", **kw)
        self._mgr      = preset_mgr
        self._on_load  = on_load_cb
        self._name_var = ctk.StringVar(value="")
        self._sel_var  = ctk.StringVar(value="")
        self._build()

    def _build(self):
        small = ctk.CTkFont(size=11)
        ctk.CTkLabel(self, text="presets", font=ctk.CTkFont(size=10),
                     text_color="#555").pack(side=tk.LEFT, padx=(12, 4))

        ctk.CTkEntry(self, textvariable=self._name_var, font=small,
                     placeholder_text="name…", width=90, height=22).pack(side=tk.LEFT, padx=2)
        ctk.CTkButton(self, text="save", font=small, width=44, height=22,
                      fg_color="#2a2a2a", hover_color="#3a3a3a", text_color="#aaa",
                      command=self._save).pack(side=tk.LEFT, padx=2)

        ctk.CTkLabel(self, text="·", text_color="#333",
                     font=ctk.CTkFont(size=14)).pack(side=tk.LEFT, padx=6)

        self._dropdown = ctk.CTkOptionMenu(
            self, variable=self._sel_var, values=["(none)"],
            font=small, width=130, height=22,
            fg_color="#2a2a2a", button_color="#2a2a2a", button_hover_color="#3a3a3a",
            text_color="#aaa")
        self._dropdown.pack(side=tk.LEFT, padx=2)
        ctk.CTkButton(self, text="load", font=small, width=44, height=22,
                      fg_color="#2a2a2a", hover_color="#3a3a3a", text_color="#aaa",
                      command=self._load).pack(side=tk.LEFT, padx=2)

        self._status = ctk.CTkLabel(self, text="", text_color="#555", font=small)
        self._status.pack(side=tk.LEFT, padx=8)
        self.refresh_dropdown()

    def refresh_dropdown(self):
        names = self._mgr.list_presets()
        if not names:
            self._dropdown.configure(values=["(none)"])
            self._sel_var.set("(none)")
        else:
            self._dropdown.configure(values=names)
            if self._sel_var.get() not in names:
                self._sel_var.set(names[0])

    def _save(self):
        name = self._name_var.get().strip()
        if not name:
            self._status.configure(text="Enter a name first.", text_color="#f39c12")
            return
        safe = "".join(c for c in name if c.isalnum() or c in "_ -")
        try:
            self._mgr.save(safe)
            self._status.configure(text=f"✓ Saved '{safe}'", text_color="#4caf50")
            self.refresh_dropdown()
            self._sel_var.set(safe)
        except Exception as e:
            self._status.configure(text=str(e), text_color="#f44336")

    def _load(self):
        name = self._sel_var.get()
        if name == "(none)":
            return
        try:
            self._on_load(name)
            self._status.configure(text=f"✓ Loaded '{name}'", text_color="#4caf50")
        except Exception as e:
            self._status.configure(text=str(e), text_color="#f44336")


# ═════════════════════════════════════════════════════════════════════════════
# Main app
# ═════════════════════════════════════════════════════════════════════════════

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("OSC Spatial Sender")
        self.geometry("1100x680")

        self.engine     = Engine()
        self.preset_mgr = PresetManager(self.engine)
        self.midi_engine = MidiEngine(self.engine)
        self.engine.start()

        self._build_topbar()
        self._build_preset_bar()
        self._build_tabs()

    def _build_topbar(self):
        bar = ctk.CTkFrame(self, height=50, corner_radius=0)
        bar.pack(fill=tk.X)

        ctk.CTkLabel(bar, text="OSC Spatial Sender",
                     font=ctk.CTkFont(size=15, weight="bold")).pack(side=tk.LEFT, padx=14)

        ctk.CTkLabel(bar, text="Sources:").pack(side=tk.LEFT, padx=(20, 2))
        self.num_var = ctk.StringVar(value="4")
        ctk.CTkEntry(bar, textvariable=self.num_var, width=40).pack(side=tk.LEFT)
        ctk.CTkButton(bar, text="Apply", width=55,
                      command=self._apply_sources).pack(side=tk.LEFT, padx=4)

        # LFO Speed multiplier
        ctk.CTkLabel(bar, text="LFO Speed:").pack(side=tk.LEFT, padx=(20, 2))
        self._speed_label = ctk.CTkLabel(bar, text="1.00×", width=40)
        self._speed_label.pack(side=tk.LEFT)
        self._speed_slider = ctk.CTkSlider(
            bar, from_=0.0, to=4.0, width=120,
            command=self._on_speed_change)
        self._speed_slider.set(1.0)
        self._speed_slider.pack(side=tk.LEFT, padx=(2, 4))

        self.ip_var   = ctk.StringVar(value=DEFAULT_IP)
        self.port_var = ctk.StringVar(value=str(DEFAULT_PORT))

        ctk.CTkLabel(bar, text="IP:").pack(side=tk.LEFT, padx=(20, 2))
        ctk.CTkEntry(bar, textvariable=self.ip_var, width=200).pack(side=tk.LEFT)
        ctk.CTkLabel(bar, text="Port:").pack(side=tk.LEFT, padx=(8, 2))
        ctk.CTkEntry(bar, textvariable=self.port_var, width=55).pack(side=tk.LEFT)
        ctk.CTkButton(bar, text="Connect", width=75,
                      command=self._connect).pack(side=tk.LEFT, padx=8)

        self.status = ctk.CTkLabel(bar, text="⚫ disconnected", text_color="gray")
        self.status.pack(side=tk.LEFT, padx=4)

    def _build_tabs(self):
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

        for name in ("Sources", "LFOs", "XY View", "MIDI"):
            self.tabs.add(name)

        self.sources_page = SourcesPage(self.tabs.tab("Sources"), self.engine)
        self.sources_page.pack(fill=tk.BOTH, expand=True)

        LFOPage(self.tabs.tab("LFOs"), self.engine).pack(fill=tk.BOTH, expand=True)

        View2DPage(self.tabs.tab("XY View"), self.engine).pack(fill=tk.BOTH, expand=True)

        MidiPage(self.tabs.tab("MIDI"), self.midi_engine, self.engine).pack(fill=tk.BOTH, expand=True)

    def _connect(self):
        try:
            self.engine.connect(self.ip_var.get().strip(),
                                 int(self.port_var.get().strip()))
            self.status.configure(
                text=f"🟢 {self.ip_var.get()}:{self.port_var.get()}",
                text_color="#4caf50")
        except Exception as e:
            self.status.configure(text=f"🔴 {e}", text_color="#f44336")

    def _on_speed_change(self, v):
        self.engine.lfo_speed_mult = float(v)
        self._speed_label.configure(text=f"{float(v):.2f}×")

    def _apply_sources(self):
        try:
            n = int(self.num_var.get())
            self.sources_page.set_num_sources(n)
        except (ValueError, tk.TclError):
            pass

    def _build_preset_bar(self):
        self.preset_bar = PresetBar(self, self.preset_mgr, on_load_cb=self._apply_preset)
        self.preset_bar.pack(fill=tk.X, pady=(4, 0))

    def _apply_preset(self, name: str):
        data = self.preset_mgr.load(name)
        n    = data.get("num_active", self.engine.num_active)

        for i, sd in enumerate(data.get("sources", [])):
            src           = self.engine.sources[i]
            src.x         = sd.get("x",         src.x)
            src.y         = sd.get("y",         src.y)
            src.z         = sd.get("z",         src.z)
            src.gain_ambi = sd.get("gain_ambi", src.gain_ambi)
            src.gain_wfs  = sd.get("gain_wfs",  src.gain_wfs)
            src.gain_lfe  = sd.get("gain_lfe",  src.gain_lfe)

        for i, ld in enumerate(data.get("lfos", [])):
            lfo            = self.engine.lfos[i]
            lfo.enabled    = ld.get("enabled",    lfo.enabled)
            lfo.rate       = ld.get("rate",        lfo.rate)
            lfo.depth      = ld.get("depth",       lfo.depth)
            lfo.phase      = ld.get("phase",       lfo.phase)
            lfo.target_src = ld.get("target_src",  lfo.target_src)
            lfo.target_par = ld.get("target_par",  lfo.target_par)

        # Rebuild pages so widgets re-read the updated model
        self.num_var.set(str(n))
        self.sources_page.set_num_sources(n)

        lfo_tab = self.tabs.tab("LFOs")
        for child in lfo_tab.winfo_children():
            child.destroy()
        LFOPage(lfo_tab, self.engine).pack(fill=tk.BOTH, expand=True)

    def on_close(self):
        self.midi_engine.disconnect()
        self.engine.stop()
        plt.close("all")
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
