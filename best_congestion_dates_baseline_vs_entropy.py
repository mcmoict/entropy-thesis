import json
from pathlib import Path

DATA_DIR = Path("results/figures/picking_animation_actual_data")

rows = []

def walk(obj):
    if isinstance(obj, dict):
        meta = obj.get("meta")

        if isinstance(meta, dict):
            date = meta.get("selected_date")
            method = meta.get("method")

            if date and method:
                rows.append({
                    "date": str(date),
                    "method": str(method),
                    "end_sec": float(meta.get("simulation_end_seconds", 0) or 0),
                    "conflicts": float(meta.get("congestion_conflicts", 0) or 0),
                    "wait_sec": float(meta.get("congestion_wait_seconds", 0) or 0),
                })

        for value in obj.values():
            walk(value)

    elif isinstance(obj, list):
        for value in obj:
            walk(value)


for path in sorted(DATA_DIR.glob("*.json")):
    print("READ:", path.name)

    with path.open("r", encoding="utf-8") as f:
        walk(json.load(f))


by_date = {}

for row in rows:
    by_date.setdefault(row["date"], {})[row["method"]] = row


result = []

for date, methods in by_date.items():

    if "observed" not in methods or "entropy" not in methods:
        continue

    base = methods["observed"]
    ent = methods["entropy"]

    conflict_diff = base["conflicts"] - ent["conflicts"]
    wait_diff = base["wait_sec"] - ent["wait_sec"]

    conflict_improvement_pct = (
        conflict_diff / base["conflicts"] * 100
        if base["conflicts"] > 0
        else 0
    )

    wait_improvement_pct = (
        wait_diff / base["wait_sec"] * 100
        if base["wait_sec"] > 0
        else 0
    )

    end_diff = base["end_sec"] - ent["end_sec"]
    end_improvement_pct = (
        end_diff / base["end_sec"] * 100
        if base["end_sec"] > 0
        else 0
    )

    result.append({
        "date": date,
        "baseline_conflicts": base["conflicts"],
        "entropy_conflicts": ent["conflicts"],
        "conflict_diff": conflict_diff,
        "conflict_improvement_pct": conflict_improvement_pct,
        "baseline_wait": base["wait_sec"],
        "entropy_wait": ent["wait_sec"],
        "wait_diff": wait_diff,
        "wait_improvement_pct": wait_improvement_pct,
        "baseline_end": base["end_sec"],
        "entropy_end": ent["end_sec"],
        "end_diff": end_diff,
        "end_improvement_pct": end_improvement_pct,
    })


def print_top(title, data, sort_key):
    ranked = sorted(data, key=sort_key, reverse=True)

    print()
    print("=" * 120)
    print(title)
    print("=" * 120)

    for rank, r in enumerate(ranked[:3], 1):
        print(
            f"{rank}. {r['date']} | "
            f"Conflicts={r['baseline_conflicts']:.0f}->{r['entropy_conflicts']:.0f} "
            f"(감소 {r['conflict_diff']:.0f}, {r['conflict_improvement_pct']:.2f}%) | "
            f"Wait={r['baseline_wait']:.2f}->{r['entropy_wait']:.2f}s "
            f"(감소 {r['wait_diff']:.2f}s, {r['wait_improvement_pct']:.2f}%) | "
            f"End={r['baseline_end']:.2f}->{r['entropy_end']:.2f}s "
            f"(단축 {r['end_diff']:.2f}s, {r['end_improvement_pct']:.2f}%)"
        )


# 1) 충돌 건수 감소율이 가장 큰 날짜
print_top(
    "Baseline vs Entropy | Conflicts 감소율 TOP 3",
    result,
    lambda x: (x["conflict_improvement_pct"], x["wait_improvement_pct"])
)

# 2) 충돌 건수 절대 감소량이 가장 큰 날짜
print_top(
    "Baseline vs Entropy | Conflicts 절대 감소량 TOP 3",
    result,
    lambda x: (x["conflict_diff"], x["conflict_improvement_pct"])
)

# 3) 대기시간 감소율이 가장 큰 날짜
print_top(
    "Baseline vs Entropy | Wait 감소율 TOP 3",
    result,
    lambda x: (x["wait_improvement_pct"], x["conflict_improvement_pct"])
)

# 4) 발표용 종합 후보:
#    Conflicts와 Wait가 모두 개선된 날짜만 대상으로,
#    두 개선율의 단순 평균을 사용
both_improved = [
    r for r in result
    if r["conflict_diff"] > 0 and r["wait_diff"] > 0
]

for r in both_improved:
    r["congestion_score"] = (
        r["conflict_improvement_pct"] + r["wait_improvement_pct"]
    ) / 2

ranked = sorted(
    both_improved,
    key=lambda x: (
        x["congestion_score"],
        x["conflict_improvement_pct"],
        x["wait_improvement_pct"],
    ),
    reverse=True
)

print()
print("=" * 120)
print("Baseline vs Entropy | 발표용 종합 혼잡 개선 TOP 3")
print("※ Conflicts와 Wait가 모두 감소한 날짜만 사용")
print("※ 점수 = (Conflicts 감소율 + Wait 감소율) / 2")
print("=" * 120)

for rank, r in enumerate(ranked[:3], 1):
    print(
        f"{rank}. {r['date']} | "
        f"종합점수={r['congestion_score']:.2f}% | "
        f"Conflicts={r['baseline_conflicts']:.0f}->{r['entropy_conflicts']:.0f} "
        f"({r['conflict_improvement_pct']:.2f}% 개선) | "
        f"Wait={r['baseline_wait']:.2f}->{r['entropy_wait']:.2f}s "
        f"({r['wait_improvement_pct']:.2f}% 개선) | "
        f"End={r['baseline_end']:.2f}->{r['entropy_end']:.2f}s "
        f"({r['end_improvement_pct']:.2f}% 개선)"
    )
