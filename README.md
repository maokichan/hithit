# hithit — LLM API prompt 缓存命中建模工具

- **状态**：v0.3（2026-10-04），selfcheck 通过，公开仓库
- **性质**：通用建模工具。任何具体业务（IM bot、agent、批处理……）都只是"会话流"抽象之下的一份**场景参数文件**，不进入程序设计。
- **纪律**：价目、TTL、匹配粒度、写入方式全部动态输入，不为任何 API 供应商硬编码；候选策略是工具建成后的**评估对象**，程序只带验证工具本身用的参照策略。

## 设计决定（2026-10-04 与需求方确认）

1. 负载抽象 = **会话流**：稳定前缀段 + 只增长历史 + 每轮新鲜输入；会话数、到达率、长度分布全是参数。
2. 策略 = **接口 + 参照策略**：`compose`（决定每个请求用什么块拼 prompt、断点放哪）+ `observe`（回合结束后历史如何增长）；内置 `passthrough` / `sliding_window` 两个参照策略仅供验证工具。
3. 缓存机制 = **TTL + 前缀匹配**：块序列 trie、条目级时间窗口过期、chunk 匹配粒度、最小可缓存长度、auto / explicit 断点两种写入方式；容量淘汰暂不做。
4. 价目 = **纯动态输入**：`input / cached_read / cache_write / output`（币种/1M token）。语义约定：`cache_write` 覆盖所写 token 的读取成本——无写溢价的自动缓存方案设 `cache_write = input`；1.25 倍写入方案设 `cache_write = 1.25 × input`。

## 来源与设计文档

hithit 起源于 OneKichan（QQ bot）项目的缓存成本问题：该项目为压低 LLM 上下文缓存成本设计了 L0-L4 分层 prompt 与"冻结块链"策略族。2026-10-04 起，这套缓存/持久化设计线整体移交本仓库维护（原项目只留部署与运维），它们是策略评估的"候选策略"与语义出处：

- [docs/02-缓存窗口设计.md](docs/02-缓存窗口设计.md) — L0-L4 分层 prompt、多会话语义、成本模型、命中率杀手清单
- [docs/03-持久化与开发纪律.md](docs/03-持久化与开发纪律.md) — 五表骨架、数据生命周期分级、append-only 等存储纪律
- [docs/04-缓存命中机制与验收.md](docs/04-缓存命中机制与验收.md) — 冻结块链、水位线淘汰、断裂预算、字节稳定性验收（策略的完整机制形态）

## 数据接入管道（真实数据 → 场景）

中间数据表示层（IR）两级，隔离真实数据格式与建模核心：

```
真实数据源（imbot 等）
   │  源侧适配程序：把真实格式导出为 raw events IR
   ▼
raw events IR（JSONL，只含特征不含内容）
   {"t": <unix秒>, "session": "<会话标识>", "role": "user"|"bot", "chars": <字符数>, "tokens": <可选>}
   │  python3 -m hithit profile events.jsonl -o profile.json --chars-per-token 2.0
   ▼
profile IR（聚合画像：回合到达间隔 / 输入 / 输出直方图 + 分会话统计）
   │  python3 -m hithit fit profile.json -o scenario.json --stable-prefix-tokens 800
   ▼
scenario JSON ──▶ hithit run / sweep / GUI
```

- **回合配对规则**（采集侧约定）：会话内按时间排序，自上一条 bot 消息以来累计的 user 消息为一次触发输入，该条 bot 消息为输出。数据源语义不同（连发回复、纯环境流量）在源侧适配时归一。
- `--chars-per-token` 是折算系数（默认 2.0），按业务 tokenizer 校准；事件自带 `tokens` 字段时优先。
- 拟合产物用**直方图重采样**驱动负载（间隔/长度都按真实分布），比参数化拟合更忠实；局限：i.i.d. 独立重采样，突发自相关与昼夜周期未建模，输出时会给提示。
- 稳定前缀长度（业务系统提示的 token 数）采集不到，需人工传入，否则保留默认并提示。

## 图形界面

```bash
python3 -m hithit.gui
```

三个页签：**运行**（场景 JSON → 参照策略对比表）、**扫描**（参数扫描 → 表格 + 每请求成本折线图）、**画像拟合**（profile → 预览 → 保存场景）。依赖 `python3-tk` 与显示服务器——本机 headless 时用 `ssh -X`，或把仓库克隆到桌面机运行（纯标准库，无三方依赖）。

## 组件（正交，只经接口见面）

| 模块 | 职责 |
| --- | --- |
| `params.py` | Workload / CacheMechanism / PriceTable / PolicyConfig；JSON 加载，未知字段报错 |
| `workload.py` | 会话流参数 → 请求流（泊松或直方图到达、参数化或直方图长度、活跃度异质性） |
| `cache.py` | API 侧缓存：trie 前缀匹配、TTL、chunk 粒度、auto/explicit 写入；不知道"会话"存在 |
| `policies.py` | 策略接口 + 两个参照策略 |
| `simulate.py` | 离散事件主循环、多种子平均、参数扫描（CLI/GUI 共用） |
| `ir.py` / `profile.py` / `fit.py` | 中间数据表示层：raw events → 画像 → 场景拟合 |
| `gui.py` | tkinter 图形界面 |
| `report.py` / `selfcheck.py` | 对比表与扫描表；机制单元检查 + 解析对照 |
| `examples/` | 合成样例（生成脚本 + 各级产物），演示全管道 |

## 用法

```bash
python3 -m hithit selfcheck                      # 验证工具本身
python3 -m hithit demo                           # 中性默认场景，参照策略对比
python3 -m hithit run --scenario hithit/examples/scenario_sample.json
python3 -m hithit sweep --scenario hithit/examples/scenario_sample.json \
    --param mechanism.ttl --start 0 --stop 3600 --steps 7 --csv out.csv
python3 examples/gen_sample.py                   # 重新生成合成样例
python3 -m hithit profile hithit/examples/raw_events_sample.jsonl -o /tmp/p.json
python3 -m hithit fit /tmp/p.json -o /tmp/s.json --stable-prefix-tokens 800
```

## 指标与验证

- 指标：token 加权命中率、成本分解（未命中输入 / 命中读 / 写入 / 输出）、总成本与每请求成本。
- 单元：TTL 过期、chunk 粒度、explicit 断点、直方图采样均值回归。
- 解析对照：同质泊松 + 常数长度 + 零稳定前缀 + 只追加下，每请求成本与命中率有闭式期望（命中概率 p = 1 − e^(−λT)），模拟值相对偏差 < 3% 判 PASS。

## 里程碑

- **M2–M3（已完成）**：模拟器、解析对照、多会话与异质性、参数扫描。
- **M4（进行中）**：数据接入管道与 GUI 已就绪；**待真实数据**——源侧适配程序按 raw events IR 导出真实群聊特征，`profile → fit` 得到真实场景；候选策略清单确定后进入策略评估。

## 仓库与版本

- Git 管理，逻辑提交；版本号见 `hithit/__init__.py`，发布打 `vX.Y.Z` 标签。
- GitHub：`github.com/maokichan/hithit`（公开）。
