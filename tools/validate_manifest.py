"""Validate a catalog manifest without fetching or executing app code."""

import argparse
import os
from pathlib import Path

from tools.check_manifest import annotation_line, check_manifest
from tools.check_registry import blocks, get
from tools.lib_comment import render_checks, verdict


def annotate(path: Path, findings: list[dict[str, str]]) -> None:
    """Print one GitHub annotation per finding; optional checks are warnings."""
    for item in findings:
        check = get(item["id"])
        message = check.problem + (f" ({item['detail']})" if item.get("detail") else "")
        message = f"{message} {check.fix}"
        line = annotation_line(path, item.get("detail", ""))
        level = "error" if blocks(item["id"], item["result"]) else "warning"
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{level} file={path},line={line},title={check.title}::{escaped}")


def main() -> int:
    """
    Print GitHub annotations and a step summary for the manifest checks.

    Non-zero status prevents the PR validation job from succeeding.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--base-ref")
    parser.add_argument(
        "--categories-url", default="https://cloud.busy.app/api/catalog/v1/categories"
    )
    args = parser.parse_args()
    findings = check_manifest(args.manifest, args.categories_url, args.base_ref)
    annotate(args.manifest, findings)
    lines = render_checks(findings, stages=("manifest",))
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as file:
            file.write("## Manifest checks\n\n" + "\n".join(lines) + "\n")
    return int(verdict(findings) == "fail")


if __name__ == "__main__":
    raise SystemExit(main())
