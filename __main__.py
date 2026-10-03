"""CLI 入口：python3 -m hithit {selfcheck|demo|run|sweep}。"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from .params import Scenario, load_scenario
from .report import compare_table, sweep_table
from .simulate import run, run_average
from . import selfcheck as _selfcheck

_SECTIONS = ("workload", "mechanism", "prices", "policy")


def _with_policy(scen: Scenario, name: str) -> Scenario:
    return dataclasses.replace(scen, policy=dataclasses.replace(scen.policy, name=name))


def cmd_selfcheck(args) -> None:
    ok = _selfcheck.run_selfcheck()
    sys.exit(0 if ok else 1)


def cmd_demo(args) -> None:
    scen = Scenario()
    results = {
        name: run_average(_with_policy(scen, name), args.seeds)
        for name in ("passthrough", "sliding_window")
    }
    print("默认中性场景（价目为占位值；正式评估请写场景 JSON，用 run / sweep 传入）\n")
    print(compare_table(results))


def cmd_run(args) -> None:
    scen = load_scenario(args.scenario)
    if args.policy:
        scen = _with_policy(scen, args.policy)
    if args.seed is not None:
        scen = dataclasses.replace(scen, seed=args.seed)
    print(json.dumps(dataclasses.asdict(scen), ensure_ascii=False, indent=1))
    print()
    print(compare_table({scen.policy.name: run(scen)}))


def cmd_sweep(args) -> None:
    scen = load_scenario(args.scenario)
    section, fieldname = args.param.split(".", 1) if "." in args.param else ("workload", args.param)
    if section not in _SECTIONS:
        sys.exit(f"未知参数段: {section}；可用: {_SECTIONS}")
    sec = getattr(scen, section)
    if fieldname not in {f.name for f in dataclasses.fields(sec)}:
        sys.exit(f"{section} 没有参数 {fieldname}")
    rows = []
    n = max(1, args.steps)
    for i in range(n):
        v = args.start + (args.stop - args.start) * (i / (n - 1) if n > 1 else 0.0)
        for pname in args.policies.split(","):
            sc = dataclasses.replace(scen, **{section: dataclasses.replace(sec, **{fieldname: v})})
            r = run_average(_with_policy(sc, pname), args.seeds)
            rows.append({
                "value": round(v, 8),
                "policy": pname,
                "hit_ratio": round(r.hit_ratio, 4),
                "cost_total": round(r.cost_total, 6),
                "cost_per_req": round(r.cost_total / r.n_requests, 8) if r.n_requests else "-",
            })
    md, csv = sweep_table(rows)
    print(md)
    if args.csv:
        with open(args.csv, "w", encoding="utf-8") as f:
            f.write(csv)
        print(f"\nCSV 已写入 {args.csv}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="hithit", description="LLM API prompt 缓存命中建模工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("selfcheck", help="机制单元检查 + 解析式对照")
    s.set_defaults(fn=cmd_selfcheck)

    s = sub.add_parser("demo", help="中性默认场景下对比参照策略")
    s.add_argument("--seeds", type=int, default=1)
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("run", help="按场景 JSON 跑一次")
    s.add_argument("--scenario", required=True)
    s.add_argument("--policy", default=None, help="覆盖场景里的策略名")
    s.add_argument("--seed", type=int, default=None)
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("sweep", help="沿一个参数扫描并对比策略")
    s.add_argument("--scenario", required=True)
    s.add_argument("--param", required=True, help="如 mechanism.ttl 或 request_rate")
    s.add_argument("--start", type=float, required=True)
    s.add_argument("--stop", type=float, required=True)
    s.add_argument("--steps", type=int, default=6)
    s.add_argument("--policies", default="passthrough,sliding_window")
    s.add_argument("--seeds", type=int, default=3)
    s.add_argument("--csv", default=None, help="CSV 输出路径")
    s.set_defaults(fn=cmd_sweep)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
