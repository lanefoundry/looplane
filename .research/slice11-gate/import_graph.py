"""Compare cycle components against the captured pre-slice checkout."""

import importlib.util
import json
from pathlib import Path

out = Path(__file__).resolve().parent
root = out.parents[1]
baseline = Path((out / "baseline-path.txt").read_text().strip())
spec = importlib.util.spec_from_file_location(
    "boundaries", root / "tests/test_modularization_boundaries.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def components(graph):
    reachable = {}
    for start in graph:
        pending = list(graph[start])
        seen = set()
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(graph.get(current, ()))
        reachable[start] = seen
    return sorted({
        tuple(sorted(
            other for other in reachable[start]
            if start in reachable.get(other, ())
        ))
        for start in graph if start in reachable[start]
    })


graphs = {}
for label, checkout in [("before", baseline), ("after", root)]:
    module.ROOT = checkout / "src/looplane"
    graph = module._graph()
    graphs[label] = {"modules": len(graph), "cycles": components(graph), "edges": {
        key: sorted(value) for key, value in sorted(graph.items())
    }}
(out / "import-graph.json").write_text(json.dumps(graphs, indent=2) + "\n")
before = set(map(tuple, graphs["before"]["cycles"]))
after = set(map(tuple, graphs["after"]["cycles"]))
assert not after - before, f"New import cycles: {after - before}"
print("No new strongly connected components.")
print("Existing cycle components:", sorted(before))
print("Module counts:", graphs["before"]["modules"], "->", graphs["after"]["modules"])
