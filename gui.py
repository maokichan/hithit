"""tkinter 图形界面：运行 / 扫描 / 画像拟合 三个页签。

用法：python3 -m hithit.gui
需要 python3-tk 与显示服务器（本机 headless 可 ssh -X，或把仓库克隆到桌面机运行）。
"""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .fit import fit_scenario
from .params import load_scenario
from .simulate import run_average, run_sweep, with_policy

POLICIES = ("passthrough", "sliding_window")
COLORS = ("#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed")
TREE_COLS = ("策略", "请求数", "命中率", "未命中输入", "命中读", "写入", "输出", "总成本", "每请求成本")


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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("hithit — 缓存命中建模")
        self.geometry("980x660")
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
        wrap.pack(fill="both", expand=True, padx=8, pady=8)
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
        opts = ttk.Frame(f, padding=(8, 0))
        opts.pack(fill="x")
        self.run_compare = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="对比全部参照策略", variable=self.run_compare).pack(side="left")
        ttk.Label(opts, text="种子数:").pack(side="left", padx=(12, 2))
        self.run_seeds = tk.StringVar(value="3")
        ttk.Spinbox(opts, from_=1, to=50, textvariable=self.run_seeds, width=5).pack(side="left")
        ttk.Button(opts, text="运行", command=self.on_run).pack(side="left", padx=12)
        self.run_tree = self._make_tree(f, TREE_COLS)

    def on_run(self) -> None:
        try:
            scen = load_scenario(self.run_scenario.get().strip())
        except Exception as e:
            messagebox.showerror("加载失败", str(e))
            return
        names = POLICIES if self.run_compare.get() else (scen.policy.name,)
        seeds = max(1, int(self.run_seeds.get()))
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
        for label, attr, default, width in (
            ("参数", "sw_param", "mechanism.ttl", 16),
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
        self.sw_canvas = tk.Canvas(f, height=240, bg="white", highlightthickness=0)
        self.sw_canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.sw_series: dict[str, list[tuple[float, float]]] = {}
        self.sw_canvas.bind("<Configure>", lambda e: self._draw_series())

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
        self._draw_series()
        self._set_status(f"扫描完成：{len(rows)} 行")

    def _draw_series(self) -> None:
        c = self.sw_canvas
        c.delete("all")
        series = self.sw_series
        if not series:
            return
        w = max(c.winfo_width(), 120)
        h = max(c.winfo_height(), 120)
        pts = [p for s in series.values() for p in s]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        if x1 <= x0:
            x1 = x0 + 1
        if y1 <= y0:
            y0, y1 = y0 - 1e-12, y1 + 1e-12
        pad = 56

        def X(x: float) -> float:
            return pad + (x - x0) / (x1 - x0) * (w - pad - 16)

        def Y(y: float) -> float:
            return h - 28 - (y - y0) / (y1 - y0) * (h - 52)

        c.create_line(pad, 20, pad, h - 28, fill="#777")
        c.create_line(pad, h - 28, w - 16, h - 28, fill="#777")
        c.create_text(pad - 6, 24, text=f"{y1:.4g}", anchor="e", font=("", 8))
        c.create_text(pad - 6, h - 28, text=f"{y0:.4g}", anchor="e", font=("", 8))
        c.create_text(w - 16, h - 12, text=f"{x1:.4g}", anchor="e", font=("", 8))
        c.create_text(pad, h - 12, text=f"{x0:.4g}", anchor="w", font=("", 8))
        for i, (name, s) in enumerate(series.items()):
            color = COLORS[i % len(COLORS)]
            last = None
            for x, y in s:
                px, py = X(x), Y(y)
                if last:
                    c.create_line(*last, px, py, fill=color, width=2)
                c.create_oval(px - 2, py - 2, px + 2, py + 2, fill=color, outline="")
                last = (px, py)
            c.create_text(w - 150, 12 + i * 16, text=f"■ {name} 每请求成本", anchor="w", fill=color, font=("", 9))

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
