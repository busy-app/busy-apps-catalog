"""Publish a catalog report and check run through a GitHub API adapter."""

import argparse
import json
import os
import re
import sys
import traceback
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from tools.bar import BINDING, judge, read_json, write_json

from tools.check_registry import REGISTRY, RESULTS, RETIRED, STAGES, blocks, get, outcome
from tools.lib_comment import SKIP_REASONS, find_comment, render_comment, verdict
from tools.lib_github import GitHubGateway
from tools.lib_license import ALLOWED
from tools.lib_media import parse_view


class ReportGateway(Protocol):
    """GitHub operations required by the report use case."""

    def pull_head(self, number: int) -> str:
        """
        Obtain the current head SHA for a pull request.

        The report must not be attributed to an obsolete revision.
        """
        ...

    def comments(self, number: int) -> Sequence[Mapping[str, object]]:
        """
        Obtain existing pull-request comments for marker matching.

        The adapter is responsible for pagination.
        """
        ...

    def create_comment(self, number: int, body: str) -> None:
        """
        Create the first bot-owned checks comment.

        The body is rendered by the report use case.
        """
        ...

    def update_comment(self, comment_id: int, body: str) -> None:
        """
        Update a previous bot-owned checks comment.

        The selected comment must belong to the configured bot.
        """
        ...

    def create_check(
        self,
        sha: str,
        conclusion: str,
        summary: str,
        annotations: Sequence[Mapping[str, object]] = (),
    ) -> None:
        """
        Create a check run for the reviewed head commit.

        Conclusion, summary and annotations reflect the supplied check findings.
        """
        ...


def annotations(
    findings: Sequence[Mapping[str, str]], manifest_path: str
) -> list[dict[str, object]]:
    """Turn problems into check-run annotations; optional ones are warnings."""
    result = []
    for item in findings:
        check = get(item["id"])
        detail = f" ({item['detail']})" if item.get("detail") else ""
        result.append(
            {
                "path": manifest_path,
                "start_line": 1,
                "end_line": 1,
                "annotation_level": "failure"
                if blocks(item["id"], item["result"])
                else "warning",
                "title": check.title,
                "message": f"{check.problem}{detail} {check.fix}"[:60000],
            }
        )
    return result


def publish_report(
    gateway: ReportGateway,
    number: int,
    sha: str,
    bot_login: str,
    findings: Sequence[Mapping[str, str]],
    compare_url: str | None = None,
    inventory: Sequence[Mapping[str, object]] = (),
    skipped: Sequence[str] = (),
    author_attested: bool = False,
    manifest_path: str = "",
    skip_reason: str = "build",
    license_name: str | None = None,
    media: dict[str, object] | None = None,
    removal: dict[str, object] | None = None,
    bar_media: dict[str, object] | None = None,
) -> None:
    """
    Publish or update a report and create a matching GitHub check run.

    Refuse stale results and never edit a comment written by another user. The check
    fails only when a required check failed; warnings stay green but are annotated.
    """
    if gateway.pull_head(number) != sha:
        raise ValueError("Pull request head changed; refuse stale report")
    body = render_comment(
        findings,
        compare_url,
        inventory,
        skipped,
        author_attested,
        skip_reason,
        license_name,
        media,
        removal,
        bar_media,
    )
    comment_id = find_comment(gateway.comments(number), bot_login)
    if comment_id is None:
        gateway.create_comment(number, body)
    else:
        gateway.update_comment(comment_id, body)
    conclusion = "failure" if verdict(findings) == "fail" else "success"
    gateway.create_check(
        sha,
        conclusion,
        body,
        annotations(findings, manifest_path) if manifest_path else (),
    )


def read_results(path: Path) -> list[dict[str, str]]:
    """
    Read and validate bounded untrusted results from the check job.

    Malformed or inconsistent results are rejected instead of reporting success.
    """
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Results file exceeds size limit")
    result = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(result, dict)
        or result.get("status") not in {"pass", "fail"}
        or not isinstance(result.get("checks"), list)
    ):
        raise ValueError("Invalid checks result")
    checks = result["checks"]
    if len(checks) > 500 or any(
        not isinstance(item, dict)
        or (item.get("id") not in REGISTRY and item.get("id") not in RETIRED)
        or item.get("result") not in RESULTS
        or not isinstance(item.get("detail", ""), str)
        or len(item.get("detail", "")) > 2000
        for item in checks
    ):
        raise ValueError("Invalid check finding")
    checks = [item for item in checks if item["id"] not in RETIRED]
    blocking = any(blocks(item["id"], item["result"]) for item in checks)
    if (result["status"] == "fail") != blocking:
        raise ValueError("Inconsistent check outcome")
    return [
        {"id": item["id"], "result": item["result"], "detail": item.get("detail", "")}
        for item in checks
    ]


def read_extra(path: Path) -> dict[str, object]:
    """Read the skipped stages and the author confirmation from trusted results."""
    data = json.loads(path.read_text(encoding="utf-8"))
    skipped = data.get("skipped", []) if isinstance(data, dict) else None
    if not isinstance(skipped, list) or any(item not in STAGES for item in skipped):
        raise ValueError("Invalid skipped stages")
    manifest = data.get("manifest_path", "")
    if not isinstance(manifest, str) or len(manifest) > 200:
        raise ValueError("Invalid manifest path")
    reason = data.get("skip_reason", "build")
    if reason not in SKIP_REASONS:
        raise ValueError("Invalid skip reason")
    return {
        "skipped": skipped,
        "skip_reason": reason,
        "author_attested": data.get("author_attested") is True,
        "manifest_path": manifest,
        "removal": read_removal(data.get("removal")),
    }


def read_removal(value: object) -> dict[str, object] | None:
    """The removal facts, kept to known fields of the right type; anything else is none."""
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise TypeError("Invalid removal")
    return {
        key: value.get(key) if isinstance(value.get(key), str) else ""
        for key in ("id", "name", "version", "author")
    } | {"published": value.get("published") is True}


def read_inventory(path: Path) -> list[dict[str, object]]:
    """
    Read a bounded list of package files from the trusted check artifact.

    Invalid entries are rejected before any report is sent.
    """
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Results file exceeds size limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    inventory = data.get("inventory", []) if isinstance(data, dict) else None
    if (
        not isinstance(inventory, list)
        or len(inventory) > 10000
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or len(item["path"]) > 1024
            or type(item.get("size")) is not int
            or item["size"] < 0
            for item in inventory
        )
    ):
        raise ValueError("Invalid package inventory")
    return [{"path": item["path"], "size": item["size"]} for item in inventory]


def read_license(path: Path) -> str | None:
    """Read the recognised license name from trusted results (only known SPDX ids)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    value = data.get("license") if isinstance(data, dict) else None
    return value if value in ALLOWED else None


def read_media(path: Path) -> dict[str, object] | None:
    """Read the validated image links of the author's repository from the results."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return parse_view(data.get("media")) if isinstance(data, dict) else None


def read_bar_media(path: Path, run_url: str, artifact_url: str) -> dict[str, object] | None:
    """Link only this run's BAR captures, without treating repository images as evidence."""
    match = re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/([1-9][0-9]*)/artifacts/[1-9][0-9]*",
        artifact_url,
    )
    if not match or not artifact_url.startswith(run_url + "/artifacts/"):
        return None
    result = read_json(path).get("bar")
    if not isinstance(result, dict) or result.get("run_id") != int(match[1]):
        return None
    firmware, seconds, artifacts = result.get("firmware"), result.get("observed_seconds"), result.get("artifacts")
    version = firmware.get("version") if isinstance(firmware, dict) else None
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        return None
    if type(seconds) is not int or not 0 <= seconds <= 60 or not isinstance(artifacts, dict):
        return None
    screenshots = [name for name in ("front.png", "back.png") if re.fullmatch(r"[0-9a-f]{64}", str(artifacts.get(name, "")))]
    if not screenshots:
        return None
    return {"artifact_url": artifact_url, "firmware_version": version, "observed_seconds": seconds, "screenshots": screenshots}


def read_compare_url(path: Path) -> str | None:
    """Read an optional same-repository GitHub compare URL from trusted results."""
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Results file exceeds size limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    value = data.get("compare_url") if isinstance(data, dict) else None
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > 500
        or not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/compare/[0-9a-f]{40}\.{3}[0-9a-f]{40}",
            value,
        )
    ):
        raise ValueError("Invalid source comparison URL")
    return value


def load_report_inputs(results: Path, run_url: str, artifact_url: str = "") -> dict:
    """
    Everything the report needs from results.json.

    Missing or broken results are not the author's fault: the trace stays in the job
    log and the report says the checks could not finish.
    """
    try:
        extra = read_extra(results)
        return {
            "findings": read_results(results),
            "inventory": read_inventory(results),
            "compare_url": read_compare_url(results),
            "license_name": read_license(results),
            "media": read_media(results),
            "bar_media": read_bar_media(results, run_url, artifact_url),
            "skipped": extra["skipped"],
            "author_attested": extra["author_attested"],
            "manifest_path": extra["manifest_path"],
            "skip_reason": extra["skip_reason"],
            "removal": extra["removal"],
        }
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return {
            "findings": [outcome("internal_error", "fail", run_url)],
            "inventory": [],
            "compare_url": None,
            "license_name": None,
            "media": None,
            "bar_media": None,
            "skipped": ["manifest", "build", "package", "catalog", "device"],
            "author_attested": False,
            "manifest_path": "",
            "skip_reason": "internal",
            "removal": None,
        }


STAND_NOT_RUN = (
    "The check on the real bar was not run because of the test stand; the moderator decides."
)


def bar_finding(request: Path, evidence: Path, head: str, run: int, attempt: int, job: str):
    """
    The check entry for the hardware result, and the evidence to keep (only on a pass).

    Only an application fault in a result of this run fails; the stand never fails the author.
    """

    def stand(why: str):
        return outcome("bar_unavailable", "not_run", f"{STAND_NOT_RUN} ({why}.)"), None

    if job in {"cancelled", "skipped"}:
        return stand("the BAR stand was not available or did not finish in time")
    try:
        expected = read_json(request)
        if (expected.get("head_sha"), expected.get("run_id"), expected.get("run_attempt")) != (head, run, attempt):
            return stand("the hardware request belongs to another run")
        observed = read_json(evidence)
    except (OSError, ValueError):
        return stand("there is no usable result from the hardware job")
    verdict, why = judge(observed, expected)
    if verdict == "pass":
        return outcome("bar_smoke", "pass"), observed
    if verdict == "fail":
        detail = f"Failed on the real bar: {why}. The app log and screenshots are in the hardware job artifact."
        return outcome("bar_smoke", "fail", detail), None
    if all(observed.get(key) == expected.get(key) for key in BINDING):
        error = observed.get("error")
        kind = error.get("kind") if isinstance(error, dict) else None
        reasons = {
            "serial_log_silent": "no native application logs were received",
            "native_log_unavailable": "the BAR log stream over USB CLI could not be opened",
            "native_log_closed": "the BAR log connection closed during observation",
            "native_log_flood": "the BAR log exceeded the observation limit",
            "native_line_too_long": "a BAR log line exceeded the observation limit",
        }
        if isinstance(kind, str):
            why = reasons.get(kind, why)
    return stand(why)


def merge_bar(results: Path, request: Path, evidence: Path, head: str, run: int, attempt: int, job: str) -> None:
    """Fold the same-run hardware result into the checks: pass, an application fail, or not run."""
    try:
        findings, data = read_results(results), read_json(results)
    except (OSError, ValueError):
        write_json(results, {"status": "fail", "checks": [outcome("internal_error")],
                            "skipped": ["manifest", "build", "package", "catalog", "device"], "skip_reason": "internal"})
        return
    if any(blocks(i["id"], i["result"]) for i in findings) or data.get("skip_reason") == "removal":
        data["skipped"] = list(dict.fromkeys([*data.get("skipped", []), "device"]))
    else:
        finding, observed = bar_finding(request, evidence, head, run, attempt, job)
        if observed is not None:
            data["bar"] = observed
        # Results list only what did not pass; a pass is the evidence alone, like every other check.
        if finding["result"] != "pass":
            findings.append(finding)
        data["checks"] = findings
        data["status"] = "fail" if any(blocks(i["id"], i["result"]) for i in findings) else "pass"
    write_json(results, data)


def main() -> int:
    """
    Publish validated results for a pull request using the GitHub job token.

    Missing input or stale revisions fail without claiming a successful check.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--bot", default="github-actions[bot]")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--bar-result", type=Path)
    parser.add_argument("--bar-request", type=Path)
    args = parser.parse_args()
    if bool(args.bar_result) != bool(args.bar_request):
        parser.error("--bar-result and --bar-request must be supplied together")
    token = os.environ.get("GITHUB_TOKEN", "")
    if args.pr < 1 or re.fullmatch(r"[0-9a-f]{40}", args.sha) is None:
        raise ValueError("Invalid pull request or commit SHA")
    if args.bar_result:
        merge_bar(args.results, args.bar_request, args.bar_result, args.sha,
                  int(os.environ["GITHUB_RUN_ID"]), int(os.environ["GITHUB_RUN_ATTEMPT"]),
                  os.environ.get("BAR_JOB_RESULT", ""))
    inputs = load_report_inputs(args.results, args.run_url, os.environ.get("BAR_ARTIFACT_URL", ""))
    findings = inputs.pop("findings")
    publish_report(
        GitHubGateway(args.repository, token),
        args.pr,
        args.sha,
        args.bot,
        findings,
        **inputs,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
