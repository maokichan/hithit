"""tkinter 图形界面：模拟 / 画像拟合 / 假设 三页。

设计原则（2026-10-05 与需求方对齐）：
- 目标函数是**经济性**（钱）。命中率只是形成成本的机制之一——丢弃信息同样省钱，
  因此默认提供"丢弃-经济曲线"（历史保留量 → 每请求成本）直接回答"丢多少最划算"。
- **参数即输入、折线即输出**：改任何参数自动重算重画，没有"运行/扫描"步骤。
- 参数用业务语言（群数量、@ 频率、系统提示长度、缓存保留分钟数）。
- 建模自带假设单列一页，解读任何结论前先读它。

用法：python3 -m hithit.gui
"""
from __future__ import annotations

import ctypes
import dataclasses
import json
import queue
import sys
import threading
import tkinter as tk
from collections import deque
from tkinter import filedialog, messagebox, ttk

from .fit import fit_scenario
from .params import Scenario, load_scenario, scenario_from_dict
from .simulate import run, run_average, with_policy

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

COLORS = ("#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed")
PLOT_HINT = "悬停读数 · 拖动平移 · 滚轮缩放 · 双击复位"
METRICS = ("累计成本", "每请求成本", "命中率")
SPEEDS = {"1x": 1, "2x": 2, "4x": 4, "8x": 8}
ECON_POINTS = 14
AUTO_MS = 280  # 参数变化后的防抖
ROUND_LIMIT = 8000  # 单次重算的总轮数上限：超过则拒绝重算（防 UI 卡死），给出调整建议

ASSUMPTIONS_TEXT = """建模自带假设（解读任何结论前先读）

1. 到达 = 泊松（指数间隔）：无突发、无昼夜周期、各轮独立。
   影响：真实群聊的连发与作息会让"间歇活跃群"的 TTL 损耗比模拟更差；
   模拟给出的是偏乐观的平滑世界。

2. 长度 = 对数正态、逐轮独立采样（cv=1）：历史之间无相关性。
   影响：历史增长是理想均匀的"每月长这么多"，真实话题聚集会造成偏差。

3. 触发时机 = 外生负载参数："@ 频率"是你给的，不是被评估的策略。
   主动回复频率、防抖、作息窗口目前**不能**被建模评估——要回答
   "什么时候主动发请求最经济"，需要把触发策略内生化（已知建模缺口）。

4. 缓存 = 容量无限、单档 TTL、路由必中（α=1）、无峰谷价、无跨账号差异。
   影响：对 DeepSeek（磁盘缓存）近似成立；对内存缓存厂商偏乐观。

5. 目标函数 = 经济性（钱），不是命中率：命中率高不等于省钱——
   丢弃历史让 prompt 变短，即使全价也可能更便宜。看"每请求成本"下结论，
   命中率只用来解释成本为什么是这样。

6. 比较的基线只有两个参照策略（直通 = 全保留、滑窗 = 超限即丢），
   它们是验证工具用的标尺，不是推荐方案；真实策略（冻结块链等）待接入。

共同词汇见 docs/05-词表.md（"先入表再使用"）。
"""

ASSUME_TITLE = ("Microsoft YaHei UI", 10)


def _moving_avg(ys: list[float], w: int) -> list[float]:
    if w <= 1:
        return ys
    out, acc, q = [], 0.0, deque()
    for y in ys:
        q.append(y)
        acc += y
        if len(q) > w:
            acc -= q.popleft()
        out.append(acc / len(q))
    return out


class PlotWidget(tk.Canvas):
    """折线图控件：多条曲线、悬停读数、拖动平移、滚轮缩放、双击复位、逐步揭示（回放）。"""

    MAX_POINTS = 600

    def __init__(self, parent, height=None):
        super().__init__(parent, bg="white", highlightthickness=0, cursor="crosshair")
        if height:
            self.configure(height=height)
        self.series: dict[str, list[tuple[float, float]]] = {}
        self.xlabel = ""
        self.yname = ""
        self.view = None
        self.reveal = None
        self._plot = None
        self._pts: list[tuple] = []
        self._drag = None
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Motion>", self._on_motion)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Double-Button-1>", self._on_reset)

    def set_series(self, series: dict, xlabel: str = "", yname: str = "") -> None:
        thinned = {}
        for name, s in series.items():
            if len(s) > self.MAX_POINTS:
                k = -(-len(s) // self.MAX_POINTS)
                s = s[::k]
            thinned[name] = s
        self.series = thinned
        self.xlabel = xlabel
        self.yname = yname
        self.view = None
        self.reveal = None
        self._draw()

    def set_reveal(self, n) -> None:
        self.reveal = n
        self._draw()

    def n_points(self) -> int:
        return min((len(s) for s in self.series.values()), default=0)

    def _visible(self) -> dict:
        if self.reveal is None:
            return self.series
        return {name: s[:max(0, self.reveal)] for name, s in self.series.items()}

    def _draw(self) -> None:
        c = self
        c.delete("all")
        self._pts = []
        series = self._visible()
        pts = [p for s in series.values() for p in s]
        if not pts:
            self._plot = None
            return
        w = max(self.winfo_width(), 120)
        h = max(self.winfo_height(), 120)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        if self.reveal is not None or self.view is None:
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
            if x1 <= x0:
                x1 = x0 + 1
            if y1 <= y0:
                y0, y1 = y0 - 1e-12, y1 + 1e-12
            self.view = (x0, x1, y0, y1)
        x0, x1, y0, y1 = self.view
        pad = 56
        plot_w = w - pad - 16
        plot_h = h - 52
        self._plot = {"pad": pad, "w": w, "h": h, "plot_w": plot_w, "plot_h": plot_h}

        def X(x: float) -> float:
            return pad + (x - x0) / (x1 - x0) * plot_w

        def Y(y: float) -> float:
            return h - 28 - (y - y0) / (y1 - y0) * plot_h

        for i in range(5):
            gy = 20 + plot_h * i / 4
            gv = y1 - (y1 - y0) * i / 4
            c.create_line(pad, gy, w - 16, gy, fill="#ededed")
            c.create_text(pad - 6, gy, text=f"{gv:.4g}", anchor="e", font=("", 8))
        c.create_line(pad, 20, pad, h - 28, fill="#777")
        c.create_line(pad, h - 28, w - 16, h - 28, fill="#777")
        c.create_text(w - 16, h - 12, text=f"{x1:.4g}", anchor="e", font=("", 8))
        c.create_text(pad, h - 12, text=f"{x0:.4g}", anchor="w", font=("", 8))
        c.create_text((pad + w - 16) / 2, h - 12, text=self.xlabel, fill="#555", font=("", 8))
        for i, (name, s) in enumerate(series.items()):
            color = COLORS[i % len(COLORS)]
            last = None
            for x, y in s:
                px, py = X(x), Y(y)
                self._pts.append((name, x, y, px, py))
                if last:
                    c.create_line(*last, px, py, fill=color, width=2)
                c.create_oval(px - 2, py - 2, px + 2, py + 2, fill=color, outline="")
                last = (px, py)
            c.create_text(w - 170, 12 + i * 16, text=f"■ {name}", anchor="w", fill=color, font=("", 9))
        c.create_text(pad + 4, 8, text=PLOT_HINT, anchor="w", fill="#aaa", font=("", 8))

    def _on_motion(self, e) -> None:
        if self._drag is not None or self._plot is None:
            return
        c = self
        c.delete("hover")
        best = None
        for name, x, y, px, py in self._pts:
            d = (px - e.x) ** 2 + (py - e.y) ** 2
            if best is None or d < best[0]:
                best = (d, name, x, y, px, py)
        if best and best[0] <= 18 ** 2:
            _, name, x, y, px, py = best
            P = self._plot
            c.create_line(px, 20, px, P["h"] - 28, fill="#c9c9c9", tags="hover")
            c.create_line(P["pad"], py, P["w"] - 16, py, fill="#c9c9c9", tags="hover")
            c.create_oval(px - 4, py - 4, px + 4, py + 4, outline="#333", tags="hover")
            c.create_text(min(px + 10, P["w"] - 150), max(py - 30, 6),
                          text=f"{name}\nx = {x:.6g}\n{self.yname} = {y:.6g}",
                          anchor="w", tags="hover", fill="#111", font=("", 9))

    def _on_press(self, e) -> None:
        self._drag = (e.x, e.y)
        self.delete("hover")

    def _on_drag(self, e) -> None:
        if self._drag is None or self._plot is None or self.reveal is not None:
            return
        P = self._plot
        dx = e.x - self._drag[0]
        dy = e.y - self._drag[1]
        x0, x1, y0, y1 = self.view
        dppx = (x1 - x0) / P["plot_w"]
        dppy = (y1 - y0) / P["plot_h"]
        self.view = (x0 - dx * dppx, x1 - dx * dppx, y0 + dy * dppy, y1 + dy * dppy)
        self._drag = (e.x, e.y)
        self._draw()

    def _on_release(self, e) -> None:
        self._drag = None

    def _on_wheel(self, e) -> None:
        if self._plot is None or self.reveal is not None:
            return
        P = self._plot
        f = 0.9 if e.delta > 0 else 1 / 0.9
        x0, x1, y0, y1 = self.view
        cx = x0 + (e.x - P["pad"]) / P["plot_w"] * (x1 - x0)
        cy = y0 + (P["h"] - 28 - e.y) / P["plot_h"] * (y1 - y0)
        self.view = (cx + (x0 - cx) * f, cx + (x1 - cx) * f,
                     cy + (y0 - cy) * f, cy + (y1 - cy) * f)
        self._draw()

    def _on_reset(self, e=None) -> None:
        if self.reveal is None:
            self.view = None
            self._draw()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("hithit — 缓存经济建模")
        try:
            self.tk.call("tk", "scaling", self.winfo_fpixels("1i") / 72.0)
        except tk.TclError:
            pass
        self.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(self)
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("Treeview", rowheight=26, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1180, sw - 60), min(780, sh - 120)
        self.geometry(f"{int(w)}x{int(h)}+{int((sw - w) / 2)}+{int(max(24, (sh - h) / 3))}")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        self.tab_main = ttk.Frame(nb)
        self.tab_fit = ttk.Frame(nb)
        self.tab_assume = ttk.Frame(nb)
        nb.add(self.tab_main, text="模拟")
        nb.add(self.tab_fit, text="画像拟合")
        nb.add(self.tab_assume, text="假设（先读）")

        self._build_main()
        self._build_fit()
        self._build_assume()

        self.status = tk.StringVar(value="就绪：拖动左侧任何参数，右侧两条折线自动重算")
        ttk.Label(self, textvariable=self.status, anchor="w", padding=(8, 2)).pack(fill="x")

        self.traces: dict[str, list] = {}
        self._playing = False
        self._play_i = 0
        self._pending = None
        self._jobid = 0
        self._queue: queue.Queue = queue.Queue()
        self.after(80, self._poll)
        self.after(200, self._recompute)  # 打开即出默认折线

    # ---------- 参数面板（业务语言） ----------
    _SLIDERS = (
        # (var 名, 标签, 单位, min, max, 初值, int?)
        ("v_sessions", "会话（群）数量", "个", 1, 200, 30, True),
        ("v_period", "@ 频率：每多少分钟一轮", "分钟/轮/群", 1, 720, 15, True),
        ("v_hours", "模拟时长", "小时", 1, 72, 24, True),
        ("v_stable", "系统提示长度（角色卡等，跨群共享）", "token", 0, 8000, 1000, True),
        ("v_input", "每轮用户输入", "token", 10, 2000, 150, True),
        ("v_output", "每轮 bot 回复", "token", 10, 4000, 250, True),
        ("v_window", "历史保留上限（超出的丢掉；经济曲线横轴就是它）", "token", 0, 20000, 4000, True),
        ("v_ttl", "缓存保留（命中会续期）", "分钟", 1, 2880, 480, True),
        ("v_chunk", "缓存匹配粒度", "token", 1, 256, 64, True),
        ("v_mincache", "最短可缓存前缀", "token", 0, 4096, 256, True),
    )
    _PRICES = (
        ("v_p_in", "输入价（未命中，¥/1M tok）", "2.0"),
        ("v_p_hit", "命中价（¥/1M tok）", "0.03"),
        ("v_p_write", "写入价（含读取成本，¥/1M tok）", "2.0"),
        ("v_p_out", "输出价（¥/1M tok）", "4.0"),
    )

    def _build_main(self) -> None:
        f = self.tab_main
        paned = ttk.PanedWindow(f, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=6, pady=6)
        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=0)
        paned.add(right, weight=1)

        # 左：滚动参数面板
        outer = ttk.Frame(left)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, width=340, highlightthickness=0, bg="#f7f7f7")
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas, padding=4)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=max(e.width, 340)))

        def _wheel(e):
            canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

        self.slider_vars = {}
        for attr, label, unit, lo, hi, init, is_int in self._SLIDERS:
            var = tk.IntVar(value=init) if is_int else tk.DoubleVar(value=init)
            setattr(self, attr, var)
            self.slider_vars[attr] = var
            box = ttk.LabelFrame(inner, text=f"{label}（{unit}）", padding=(6, 1))
            box.pack(fill="x", pady=2)
            sc = tk.Scale(box, variable=var, from_=lo, to=hi, orient="horizontal",
                          resolution=1 if is_int else 0.5, showvalue=True)
            sc.pack(fill="x")
            var.trace_add("write", self._schedule)
        self.v_sessions: tk.IntVar
        self.v_period: tk.IntVar
        self.v_hours: tk.IntVar
        self.v_stable: tk.IntVar
        self.v_input: tk.IntVar
        self.v_output: tk.IntVar
        self.v_window: tk.IntVar
        self.v_ttl: tk.IntVar
        self.v_chunk: tk.IntVar
        self.v_mincache: tk.IntVar

        pbox = ttk.LabelFrame(inner, text="价目（¥ / 1M token；DeepSeek 示例价，用前复核）", padding=(6, 2))
        pbox.pack(fill="x", pady=2)
        for attr, label, init in self._PRICES:
            row = ttk.Frame(pbox)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=label, width=30).pack(side="left")
            var = tk.StringVar(value=init)
            setattr(self, attr, var)
            ttk.Entry(row, textvariable=var, width=8).pack(side="right")
            var.trace_add("write", self._schedule)
        self.v_p_in: tk.StringVar
        self.v_p_hit: tk.StringVar
        self.v_p_write: tk.StringVar
        self.v_p_out: tk.StringVar

        mbox = ttk.LabelFrame(inner, text="写入模式", padding=(6, 2))
        mbox.pack(fill="x", pady=2)
        self.v_mode = tk.StringVar(value="auto")
        ttk.Combobox(mbox, textvariable=self.v_mode, values=("auto", "explicit"),
                     state="readonly", width=10).pack(anchor="w")
        self.v_mode.trace_add("write", self._schedule)

        sbox = ttk.LabelFrame(inner, text="随机世界编号（seed）", padding=(6, 2))
        sbox.pack(fill="x", pady=2)
        row = ttk.Frame(sbox)
        row.pack(fill="x")
        self.v_seed = tk.IntVar(value=42)
        ttk.Spinbox(row, from_=0, to=10 ** 9, textvariable=self.v_seed, width=8).pack(side="left")
        ttk.Button(row, text="换一个世界", command=self._next_world).pack(side="left", padx=6)
        self.v_seed.trace_add("write", self._schedule)

        jbox = ttk.Frame(inner)
        jbox.pack(fill="x", pady=6)
        ttk.Button(jbox, text="从场景 JSON 载入…", command=self.on_load_json).pack(side="left")
        ttk.Label(inner, text="自动重算：拖动停止约 0.3 秒后刷新两条折线。\n"
                              "经济曲线横轴 = 「历史保留上限」滑块的范围同一含义。",
                  wraplength=320, justify="left", foreground="#555", font=("", 8)).pack(anchor="w", pady=4)
        self.est_label = ttk.Label(inner, text="", wraplength=320, justify="left",
                                   foreground="#b45309", font=("", 9))
        self.est_label.pack(anchor="w")

        def _bind_wheel(w):
            if isinstance(w, (ttk.Combobox, ttk.Spinbox, tk.Spinbox)):
                return
            w.bind("<MouseWheel>", _wheel)
            for ch in w.winfo_children():
                _bind_wheel(ch)

        _bind_wheel(inner)
        _bind_wheel(canvas)

        # 右：时序图 + 经济曲线
        vpan = ttk.PanedWindow(right, orient="vertical")
        vpan.pack(fill="both", expand=True)
        tzone = ttk.Frame(vpan)
        vpan.add(tzone, weight=3)
        bar = ttk.Frame(tzone, padding=(0, 2))
        bar.pack(fill="x")
        ttk.Label(bar, text="时序图:").pack(side="left")
        self.trace_metric = tk.StringVar(value=METRICS[0])
        ttk.Combobox(bar, textvariable=self.trace_metric, values=METRICS, width=10,
                     state="readonly").pack(side="left", padx=(2, 8))
        self.trace_metric.trace_add("write", lambda *_: self._update_trace_plot())
        self.trace_smooth = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="平滑", variable=self.trace_smooth,
                        command=self._update_trace_plot).pack(side="left")
        self.play_btn = ttk.Button(bar, text="▶ 回放", command=self._toggle_play)
        self.play_btn.pack(side="left", padx=8)
        ttk.Label(bar, text="倍速:").pack(side="left")
        self.trace_speed = tk.StringVar(value="4x")
        ttk.Combobox(bar, textvariable=self.trace_speed, values=tuple(SPEEDS), width=3,
                     state="readonly").pack(side="left", padx=(2, 0))
        self.plot_run = PlotWidget(tzone, height=200)
        self.plot_run.pack(fill="both", expand=True)

        econ = ttk.Frame(vpan)
        vpan.add(econ, weight=2)
        ttk.Label(econ, text="丢弃-经济曲线：历史保留多少最省钱？（蓝线最低点 = 最优丢弃量；红虚线 = 全保留参考）",
                  padding=(0, 2)).pack(anchor="w")
        self.plot_econ = PlotWidget(econ, height=170)
        self.plot_econ.pack(fill="both", expand=True)

    def _next_world(self) -> None:
        self.v_seed.set(self.v_seed.get() + 1)

    def _collect(self) -> Scenario:
        data = {
            "seed": int(self.v_seed.get()),
            "workload": {
                "n_sessions": int(self.v_sessions.get()),
                "sim_time": float(self.v_hours.get()) * 3600.0,
                "request_rate": 1.0 / (float(self.v_period.get()) * 60.0),
                "stable_prefix_tokens": float(self.v_stable.get()),
                "input_tokens_mean": float(self.v_input.get()),
                "input_tokens_cv": 1.0,
                "output_tokens_mean": float(self.v_output.get()),
                "output_tokens_cv": 1.0,
                "length_dist": "lognormal",
            },
            "mechanism": {
                "ttl": float(self.v_ttl.get()) * 60.0,
                "chunk_tokens": int(self.v_chunk.get()),
                "min_cacheable_tokens": float(self.v_mincache.get()),
                "mode": self.v_mode.get(),
                "max_breakpoints": 4,
            },
            "prices": {
                "input": float(self.v_p_in.get()),
                "cached_read": float(self.v_p_hit.get()),
                "cache_write": float(self.v_p_write.get()),
                "output": float(self.v_p_out.get()),
            },
            "policy": {
                "name": "sliding_window",
                "window_max_tokens": float(self.v_window.get()),
            },
        }
        return scenario_from_dict(data)

    def on_load_json(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            scen = load_scenario(path)
        except Exception as e:
            messagebox.showerror("加载失败", str(e))
            return
        wl = scen.workload
        hist = any(getattr(wl, f) is not None for f in
                   ("input_hist_edges", "input_hist_weights", "output_hist_edges",
                    "output_hist_weights", "gap_hist_edges", "gap_hist_weights"))
        if hist:
            messagebox.showwarning(
                "直方图场景", "该场景含直方图分布，业务面板不覆盖直方图；"
                             "已载入可载入的参数，直方图部分请走 CLI（hithit run）。")
        self.v_sessions.set(max(1, min(200, round(wl.n_sessions))))
        self.v_period.set(max(1, min(720, round(1.0 / (wl.request_rate * 60.0)))))
        self.v_hours.set(max(1, min(72, round(wl.sim_time / 3600.0))))
        self.v_stable.set(max(0, min(8000, round(wl.stable_prefix_tokens))))
        if wl.input_hist_edges is None:
            self.v_input.set(max(10, min(2000, round(wl.input_tokens_mean))))
        if wl.output_hist_edges is None:
            self.v_output.set(max(10, min(4000, round(wl.output_tokens_mean))))
        self.v_ttl.set(max(1, min(2880, round(scen.mechanism.ttl / 60.0))))
        self.v_chunk.set(max(1, min(256, scen.mechanism.chunk_tokens)))
        self.v_mincache.set(max(0, min(4096, round(scen.mechanism.min_cacheable_tokens))))
        self.v_mode.set(scen.mechanism.mode if scen.mechanism.mode in ("auto", "explicit") else "auto")
        self.v_seed.set(scen.seed)
        self._set_status(f"已从 JSON 载入：{path}")

    def _schedule(self, *_):
        est = self._estimate_rounds()
        self.est_label.config(
            text=f"预估轮数：{est:,}" + (f"（超上限 {ROUND_LIMIT}，请调小参数）" if est > ROUND_LIMIT else "，在可算范围内"))
        if self._pending is not None:
            self.after_cancel(self._pending)
        self._pending = self.after(AUTO_MS, self._recompute)

    def _estimate_rounds(self) -> int:
        try:
            return int(self.v_sessions.get() * self.v_hours.get() * 3600.0
                       / max(1e-9, self.v_period.get() * 60.0))
        except Exception:
            return 0

    # ---------- 自动重算（后台线程，UI 不冻结） ----------
    def _recompute(self) -> None:
        self._pending = None
        try:
            scen = self._collect()
        except Exception as e:
            self._set_status(f"参数无效：{e}")
            return
        est = self._estimate_rounds()
        if est > ROUND_LIMIT:
            self._set_status(f"预估 {est:,} 轮，超上限 {ROUND_LIMIT}：请调小「模拟时长 / 群数量」，或调低 @ 频率")
            return
        self._jobid += 1
        self._set_status(f"计算中：预估 {est:,} 轮 × 2 策略 + 经济曲线 …")
        threading.Thread(target=self._worker, args=(self._jobid, scen), daemon=True).start()

    def _worker(self, job: int, scen: Scenario) -> None:
        try:
            traces: dict[str, list] = {}
            for n in ("passthrough", "sliding_window"):
                tr: list = []
                run(with_policy(scen, n), trace=tr)
                traces[n] = tr
            xs, ys, y_full = self._economy_curve(scen)
            self._queue.put((job, traces, xs, ys, y_full))
        except Exception as e:
            self._queue.put((job, None, None, None, str(e)))

    def _poll(self) -> None:
        try:
            while True:
                job, traces, xs, ys, y_full = self._queue.get_nowait()
                if job != self._jobid:
                    continue  # 过期结果（参数又变了）直接丢弃
                if traces is None:
                    self._set_status(f"计算失败：{y_full}")
                    continue
                self._stop_play(reset=True)
                self.traces = traces
                self._update_trace_plot()
                series = {
                    "滑窗（丢弃超出部分）": list(zip(xs, ys)),
                    "直通（全保留）参考线": [(xs[0], y_full), (xs[-1], y_full)],
                }
                self.plot_econ.set_series(series, xlabel="历史保留上限（token；0 = 全部丢弃）", yname="每请求成本（¥）")
                self._set_status(f"已重算：{len(next(iter(traces.values()))):,} 轮 × 2 策略 + 经济曲线 {len(xs)} 点")
        except queue.Empty:
            pass
        self.after(60, self._poll)

    def _economy_curve(self, scen: Scenario):
        span = (scen.workload.stable_prefix_tokens * 2
                + 8 * (scen.workload.input_tokens_mean + scen.workload.output_tokens_mean))
        xs = [span * i / (ECON_POINTS - 1) for i in range(ECON_POINTS)]
        ys = []
        for x in xs:
            sc = dataclasses.replace(scen, policy=dataclasses.replace(scen.policy, window_max_tokens=x))
            r = run_average(with_policy(sc, "sliding_window"), 1)
            ys.append(r.cost_total / max(1, r.n_requests))
        r = run_average(with_policy(scen, "passthrough"), 1)
        return xs, ys, r.cost_total / max(1, r.n_requests)

    # ---------- 时序图与回放 ----------
    def _update_trace_plot(self) -> None:
        if not self.traces:
            return
        self._stop_play(reset=True)
        n = min(len(t) for t in self.traces.values())
        metric = self.trace_metric.get()
        series = {}
        for name, tr in self.traces.items():
            tr = tr[:n]
            if metric == "累计成本":
                ys, acc = [], 0.0
                for r in tr:
                    acc += r["cost"]
                    ys.append(acc)
            elif metric == "命中率":
                ys = [r["hit"] for r in tr]
            else:
                ys = [r["cost"] for r in tr]
            if self.trace_smooth.get():
                ys = _moving_avg(ys, max(1, n // 50))
            series[name] = list(zip(range(n), ys))
        self.plot_run.set_series(series, xlabel="轮次（全部会话按时间混合）", yname=metric)

    def _toggle_play(self) -> None:
        if self._playing:
            self._stop_play(reset=False)
            return
        if not self.traces:
            return
        if self._play_i >= self.plot_run.n_points():
            self._play_i = 0
        self._playing = True
        self.play_btn.config(text="⏸ 暂停")
        self._play_step()

    def _play_step(self) -> None:
        if not self._playing:
            return
        speed = SPEEDS.get(self.trace_speed.get(), 2)
        total = self.plot_run.n_points()
        self._play_i = min(self._play_i + speed, total)
        self.plot_run.set_reveal(self._play_i)
        if self._play_i >= total:
            self._stop_play(reset=True)
        else:
            self.after(40, self._play_step)

    def _stop_play(self, reset=True) -> None:
        self._playing = False
        self.play_btn.config(text="▶ 回放")
        if reset:
            self._play_i = 0
            if hasattr(self, "plot_run"):
                self.plot_run.set_reveal(None)

    # ---------- 拟合页 ----------
    def _build_fit(self) -> None:
        f = self.tab_fit
        top = ttk.Frame(f, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="画像 JSON:").pack(side="left")
        self.fit_profile = ttk.Entry(top)
        self.fit_profile.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="浏览…", command=self._pick_profile).pack(side="left")
        row2 = ttk.Frame(f, padding=(8, 4))
        row2.pack(fill="x")
        ttk.Label(row2, text="系统提示长度 tokens:").pack(side="left")
        self.fit_stable = tk.StringVar(value="")
        ttk.Entry(row2, textvariable=self.fit_stable, width=10).pack(side="left", padx=(2, 10))
        ttk.Button(row2, text="拟合预览", command=self.on_fit_preview).pack(side="left")
        ttk.Button(row2, text="保存场景 JSON…", command=self.on_fit_save).pack(side="left", padx=8)
        self.fit_text = tk.Text(f, height=24)
        self.fit_text.pack(fill="both", expand=True, padx=8, pady=8)
        self.fit_result: dict | None = None

    def _pick_profile(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if path:
            self.fit_profile.delete(0, "end")
            self.fit_profile.insert(0, path)

    def on_fit_preview(self) -> None:
        try:
            with open(self.fit_profile.get().strip(), encoding="utf-8") as fh:
                profile = json.load(fh)
            stable = self.fit_stable.get().strip()
            scenario, warnings = fit_scenario(profile, float(stable) if stable else None)
        except Exception as e:
            messagebox.showerror("拟合失败", str(e))
            return
        self.fit_result = scenario
        w = scenario["workload"]
        lines = [
            f"会话数 {w['n_sessions']}   模拟窗口 {w['sim_time'] / 3600:.1f} h   请求率 {w['request_rate']:.5f}/s",
            f"活跃度 cv {w['rate_heterogeneity_cv']}   稳定前缀 {w.get('stable_prefix_tokens', 1000):.0f} tokens",
            "",
        ] + [f"注意: {x}" for x in warnings] + ["", json.dumps(scenario, ensure_ascii=False, indent=1)]
        self.fit_text.delete("1.0", "end")
        self.fit_text.insert("1.0", "\n".join(lines))
        self._set_status("拟合预览完成，可保存场景 JSON（保存后在模拟页「从场景 JSON 载入」）")

    def on_fit_save(self) -> None:
        if self.fit_result is None:
            messagebox.showinfo("提示", "先做拟合预览")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("JSON", "*.json")], initialfile="scenario.json")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.fit_result, f, ensure_ascii=False, indent=1)
        self._set_status(f"场景已保存: {path}")

    # ---------- 假设页 ----------
    def _build_assume(self) -> None:
        f = self.tab_assume
        txt = tk.Text(f, wrap="word", padx=16, pady=12, relief="flat", bg="#fbfbf7")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", ASSUMPTIONS_TEXT)
        txt.configure(state="disabled")

    # ---------- 状态栏 ----------
    def _set_status(self, text: str) -> None:
        self.status.set(text)
        self.update_idletasks()


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
