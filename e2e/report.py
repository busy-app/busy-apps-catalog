"""Render observable scenario results grouped by the owning MVP task."""

NETWORK = "network / harness"
UNFINISHED = "not implemented"


def render(results: list[tuple]) -> str:
    """
    Count pass/failure outcomes per task while retaining failing scenario names.

    A scenario without a task is an invalid suite, never an unreported omission.
    A result may carry a fourth item, the failure cause; each failed scenario is
    counted once per cause even if it covers two tasks.
    """
    tasks = {}
    causes: dict[str, set[str]] = {}
    for task, scenario, outcome, *rest in results:
        if not task:
            raise ValueError(f"Missing task marker: {scenario}")
        counts = tasks.setdefault(task, [0, 0])
        counts[0 if outcome == "passed" else 1] += 1
        if outcome != "passed":
            causes.setdefault(rest[0] if rest else UNFINISHED, set()).add(scenario)
    lines = ["# E2E report", "", "| Task | Passed | Failed |", "|---|---:|---:|"]
    lines += [
        f"| {task} | {counts[0]} | {counts[1]} |"
        for task, counts in sorted(tasks.items())
    ]
    if causes:
        lines += ["", "| Failure cause | Scenarios |", "|---|---:|"]
        lines += [
            f"| {label} | {len(names)} |" for label, names in sorted(causes.items())
        ]
    lines += ["", "| Scenario | Task | Outcome |", "|---|---|---|"]
    lines += [
        f"| {scenario} | {task} | {outcome} |"
        for task, scenario, outcome, *_ in results
    ]
    return "\n".join(lines) + "\n"
