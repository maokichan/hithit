"""负载生成：把 Workload 参数变成按时间排序的请求流（会话流抽象）。"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .params import Workload


@dataclass
class Request:
    t: float             # 请求时刻（秒）
    session: int
    seq: int             # 会话内第几轮（0 起）
    input_tokens: float
    output_tokens: float


def _sample_length(rng: random.Random, mean: float, cv: float, dist: str) -> float:
    if dist == "constant" or cv <= 0:
        return float(mean)
    if dist == "exponential":
        return rng.expovariate(1.0 / mean)
    if dist == "lognormal":
        sigma = math.sqrt(math.log1p(cv * cv))
        mu = math.log(mean) - sigma * sigma / 2.0
        return rng.lognormvariate(mu, sigma)
    raise ValueError(f"未知长度分布: {dist}")


def per_session_rates(w: Workload, rng: random.Random) -> list[float]:
    """会话间活跃度异质性：对数正态缩放，均值保持在 request_rate。"""
    if w.rate_heterogeneity_cv <= 0:
        return [w.request_rate] * w.n_sessions
    sigma = math.sqrt(math.log1p(w.rate_heterogeneity_cv ** 2))
    mu = -sigma * sigma / 2.0
    return [w.request_rate * math.exp(mu + rng.gauss(0.0, sigma)) for _ in range(w.n_sessions)]


def generate_requests(w: Workload, seed: int) -> list[Request]:
    rng = random.Random(seed)
    rates = per_session_rates(w, rng)
    events: list[Request] = []
    for s, lam in enumerate(rates):
        t, seq = 0.0, 0
        while True:
            t += rng.expovariate(lam)
            if t > w.sim_time:
                break
            it = _sample_length(rng, w.input_tokens_mean, w.input_tokens_cv, w.length_dist)
            ot = _sample_length(rng, w.output_tokens_mean, w.output_tokens_cv, w.length_dist)
            events.append(Request(t, s, seq, it, ot))
            seq += 1
    events.sort(key=lambda r: r.t)
    return events
