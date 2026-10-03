"""策略接口与参照策略。

策略决定两件事：
- ``compose``：每个请求用什么块拼 prompt、（explicit 模式）断点放哪；
- ``observe``：一个回合结束后会话历史如何增长。

内置两个参照策略只用于验证工具本身；候选策略是工具建成后的评估对象，
按同样接口实现即可接入 run / sweep。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .cache import Block
from .params import PolicyConfig, Workload
from .workload import Request


def stable_blocks(w: Workload) -> list[Block]:
    n = max(1, w.n_stable_blocks)
    per = w.stable_prefix_tokens / n
    return [Block(("stable", i), per) for i in range(n)]


class SessionState:
    __slots__ = ("history",)

    def __init__(self) -> None:
        self.history: list[Block] = []


class Policy(ABC):
    name = "?"

    @abstractmethod
    def compose(self, state: SessionState, req: Request, w: Workload):
        """返回 (prompt 块序列, 断点列表或 None)。

        断点为 1 起的块数下标：写到该块（含）为止的前缀入缓存。
        """

    def observe(self, state: SessionState, req: Request, w: Workload) -> None:
        state.history.append(Block(("msg", req.session, req.seq, "in"), req.input_tokens))
        state.history.append(Block(("msg", req.session, req.seq, "out"), req.output_tokens))


class PassThrough(Policy):
    """参照策略：完整历史只追加，最大化自然前缀命中。"""

    name = "passthrough"

    def compose(self, state, req, w):
        fresh = Block(("msg", req.session, req.seq, "in"), req.input_tokens)
        return stable_blocks(w) + state.history + [fresh], None


class SlidingWindow(Policy):
    """参照策略：历史视图超上限就从头部丢弃，新块自然追加。

    每轮滑动只移出少量块，工具会如实呈现"渐进滑动"与"一次性大截断"
    对前缀匹配的不同影响。
    """

    name = "sliding_window"

    def __init__(self, max_tokens: float):
        self.max_tokens = max_tokens

    def compose(self, state, req, w):
        base = stable_blocks(w)
        fresh = Block(("msg", req.session, req.seq, "in"), req.input_tokens)
        budget = self.max_tokens - sum(b.tokens for b in base) - fresh.tokens
        kept: list[Block] = []
        acc = 0.0
        for b in reversed(state.history):
            if acc + b.tokens > budget:
                break
            kept.append(b)
            acc += b.tokens
        kept.reverse()
        return base + kept + [fresh], None


def make_policy(pc: PolicyConfig) -> Policy:
    if pc.name == "passthrough":
        return PassThrough()
    if pc.name == "sliding_window":
        return SlidingWindow(pc.window_max_tokens)
    raise ValueError(f"未知策略: {pc.name}；可用: passthrough, sliding_window")
