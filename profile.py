"""采集层：raw events IR → profile IR（特征画像）。

回合配对规则：会话内按时间排序后，自上一条 bot 消息以来累计的 user 消息
构成一次"触发输入"，该条 bot 消息为对应"输出"。token 优先取事件自带
tokens 字段，否则按 chars_per_token 折算。数据源若有不同语义（多条连发
回复、纯环境流量等），应在源侧适配时归一成上述回合结构。
"""
from __future__ import annotations

import json
import statistics
import time

from .ir import IR_VERSION, PROFILE_TYPE, REQUIRED_EVENT_FIELDS, ROLES, validate_profile

# 到达间隔直方图固定分箱（秒），末箱兜底长尾
GAP_EDGES = [0, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200, 14400, 43200, 86400, 2592000]


def load_events(path: str) -> list[dict]:
    events: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"第 {ln} 行不是合法 JSON: {e}") from e
            missing = REQUIRED_EVENT_FIELDS - set(ev)
            if missing:
                raise ValueError(f"第 {ln} 行缺少字段: {sorted(missing)}")
            if ev["role"] not in ROLES:
                raise ValueError(f"第 {ln} 行 role 非法: {ev['role']!r}（可用: {ROLES}）")
            if not isinstance(ev["chars"], (int, float)) or ev["chars"] < 0:
                raise ValueError(f"第 {ln} 行 chars 非法")
            events.append(ev)
    if not events:
        raise ValueError("事件文件为空")
    return events


def _quantile_edges(values: list[float], nbins: int = 24) -> list[float]:
    vs = sorted(values)
    if vs[-1] <= vs[0]:
        return [vs[0], vs[0] + 1.0]
    edges = [vs[round(i * (len(vs) - 1) / nbins)] for i in range(nbins + 1)]
    dedup = [edges[0]]
    for e in edges[1:]:
        if e > dedup[-1]:
            dedup.append(e)
    if len(dedup) < 2:
        return [vs[0], vs[-1]]
    dedup[-1] = vs[-1]
    return dedup


def _hist_counts(values: list[float], edges: list[float]) -> list[int]:
    counts = [0] * (len(edges) - 1)
    for v in values:
        for i in range(len(edges) - 2, -1, -1):
            if v >= edges[i]:
                counts[i] += 1
                break
        else:
            counts[0] += 1
    return counts


def _stats(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = statistics.fmean(values)
    if len(values) < 2 or mean <= 0:
        return mean, 0.0
    return mean, statistics.pstdev(values) / mean


def build_profile(events: list[dict], chars_per_token: float = 2.0, source: str = "") -> dict:
    cpt = chars_per_token if chars_per_token > 0 else 2.0
    events = sorted(events, key=lambda e: (str(e["session"]), float(e["t"])))
    t0 = min(float(e["t"]) for e in events)
    t1 = max(float(e["t"]) for e in events)

    per_session: list[dict] = []
    all_turns: list[tuple[float, float]] = []   # (in_tokens, out_tokens)
    gap_turns: list[float] = []
    for sid in sorted({str(e["session"]) for e in events}):
        msgs = [e for e in events if str(e["session"]) == sid]
        rows: list[tuple[float | None, float, float]] = []
        acc_in = 0.0
        prev_bot_t: float | None = None
        for ev in msgs:
            toks = float(ev["tokens"]) if ev.get("tokens") is not None else float(ev["chars"]) / cpt
            if ev["role"] == "user":
                acc_in += toks
            else:
                gap = (float(ev["t"]) - prev_bot_t) if prev_bot_t is not None else None
                rows.append((gap, acc_in, toks))
                acc_in = 0.0
                prev_bot_t = float(ev["t"])
        span = max(1.0, float(msgs[-1]["t"]) - float(msgs[0]["t"]))
        in_vals = [r[1] for r in rows]
        out_vals = [r[2] for r in rows]
        im, icv = _stats(in_vals)
        om, ocv = _stats(out_vals)
        per_session.append({
            "session": sid,
            "turns": len(rows),
            "span_seconds": span,
            "rate": len(rows) / span,
            "input_mean": im, "input_cv": icv,
            "output_mean": om, "output_cv": ocv,
        })
        all_turns.extend((r[1], r[2]) for r in rows)
        gap_turns.extend(r[0] for r in rows if r[0] is not None)

    in_vals = [t[0] for t in all_turns]
    out_vals = [t[1] for t in all_turns]
    in_edges = _quantile_edges(in_vals)
    out_edges = _quantile_edges(out_vals)
    profile = {
        "ir_version": IR_VERSION,
        "type": PROFILE_TYPE,
        "source": source,
        "generated_at": time.time(),
        "chars_per_token": cpt,
        "window": {"start": t0, "end": t1, "seconds": max(0.0, t1 - t0)},
        "n_sessions": len(per_session),
        "n_turns": len(all_turns),
        "per_session": per_session,
        "gap_hist": {"edges": GAP_EDGES, "weights": _hist_counts(gap_turns, GAP_EDGES)},
        "input_hist": {"edges": in_edges, "weights": _hist_counts(in_vals, in_edges)},
        "output_hist": {"edges": out_edges, "weights": _hist_counts(out_vals, out_edges)},
    }
    validate_profile(profile)
    return profile
