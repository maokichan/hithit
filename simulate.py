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


def run(scen: Scenario) -> Result:
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
