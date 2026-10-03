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


def sample_from_hist(rng: random.Random, edges: list[float], weights: list[float]) -> float:
    """按权重选箱、箱内均匀采样。"""
    if sum(weights) <= 0:
        raise ValueError("直方图权重总和必须为正")
    i = rng.choices(range(len(weights)), weights=weights)[0]
    lo, hi = edges[i], edges[i + 1]
    if hi <= lo:
        return lo
    return rng.uniform(lo, hi)


def hist_mean(edges: list[float], weights: list[float]) -> float:
    total = sum(weights)
    return sum(w * (lo + hi) / 2.0 for lo, hi, w in zip(edges, edges[1:], weights)) / total


def _length_sampler(rng: random.Random, mean: float, cv: float, dist: str, edges, weights):
    if edges is not None and weights is not None:
        return lambda: sample_from_hist(rng, edges, weights)
    return lambda: _sample_length(rng, mean, cv, dist)


def generate_requests(w: Workload, seed: int) -> list[Request]:
    rng = random.Random(seed)
    rates = per_session_rates(w, rng)
    in_s = _length_sampler(rng, w.input_tokens_mean, w.input_tokens_cv, w.length_dist,
                           w.input_hist_edges, w.input_hist_weights)
    out_s = _length_sampler(rng, w.output_tokens_mean, w.output_tokens_cv, w.length_dist,
                            w.output_hist_edges, w.output_hist_weights)
    # 直方图到达：按会话速率缩放采样间隔，保留会话间活跃度异质性
    gap_factor: list[float] | None = None
    if w.gap_hist_edges is not None and w.gap_hist_weights is not None:
        gm = hist_mean(w.gap_hist_edges, w.gap_hist_weights)
        if gm <= 0:
            raise ValueError("gap 直方图均值非法")
        gap_factor = [1.0 / (lam * gm) for lam in rates]
    events: list[Request] = []
    for s, lam in enumerate(rates):
        t, seq = 0.0, 0
        while True:
            if gap_factor is not None:
                t += max(sample_from_hist(rng, w.gap_hist_edges, w.gap_hist_weights) * gap_factor[s], 1e-6)
            else:
                t += rng.expovariate(lam)
            if t > w.sim_time:
                break
            events.append(Request(t, s, seq, in_s(), out_s()))
            seq += 1
    events.sort(key=lambda r: r.t)
    return events
