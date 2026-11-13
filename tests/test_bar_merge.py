"""Merging the real-bar result into the checks results; registry; publication."""

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml

HEAD, RUN, ATT, JOB = "c" * 40, 8801, 1, "failure"
APP = "app.zzq.merge"
PKG = "5" * 64
H = "e" * 64
NOT_RUN = "The check on the real bar was not run because of the test stand; the moderator decides."


def req() -> dict:
    return {
        "head_sha": HEAD,
        "run_id": RUN,
        "run_attempt": ATT,
        "app_id": APP,
        "version": "2.0.1",
        "package_sha256": PKG,
    }


def good() -> dict:
    return {
        **req(),
        "status": "pass",
        "cleanup": "pass",
        "launch": "accepted",
        "observed_seconds": 60,
        "script_seen": True,
        "log_complete": True,
        "runtime_errors": [],
        "artifacts": {"app.log": H, "front.png": H, "back.png": H},
        "firmware": {"target": "f21", "version": "1.2.3", "sha256": H},
    }


def run_merge(
    tmp_path, evidence, *, job="success", request=None, checks=None, extra=None
):
    from tools.pipeline_report import merge_bar

    results = tmp_path / "results.json"
    base = {"status": "pass", "checks": checks or [], "skipped": []}
    base.update(extra or {})
    results.write_text(json.dumps(base))
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request or req()))
    ev = tmp_path / "evidence.json"
    if isinstance(evidence, (dict, list)):
        ev.write_text(json.dumps(evidence))
    elif isinstance(evidence, str):
        ev.write_text(evidence)
    merge_bar(results, request_path, ev, HEAD, RUN, ATT, job)
    return json.loads(results.read_text())


def bar_checks(data):
    return [c for c in data["checks"] if c["id"] in ("bar_smoke", "bar_unavailable")]


def assert_not_run(data):
    (check,) = bar_checks(data)
    assert (check["id"], check["result"]) == ("bar_unavailable", "not_run")
    assert check["detail"].startswith(NOT_RUN) and "Traceback" not in check["detail"]
    assert data["status"] == "pass"


def assert_app_fail(data):
    (check,) = bar_checks(data)
    assert check["id"] == "bar_smoke" and check["result"] == "fail"
    assert check["detail"].strip()
    assert data["status"] == "fail"
    return check


def test_full_pass(tmp_path):
    ev = good()
    data = run_merge(tmp_path, ev)
    assert bar_checks(data) == [{"id": "bar_smoke", "result": "pass"}]
    assert data["bar"] == ev and data["status"] == "pass"


def test_existing_block_skips_bar(tmp_path):
    data = run_merge(
        tmp_path,
        good(),
        checks=[{"id": "build", "result": "fail"}],
        extra={"status": "fail"},
    )
    assert "device" in data["skipped"] and bar_checks(data) == []
    assert data["status"] == "fail"


def test_removal_skips_bar(tmp_path):
    data = run_merge(tmp_path, good(), extra={"skip_reason": "removal"})
    assert "device" in data["skipped"] and bar_checks(data) == []


def test_request_of_another_run(tmp_path):
    other = {**req(), "run_id": RUN + 1}
    assert_not_run(run_merge(tmp_path, good(), request=other))
    other = {**req(), "head_sha": "d" * 40}
    assert_not_run(run_merge(tmp_path, good(), request=other))
    other = {**req(), "run_attempt": 2}
    assert_not_run(run_merge(tmp_path, good(), request=other))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(runtime_errors=["js_exception"]),
        lambda e: e.update(runtime_errors=["bar_crash_or_restart"]),
        lambda e: e.update(
            status="fail", error={"step": "observe", "kind": "script_not_seen"}
        ),
        lambda e: e.update(
            status="fail",
            error={"step": "stage", "type": "DeviceError", "http_status": 400},
        ),
        lambda e: e.update(
            status="fail",
            error={"step": "install", "type": "DeviceError", "http_status": 422},
        ),
        lambda e: e.update(
            status="fail",
            error={
                "step": "list",
                "type": "ObservationError",
                "kind": "installed_version_mismatch",
            },
        ),
        lambda e: e.update(
            status="fail",
            observed_seconds=23,
            error={"step": "observe", "kind": "bar_rebooted"},
        ),
    ],
)
def test_application_faults_block(tmp_path, mutate):
    ev = good()
    mutate(ev)
    ev["status"] = "fail"
    ev.setdefault("error", {})
    if "error" in ev and not ev["error"]:
        del ev["error"]
    check = assert_app_fail(run_merge(tmp_path, ev, job="failure"))
    assert "app log and screenshots" in check["detail"]


def test_app_fault_not_hidden_by_job_failure_or_success(tmp_path):
    ev = {**good(), "status": "fail", "runtime_errors": ["js_exception"]}
    for job in ("failure", "success"):
        assert_app_fail(run_merge(tmp_path, ev, job=job))


def test_app_fault_wins_over_stand_error(tmp_path):
    ev = {
        **good(),
        "status": "fail",
        "runtime_errors": ["js_exception"],
        "error": {"step": "observe", "kind": "uart_unavailable"},
    }
    assert_app_fail(run_merge(tmp_path, ev, job="failure"))


def test_detail_has_no_secret(tmp_path):
    ev = {
        **good(),
        "status": "fail",
        "runtime_errors": ["js_exception"],
        "error": {
            "step": "stage",
            "type": "DeviceError",
            "http_status": 400,
            "message": "token=SECRET-ZQ-TOKEN Authorization: Bearer SECRET-ZQ-TOKEN",
        },
    }
    data = run_merge(tmp_path, ev)
    check = assert_app_fail(data)
    assert "SECRET-ZQ-TOKEN" not in json.dumps(check) and "bar" not in data


@pytest.mark.parametrize("evidence", [None, "not json {", "[1, 2]", '"text"', "null"])
def test_unreadable_evidence_is_not_run(tmp_path, evidence):
    assert_not_run(run_merge(tmp_path, evidence))


def test_foreign_result_is_not_run(tmp_path):
    for field, value in [
        ("package_sha256", "0" * 64),
        ("run_id", RUN + 5),
        ("head_sha", "d" * 40),
        ("app_id", "app.other.x"),
        ("version", "9.9.9"),
        ("run_attempt", 7),
    ]:
        ev = {**good(), field: value}
        assert_not_run(run_merge(tmp_path, ev))
        ev = {
            **good(),
            field: value,
            "status": "fail",
            "runtime_errors": ["js_exception"],
        }
        assert_not_run(run_merge(tmp_path, ev))


@pytest.mark.parametrize("job", ["cancelled", "skipped"])
def test_cancelled_or_skipped_job(tmp_path, job):
    assert_not_run(run_merge(tmp_path, good(), job=job))
    assert_not_run(
        run_merge(
            tmp_path,
            {**good(), "status": "fail", "runtime_errors": ["js_exception"]},
            job=job,
        )
    )


def test_plain_string_error(tmp_path):
    ev = {**good(), "status": "fail", "error": "BAR check could not complete (x)"}
    assert_not_run(run_merge(tmp_path, ev))


@pytest.mark.parametrize(
    "kind",
    [
        "uart_unavailable",
        "serial_log_lost",
        "serial_log_flood",
        "serial_line_too_long",
        "application_log_flood",
        "wifi_reconnected",
        "invalid_serial_log",
    ],
)
def test_stand_error_kinds(tmp_path, kind):
    ev = {**good(), "status": "fail", "error": {"step": "observe", "kind": kind}}
    assert_not_run(run_merge(tmp_path, ev))


def test_cleanup_failure_alone(tmp_path):
    assert_not_run(run_merge(tmp_path, {**good(), "cleanup": "fail"}))


@pytest.mark.parametrize(
    "step", ["prepare", "screenshot_front", "screenshot_back", "cleanup"]
)
def test_stand_steps(tmp_path, step):
    ev = {
        **good(),
        "status": "fail",
        "error": {"step": step, "type": "DeviceError", "http_status": 400},
    }
    assert_not_run(run_merge(tmp_path, ev))


@pytest.mark.parametrize("step", ["stage", "install"])
@pytest.mark.parametrize("status", [0, 500, 503])
def test_device_error_non_4xx(tmp_path, step, status):
    ev = {
        **good(),
        "status": "fail",
        "error": {"step": step, "type": "DeviceError", "http_status": status},
    }
    assert_not_run(run_merge(tmp_path, ev))


def test_unsure_failure(tmp_path):
    ev = {**good(), "status": "fail", "error": {"step": "launch", "type": "KeyError"}}
    assert_not_run(run_merge(tmp_path, ev))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(observed_seconds=59),
        lambda e: e.update(script_seen=False),
        lambda e: e.update(artifacts={"app.log": H}),
        lambda e: e.update(firmware={"target": "f99", "version": "1.2.3", "sha256": H}),
    ],
)
def test_incomplete_pass_is_never_pass(tmp_path, mutate):
    ev = good()
    mutate(ev)
    data = run_merge(tmp_path, ev)
    assert all(c["result"] != "pass" for c in bar_checks(data))


# ---- registry ---------------------------------------------------------------


def test_registry_bar_checks():
    from tools.check_registry import ICON_WARN, REGISTRY, STAGES, blocks, icon

    assert blocks("bar_smoke", "fail") is True
    assert REGISTRY["bar_smoke"].required is True
    unavailable = REGISTRY["bar_unavailable"]
    assert unavailable.stage == "device" and "device" in STAGES
    assert unavailable.required is False
    assert blocks("bar_unavailable", "not_run") is False
    assert icon("bar_unavailable", "not_run") == ICON_WARN
    assert unavailable.title != REGISTRY["bar_smoke"].title
    assert unavailable.problem.strip() and unavailable.fix.strip()


# ---- publication ------------------------------------------------------------


def make_build(tmp_path: Path, make_png):
    build_dir = tmp_path / "build_dir"
    (build_dir / "media").mkdir(parents=True)
    original = {
        "sourcecode": {
            "type": "git",
            "location": {
                "origin": "https://github.com/owner/clock",
                "commit_sha": "a" * 40,
            },
        },
        "category": "clocks",
        "short_description": "A small clock for the bar",
        "description": "A small clock for the BUSY Bar that shows the time and the date.",
        "changelog": "Initial release",
        "screenshots": ["screen.png"],
    }
    appmeta = {
        "format_version": 1,
        "id": "app.busy.clock",
        "name": "Clock",
        "version": "1.0.0",
        "api_version": "27.14.0",
    }
    resolved = {
        **original,
        "screenshots": ["screenshot-1.png"],
        "appmeta": appmeta,
        "toolchain": "0.2.1",
    }
    (build_dir / "manifest.resolved.json").write_text(json.dumps(resolved))
    (build_dir / "sources.tar.gz").write_bytes(b"sources")
    (build_dir / "media" / "screenshot-1.png").write_bytes(make_png(72, 16))
    with tarfile.open(
        build_dir / "package.tgz", "w:gz", format=tarfile.USTAR_FORMAT
    ) as tar:
        for name, content in {
            "app.busy.clock/appmeta/manifest.json": json.dumps(appmeta).encode(),
            "app.busy.clock/scripts/main.js": b"// test",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    manifest = tmp_path / "manifest.yml"
    manifest.write_text(yaml.safe_dump(original))
    return build_dir, manifest


def bar_block(build_dir: Path, sha: str | None = None) -> dict:
    return {
        "app_id": "app.busy.clock",
        "version": "1.0.0",
        "head_sha": "b" * 40,
        "run_id": 9,
        "run_attempt": 1,
        "package_sha256": sha
        or hashlib.sha256((build_dir / "package.tgz").read_bytes()).hexdigest(),
        "status": "pass",
        "cleanup": "pass",
        "launch": "accepted",
        "observed_seconds": 60,
        "script_seen": True,
        "log_complete": True,
        "runtime_errors": [],
        "artifacts": {"app.log": "d" * 64, "front.png": "e" * 64, "back.png": "f" * 64},
        "firmware": {"version": "1.4.1", "target": "f22", "sha256": "f" * 64},
    }


def publish(tmp_path, build_dir, manifest, results_obj, name="o"):
    from tools.lib_publication_data import prepare_publication

    results = tmp_path / f"results-{name}.json"
    results.write_text(json.dumps(results_obj))
    return prepare_publication(
        build_dir,
        results,
        manifest,
        tmp_path / name,
        7,
        "b" * 40,
        "c" * 40,
        "reviewer",
        "app.busy.clock",
    )


def test_publication_without_bar(tmp_path, make_png):
    build_dir, manifest = make_build(tmp_path, make_png)
    data = publish(tmp_path, build_dir, manifest, {"status": "pass", "checks": []})
    assert data["alias"] == "app.busy.clock"


def test_publication_with_bound_bar(tmp_path, make_png):
    build_dir, manifest = make_build(tmp_path, make_png)
    data = publish(
        tmp_path,
        build_dir,
        manifest,
        {"status": "pass", "checks": [], "bar": bar_block(build_dir)},
    )
    assert data["alias"] == "app.busy.clock"


def test_publication_rejects_foreign_bar(tmp_path, make_png):
    build_dir, manifest = make_build(tmp_path, make_png)
    with pytest.raises(ValueError):
        publish(
            tmp_path,
            build_dir,
            manifest,
            {"status": "pass", "checks": [], "bar": bar_block(build_dir, "0" * 64)},
        )


def test_read_results_without_bar_check(tmp_path):
    from tools.pipeline_report import read_results

    path = tmp_path / "results.json"
    path.write_text(
        '{"status":"pass","checks":[{"id":"category_alias","result":"pass"}],"skipped":[]}'
    )
    assert read_results(path) == [
        {"id": "category_alias", "result": "pass", "detail": ""}
    ]
    path.write_text('{"status":"pass","checks":[]}')
    assert read_results(path) == []


@pytest.mark.parametrize("status", [401, 403, 404, 408, 409, 423, 429])
@pytest.mark.parametrize("step", ["stage", "install"])
def test_stand_like_device_answers_are_not_the_authors(tmp_path, step, status):
    """408 is a slow upload, 409/423 a busy stand: only a refused package is the app's."""
    ev = {
        **good(),
        "status": "fail",
        "error": {"step": step, "type": "DeviceError", "http_status": status},
    }
    assert_not_run(run_merge(tmp_path, ev))


@pytest.mark.parametrize(
    "error",
    [
        {"step": "list", "type": "ValueError"},
        {"step": "observe", "kind": "script_not_seen"},
        {"step": "observe", "kind": "bar_rebooted"},
        {"step": "observe", "kind": "bar_identity_changed"},
        {"step": "observe", "kind": "serial_log_silent"},
        {"step": "observe", "kind": "application_log_flood"},
    ],
)
def test_unsure_or_stand_kinds_are_not_run(tmp_path, error):
    """Without a launch (or with a swapped bar, a silent logger) nothing is the author's."""
    ev = {**good(), "status": "fail", "error": error}
    if error.get("kind") in ("script_not_seen", "bar_rebooted"):
        ev["launch"] = "not_run"
    assert_not_run(run_merge(tmp_path, ev))


def test_restart_before_the_script_ran_is_not_the_apps(tmp_path):
    """A reboot line in the log counts against the app only after its script was seen."""
    ev = {
        **good(),
        "status": "fail",
        "script_seen": False,
        "runtime_errors": ["bar_crash_or_restart"],
    }
    assert_not_run(run_merge(tmp_path, ev))


def test_malformed_fields_never_crash_or_fail(tmp_path):
    """Broken types are "unsure": not_run, and the report still gets written."""
    for ev in (
        {**good(), "status": "fail", "runtime_errors": [[1]]},
        {**good(), "status": "fail", "runtime_errors": [{}], "error": {"step": []}},
        {
            **good(),
            "status": "fail",
            "error": {"step": "stage", "type": "DeviceError", "http_status": [400]},
        },
        {**good(), "status": "fail", "runtime_errors": "js_exception"},
    ):
        assert_not_run(run_merge(tmp_path, ev))


def test_application_fault_directly():
    from tools.bar import application_fault

    assert application_fault(
        {"launch": "accepted", "script_seen": True, "runtime_errors": ["js_exception"]}
    )
    assert application_fault(
        {"launch": "accepted", "error": {"kind": "script_not_seen"}}
    )
    assert (
        application_fault({"launch": "accepted", "error": {"kind": "uart_unavailable"}})
        is None
    )
    assert application_fault({}) is None
    assert (
        application_fault({"error": "BAR check could not complete (ValueError)"})
        is None
    )


def test_comment_counts_do_not_call_a_not_run_a_pass():
    from tools.lib_comment import passed_count

    not_run = [{"id": "bar_unavailable", "result": "not_run"}]
    failed = [{"id": "bar_smoke", "result": "fail"}]
    assert passed_count("device", not_run) == 0
    assert passed_count("device", failed) == 0
    assert passed_count("device", []) == 1
