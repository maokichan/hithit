"""参数模型：负载 / 缓存机制 / 价目表 / 策略配置。

一切机制与价格参数都是动态输入（JSON 或代码构造），程序不为任何 API
供应商硬编码行为。价目语义：``cache_write`` 覆盖所写 token 的读取成本——
无写溢价的自动缓存方案设 ``cache_write = input``；写溢价方案（如 1.25×）
设 ``cache_write = 1.25 × input``。
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
import json


@dataclass
class Workload:
    """负载生成参数——只描述请求流的统计形状（会话流抽象），无业务语义。"""

    n_sessions: int = 30                  # 独立前缀链数量
    sim_time: float = 86400.0             # 观察窗口（秒）
    request_rate: float = 1.0 / 900.0     # 每会话请求到达率（次/秒，泊松）
    rate_heterogeneity_cv: float = 0.0    # 会话间活跃度异质性（对数正态 cv，0=同质）
    stable_prefix_tokens: float = 1000.0  # 跨会话共享的稳定前缀（token）
    n_stable_blocks: int = 4              # 稳定前缀切成的块数
    input_tokens_mean: float = 150.0      # 每请求新鲜输入 token 均值
    input_tokens_cv: float = 1.0
    output_tokens_mean: float = 250.0     # 每请求输出 token 均值（并入下一轮历史）
    output_tokens_cv: float = 1.0
    length_dist: str = "lognormal"        # lognormal | exponential | constant（直方图字段设置时忽略）
    # 直方图采样（profile 拟合产物）；设置后覆盖对应参数化分布
    input_hist_edges: list[float] | None = None    # 长度 = 对应 weights 长度 + 1
    input_hist_weights: list[float] | None = None
    output_hist_edges: list[float] | None = None
    output_hist_weights: list[float] | None = None
    gap_hist_edges: list[float] | None = None      # 到达间隔（秒）；设置后替代指数到达
    gap_hist_weights: list[float] | None = None


@dataclass
class CacheMechanism:
    """API 侧缓存机制（按公开行为抽象为可配置项）。"""

    ttl: float = 300.0                    # 时间窗口：缓存条目存活秒数
    chunk_tokens: int = 64                # 前缀匹配粒度（token），1=逐 token
    min_cacheable_tokens: float = 0.0     # 低于该长度的前缀不写缓存
    mode: str = "auto"                    # auto=整条自动写 | explicit=按断点写
    max_breakpoints: int = 4              # explicit 模式每请求断点上限


@dataclass
class PriceTable:
    """价目表（币种 / 1M token），纯动态输入。"""

    input: float = 1.0
    cached_read: float = 0.1
    cache_write: float = 1.0
    output: float = 2.0


@dataclass
class PolicyConfig:
    name: str = "passthrough"
    window_max_tokens: float = 4000.0     # sliding_window 参照策略的历史上限


@dataclass
class Scenario:
    workload: Workload = field(default_factory=Workload)
    mechanism: CacheMechanism = field(default_factory=CacheMechanism)
    prices: PriceTable = field(default_factory=PriceTable)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    seed: int = 42


_SECTIONS = {
    "workload": Workload,
    "mechanism": CacheMechanism,
    "prices": PriceTable,
    "policy": PolicyConfig,
}


def scenario_from_dict(data: dict) -> Scenario:
    kw: dict = {}
    for name, cls in _SECTIONS.items():
        section = data.get(name, {})
        known = {f.name for f in fields(cls)}
        bad = set(section) - known
        if bad:
            raise ValueError(f"{name} 未知参数: {sorted(bad)}；可用: {sorted(known)}")
        kw[name] = cls(**section)
    bad = set(data) - set(_SECTIONS) - {"seed"}
    if bad:
        raise ValueError(f"未知顶层键: {sorted(bad)}；可用: {sorted(_SECTIONS)} + seed")
    return Scenario(**kw, seed=data.get("seed", 42))


def load_scenario(path: str) -> Scenario:
    with open(path, encoding="utf-8") as f:
        return scenario_from_dict(json.load(f))
