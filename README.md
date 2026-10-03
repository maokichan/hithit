# hithit — LLM API prompt 缓存命中建模工具

- **状态**：v0.2 已落地（2026-10-04），selfcheck 通过
- **性质**：通用建模工具。任何具体业务（IM bot、agent、批处理……）都只是"会话流"抽象之下的一份**场景参数文件**，不进入程序设计。
- **纪律**：价目、TTL、匹配粒度、写入方式全部动态输入，不为任何 API 供应商硬编码；候选策略是工具建成后的**评估对象**，程序只带验证工具本身用的参照策略。

## 设计决定（2026-10-04 与需求方确认）

1. 负载抽象 = **会话流**：稳定前缀段 + 只增长历史 + 每轮新鲜输入；会话数、到达率、长度分布全是参数。
2. 策略 = **接口 + 参照策略**：`compose`（决定每个请求用什么块拼 prompt、断点放哪）+ `observe`（回合结束后历史如何增长）；内置 `passthrough` / `sliding_window` 两个参照策略仅供验证工具。
3. 缓存机制 = **TTL + 前缀匹配**：块序列 trie、条目级时间窗口过期、chunk 匹配粒度、最小可缓存长度、auto / explicit 断点两种写入方式；容量淘汰暂不做。
4. 价目 = **纯动态输入**：`input / cached_read / cache_write / output`（币种/1M token）。语义约定：`cache_write` 覆盖所写 token 的读取成本——无写溢价的自动缓存方案设 `cache_write = input`；1.25 倍写入方案设 `cache_write = 1.25 × input`。

## 组件（正交，只经接口见面）

| 模块 | 职责 |
| --- | --- |
| `params.py` | Workload / CacheMechanism / PriceTable / PolicyConfig；JSON 加载，未知字段报错 |
| `workload.py` | 会话流参数 → 按时排序的请求流（泊松到达、长度分布、会话活跃度异质性） |
| `cache.py` | API 侧缓存：trie 前缀匹配、TTL、chunk 粒度、auto/explicit 写入；不知道"会话"存在 |
| `policies.py` | 策略接口 + 两个参照策略 |
| `simulate.py` | 离散事件主循环与指标聚合 |
| `report.py` | 对比表 / 扫描表（markdown + CSV） |
| `selfcheck.py` | 机制单元检查 + 闭式期望 vs 模拟对照 |

## 用法

```bash
python3 -m hithit selfcheck                 # 验证工具本身
python3 -m hithit demo                      # 中性默认场景，参照策略对比
python3 -m hithit run --scenario hithit/scenarios/generic.json --policy sliding_window
python3 -m hithit sweep --scenario hithit/scenarios/generic.json \
    --param mechanism.ttl --start 0 --stop 3000 --steps 7 --csv out.csv
```

## 指标

token 加权命中率、成本分解（未命中输入 / 命中读 / 写入 / 输出）、总成本与每请求成本；`sweep` 沿任一参数扫描，观察策略优劣随负载/机制的切换。

## 验证

- **单元**：TTL 过期、chunk 粒度、explicit 断点写入的行为断言。
- **解析对照**：同质泊松 + 常数长度 + 零稳定前缀 + 只追加下，每请求成本与命中率有闭式期望（命中概率 p = 1 − e^(−λT)），模拟值相对偏差 < 3% 判 PASS。

## 里程碑

- **M2（已完成）**：模拟器 + 解析对照 + 参照策略。
- **M3（已完成）**：多会话、活跃度异质性、参数扫描。
- **M4（待需求方输入）**：真实场景参数文件（负载特征 + 真实牌价）与候选策略清单，进入策略评估阶段。

## 待需求方提供

1. 场景参数文件：业务负载特征 + 真实牌价。
2. 想评估的候选策略清单（自然语言描述即可，实现走 `policies.Policy` 接口）。
