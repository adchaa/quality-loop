"""L2 arbiter: Pareto filter and weighted-deficit ordering."""
import json


def pareto_filter(patches):
    kept = []
    for p in patches:
        if p.get("regression_verdict") != "pass":
            continue
        dominated = False
        for q in patches:
            if q is p or q.get("regression_verdict") != "pass":
                continue
            pd = p.get("score_deltas", {})
            qd = q.get("score_deltas", {})
            keys = set(pd) | set(qd)
            if keys and all(qd.get(k, 0) >= pd.get(k, 0) for k in keys) and any(qd.get(k, 0) > pd.get(k, 0) for k in keys):
                dominated = True
                break
        if not dominated:
            kept.append(p)
    return kept


def weighted_deficit(scores, gates, weights):
    deficit = {}
    for char, score in scores.items():
        gate = gates.get(char, {})
        target = gate.get("target", 100)
        shortfall = max(0.0, target - float(score))
        deficit[char] = shortfall * float(weights.get(char, 0.25))
    return dict(sorted(deficit.items(), key=lambda kv: kv[1], reverse=True))


def select_next_round(issues, deficit_order, budget):
    ranked = sorted(
        issues,
        key=lambda i: deficit_order.get(i.get("iso_characteristic", ""), 0),
        reverse=True,
    )
    return ranked[:budget]


def main_demo():
    patches = [
        {"id": "perf-1", "regression_verdict": "pass", "score_deltas": {"performance_efficiency": 5, "interaction_capability": -1}},
        {"id": "a11y-1", "regression_verdict": "pass", "score_deltas": {"performance_efficiency": 1, "interaction_capability": 4}},
        {"id": "sec-1", "regression_verdict": "fail", "score_deltas": {"security": 9}},
    ]
    print(json.dumps({"frontier": pareto_filter(patches)}, indent=2))


if __name__ == "__main__":
    main_demo()
