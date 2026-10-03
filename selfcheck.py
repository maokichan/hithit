"""selfcheck：机制单元检查 + 闭式期望 vs 模拟对照。

解析情形：同质泊松到达、常数长度、零稳定前缀、只追加策略、auto 写入。
每会话轮数 m ~ Poisson(λ·sim_time)；第 j 轮（0 起）历史 H_j = j·(L_in+L_out)；
相邻请求间隔 Exp(λ)，命中概率 p = 1 − e^(−λ·TTL)。
"""
from __future__ import annotations

import math

from .cache import ApiCache, Block
from .params import CacheMechanism, PolicyConfig, PriceTable, Scenario, Workload
from .simulate import run


def _poisson_sf(j: int, mu: float) -> float:
    """P(Poisson(mu) > j)。"""
    term = math.exp(-mu)
    cdf = term
    for i in range(1, j + 1):
        term *= mu / i
        cdf += term
    return max(0.0, 1.0 - min(cdf, 1.0))


def _unit_checks() -> None:
    c = ApiCache(CacheMechanism(ttl=100.0, chunk_tokens=1))
    b = [Block(("a",), 10.0), Block(("b",), 20.0)]
    r1 = c.access(b, now=0.0)
    assert r1.matched == 0 and r1.written == 30 and r1.tail == 0, r1
    r2 = c.access(b + [Block(("c",), 5.0)], now=50.0)
    assert r2.matched == 30 and r2.written == 5 and r2.tail == 0, r2
    r3 = c.access(b + [Block(("c",), 5.0)], now=200.0)
    assert r3.matched == 0 and r3.written == 35, r3  # TTL 过期 → 全冷

    c2 = ApiCache(CacheMechanism(ttl=100.0, chunk_tokens=16))
    r4 = c2.access([Block(("x",), 10.0)], now=0.0)
    assert r4.matched == 0 and r4.written == 10, r4
    r5 = c2.access([Block(("x",), 10.0), Block(("y",), 10.0)], now=1.0)
    assert r5.matched == 0 and r5.written == 20, r5  # 10 < chunk 16 → 命中不计

    c3 = ApiCache(CacheMechanism(ttl=100.0, chunk_tokens=1, mode="explicit"))
    r6 = c3.access([Block(("p",), 10.0), Block(("q",), 20.0)], breakpoints=[1], now=0.0)
    assert r6.written == 10 and r6.tail == 20, r6  # 只写断点前缀，其后按 input 价
    r7 = c3.access(
        [Block(("p",), 10.0), Block(("q",), 20.0), Block(("z",), 5.0)], breakpoints=[2], now=1.0
    )
    assert r7.matched == 10 and r7.written == 20 and r7.tail == 5, r7


def _analytic(scen: Scenario) -> dict:
    w, mech, pr = scen.workload, scen.mechanism, scen.prices
    lam = w.request_rate
    p_hot = 1.0 - math.exp(-lam * mech.ttl)
    lin, lout = w.input_tokens_mean, w.output_tokens_mean
    mu = lam * w.sim_time
    cost = matched = prompt = 0.0
    j = 0
    while True:
        pk = _poisson_sf(j, mu)  # P(该会话存在第 j 轮)
        if j > mu and pk < 1e-12:
            break
        h = j * (lin + lout)
        cost += pk * (
            p_hot * pr.cached_read * h
            + (1.0 - p_hot) * pr.cache_write * h
            + pr.cache_write * lin
        )
        matched += pk * p_hot * h
        prompt += pk * (h + lin)
        j += 1
    return {
        "p_hot": p_hot,
        "cost_per_req": (cost / mu + lout * pr.output) / 1e6,
        "hit_ratio": matched / prompt,
        "n_requests": w.n_sessions * mu,
    }


def run_selfcheck() -> bool:
    _unit_checks()
    print("单元检查：通过（TTL 过期 / chunk 粒度 / explicit 断点）")

    scen = Scenario(
        workload=Workload(
            n_sessions=80,
            sim_time=86400.0,
            request_rate=1.0 / 600.0,
            stable_prefix_tokens=0.0,
            n_stable_blocks=1,
            input_tokens_mean=150.0,
            output_tokens_mean=250.0,
            length_dist="constant",
        ),
        mechanism=CacheMechanism(ttl=300.0, chunk_tokens=1),
        prices=PriceTable(input=1.0, cached_read=0.1, cache_write=1.0, output=2.0),
        policy=PolicyConfig(name="passthrough"),
        seed=7,
    )
    res = run(scen)
    ana = _analytic(scen)
    sim = {
        "cost_per_req": res.cost_total / res.n_requests,
        "hit_ratio": res.hit_ratio,
        "n_requests": float(res.n_requests),
    }
    print(
        f"\n解析对照（λT = {scen.workload.request_rate * scen.mechanism.ttl:.3f}，"
        f"期望命中概率 p = {ana['p_hot']:.3f}）："
    )
    print(f"{'指标':<14}{'解析':>14}{'模拟':>14}{'偏差':>9}")
    ok = True
    for k in ("cost_per_req", "hit_ratio", "n_requests"):
        a, s = ana[k], sim[k]
        rel = abs(s - a) / a if a else 0.0
        flag = "OK" if rel < 0.03 else "FAIL"
        ok = ok and rel < 0.03
        print(f"{k:<14}{a:>14.5f}{s:>14.5f}{rel:>8.1%}  {flag}")
    print("\nselfcheck：", "PASS" if ok else "FAIL")
    return ok
