"""tkinter 图形界面：运行 / 扫描 / 画像拟合 三个页签。

用法：python3 -m hithit.gui
需要 python3-tk 与显示服务器（本机 headless 可 ssh -X，或把仓库克隆到桌面机运行）。

交互：
- 运行页左侧参数编辑器可手动改全部标量参数（负载/机制/价目/策略），打开即可改即跑；
  直方图字段（*_hist_*）不在编辑器内，含直方图的场景请走 JSON 路径。
- 扫描页折线图：悬停显示最近数据点数值；按住拖动平移；滚轮缩放；双击复位。
"""
from __future__ import annotations

import dataclasses
import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .fit import fit_scenario
from .params import (CacheMechanism, PolicyConfig, PriceTable, Scenario,
                     Workload, load_scenario, scenario_from_dict)
from .simulate import run_average, run_sweep, with_policy

POLICIES = ("passthrough", "sliding_window")
POLICY_HELP = {
    "passthrough": "全历史直通：每轮 prompt = 稳定前缀 + 全部历史 + 本轮新输入。前缀只追加不变 → 命中率最高；但 prompt 随会话无限变长，每请求成本持续上升（基线 1）。",
    "sliding_window": "滑动窗口：历史超出 window_max_tokens 就截掉最老部分。prompt 长度封顶；但每次截断 = 前缀断裂（从截断点起全 miss 重建），命中率低于直通（基线 2）。",
}
COLORS = ("#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed")
TREE_COLS = ("策略", "请求数", "命中率", "未命中输入", "命中读", "写入", "输出", "总成本", "每请求成本")
HIST_FIELDS = ("input_hist_edges", "input_hist_weights", "output_hist_edges",
               "output_hist_weights", "gap_hist_edges", "gap_hist_weights")
_SECTIONS = (("负载 workload", Workload, "workload"),
             ("机制 mechanism", CacheMechanism, "mechanism"),
             ("价目 prices（币种 / 1M token）", PriceTable, "prices"),
             ("策略 policy", PolicyConfig, "policy"))
_CONV = {"int": int, "float": float, "str": str}


def _result_row(name, r) -> list:
    return [
        name,
        int(r.n_requests),
        f"{r.hit_ratio:.1%}",
        f"{r.cost_input:.4g}",
        f"{r.cost_cached:.4g}",
        f"{r.cost_write:.4g}",
        f"{r.cost_output:.4g}",
        f"{r.cost_total:.4g}",
        f"{r.cost_total / r.n_requests:.4g}" if r.n_requests else "-",
    ]


def _fmt(v) -> str:
    return f"{v:.8g}" if isinstance(v, float) else str(v)


def _sweepable_params() -> list[str]:
    out = []
    for _, cls, prefix in _SECTIONS:
        for f in dataclasses.fields(cls):
            if f.type in ("int", "float"):
                out.append(f"{prefix}.{f.name}")
    return out


SWEEP_PARAMS = _sweepable_params()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("hithit — 缓存命中建模")
        self.geometry("1120x720")
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        self.tab_run = ttk.Frame(nb)
        self.tab_sweep = ttk.Frame(nb)
        self.tab_fit = ttk.Frame(nb)
        nb.add(self.tab_run, text="运行")
        nb.add(self.tab_sweep, text="扫描")
        nb.add(self.tab_fit, text="画像拟合")
        self._build_run()
        self._build_sweep()
        self._build_fit()
        self.status = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self.status, anchor="w", padding=(8, 2)).pack(fill="x")
        # 绘图视图状态：(x0, x1, y0, y1) 数据坐标；None = 自动适配
        self.view = None
        self._plot = None
        self._pts: list[tuple] = []
        self._drag = None

    def _set_status(self, text: str) -> None:
        self.status.set(text)
        self.update_idletasks()

    def _pick_file(self, entry: ttk.Entry) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if path:
            entry.delete(0, "end")
            entry.insert(0, path)

    @staticmethod
    def _make_tree(parent, cols) -> ttk.Treeview:
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=cols, show="headings", height=11)
        for i, c in enumerate(cols):
            tree.heading(c, text=c)
            tree.column(c, width=110, anchor="w" if i == 0 else "e")
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=ysb.set)
        tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="left", fill="y")
        return tree

    # ---------- 运行页 ----------
    def _build_run(self) -> None:
        f = self.tab_run
        top = ttk.Frame(f, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="场景 JSON:").pack(side="left")
        self.run_scenario = ttk.Entry(top)
        self.run_scenario.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="浏览…", command=lambda: self._pick_file(self.run_scenario)).pack(side="left")
        ttk.Button(top, text="载入到编辑器", command=self.on_load_editor).pack(side="left", padx=(6, 0))

        opts = ttk.Frame(f, padding=(8, 0))
        opts.pack(fill="x")
        self.run_compare = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="对比全部参照策略", variable=self.run_compare).pack(side="left")
        ttk.Label(opts, text="种子数:").pack(side="left", padx=(12, 2))
        self.run_seeds = tk.StringVar(value="3")
        ttk.Spinbox(opts, from_=1, to=50, textvariable=self.run_seeds, width=5).pack(side="left")
        ttk.Button(opts, text="运行（JSON）", command=self.on_run_json).pack(side="left", padx=12)

        paned = ttk.PanedWindow(f, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=0)
        paned.add(right, weight=1)
        self._build_editor(left)
        self.run_tree = self._make_tree(right, TREE_COLS)

    def _build_editor(self, parent) -> None:
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, width=370, highlightthickness=0, bg="#f7f7f7")
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas, padding=4)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=max(e.width, 370)))

        def _wheel(e):
            canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

        defaults = Scenario()
        self.ed_vars: dict[str, list] = {}
        for title, cls, key in _SECTIONS:
            box = ttk.LabelFrame(inner, text=title, padding=4)
            box.pack(fill="x", pady=3)
            section = getattr(defaults, key)
            self.ed_vars[key] = []
            extra_row = 0
            for fd in dataclasses.fields(cls):
                if fd.type not in _CONV:
                    continue
                conv = _CONV[fd.type]
                var = tk.StringVar(value=_fmt(getattr(section, fd.name)))
                ttk.Label(box, text=fd.name).grid(row=extra_row, column=0, sticky="w", padx=2, pady=1)
                if key == "policy" and fd.name == "name":
                    w = ttk.Combobox(box, textvariable=var, values=POLICIES, width=10)
                    w.bind("<<ComboboxSelected>>", lambda e: self._update_policy_help())
                else:
                    w = ttk.Entry(box, textvariable=var, width=12)
                w.grid(row=extra_row, column=1, sticky="ew", padx=2, pady=1)
                box.columnconfigure(1, weight=1)
                self.ed_vars[key].append((fd.name, conv, var))
                extra_row += 1
            if key == "workload":
                self._hint(box, "直方图字段（*_hist_*）不在编辑器：含直方图的场景请用「运行（JSON）」。",
                           row=extra_row, column=0, columnspan=2, sticky="w", pady=(4, 0))
            elif key == "prices":
                self._hint(box, "cache_write 含所写 token 的读取成本：无写溢价设 = input；"
                                "1.25× 写溢价方案设 = 1.25×input。",
                           row=extra_row, column=0, columnspan=2, sticky="w", pady=(4, 0))
            elif key == "policy":
                self.policy_help = self._hint(box, POLICY_HELP[defaults.policy.name],
                                              row=extra_row, column=0, columnspan=2, sticky="w", pady=(4, 0))

        sbox = ttk.LabelFrame(inner, text="随机种子 seed", padding=4)
        sbox.pack(fill="x", pady=3)
        self.ed_seed = tk.StringVar(value=str(defaults.seed))
        ttk.Spinbox(sbox, from_=0, to=10 ** 9, textvariable=self.ed_seed, width=12).pack(anchor="w")

        btns = ttk.Frame(inner)
        btns.pack(fill="x", pady=6)
        ttk.Button(btns, text="▶ 运行（编辑器参数）", command=self.on_run_editor).pack(side="left", padx=2)
        ttk.Button(btns, text="保存编辑器为 JSON…", command=self.on_save_editor).pack(side="left", padx=2)

        def _bind_wheel(w):
            if isinstance(w, (ttk.Combobox, ttk.Spinbox, tk.Spinbox)):
                return
            w.bind("<MouseWheel>", _wheel)
            for ch in w.winfo_children():
                _bind_wheel(ch)

        _bind_wheel(inner)
        _bind_wheel(canvas)

    @staticmethod
    def _hint(parent, text, **grid) -> tk.Label:
        lbl = tk.Label(parent, text=text, wraplength=330, justify="left", fg="#555", font=("", 8))
        lbl.grid(**grid)
        return lbl

    def _update_policy_help(self) -> None:
        name = self.ed_vars["policy"][0][2].get().strip()  # policy.name 的 var
        self.policy_help.config(text=POLICY_HELP.get(
            name, f"「{name}」不是内置参照策略；内置仅 passthrough / sliding_window，自定义策略经策略接口接入后可用。"))

    def _fill_editor(self, scen: Scenario) -> None:
        for key, items in self.ed_vars.items():
            section = getattr(scen, key)
            for fname, _conv, var in items:
                var.set(_fmt(getattr(section, fname)))
        self.ed_seed.set(str(scen.seed))
        self._update_policy_help()

    def _collect_editor(self) -> dict:
        data = {}
        for key, items in self.ed_vars.items():
            vals = {}
            for fname, conv, var in items:
                raw = var.get().strip()
                try:
                    vals[fname] = conv(raw)
                except (TypeError, ValueError):
                    raise ValueError(f"{key}.{fname}: {raw!r} 不是合法 {conv.__name__}")
            data[key] = vals
        try:
            data["seed"] = int(self.ed_seed.get())
        except ValueError:
            raise ValueError(f"seed: {self.ed_seed.get()!r} 不是合法 int")
        return data

    def _get_seeds(self) -> int:
        try:
            return max(1, int(self.run_seeds.get()))
        except ValueError:
            raise ValueError("种子数不是整数")

    def on_load_editor(self) -> None:
        try:
            scen = load_scenario(self.run_scenario.get().strip())
        except Exception as e:
            messagebox.showerror("加载失败", str(e))
            return
        self._fill_editor(scen)
        if any(getattr(scen.workload, f) is not None for f in HIST_FIELDS):
            messagebox.showwarning(
                "直方图字段",
                "该场景含直方图字段（*_hist_*），编辑器不覆盖它们。\n"
                "用「运行（编辑器参数）」会丢掉直方图、退回参数化分布；要保留直方图请直接「运行（JSON）」。")
        self._set_status("已载入编辑器")

    def on_run_editor(self) -> None:
        try:
            scen = scenario_from_dict(self._collect_editor())
            seeds = self._get_seeds()
        except Exception as e:
            messagebox.showerror("参数错误", str(e))
            return
        names = POLICIES if self.run_compare.get() else (scen.policy.name,)
        self._run_and_fill(scen, names, seeds)

    def on_run_json(self) -> None:
        try:
            scen = load_scenario(self.run_scenario.get().strip())
            seeds = self._get_seeds()
        except Exception as e:
            messagebox.showerror("加载失败", str(e))
            return
        names = POLICIES if self.run_compare.get() else (scen.policy.name,)
        self._run_and_fill(scen, names, seeds)

    def on_save_editor(self) -> None:
        try:
            data = self._collect_editor()
        except Exception as e:
            messagebox.showerror("参数错误", str(e))
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("JSON", "*.json")], initialfile="scenario.json")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        self._set_status(f"场景已保存: {path}")

    def _run_and_fill(self, scen, names, seeds) -> None:
        self._set_status(f"运行中：{len(names)} 个策略 × {seeds} 种子 …")
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            results = {n: run_average(with_policy(scen, n), seeds) for n in names}
        except Exception as e:
            self.config(cursor="")
            self._set_status("失败")
            messagebox.showerror("运行失败", str(e))
            return
        self.config(cursor="")
        tree = self.run_tree
        tree.delete(*tree.get_children())
        for n, r in results.items():
            tree.insert("", "end", values=_result_row(n, r))
        self._set_status(f"完成：{len(names)} 个策略，每次运行 {int(results[names[0]].n_requests)} 请求")

    # ---------- 扫描页 ----------
    def _build_sweep(self) -> None:
        f = self.tab_sweep
        top = ttk.Frame(f, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="场景 JSON:").pack(side="left")
        self.sw_scenario = ttk.Entry(top)
        self.sw_scenario.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="浏览…", command=lambda: self._pick_file(self.sw_scenario)).pack(side="left")
        row2 = ttk.Frame(f, padding=(8, 4))
        row2.pack(fill="x")
        self.sw_param = tk.StringVar(value="mechanism.ttl")
        ttk.Label(row2, text="参数:").pack(side="left")
        ttk.Combobox(row2, textvariable=self.sw_param, values=SWEEP_PARAMS, width=26).pack(side="left", padx=(2, 10))
        for label, attr, default, width in (
            ("起", "sw_start", "0", 8),
            ("止", "sw_stop", "3600", 8),
            ("步数", "sw_steps", "6", 5),
            ("种子", "sw_seeds", "2", 4),
            ("策略", "sw_policies", ",".join(POLICIES), 30),
        ):
            ttk.Label(row2, text=label + ":").pack(side="left")
            var = tk.StringVar(value=default)
            ttk.Entry(row2, textvariable=var, width=width).pack(side="left", padx=(2, 10))
            setattr(self, attr, var)
        ttk.Button(row2, text="扫描并绘图", command=self.on_sweep).pack(side="left")
        self.sw_tree = self._make_tree(f, ("value", "policy", "hit_ratio", "cost_total", "cost_per_req"))
        self.sw_canvas = tk.Canvas(f, height=240, bg="white", highlightthickness=0, cursor="crosshair")
        self.sw_canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.sw_series: dict[str, list[tuple[float, float]]] = {}
        self._last_param = ""
        self.sw_canvas.bind("<Configure>", lambda e: self._draw_series())
        self.sw_canvas.bind("<Motion>", self._plot_motion)
        self.sw_canvas.bind("<ButtonPress-1>", self._plot_press)
        self.sw_canvas.bind("<B1-Motion>", self._plot_drag)
        self.sw_canvas.bind("<ButtonRelease-1>", self._plot_release)
        self.sw_canvas.bind("<MouseWheel>", self._plot_wheel)
        self.sw_canvas.bind("<Double-Button-1>", self._plot_reset)

    def on_sweep(self) -> None:
        try:
            scen = load_scenario(self.sw_scenario.get().strip())
            rows = run_sweep(
                scen,
                self.sw_param.get().strip(),
                float(self.sw_start.get()),
                float(self.sw_stop.get()),
                int(self.sw_steps.get()),
                [p.strip() for p in self.sw_policies.get().split(",")],
                max(1, int(self.sw_seeds.get())),
            )
        except Exception as e:
            messagebox.showerror("扫描失败", str(e))
            return
        tree = self.sw_tree
        tree.delete(*tree.get_children())
        for row in rows:
            tree.insert("", "end", values=[
                row["value"], row["policy"], row["hit_ratio"], row["cost_total"], row["cost_per_req"]])
        series: dict[str, list[tuple[float, float]]] = {}
        for row in rows:
            if row["cost_per_req"] is not None:
                series.setdefault(row["policy"], []).append((row["value"], row["cost_per_req"]))
        self.sw_series = series
        self._last_param = self.sw_param.get().strip()
        self.view = None  # 新数据重新自动适配
        self._draw_series()
        self._set_status(f"扫描完成：{len(rows)} 行（图：悬停读数 · 拖动平移 · 滚轮缩放 · 双击复位）")

    def _draw_series(self) -> None:
        c = self.sw_canvas
        c.delete("all")
        self._pts = []
        series = self.sw_series
        if not series:
            self._plot = None
            return
        w = max(c.winfo_width(), 120)
        h = max(c.winfo_height(), 120)
        pts = [p for s in series.values() for p in s]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        if self.view is None:
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
        c.create_text((pad + w - 16) / 2, h - 12, text=self._last_param or "扫描参数", fill="#555", font=("", 8))
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
            c.create_text(w - 150, 12 + i * 16, text=f"■ {name} 每请求成本", anchor="w", fill=color, font=("", 9))
        c.create_text(pad + 4, 8, text="悬停读数 · 拖动平移 · 滚轮缩放 · 双击复位", anchor="w", fill="#aaa", font=("", 8))

    def _plot_motion(self, e) -> None:
        if self._drag is not None or self._plot is None:
            return
        c = self.sw_canvas
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
                          text=f"{name}\n{self._last_param} = {x:.6g}\n每请求成本 = {y:.6g}",
                          anchor="w", tags="hover", fill="#111", font=("", 9))

    def _plot_press(self, e) -> None:
        self._drag = (e.x, e.y)
        self.sw_canvas.delete("hover")

    def _plot_drag(self, e) -> None:
        if self._drag is None or self._plot is None:
            return
        P = self._plot
        dx = e.x - self._drag[0]
        dy = e.y - self._drag[1]
        x0, x1, y0, y1 = self.view
        dppx = (x1 - x0) / P["plot_w"]
        dppy = (y1 - y0) / P["plot_h"]
        self.view = (x0 - dx * dppx, x1 - dx * dppx, y0 + dy * dppy, y1 + dy * dppy)
        self._drag = (e.x, e.y)
        self._draw_series()

    def _plot_release(self, e) -> None:
        self._drag = None

    def _plot_wheel(self, e) -> None:
        if self._plot is None:
            return
        P = self._plot
        f = 0.9 if e.delta > 0 else 1 / 0.9
        x0, x1, y0, y1 = self.view
        cx = x0 + (e.x - P["pad"]) / P["plot_w"] * (x1 - x0)
        cy = y0 + (P["h"] - 28 - e.y) / P["plot_h"] * (y1 - y0)
        self.view = (cx + (x0 - cx) * f, cx + (x1 - cx) * f,
                     cy + (y0 - cy) * f, cy + (y1 - cy) * f)
        self._draw_series()

    def _plot_reset(self, e=None) -> None:
        self.view = None
        self._draw_series()

    # ---------- 拟合页 ----------
    def _build_fit(self) -> None:
        f = self.tab_fit
        top = ttk.Frame(f, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="画像 JSON:").pack(side="left")
        self.fit_profile = ttk.Entry(top)
        self.fit_profile.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="浏览…", command=lambda: self._pick_file(self.fit_profile)).pack(side="left")
        row2 = ttk.Frame(f, padding=(8, 4))
        row2.pack(fill="x")
        ttk.Label(row2, text="稳定前缀 tokens:").pack(side="left")
        self.fit_stable = tk.StringVar(value="")
        ttk.Entry(row2, textvariable=self.fit_stable, width=10).pack(side="left", padx=(2, 10))
        ttk.Button(row2, text="拟合预览", command=self.on_fit_preview).pack(side="left")
        ttk.Button(row2, text="保存场景 JSON…", command=self.on_fit_save).pack(side="left", padx=8)
        self.fit_text = tk.Text(f, height=24)
        self.fit_text.pack(fill="both", expand=True, padx=8, pady=8)
        self.fit_result: dict | None = None

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
        self._set_status("拟合预览完成，可保存场景 JSON")

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


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
