"""API 侧缓存机制模拟。

请求 prompt 被看作"块"序列（同 key = 同内容同长度）；缓存按前缀（trie 节点）
存储条目，命中 = 与当前 prompt 前缀重合的、最深的、未过期的条目。缓存层
不知道"会话"概念——跨请求 / 跨会话的前缀复用完全由块 key 的同一性自然产生。
匹配粒度、时间窗口、最小可缓存长度、auto/explicit 写入方式全部来自
CacheMechanism；价目在 simulate 层结算。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class Block:
    key: tuple    # 内容身份；同 key 即同内容同长度
    tokens: float


@dataclass
class AccessResult:
    prompt_tokens: float
    matched: float   # 命中段（已落到 chunk 粒度），按 cached_read 计价
    written: float   # 本次写入缓存的段，按 cache_write 计价（含读取成本）
    tail: float      # 未写入段（explicit 模式断点之后），按 input 计价


class ApiCache:
    """前缀 trie + 条目级 TTL。容量淘汰（LRU）留作扩展，当前容量无限。"""

    def __init__(self, mech):
        self.mech = mech
        self.children: dict[int, dict] = {0: {}}
        self.meta: dict[int, Optional[list]] = {0: None}  # node -> [tokens, expiry]；None=未写缓存
        self._next = 1

    def _new_node(self) -> int:
        nid = self._next
        self._next += 1
        self.children[nid] = {}
        self.meta[nid] = None
        return nid

    def access(self, blocks: Sequence[Block], breakpoints=None, now: float = 0.0) -> AccessResult:
        mech = self.mech
        total = sum(b.tokens for b in blocks)

        # 匹配：沿 trie 走到存在的前缀最深处；条目未过期才计入命中
        node = 0
        matched_tokens = 0.0
        for b in blocks:
            nxt = self.children[node].get(b.key)
            if nxt is None:
                break
            m = self.meta[nxt]
            if m is not None and m[1] > now:
                matched_tokens = m[0]
            node = nxt
        matched = (matched_tokens // mech.chunk_tokens) * mech.chunk_tokens

        if mech.mode == "explicit":
            write_upto = 0.0
            if breakpoints:
                bps = sorted({int(i) for i in breakpoints})[: max(0, mech.max_breakpoints)]
                prefix = 0.0
                node = 0
                for i, b in enumerate(blocks, 1):
                    ch = self.children[node]
                    nxt = ch.get(b.key)
                    if nxt is None:
                        nxt = self._new_node()
                        ch[b.key] = nxt
                    node = nxt
                    prefix += b.tokens
                    if i in bps and prefix >= mech.min_cacheable_tokens:
                        write_upto = prefix
                        self.meta[node] = [prefix, now + mech.ttl]
            written = max(0.0, write_upto - matched)
            tail = total - matched - written
        else:  # auto：整条 prompt 沿块边界逐级写入缓存（真实 API 的逐块缓存行为）
            if total >= mech.min_cacheable_tokens:
                node = 0
                prefix = 0.0
                for b in blocks:
                    ch = self.children[node]
                    nxt = ch.get(b.key)
                    if nxt is None:
                        nxt = self._new_node()
                        ch[b.key] = nxt
                    node = nxt
                    prefix += b.tokens
                    if prefix >= mech.min_cacheable_tokens:
                        self.meta[node] = [prefix, now + mech.ttl]
            written = max(0.0, total - matched)
            tail = 0.0

        return AccessResult(total, matched, written, max(tail, 0.0))
