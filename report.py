"""结果呈现：对比表与扫描表（markdown + CSV）。"""
from __future__ import annotations

from .simulate import Result


def compare_table(results: dict[str, Result]) -> str:
    header = ["策略", "请求数", "命中率", "未命中输入", "命中读", "写入", "输出", "总成本", "每请求成本"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for name, r in results.items():
        row = [
            name,
            r.n_requests,
            f"{r.hit_ratio:.1%}",
            f"{r.cost_input:.4g}",
            f"{r.cost_cached:.4g}",
            f"{r.cost_write:.4g}",
            f"{r.cost_output:.4g}",
            f"{r.cost_total:.4g}",
            f"{r.cost_total / r.n_requests:.4g}" if r.n_requests else "-",
        ]
        lines.append("| " + " | ".join(str(x) for x in row) + " |")
    return "\n".join(lines)


def sweep_table(rows: list[dict]) -> tuple[str, str]:
    keys = list(rows[0].keys())
    md = ["| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
    csv = [",".join(keys)]
    for row in rows:
        md.append("| " + " | ".join(str(row[k]) for k in keys) + " |")
        csv.append(",".join(str(row[k]) for k in keys))
    return "\n".join(md), "\n".join(csv) + "\n"
