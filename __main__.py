"""CLI 入口：python3 -m hithit {selfcheck|demo|run|sweep|profile|fit}。"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from .params import Scenario, load_scenario
from .report import compare_table, sweep_table
from .simulate import run, run_average, run_sweep, with_policy
from . import selfcheck as _selfcheck


def cmd_selfcheck(args) -> None:
    ok = _selfcheck.run_selfcheck()
    sys.exit(0 if ok else 1)


def cmd_demo(args) -> None:
    scen = Scenario()
    results = {
        name: run_average(with_policy(scen, name), args.seeds)
        for name in ("passthrough", "sliding_window")
    }
    print("默认中性场景（价目为占位值；正式评估请写场景 JSON，用 run / sweep 传入）\n")
    print(compare_table(results))


def cmd_run(args) -> None:
    scen = load_scenario(args.scenario)
    if args.policy:
        scen = with_policy(scen, args.policy)
    if args.seed is not None:
        scen = dataclasses.replace(scen, seed=args.seed)
    print(json.dumps(dataclasses.asdict(scen), ensure_ascii=False, indent=1))
    print()
    print(compare_table({scen.policy.name: run(scen)}))


def cmd_sweep(args) -> None:
    scen = load_scenario(args.scenario)
    rows = run_sweep(scen, args.param, args.start, args.stop, args.steps,
                     [p.strip() for p in args.policies.split(",")], args.seeds)
    md, csv = sweep_table(rows)
    print(md)
    if args.csv:
        with open(args.csv, "w", encoding="utf-8") as f:
            f.write(csv)
        print(f"\nCSV 已写入 {args.csv}")


def cmd_profile(args) -> None:
    from .profile import build_profile, load_events

    events = load_events(args.events)
    prof = build_profile(events, args.chars_per_token, source=args.source or args.events)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(prof, f, ensure_ascii=False, indent=1)
    days = prof["window"]["seconds"] / 86400
    print(f"画像已写入 {args.out}：会话 {prof['n_sessions']}，回合 {prof['n_turns']}，窗口 {days:.2f} 天")


def cmd_fit(args) -> None:
    from .fit import fit_scenario

    with open(args.profile, encoding="utf-8") as f:
        profile = json.load(f)
    scenario, warnings = fit_scenario(profile, args.stable_prefix_tokens, args.sim_time_cap)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(scenario, f, ensure_ascii=False, indent=1)
    print(f"场景已写入 {args.out}")
    for w in warnings:
        print(f"注意: {w}")


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

    s = sub.add_parser("profile", help="raw events JSONL → 特征画像 profile IR")
    s.add_argument("events")
    s.add_argument("-o", "--out", required=True)
    s.add_argument("--chars-per-token", type=float, default=2.0, help="chars→tokens 折算系数")
    s.add_argument("--source", default="", help="来源标注")
    s.set_defaults(fn=cmd_profile)

    s = sub.add_parser("fit", help="特征画像 profile IR → 场景 JSON")
    s.add_argument("profile")
    s.add_argument("-o", "--out", required=True)
    s.add_argument("--stable-prefix-tokens", type=float, default=None)
    s.add_argument("--sim-time-cap", type=float, default=604800.0, help="模拟窗口上限秒数")
    s.set_defaults(fn=cmd_fit)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
