"""离散事件主循环：请求流 → 策略拼装 → 缓存结算 → 计价 → 指标聚合。"""
from __future__ import annotations

import dataclasses

from .cache import ApiCache
from .params import Scenario
from .policies import SessionState, make_policy
from .workload import generate_requests


@dataclasses.dataclass
class Result:
    n_requests: int = 0
    prompt_tokens: float = 0.0
    matched_tokens: float = 0.0
    written_tokens: float = 0.0
    tail_tokens: float = 0.0
    output_tokens: float = 0.0
    cost_input: float = 0.0    # 未写入段按 input 价
    cost_cached: float = 0.0   # 命中段按 cached_read 价
    cost_write: float = 0.0    # 写入段按 cache_write 价（含读取成本）
    cost_output: float = 0.0

    @property
    def cost_total(self) -> float:
        return self.cost_input + self.cost_cached + self.cost_write + self.cost_output

    @property
    def hit_ratio(self) -> float:
        return self.matched_tokens / self.prompt_tokens if self.prompt_tokens else 0.0


def run(scen: Scenario, trace: list | None = None) -> Result:
    """跑一次模拟。trace 传入 list 时逐请求追加记录（t/session/prompt/matched/hit/cost）。"""
    res = Result()
    reqs = generate_requests(scen.workload, scen.seed)
    cache = ApiCache(scen.mechanism)
    policy = make_policy(scen.policy)
    states: dict[int, SessionState] = {}
    pr = scen.prices
    for r in reqs:
        st = states.setdefault(r.session, SessionState())
        blocks, bps = policy.compose(st, r, scen.workload)
        m = cache.access(blocks, bps, now=r.t)
        res.n_requests += 1
        res.prompt_tokens += m.prompt_tokens
        res.matched_tokens += m.matched
        res.written_tokens += m.written
        res.tail_tokens += m.tail
        res.output_tokens += r.output_tokens
        res.cost_input += m.tail * pr.input / 1e6
        res.cost_cached += m.matched * pr.cached_read / 1e6
        res.cost_write += m.written * pr.cache_write / 1e6
        res.cost_output += r.output_tokens * pr.output / 1e6
        policy.observe(st, r, scen.workload)
        if trace is not None:
            trace.append({
                "t": r.t,
                "session": r.session,
                "prompt": m.prompt_tokens,
                "matched": m.matched,
                "hit": m.matched / m.prompt_tokens if m.prompt_tokens else 0.0,
                "cost": (m.tail * pr.input + m.matched * pr.cached_read
                         + m.written * pr.cache_write + r.output_tokens * pr.output) / 1e6,
            })
    return res


def run_average(scen: Scenario, seeds: int = 1) -> Result:
    """多种子平均：标量字段取均值；cost_total / hit_ratio 为派生属性，自动一致。"""
    seeds = max(1, seeds)
    acc: dict[str, float] = {}
    for s in range(seeds):
        r = run(dataclasses.replace(scen, seed=scen.seed + s))
        for f in dataclasses.fields(Result):
            acc[f.name] = acc.get(f.name, 0.0) + getattr(r, f.name)
    return Result(**{k: v / seeds for k, v in acc.items()})


POLICY_SECTIONS = ("workload", "mechanism", "prices", "policy")


def with_policy(scen: Scenario, name: str) -> Scenario:
    return dataclasses.replace(scen, policy=dataclasses.replace(scen.policy, name=name))


def run_sweep(scen: Scenario, param: str, start: float, stop: float, steps: int,
              policies: list[str], seeds: int = 1) -> list[dict]:
    """沿一个参数扫描多个策略；返回行字典列表（CLI 与 GUI 共用）。"""
    section, fieldname = param.split(".", 1) if "." in param else ("workload", param)
    if section not in POLICY_SECTIONS:
        raise ValueError(f"未知参数段: {section}；可用: {POLICY_SECTIONS}")
    sec = getattr(scen, section)
    if fieldname not in {f.name for f in dataclasses.fields(sec)}:
        raise ValueError(f"{section} 没有参数 {fieldname}")
    rows: list[dict] = []
    n = max(1, steps)
    for i in range(n):
        v = start + (stop - start) * (i / (n - 1) if n > 1 else 0.0)
        for pname in policies:
            sc = dataclasses.replace(scen, **{section: dataclasses.replace(sec, **{fieldname: v})})
            r = run_average(with_policy(sc, pname), seeds)
            rows.append({
                "value": round(v, 8),
                "policy": pname,
                "hit_ratio": round(r.hit_ratio, 4),
                "cost_total": round(r.cost_total, 6),
                "cost_per_req": round(r.cost_total / r.n_requests, 8) if r.n_requests else None,
            })
    return rows
