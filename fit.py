"""拟合层：profile IR → hithit 场景 JSON。

负载部分直接采用画像直方图（i.i.d. 重采样；突发自相关与昼夜周期不在 v1
建模范围，输出时会给出提示）。机制与价目保持默认占位，由使用者按真实
牌价覆盖——拟合产物只是起点，不是结论。
"""
from __future__ import annotations

import json
import statistics

from .ir import validate_profile
from .params import scenario_from_dict


def fit_scenario(
    profile: dict,
    stable_prefix_tokens: float | None = None,
    sim_time_cap: float = 604800.0,
) -> tuple[dict, list[str]]:
    validate_profile(profile)
    warnings: list[str] = []
    rates = [r["rate"] for r in profile["per_session"] if r["turns"] > 0]
    mean_rate = statistics.fmean(rates) if rates else 0.0
    cv = statistics.pstdev(rates) / mean_rate if len(rates) > 1 and mean_rate > 0 else 0.0

    workload: dict = {
        "n_sessions": profile["n_sessions"],
        "sim_time": round(min(profile["window"]["seconds"], sim_time_cap), 1),
        "request_rate": round(mean_rate, 8),
        "rate_heterogeneity_cv": round(cv, 4),
        "length_dist": "histogram",
        "input_hist_edges": profile["input_hist"]["edges"],
        "input_hist_weights": profile["input_hist"]["weights"],
        "output_hist_edges": profile["output_hist"]["edges"],
        "output_hist_weights": profile["output_hist"]["weights"],
        "gap_hist_edges": profile["gap_hist"]["edges"],
        "gap_hist_weights": profile["gap_hist"]["weights"],
    }
    if stable_prefix_tokens is not None:
        workload["stable_prefix_tokens"] = float(stable_prefix_tokens)
    else:
        warnings.append("稳定前缀长度未提供：保留默认 1000 token，请按业务真实系统提示长度修改。")
    warnings.append("直方图为独立重采样：突发自相关与昼夜周期未建模，请结合业务判断结果偏差方向。")

    scenario = {"seed": 42, "workload": workload, "mechanism": {}, "prices": {}, "policy": {}}
    scenario_from_dict(scenario)  # 往返校验：字段名与取值必须能被直接加载
    return scenario, warnings


def load_profile(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        profile = json.load(f)
    validate_profile(profile)
    return profile
