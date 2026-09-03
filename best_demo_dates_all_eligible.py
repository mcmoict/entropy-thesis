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
            end_sec = meta.get("simulation_end_seconds")

            if date and method and end_sec is not None:
                rows.append({
                    "date": str(date),
                    "method": str(method),
                    "end_sec": float(end_sec),
                    "conflicts": float(meta.get("congestion_conflicts", 0)),
                    "wait_sec": float(meta.get("congestion_wait_seconds", 0)),
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


comparators = {
    "Baseline": "observed",
    "Equal": "equal",
    "Random": "random",
}

for title, comparator in comparators.items():

    result = []

    for date, methods in by_date.items():

        if comparator not in methods or "entropy" not in methods:
            continue

        comp = methods[comparator]
        ent = methods["entropy"]

        diff_sec = comp["end_sec"] - ent["end_sec"]

        improvement_pct = (
            diff_sec / comp["end_sec"] * 100
            if comp["end_sec"] > 0
            else 0
        )

        result.append({
            "date": date,
            "comparator_sec": comp["end_sec"],
            "entropy_sec": ent["end_sec"],
            "diff_sec": diff_sec,
            "improvement_pct": improvement_pct,
            "comparator_conflicts": comp["conflicts"],
            "entropy_conflicts": ent["conflicts"],
            "comparator_wait": comp["wait_sec"],
            "entropy_wait": ent["wait_sec"],
        })

    result.sort(
        key=lambda x: (x["diff_sec"], x["improvement_pct"]),
        reverse=True
    )

    print()
    print("=" * 100)
    print(f"{title} vs Entropy | 전체 날짜 TOP 3 (작업 종료시간 차이 기준)")
    print("=" * 100)

    for rank, r in enumerate(result[:3], 1):
        print(
            f"{rank}. {r['date']} | "
            f"{title}={r['comparator_sec']:.2f}s | "
            f"Entropy={r['entropy_sec']:.2f}s | "
            f"단축={r['diff_sec']:.2f}s | "
            f"개선={r['improvement_pct']:.2f}% | "
            f"Conflicts={r['comparator_conflicts']:.0f}->{r['entropy_conflicts']:.0f} | "
            f"Wait={r['comparator_wait']:.2f}->{r['entropy_wait']:.2f}s"
        )
