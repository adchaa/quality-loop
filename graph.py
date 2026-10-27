"""State-graph engine: nodes, edges, conditional routing, checkpoints, budgets."""
import json
import time
from pathlib import Path


END = "END"


class Graph:
    def __init__(self, artifacts="loop/artifacts", budgets=None):
        self.nodes = {}
        self.edges = {}
        self.art = Path(artifacts)
        self.art.mkdir(parents=True, exist_ok=True)
        self.budgets = {"max_steps": 50, "tokens_per_run": 50000}
        if budgets:
            self.budgets.update(budgets)
        self.audit = []

    def add_node(self, name, fn):
        self.nodes[name] = fn

    def add_edge(self, src, dst):
        self.edges[src] = dst

    def add_cond(self, src, fn):
        self.edges[src] = fn

    def charge(self, state, n):
        state["tokens_used"] = state.get("tokens_used", 0) + n

    def checkpoint(self, state, node):
        small = {k: v for k, v in state.items() if k not in ("issues", "shortlist", "patches")}
        small["issues"] = len(state.get("issues", []))
        small["shortlist"] = len(state.get("shortlist", []))
        small["patches"] = len(state.get("patches", []))
        snap = {"node": node, "tokens": state.get("tokens_used", 0), "t": time.time(),
                "state": json.loads(json.dumps(small))}
        (self.art / f"ckpt-{node}.json").write_text(json.dumps(snap, indent=2))

    def run(self, entry, state):
        cur = entry
        seen = {}
        for step in range(self.budgets["max_steps"]):
            if cur == END:
                break
            if cur not in self.nodes:
                raise KeyError(f"unknown node: {cur}")
            self.audit.append({"step": step, "node": cur})
            out = self.nodes[cur](state, self)
            if out is not None:
                state = out
            self.checkpoint(state, cur)
            if state.get("tokens_used", 0) > self.budgets["tokens_per_run"]:
                state["stopped"] = "token_budget"
                break
            nxt = self.edges.get(cur)
            nxt = nxt(state) if callable(nxt) else nxt
            key = (cur, str(state.get("sig")))
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 3:
                state["loop_detected"] = cur
                cur = "memory"
                continue
            cur = nxt or END
        (self.art / "audit.json").write_text(json.dumps(self.audit, indent=2))
        return state
