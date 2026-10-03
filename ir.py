"""中间数据表示层（IR）。

两级表示，隔离真实数据格式与建模核心：

1. raw events IR（JSONL，每行一条消息事件；只含特征，不含消息内容）::

       {"t": <unix秒>, "session": "<会话/群标识>", "role": "user"|"bot",
        "chars": <字符数>, "tokens": <可选，token 数>}

   以 ``#`` 开头的行视为注释跳过。

2. profile IR（JSON 聚合画像）::

       {"ir_version": 1, "type": "hithit_profile", "window": {...},
        "n_sessions": .., "n_turns": .., "per_session": [...],
        "gap_hist" / "input_hist" / "output_hist": {"edges": [...], "weights": [...]}}

数据源侧的适配程序只需把真实格式导出成 raw events IR；hithit.profile 把它
聚合为 profile，hithit.fit 把 profile 拟合成场景 JSON。约定：t 为 UTC unix
秒；role="bot" 表示该消息由 bot 发出（对应一次 LLM 请求的输出）。
"""
from __future__ import annotations

IR_VERSION = 1
PROFILE_TYPE = "hithit_profile"
REQUIRED_EVENT_FIELDS = {"t", "session", "role", "chars"}
ROLES = ("user", "bot")


def _check_hist(name: str, edges, weights) -> None:
    if not isinstance(edges, list) or not isinstance(weights, list):
        raise ValueError(f"{name}: edges/weights 必须是列表")
    if len(edges) != len(weights) + 1:
        raise ValueError(f"{name}: edges 长度必须为 weights+1")
    if any(b <= a for a, b in zip(edges, edges[1:])):
        raise ValueError(f"{name}: edges 必须严格递增")
    if any(w < 0 for w in weights) or sum(weights) <= 0:
        raise ValueError(f"{name}: weights 必须非负且总和为正")


def validate_profile(p: dict) -> None:
    if p.get("type") != PROFILE_TYPE:
        raise ValueError(f"type 须为 {PROFILE_TYPE!r}")
    if p.get("ir_version") != IR_VERSION:
        raise ValueError(f"ir_version 须为 {IR_VERSION}")
    for key in ("window", "per_session", "gap_hist", "input_hist", "output_hist"):
        if key not in p:
            raise ValueError(f"profile 缺少字段: {key}")
    for name in ("gap_hist", "input_hist", "output_hist"):
        _check_hist(name, p[name].get("edges"), p[name].get("weights"))
