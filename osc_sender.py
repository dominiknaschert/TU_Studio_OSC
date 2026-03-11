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
        self._last_t    = None   # last time value() was called
        self._phase_acc = 0.0    # accumulated angle (rad), only speed of change varies
        self.phase_jitter = 0.0  # chaos: random phase offset
        self.rate_chaos = 0.0    # chaos: rate variation (0-1)
        self.waveform_distort = 0.0  # chaos: non-linear distortion (0-1)
        self._jitter_acc = 0.0   # accumulated jitter offset

    def value(self, speed_mult: float = 1.0,
              rate_mult: float = 1.0,
              depth_mult: float = 1.0,
              phase_mult: float = 1.0) -> float:
        now = time.time()
        if self._last_t is None:
            self._last_t = now
        dt = now - self._last_t
        self._last_t = now
        
        # Add rate chaos
        rate_mod = 1.0
        if self.rate_chaos > 0:
            # pseudo-random oscillation based on time
            rate_mod = 1.0 + math.sin(now * (1 + self.rate_chaos * 5)) * self.rate_chaos * 0.5
        
        # Add phase jitter
        if self.phase_jitter > 0:
            self._jitter_acc += math.sin(now * 3.7) * self.phase_jitter * 0.1
            self._jitter_acc = max(-math.pi, min(math.pi, self._jitter_acc))
        
        # advance phase by (angle per second) * dt; changing speed_mult only changes future rate, not current position
        self._phase_acc += 2 * math.pi * self.rate * rate_mult * speed_mult * rate_mod * dt
        self._phase_acc %= 2 * math.pi  # keep in [0, 2*pi) for precision
        
        effective_depth = self.depth * depth_mult
        effective_phase = self.phase * phase_mult
        
        v = math.sin(self._phase_acc + effective_phase + self._jitter_acc) * effective_depth
        
        # Apply waveform distortion (non-linear shaping)
        if self.waveform_distort > 0:
            # soft tanh-like distortion mixed with original
            distorted = math.tanh(v * (1 + self.waveform_distort * 3))
            v = v * (1 - self.waveform_distort) + distorted * self.waveform_distort
        
        return v


# ═════════════════════════════════════════════════════════════════════════════
# Chaos System
# ═════════════════════════════════════════════════════════════════════════════

class ChaosSlider:
    """A fixed multiplier slider that can control multiple parameters."""
    def __init__(self, name: str, lo: float = 0.0, hi: float = 2.0):
        self.name   = name
        self.value  = 1.0
        self.lo     = lo
        self.hi     = hi
        self.bindings: list[str] = []

    def to_dict(self) -> dict:
        return {"name": self.name, "value": self.value, "lo": self.lo, "hi": self.hi, "bindings": self.bindings}

    @staticmethod
    def from_dict(d: dict) -> "ChaosSlider":
        cs = ChaosSlider(d["name"], d.get("lo", 0.0), d.get("hi", 1.0))
        cs.value = d.get("value", 0.0)
        cs.bindings = d.get("bindings", [])
        return cs


class ChaosSystem:
    """Manages three fixed multiplier sliders."""
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.sliders: list[ChaosSlider] = []
        self._init_default_sliders()

    def _init_default_sliders(self):
        """Initialize exactly 3 sliders."""
        if not self.sliders:
            self.sliders.append(ChaosSlider("Slider 1"))
            self.sliders.append(ChaosSlider("Slider 2"))
            self.sliders.append(ChaosSlider("Slider 3"))

    def set_sliders_from_data(self, slider_data: list[dict] | None):
        defaults = [ChaosSlider("Slider 1"), ChaosSlider("Slider 2"), ChaosSlider("Slider 3")]
        if not slider_data:
            self.sliders = defaults
            return

        loaded = [ChaosSlider.from_dict(d) for d in slider_data[:3]]
        while len(loaded) < 3:
            loaded.append(defaults[len(loaded)])
        for i, slider in enumerate(loaded):
            if not slider.name:
                slider.name = defaults[i].name
        self.sliders = loaded

    def get_target_multipliers(self) -> dict[str, float]:
        multipliers: dict[str, float] = {}
        for slider in self.sliders:
            if not slider.bindings:
                continue
            for binding in slider.bindings:
                multipliers[binding] = multipliers.get(binding, 1.0) * slider.value
        return multipliers

    def save(self, path: str):
        with open(path, "w") as f:
            json.dump([s.to_dict() for s in self.sliders], f, indent=2)

    def load(self, path: str):
        try:
            with open(path) as f:
                self.set_sliders_from_data(json.load(f))
        except FileNotFoundError:
            pass


# ═════════════════════════════════════════════════════════════════════════════
# Engine
# ═════════════════════════════════════════════════════════════════════════════

class Engine:
    def __init__(self):
        self.sources:        list[SourceData] = [SourceData(i) for i in range(1, MAX_SOURCES + 1)]
        self.lfos:           list[LFO]        = [LFO() for _ in range(16)]
        self.client:         udp_client.SimpleUDPClient | None = None
        self.num_active      = 4
        self.num_active_lfos = 4
        self.lfo_speed_mult  = 1.0   # global LFO speed multiplier
        self.running         = False
        # last rendered positions (with LFO applied) for the 3-D view
        self.rendered: list[tuple[float, float, float]] = [(0, 0, 0)] * MAX_SOURCES
        self.chaos_system    = ChaosSystem(self)

    def connect(self, ip: str, port: int):
        self.client = udp_client.SimpleUDPClient(ip, port)

    def disconnect(self):
        self.client = None

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
        multipliers = self.chaos_system.get_target_multipliers()
        speed_mult = self.lfo_speed_mult * multipliers.get("lfo_speed", 1.0)
        
        offsets: dict[tuple[int, str], float] = {}
        for i, lfo in enumerate(self.lfos[:self.num_active_lfos], start=1):
            if not lfo.enabled:
                continue
            key = (lfo.target_src, lfo.target_par)
            offsets[key] = offsets.get(key, 0.0) + lfo.value(
                speed_mult,
                rate_mult=multipliers.get(f"lfo{i}_rate", 1.0),
                depth_mult=multipliers.get(f"lfo{i}_depth", 1.0),
                phase_mult=multipliers.get(f"lfo{i}_phase", 1.0),
            )

        rendered = list(self.rendered)
        for src in self.sources[:self.num_active]:
            si         = src.index
            x_mult     = multipliers.get(f"src{si}_x", 1.0)
            y_mult     = multipliers.get(f"src{si}_y", 1.0)
            z_mult     = multipliers.get(f"src{si}_z", 1.0)
            ambi_mult  = multipliers.get(f"src{si}_gain_ambi", 1.0)
            wfs_mult   = multipliers.get(f"src{si}_gain_wfs", 1.0)
            lfe_mult   = multipliers.get(f"src{si}_gain_lfe", 1.0)
            x          = clamp(src.x * x_mult + offsets.get((si, "x"), 0.0))
            y          = clamp(src.y * y_mult + offsets.get((si, "y"), 0.0))
            z          = clamp(src.z * z_mult + offsets.get((si, "z"), 0.0))
            gain_ambi  = clamp(src.gain_ambi * ambi_mult + offsets.get((si, "gain_ambi"), 0.0), 0.0, 1.0)
            gain_wfs   = clamp(src.gain_wfs  * wfs_mult  + offsets.get((si, "gain_wfs"),  0.0), 0.0, 1.0)
            gain_lfe   = clamp(src.gain_lfe  * lfe_mult  + offsets.get((si, "gain_lfe"),  0.0), 0.0, 1.0)
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
    def __init__(self, parent, idx: int, lfo: LFO, engine: Engine, **kw):
        super().__init__(parent, corner_radius=8, **kw)
        self.lfo = lfo
        self.engine = engine
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
        v     = self.lfo.value(self.engine.lfo_speed_mult)
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
        for i, lfo in enumerate(engine.lfos[:engine.num_active_lfos]):
            LFOStrip(self, i, lfo, engine).pack(side=tk.LEFT, padx=6, pady=6, fill=tk.Y)


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
        self.fig, (self.ax_xy, self.ax_xz) = plt.subplots(1, 2, figsize=(10, 5), facecolor="#1c1c1c")
        
        # Configure XY plot
        self.ax_xy.set_facecolor("#1c1c1c")
        self.ax_xy.set_xlim(-1.1, 1.1)
        self.ax_xy.set_ylim(-1.1, 1.1)
        self.ax_xy.set_xlabel("X", color="#aaa")
        self.ax_xy.set_ylabel("Y", color="#aaa")
        self.ax_xy.tick_params(colors="#666", labelsize=8)
        self.ax_xy.axhline(0, color="#333", linewidth=0.8)
        self.ax_xy.axvline(0, color="#333", linewidth=0.8)
        self.ax_xy.set_aspect("equal")
        self.ax_xy.grid(True, color="#2a2a2a", linewidth=0.5)

        # unit circle for XY reference
        theta = [i * 2 * math.pi / 120 for i in range(121)]
        self.ax_xy.plot([math.cos(t) for t in theta],
                        [math.sin(t) for t in theta],
                        color="#333", linewidth=0.8, linestyle="--")

        # Configure XZ plot
        self.ax_xz.set_facecolor("#1c1c1c")
        self.ax_xz.set_xlim(-1.1, 1.1)
        self.ax_xz.set_ylim(-1.1, 1.1)
        self.ax_xz.set_xlabel("X", color="#aaa")
        self.ax_xz.set_ylabel("Z", color="#aaa")
        self.ax_xz.tick_params(colors="#666", labelsize=8)
        self.ax_xz.axhline(0, color="#333", linewidth=0.8)
        self.ax_xz.axvline(0, color="#333", linewidth=0.8)
        self.ax_xz.set_aspect("equal")
        self.ax_xz.grid(True, color="#2a2a2a", linewidth=0.5)

        # unit circle for XZ reference
        self.ax_xz.plot([math.cos(t) for t in theta],
                        [math.sin(t) for t in theta],
                        color="#333", linewidth=0.8, linestyle="--")

        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # pre-create scatter + labels for XY plot (all hidden)
        self._scatter_xy = self.ax_xy.scatter(
            [0.0] * MAX_SOURCES, [0.0] * MAX_SOURCES,
            c=SOURCE_COLORS, s=[80] * MAX_SOURCES,
            edgecolors="white", linewidths=0.5, zorder=5, alpha=0.0,
        )
        self._texts_xy = [
            self.ax_xy.text(0, 0, f" S{i+1}",
                            color=SOURCE_COLORS[i % len(SOURCE_COLORS)],
                            fontsize=8, visible=False, zorder=6)
            for i in range(MAX_SOURCES)
        ]

        # pre-create scatter + labels for XZ plot (all hidden)
        self._scatter_xz = self.ax_xz.scatter(
            [0.0] * MAX_SOURCES, [0.0] * MAX_SOURCES,
            c=SOURCE_COLORS, s=[80] * MAX_SOURCES,
            edgecolors="white", linewidths=0.5, zorder=5, alpha=0.0,
        )
        self._texts_xz = [
            self.ax_xz.text(0, 0, f" S{i+1}",
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
        zs = [rendered[i][2] if i < n else 0.0 for i in range(MAX_SOURCES)]

        sizes = [max(40, self.engine.sources[i].gain_ambi * 200) if i < n else 1
                 for i in range(MAX_SOURCES)]

        # Update XY plot
        self._scatter_xy.set_offsets(list(zip(xs, ys)))
        self._scatter_xy.set_alpha(1.0)
        self._scatter_xy.set_sizes(sizes)

        for i, txt in enumerate(self._texts_xy):
            if i < n:
                txt.set_position((xs[i] + 0.03, ys[i] + 0.03))
                txt.set_visible(True)
            else:
                txt.set_visible(False)

        # Update XZ plot
        self._scatter_xz.set_offsets(list(zip(xs, zs)))
        self._scatter_xz.set_alpha(1.0)
        self._scatter_xz.set_sizes(sizes)

        for i, txt in enumerate(self._texts_xz):
            if i < n:
                txt.set_position((xs[i] + 0.03, zs[i] + 0.03))
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
        if target.startswith("chaos_slider_"):
            try:
                idx = int(target.split("_")[2]) - 1
                slider = self.engine.chaos_system.sliders[idx]
                return slider.lo, slider.hi
            except (ValueError, IndexError):
                return 0.0, 2.0
        if target.startswith("lfo") and "_" in target:
            # lfo1_rate, lfo2_depth, lfo3_phase
            if target.endswith("_rate"):
                return 0.01, 5.0
            if target.endswith("_phase"):
                return 0.0, 2.0 * math.pi
            if target.endswith("_depth"):
                return 0.0, 1.0
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
            eng.lfo_speed_mult = max(0.0, min(4.0, value))
            return
        # Chaos sliders: "chaos_slider_1", "chaos_slider_2", "chaos_slider_3"
        if target.startswith("chaos_slider_"):
            try:
                idx = int(target.split("_")[2]) - 1
                if 0 <= idx < len(eng.chaos_system.sliders):
                    slider = eng.chaos_system.sliders[idx]
                    slider.value = max(slider.lo, min(slider.hi, value))
            except (ValueError, IndexError):
                pass
            return
        # "lfo{i}_rate" / "lfo{i}_depth" / "lfo{i}_phase"
        if target.startswith("lfo") and "_" in target:
            rest = target[3:].split("_", 1)  # e.g. "1_rate"
            try:
                idx = int(rest[0]) - 1
                attr = rest[1]
                if 0 <= idx < len(eng.lfos) and attr in ("rate", "depth", "phase"):
                    lfo = eng.lfos[idx]
                    if attr == "phase":
                        setattr(lfo, attr, value % (2 * math.pi))
                    else:
                        setattr(lfo, attr, max(0.0, min(5.0 if attr == "rate" else 1.0, value)))
            except (ValueError, IndexError):
                pass
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

        # ── learn section: Index (Source/LFO) + Parameter + Learn CC ───────────
        learn_frame = ctk.CTkFrame(self, corner_radius=8)
        learn_frame.pack(fill=tk.X, padx=12, pady=6)

        ctk.CTkLabel(learn_frame, text="Add Mapping",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=10, pady=(8,4))

        row = ctk.CTkFrame(learn_frame, fg_color="transparent")
        row.pack(fill=tk.X, padx=10, pady=(0, 8))

        ctk.CTkLabel(row, text="Index:").pack(side=tk.LEFT, padx=(0, 4))
        self._index_var = ctk.StringVar(value="Source 1")
        self._index_menu = ctk.CTkOptionMenu(
            row, variable=self._index_var, width=120,
            command=self._on_index_select)
        self._index_menu.pack(side=tk.LEFT, padx=4)

        ctk.CTkLabel(row, text="Parameter:").pack(side=tk.LEFT, padx=(12, 4))
        self._param_var = ctk.StringVar(value="X")
        self._param_menu = ctk.CTkOptionMenu(
            row, variable=self._param_var, width=100,
            command=self._on_param_select)
        self._param_menu.pack(side=tk.LEFT, padx=4)

        self._learn_btn = ctk.CTkButton(row, text="Learn CC", width=90,
                                         command=self._start_learn)
        self._learn_btn.pack(side=tk.LEFT, padx=(12, 4))
        self._learn_status = ctk.CTkLabel(row, text="", text_color="#888",
                                          font=ctk.CTkFont(size=11))
        self._learn_status.pack(side=tk.LEFT, padx=4)

        self._target_var = ctk.StringVar(value="")  # internal target id (src1_x, lfo2_rate, …)
        self._refresh_index_dropdown()
        self._update_param_dropdown()
        self._sync_target_from_selection()

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

    # Index = Source 1..N, LFO 1..16, LFO Speed. Parameter = depends on index.
    _SOURCE_PARAMS = [("X", "x"), ("Y", "y"), ("Z", "z"), ("Ambi", "gain_ambi"), ("WFS", "gain_wfs"), ("LFE", "gain_lfe")]
    _LFO_PARAMS = [("Rate", "rate"), ("Depth", "depth"), ("Phase", "phase")]

    def _refresh_index_dropdown(self):
        eng = self.engine
        indices = [f"Source {i}" for i in range(1, eng.num_active + 1)]
        indices += [f"LFO {i}" for i in range(1, eng.num_active_lfos + 1)]
        indices.append("LFO Speed")
        indices += [f"Chaos Slider {i}" for i in range(1, len(eng.chaos_system.sliders) + 1)]
        self._index_menu.configure(values=indices)
        cur = self._index_var.get()
        if cur not in indices:
            self._index_var.set(indices[0])
        self._update_param_dropdown()
        self._sync_target_from_selection()

    def _update_param_dropdown(self):
        idx = self._index_var.get()
        if idx.startswith("Source "):
            param_labels = [p[0] for p in self._SOURCE_PARAMS]
        elif idx == "LFO Speed":
            param_labels = ["Mult"]
        elif idx.startswith("Chaos Slider "):
            param_labels = ["Mult"]
        else:
            param_labels = [p[0] for p in self._LFO_PARAMS]
        self._param_menu.configure(values=param_labels)
        cur = self._param_var.get()
        if cur not in param_labels:
            self._param_var.set(param_labels[0])
        self._sync_target_from_selection()

    def _on_index_select(self, _value: str):
        self._update_param_dropdown()

    def _on_param_select(self, _value: str):
        self._sync_target_from_selection()

    def _sync_target_from_selection(self):
        idx = self._index_var.get()
        param_label = self._param_var.get()
        if idx.startswith("Source "):
            try:
                i = int(idx.split()[1])
                for lab, attr in self._SOURCE_PARAMS:
                    if lab == param_label:
                        self._target_var.set(f"src{i}_{attr}")
                        return
            except (ValueError, IndexError):
                pass
        elif idx == "LFO Speed":
            if param_label == "Mult":
                self._target_var.set("lfo_speed")
            return
        elif idx.startswith("Chaos Slider "):
            try:
                i = int(idx.split()[2])
                if param_label == "Mult":
                    self._target_var.set(f"chaos_slider_{i}")
            except (ValueError, IndexError):
                pass
            return
        elif idx.startswith("LFO "):
            try:
                i = int(idx.split()[1])
                for lab, attr in self._LFO_PARAMS:
                    if lab == param_label:
                        self._target_var.set(f"lfo{i}_{attr}")
                        return
            except (ValueError, IndexError):
                pass
        self._target_var.set("")

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
        if not target:
            self._learn_status.configure(text="Select a target (Source or LFO) first.", text_color="#e67e22")
            return
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
# Chaos page
# ═════════════════════════════════════════════════════════════════════════════

class ChaosPage(ctk.CTkScrollableFrame):
    _SOURCE_PARAMS = [("X", "x"), ("Y", "y"), ("Z", "z"), ("Ambi", "gain_ambi"), ("WFS", "gain_wfs"), ("LFE", "gain_lfe")]
    _LFO_PARAMS = [("Rate", "rate"), ("Depth", "depth"), ("Phase", "phase")]

    def __init__(self, parent, engine: Engine, **kw):
        super().__init__(parent, orientation="vertical", **kw)
        self.engine = engine
        self._build()

    def _build(self):
        ctk.CTkLabel(self, text="Chaos Sliders",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="#4a9eff").pack(anchor="w", padx=15, pady=(12, 6))
        ctk.CTkLabel(self,
                     text="Jeder Slider arbeitet als Multiplikator. Ohne zugewiesene Parameter ist er im Bypass.",
                     font=ctk.CTkFont(size=10), text_color="#888").pack(anchor="w", padx=15, pady=(0, 12))

        for idx, slider in enumerate(self.engine.chaos_system.sliders, start=1):
            self._build_slider_block(idx, slider).pack(fill=tk.X, padx=12, pady=6)

    def _index_values(self) -> list[str]:
        values = [f"Source {i}" for i in range(1, self.engine.num_active + 1)]
        values += [f"LFO {i}" for i in range(1, self.engine.num_active_lfos + 1)]
        values.append("LFO Speed")
        return values

    def _parameter_values(self, index_value: str) -> list[str]:
        if index_value.startswith("Source "):
            return [label for label, _ in self._SOURCE_PARAMS]
        if index_value == "LFO Speed":
            return ["Mult"]
        return [label for label, _ in self._LFO_PARAMS]

    def _target_from_selection(self, index_value: str, param_label: str) -> str:
        if index_value.startswith("Source "):
            try:
                src_idx = int(index_value.split()[1])
            except (ValueError, IndexError):
                return ""
            for label, attr in self._SOURCE_PARAMS:
                if label == param_label:
                    return f"src{src_idx}_{attr}"
            return ""
        if index_value == "LFO Speed":
            return "lfo_speed" if param_label == "Mult" else ""
        if index_value.startswith("LFO "):
            try:
                lfo_idx = int(index_value.split()[1])
            except (ValueError, IndexError):
                return ""
            for label, attr in self._LFO_PARAMS:
                if label == param_label:
                    return f"lfo{lfo_idx}_{attr}"
        return ""

    def _target_label(self, binding: str) -> str:
        if binding == "lfo_speed":
            return "LFO Speed · Mult"
        if binding.startswith("src") and "_" in binding:
            head, attr = binding.split("_", 1)
            try:
                src_idx = int(head[3:])
            except ValueError:
                return binding
            for label, source_attr in self._SOURCE_PARAMS:
                if source_attr == attr:
                    return f"Source {src_idx} · {label}"
        if binding.startswith("lfo") and "_" in binding:
            head, attr = binding.split("_", 1)
            try:
                lfo_idx = int(head[3:])
            except ValueError:
                return binding
            for label, lfo_attr in self._LFO_PARAMS:
                if lfo_attr == attr:
                    return f"LFO {lfo_idx} · {label}"
        return binding

    def _build_slider_block(self, slider_number: int, slider: ChaosSlider) -> ctk.CTkFrame:
        block = ctk.CTkFrame(self, corner_radius=8)

        ctk.CTkLabel(block, text=f"Slider {slider_number}", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=12, pady=(10, 4))

        top = ctk.CTkFrame(block, fg_color="transparent")
        top.pack(fill=tk.X, padx=12, pady=(0, 8))

        ctk.CTkLabel(top, text="Min", font=ctk.CTkFont(size=10)).pack(side=tk.LEFT)
        lo_var = ctk.StringVar(value=f"{slider.lo:.2f}")
        lo_entry = ctk.CTkEntry(top, textvariable=lo_var, width=58, height=24)
        lo_entry.pack(side=tk.LEFT, padx=(4, 8))

        slider_var = ctk.DoubleVar(value=slider.value)
        slider_widget = ctk.CTkSlider(top, from_=slider.lo, to=slider.hi, variable=slider_var, width=140)
        slider_widget.pack(side=tk.LEFT, padx=(0, 8))

        ctk.CTkLabel(top, text="Max", font=ctk.CTkFont(size=10)).pack(side=tk.LEFT)
        hi_var = ctk.StringVar(value=f"{slider.hi:.2f}")
        hi_entry = ctk.CTkEntry(top, textvariable=hi_var, width=58, height=24)
        hi_entry.pack(side=tk.LEFT, padx=(4, 12))

        value_lbl = ctk.CTkLabel(top, text=f"{slider.value:.2f}", width=46)
        value_lbl.pack(side=tk.LEFT, padx=(0, 12))

        def sync_slider_range():
            try:
                lo = float(lo_var.get())
                hi = float(hi_var.get())
            except ValueError:
                return
            if lo >= hi:
                return
            slider.lo = lo
            slider.hi = hi
            slider.value = max(lo, min(hi, slider.value))
            slider_var.set(slider.value)
            slider_widget.configure(from_=lo, to=hi)
            value_lbl.configure(text=f"{slider.value:.2f}")

        lo_entry.bind("<Return>", lambda _e: sync_slider_range())
        lo_entry.bind("<FocusOut>", lambda _e: sync_slider_range())
        hi_entry.bind("<Return>", lambda _e: sync_slider_range())
        hi_entry.bind("<FocusOut>", lambda _e: sync_slider_range())

        def on_slider_change(v):
            slider.value = float(v)
            value_lbl.configure(text=f"{slider.value:.2f}")

        slider_widget.configure(command=on_slider_change)

        index_values = self._index_values()
        index_var = ctk.StringVar(value=index_values[0])
        index_menu = ctk.CTkOptionMenu(top, values=index_values, variable=index_var, width=140, height=28)
        index_menu.pack(side=tk.LEFT, padx=(0, 8))

        param_values = self._parameter_values(index_var.get())
        param_var = ctk.StringVar(value=param_values[0])
        param_menu = ctk.CTkOptionMenu(top, values=param_values, variable=param_var, width=120, height=28)
        param_menu.pack(side=tk.LEFT, padx=(0, 8))

        def refresh_param_menu(_choice=None):
            params = self._parameter_values(index_var.get())
            param_menu.configure(values=params)
            if param_var.get() not in params:
                param_var.set(params[0])

        index_menu.configure(command=refresh_param_menu)

        table = ctk.CTkFrame(block, fg_color="#171717")
        table.pack(fill=tk.X, padx=12, pady=(0, 12))

        def rebuild_table():
            for child in table.winfo_children():
                child.destroy()
            if not slider.bindings:
                ctk.CTkLabel(table, text="Bypass: keine Parameter zugewiesen",
                             text_color="#666", font=ctk.CTkFont(size=10)).pack(anchor="w", padx=10, pady=8)
                return

            row = None
            for i, binding in enumerate(slider.bindings):
                if i % 3 == 0:
                    row = ctk.CTkFrame(table, fg_color="transparent")
                    row.pack(fill=tk.X, padx=6, pady=(6 if i == 0 else 2, 0))

                item = ctk.CTkFrame(row, fg_color="#232323", corner_radius=6)
                item.pack(side=tk.LEFT, padx=(0, 6), pady=0)
                ctk.CTkLabel(item, text=self._target_label(binding),
                             font=ctk.CTkFont(size=10)).pack(side=tk.LEFT, padx=(8, 4), pady=4)
                ctk.CTkButton(item, text="✕", width=22, height=20,
                              fg_color="#5a1a1a", hover_color="#7a2a2a",
                              command=lambda b=binding: self._remove_binding(slider, b, rebuild_table)
                              ).pack(side=tk.LEFT, padx=(0, 6), pady=3)

        def add_binding():
            target = self._target_from_selection(index_var.get(), param_var.get())
            if target and target not in slider.bindings:
                slider.bindings.append(target)
                rebuild_table()

        ctk.CTkButton(top, text="Add", width=64, height=28, command=add_binding).pack(side=tk.LEFT)
        rebuild_table()
        return block

    def _remove_binding(self, slider: ChaosSlider, binding: str, rebuild_callback):
        if binding in slider.bindings:
            slider.bindings.remove(binding)
            rebuild_callback()


# ═════════════════════════════════════════════════════════════════════════════
# Preset manager
# ═════════════════════════════════════════════════════════════════════════════

PRESETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets")


class PresetManager:
    def __init__(self, engine: Engine):
        self.engine = engine
        os.makedirs(PRESETS_DIR, exist_ok=True)

    def save(self, name: str, midi_mappings: list | None = None):
        eng  = self.engine
        data = {
            "num_active": eng.num_active,
            "num_active_lfos": eng.num_active_lfos,
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
            "chaos_sliders": [s.to_dict() for s in eng.chaos_system.sliders],
        }
        if midi_mappings is not None:
            data["midi_mappings"] = [m.to_dict() for m in midi_mappings]
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
    def __init__(self, parent, preset_mgr: PresetManager, midi_engine: "MidiEngine", on_load_cb, **kw):
        super().__init__(parent, height=28, corner_radius=0,
                         fg_color="#1a1a1a", **kw)
        self._mgr      = preset_mgr
        self._midi     = midi_engine
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
            self._mgr.save(safe, midi_mappings=self._midi.mappings)
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
        self.geometry("1250x650")

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

        ctk.CTkLabel(bar, text="LFOs:").pack(side=tk.LEFT, padx=(14, 2))
        self.lfo_count_var = ctk.StringVar(value=str(self.engine.num_active_lfos))
        ctk.CTkOptionMenu(
            bar,
            variable=self.lfo_count_var,
            values=[str(i) for i in range(1, len(self.engine.lfos) + 1)],
            width=70,
            command=self._on_lfo_count_change,
        ).pack(side=tk.LEFT)

        # LFO Speed multiplier (target in real time; smoothing avoids discrete jumps)
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
        self._conn_btn = ctk.CTkButton(bar, text="Connect", width=85,
                                        command=self._connect)
        self._conn_btn.pack(side=tk.LEFT, padx=8)

        self.status = ctk.CTkLabel(bar, text="⚫ disconnected", text_color="gray")
        self.status.pack(side=tk.LEFT, padx=4)

    def _build_tabs(self):
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

        for name in ("Sources", "LFOs", "Chaos", "MIDI", "2D Plane"):
            self.tabs.add(name)

        self.sources_page = SourcesPage(self.tabs.tab("Sources"), self.engine)
        self.sources_page.pack(fill=tk.BOTH, expand=True)

        self._rebuild_lfo_tab()

        self.chaos_page = ChaosPage(self.tabs.tab("Chaos"), self.engine)
        self.chaos_page.pack(fill=tk.BOTH, expand=True)

        self.midi_page = MidiPage(self.tabs.tab("MIDI"), self.midi_engine, self.engine)
        self.midi_page.pack(fill=tk.BOTH, expand=True)

        View2DPage(self.tabs.tab("2D Plane"), self.engine).pack(fill=tk.BOTH, expand=True)

    def _connect(self):
        try:
            self.engine.connect(self.ip_var.get().strip(),
                                 int(self.port_var.get().strip()))
            self.status.configure(
                text=f"🟢 {self.ip_var.get()}:{self.port_var.get()}",
                text_color="#4caf50")
            self._conn_btn.configure(text="Disconnect", command=self._disconnect)
        except Exception as e:
            self.status.configure(text=f"🔴 {e}", text_color="#f44336")

    def _disconnect(self):
        self.engine.disconnect()
        self.status.configure(text="⚫ disconnected", text_color="gray")
        self._conn_btn.configure(text="Connect", command=self._connect)

    def _on_speed_change(self, v):
        v = max(0.0, min(4.0, float(v)))
        self.engine.lfo_speed_mult = v
        self._speed_label.configure(text=f"{v:.2f}×")

    def _apply_sources(self):
        try:
            n = int(self.num_var.get())
            self.sources_page.set_num_sources(n)
            self._rebuild_chaos_tab()
            self.midi_page._refresh_index_dropdown()
        except (ValueError, tk.TclError):
            pass

    def _on_lfo_count_change(self, value: str):
        try:
            n = max(1, min(len(self.engine.lfos), int(value)))
        except ValueError:
            return
        self.engine.num_active_lfos = n
        self.lfo_count_var.set(str(n))
        self._rebuild_lfo_tab()
        self._rebuild_chaos_tab()
        self.midi_page._refresh_index_dropdown()

    def _rebuild_lfo_tab(self):
        lfo_tab = self.tabs.tab("LFOs")
        for child in lfo_tab.winfo_children():
            child.destroy()
        LFOPage(lfo_tab, self.engine).pack(fill=tk.BOTH, expand=True)

    def _rebuild_chaos_tab(self):
        chaos_tab = self.tabs.tab("Chaos")
        for child in chaos_tab.winfo_children():
            child.destroy()
        self.chaos_page = ChaosPage(chaos_tab, self.engine)
        self.chaos_page.pack(fill=tk.BOTH, expand=True)

    def _build_preset_bar(self):
        self.preset_bar = PresetBar(self, self.preset_mgr, self.midi_engine, on_load_cb=self._apply_preset)
        self.preset_bar.pack(fill=tk.X, pady=(4, 0))

    def _apply_preset(self, name: str):
        data = self.preset_mgr.load(name)
        n    = data.get("num_active", self.engine.num_active)
        lfo_n = data.get("num_active_lfos", self.engine.num_active_lfos)

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

        # Load chaos sliders and keep the fixed three sliders as fallback for older presets.
        chaos_data = data.get("chaos_sliders")
        self.engine.chaos_system.set_sliders_from_data(chaos_data)

        # Rebuild pages so widgets re-read the updated model
        self.num_var.set(str(n))
        self.sources_page.set_num_sources(n)
        self.engine.num_active_lfos = max(1, min(len(self.engine.lfos), int(lfo_n)))
        self.lfo_count_var.set(str(self.engine.num_active_lfos))

        self._rebuild_lfo_tab()
        self._rebuild_chaos_tab()

        self.midi_page._refresh_index_dropdown()

        # load MIDI mappings from preset so they work immediately if device is connected
        midi_data = data.get("midi_mappings", [])
        self.midi_engine.mappings = [MidiMapping.from_dict(d) for d in midi_data]
        self.midi_page._refresh_table()

    def on_close(self):
        self.midi_engine.disconnect()
        self.engine.stop()
        plt.close("all")
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
