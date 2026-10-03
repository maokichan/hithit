"""生成合成 raw events 样例（仅演示管道格式，非真实数据）。

用法：python3 examples/gen_sample.py
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

BASE_T = 1767225600.0   # 固定基准时刻，保证样例可复现
WINDOW = 3 * 86400.0

GROUPS = {
    "g_active": 1 / 900.0,
    "g_normal": 1 / 1500.0,
    "g_quiet": 1 / 3600.0,
    "g_bursty": 1 / 2400.0,
}


def main() -> None:
    rng = random.Random(11)
    out = Path(__file__).parent / "raw_events_sample.jsonl"
    n_lines = 0
    with out.open("w", encoding="utf-8") as f:
        f.write("# 合成样例：raw events IR（t=UTC unix 秒，chars=消息字符数）\n")
        for sid, lam in GROUPS.items():
            t = BASE_T + rng.expovariate(lam)
            while t < BASE_T + WINDOW:
                for _ in range(rng.choices([1, 2, 3], [0.6, 0.3, 0.1])[0]):
                    chars = int(rng.lognormvariate(math.log(160.0), 0.9))
                    f.write(json.dumps({"t": round(t, 1), "session": sid,
                                        "role": "user", "chars": chars}) + "\n")
                    t += rng.expovariate(4.0 * lam)
                    n_lines += 1
                f.write(json.dumps({
                    "t": round(t, 1), "session": sid, "role": "bot",
                    "chars": int(rng.lognormvariate(math.log(320.0), 0.8)),
                }) + "\n")
                n_lines += 1
                t += rng.expovariate(lam)
    print(f"已生成 {out}（{n_lines} 行事件）")


if __name__ == "__main__":
    main()
