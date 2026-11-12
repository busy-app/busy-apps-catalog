"""AI review CI: request body, backend answer handling and the single comment."""

import json
from pathlib import Path

import httpx2
import pytest

from tools.review_cli import ReviewError, answer_or_error, ask_backend
from tools.review_comment import (
    MARKER,
    clean,
    find_review_comment,
    post_review,
    render_review,
)
from tools.review_request import ReviewInputError, build_request

SOURCE = {
    "origin": "https://github.com/author/app.git",
    "commit_sha": "a" * 40,
    "subdir": "apps/clock",
}


def write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A build directory, results.json and the PR manifest, as the pipeline leaves them."""
    build = tmp_path / "build"
    build.mkdir()
    (build / "manifest.resolved.json").write_text(
        json.dumps(
            {
                "category": "tools",
                "short_description": "Shows the time",
                "description": "Long text from README",
                "changelog": "First version",
                "screenshots": ["screenshot-1.png"],
                "appmeta": {
                    "id": "app.busy.clock",
                    "name": "Clock",
                    "version": "1.0.0",
                    "api_version": "1.2.0",
                },
            }
        )
    )
    results = tmp_path / "results.json"
    results.write_text(
        json.dumps(
            {"status": "pass", "checks": [{"id": "icon_missing", "result": "fail"}]}
        )
    )
    manifest = tmp_path / "manifest.yml"
    manifest.write_text(
        "sourcecode:\n  location:\n    origin: https://github.com/author/app\n"
        f"    commit_sha: {'a' * 40}\n    subdir: apps/clock\n"
        "screenshots: [media/one.png]\nback_screenshots: [media/back.png]\n"
        "description: '@README.md'\n"
    )
    return build, results, manifest


def test_request_uses_resolved_texts_and_original_paths(tmp_path: Path) -> None:
    """Texts come after `@file` substitution; screenshot paths stay the author's own."""
    build, results, manifest = write_inputs(tmp_path)
    body = build_request(build, results, manifest, 7, "moderator")
    assert body["pr_number"] == 7 and body["requested_by"] == "moderator"
    assert body["sourcecode"] == {
        "origin": "https://github.com/author/app",
        "commit_sha": "a" * 40,
        "subdir": "apps/clock",
    }
    text = body["manifest"]
    assert text["description"] == "Long text from README"
    assert text["screenshots"] == ["media/one.png"]
    assert text["back_screenshots"] == ["media/back.png"]
    assert body["facts"][0]["id"] == "icon_missing"


def test_request_refuses_a_symlinked_resolved_manifest(tmp_path: Path) -> None:
    """A symlink in the downloaded artifact is never followed."""
    build, results, manifest = write_inputs(tmp_path)
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (build / "manifest.resolved.json").unlink()
    (build / "manifest.resolved.json").symlink_to(target)
    with pytest.raises(ReviewInputError):
        build_request(build, results, manifest, 7, "moderator")


def test_clean_neutralises_html_and_mentions() -> None:
    """Model output cannot inject HTML or ping people."""
    text = clean("<img src=x> @octocat & more")
    assert "<" not in text and "@octocat" not in text and "&amp;" in text


def test_render_has_hint_header_sections_and_file_links() -> None:
    """The comment says it is a hint, links findings to the author's commit, shows usage."""
    answer = {
        "triage": {"flags": ["network access"]},
        "steps": {
            "content": {"status": "ok", "text": "Texts are fine."},
            "analyst": {"status": "ok", "text": "Reads the weather."},
            "reviewer": {
                "status": "ok",
                "findings": [
                    {
                        "title": "Eval",
                        "file": "src/main.js",
                        "line": 12,
                        "detail": "uses eval",
                    }
                ],
            },
        },
        "models": {"reviewer": "claude-sonnet-5-5"},
        "usage": {"input": 100, "output": 20},
        "duration_ms": 41200,
    }
    body = render_review(answer, SOURCE, "moderator")
    assert body.startswith(MARKER)
    assert "a hint for moderators, not a decision" in body
    assert "network access" in body and "Texts are fine." in body
    assert (
        f"https://github.com/author/app/blob/{'a' * 40}/apps/clock/src/main.js#L12"
        in body
    )
    assert "claude-sonnet-5-5" in body and "41.2 s" in body


def test_failed_step_is_named_and_others_still_render() -> None:
    """One failed expert does not hide the rest."""
    answer = {
        "triage": {"status": "failed"},
        "steps": {
            "content": {"status": "failed", "error": "invalid JSON twice"},
            "analyst": {"status": "ok", "text": "Dossier text"},
        },
    }
    body = render_review(answer, SOURCE, "moderator")
    assert "step failed: invalid JSON twice" in body
    assert "Dossier text" in body
    assert "default context was used" in body
    assert "### Findings" in body and "no answer" in body


def test_malformed_answer_does_not_raise() -> None:
    """An odd backend answer still renders a comment."""
    assert MARKER in render_review({"steps": "oops", "usage": 5}, SOURCE, "x")


class FakeGateway:
    """Comments of one PR; records what was written."""

    def __init__(self, comments: list[dict]) -> None:
        self._comments = comments
        self.created: list[str] = []
        self.updated: list[tuple[int, str]] = []

    def comments(self, number: int) -> list[dict]:
        return self._comments

    def create_comment(self, number: int, body: str) -> None:
        self.created.append(body)

    def update_comment(self, comment_id: int, body: str) -> None:
        self.updated.append((comment_id, body))


def test_first_run_creates_and_second_run_updates() -> None:
    """There is never a second AI review comment."""
    first = FakeGateway([])
    post_review(first, 7, f"{MARKER}\none")
    assert first.created and not first.updated
    existing = [
        {"id": 5, "user": {"login": "github-actions[bot]"}, "body": f"{MARKER}\nold"}
    ]
    second = FakeGateway(existing)
    post_review(second, 7, f"{MARKER}\ntwo")
    assert second.updated == [(5, f"{MARKER}\ntwo")] and not second.created


def test_marker_in_another_users_comment_is_ignored() -> None:
    """The workflow never edits a comment it does not own."""
    foreign = [{"id": 9, "user": {"login": "someone"}, "body": f"{MARKER}\nfake"}]
    assert find_review_comment(foreign, "github-actions[bot]") is None


def test_backend_called_once_with_the_token_header(mock_http) -> None:
    """The request goes to the review endpoint with the CI `Token` header and is never retried."""
    seen: list[httpx2.Request] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        return httpx2.Response(500, json={"detail": "provider down"})

    mock_http(handler)
    status, data = ask_backend("https://api.test/", "secret-token", {"pr_number": 1})
    assert status == 500 and data == {"detail": "provider down"}
    assert len(seen) == 1
    assert str(seen[0].url) == "https://api.test/api/catalog/v1/ci/reviews"
    assert seen[0].headers["token"] == "secret-token"


def test_unreachable_backend_is_a_clear_error(mock_http) -> None:
    """A transport error does not leak the URL or token."""

    def handler(req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("boom", request=req)

    mock_http(handler)
    with pytest.raises(ReviewError) as error:
        ask_backend("https://api.test", "secret-token", {})
    assert "unreachable" in str(error.value) and "secret-token" not in str(error.value)


@pytest.mark.parametrize(
    ("status", "needle"),
    [
        (404, "no review endpoint yet"),
        (401, "token was refused"),
        (403, "no permission"),
    ],
)
def test_error_statuses_have_clear_messages(status: int, needle: str) -> None:
    """The 404 of a backend without reviews says so plainly."""
    with pytest.raises(ReviewError, match=needle):
        answer_or_error(status, {})


def test_validation_error_shows_the_backend_reason() -> None:
    """A 422 such as `source_file_missing` is shown to the moderator."""
    with pytest.raises(ReviewError, match="source_file_missing"):
        answer_or_error(422, {"detail": "source_file_missing: media/one.png"})


def test_timeout_with_partial_result_is_rendered_as_partial() -> None:
    """A 504 carrying finished steps is not thrown away."""
    answer, partial = answer_or_error(504, {"steps": {"content": {"status": "ok"}}})
    assert partial and "content" in answer["steps"]
    with pytest.raises(ReviewError):
        answer_or_error(504, {})


SHA = "b" * 40


class FakeApi:
    """GitHub REST answers by path prefix; `_request` is what the tools call."""

    def __init__(self, pull: dict, files: list, runs: list, artifacts: dict) -> None:
        self.pull, self.files, self.runs, self.artifacts = pull, files, runs, artifacts

    def _request(self, method: str, path: str, body=None):
        import base64

        if path.startswith("pulls/7/files"):
            return self.files
        if path.startswith("pulls/7"):
            return self.pull
        if path.startswith("contents/"):
            text = base64.b64encode(b"sourcecode: {}\n").decode()
            return {"content": text}
        if path.startswith("actions/workflows"):
            return {"workflow_runs": self.runs}
        if path.startswith("actions/runs/"):
            run_id = int(path.split("/")[2])
            return {"artifacts": self.artifacts.get(run_id, [])}
        raise AssertionError(path)


def api(open_state: str = "open", artifacts: dict | None = None, files=None) -> FakeApi:
    """A PR with one manifest and one successful pipeline run with both artifacts."""
    names = [
        {"name": f"build-{SHA}", "expired": False},
        {"name": f"results-{SHA}", "expired": False},
    ]
    run = {
        "id": 11,
        "event": "workflow_run",
        "conclusion": "success",
        "run_number": 3,
        "run_attempt": 1,
    }
    return FakeApi(
        {"state": open_state, "head": {"sha": SHA}},
        files
        if files is not None
        else [{"filename": "applications/app.busy.clock/manifest.yml"}],
        [run],
        {11: names} if artifacts is None else artifacts,
    )


def test_discover_resolves_head_manifest_and_run(tmp_path: Path) -> None:
    """The outputs name the exact head, run and build artifact; the manifest is saved."""
    from tools.review_request import discover

    result = discover(api(), 7, tmp_path / "approved")
    assert result["head_sha"] == SHA and result["run_id"] == 11
    assert result["build_artifact"] == f"build-{SHA}"
    assert result["app_id"] == "app.busy.clock"
    assert (tmp_path / "approved" / "manifest.yml").is_file()


def test_discover_asks_to_run_the_checks_first(tmp_path: Path) -> None:
    """No run with artifacts for the head → a plain instruction."""
    from tools.review_request import discover

    with pytest.raises(ReviewInputError, match="Run the checks first"):
        discover(api(artifacts={}), 7, tmp_path / "approved")


def test_discover_refuses_closed_or_multi_file_prs(tmp_path: Path) -> None:
    """Only an open PR that changes exactly one manifest is reviewed."""
    from tools.review_request import discover

    with pytest.raises(ReviewInputError, match="not open"):
        discover(api(open_state="closed"), 7, tmp_path / "a")
    two = [
        {"filename": "applications/app.busy.clock/manifest.yml"},
        {"filename": "tools/x.py"},
    ]
    with pytest.raises(ReviewInputError, match="exactly one"):
        discover(api(files=two), 7, tmp_path / "b")


def test_the_footer_shows_tokens_per_step_not_raw_dicts() -> None:
    """`usage` is a dict of dicts: each step is `in/out`, cache reads and writes counted as input."""
    answer = {
        "usage": {
            "triage": {
                "input_tokens": 300,
                "output_tokens": 50,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 1200,
            },
            "reviewer": {
                "input_tokens": 20,
                "output_tokens": 4000,
                "cache_read_tokens": 1000,
            },
            "odd": "text",
        },
        "models": {"reviewer": "claude-sonnet-5-5"},
        "duration_ms": 67700,
    }
    body = render_review(answer, SOURCE, "moderator")
    assert "tokens (in/out): triage 1500/50, reviewer 1020/4000" in body
    assert "{'" not in body and "67.7 s" in body
    assert "tokens (in/out): n/a" in render_review({"usage": {"x": 1}}, SOURCE, "m")


TERMS_ANSWER = {
    "issues": [
        {
            "text": "Overrides the built-in BUSY timer after each Start press.",
            "term": "No bypassing limits",
            "step": "reviewer",
            "verified": True,
            "file": "src/main.ts",
            "line": 94,
            "where": None,
        },
        {
            "text": "Casino ad in a screenshot",
            "term": "UGC policies",
            "step": "content",
            "verified": True,
            "file": None,
            "line": None,
            "where": "art/shot.png",
        },
    ],
    "network": {"text": "none, local bar API only", "citations": []},
    "bar_api": {
        "text": "Draws, plays sounds",
        "citations": [{"file": "shared/device.ts", "line": 65}],
    },
    "content": "review",
    "about": "A two-player chess clock.",
    "read": [
        "src/main.ts",
        "src/appmeta/manifest.json",
        "package.json",
        "src/clock.ts",
    ],
    "not_read": [],
    "failed": {},
    "models": {"reviewer": "claude-sonnet-5-5"},
    "usage": {},
    "duration_ms": 4200,
}


def test_terms_answer_renders_count_issues_checked_and_links() -> None:
    """The terms shape: a count, numbered issues with term and link, Checked, About, Read."""
    body = render_review(TERMS_ANSWER, SOURCE, "moderator")
    assert body.startswith(
        MARKER + "\n### AI review\n\n**2 issues with the terms found.**"
    )
    assert (
        "1. Overrides the built-in BUSY timer after each Start press. Term: No bypassing limits. "
        "[`src/main.ts:94`](https://github.com/author/app/blob/"
        + "a" * 40
        + "/apps/clock/src/main.ts#L94)"
    ) in body
    assert "2. Casino ad in a screenshot. Term: UGC policies. `art/shot.png`" in body
    assert "- Network: none, local bar API only." in body
    assert "- Bar API: Draws, plays sounds. [`shared/device.ts:65`](" in body
    assert "- Content: a person should look." in body
    assert "**About the app:** A two-player chess clock.\n" in body
    assert "<details>" not in body
    assert (
        "<sub>Read: `src/main.ts`, `src/appmeta/manifest.json`, `package.json` and 1 more"
        " · `aaaaaaa` · 4 s</sub>"
    ) in body
    assert "Suggestion" not in body and "run by" not in body


def test_every_issue_is_listed_not_just_three() -> None:
    """Five issues are counted and listed; only a runaway answer is cut, and says so."""
    five = [{"text": f"Issue {n}", "term": "UGC policies"} for n in range(1, 6)]
    body = render_review({**TERMS_ANSWER, "issues": five}, SOURCE, "m")
    assert "**5 issues with the terms found.**" in body
    for n in range(1, 6):
        assert f"{n}. Issue {n}. Term: UGC policies. (place not confirmed)" in body
    assert "more issue" not in body
    many = [{"text": f"Issue {n}", "term": "UGC policies"} for n in range(32)]
    body = render_review({**TERMS_ANSWER, "issues": many}, SOURCE, "m")
    assert "**32 issues with the terms found.**" in body
    assert "30. Issue 29." in body and "31. " not in body
    assert "(place not confirmed)\n\nAnd 2 more issues, not shown.\n" in body


def test_no_issues_and_what_was_not_covered_are_said_exactly() -> None:
    """Zero issues says so; unread code or a failed content check narrows the claim."""
    answer = {**TERMS_ANSWER, "issues": [], "content": "clean"}
    assert "**No issues with the terms found.**" in render_review(answer, SOURCE, "m")
    partial = {**answer, "not_read": ["src/screens/clock.ts"]}
    body = render_review(partial, SOURCE, "m")
    assert "**No issues with the terms found in the files read.**" in body
    assert "not read: `src/screens/clock.ts`" in body
    no_content = {**answer, "content": None, "failed": {"content": "timed out"}}
    body = render_review(no_content, SOURCE, "m")
    assert "**No issues with the terms found; the content was not checked.**" in body
    assert "_The content check did not finish: timed out._" in body
    one = {**TERMS_ANSWER, "issues": TERMS_ANSWER["issues"][:1]}
    assert "**1 issue with the terms found.**" in render_review(one, SOURCE, "m")


@pytest.mark.parametrize(
    "changes",
    [
        {"failed": {"reviewer": "the model declined to review this submission"}},
        {"failed": ["reviewer"]},
        {"failed": {}, "network": None},
        {"failed": None},
    ],
)
def test_an_unfinished_code_review_never_reads_as_no_issues(changes) -> None:
    """Without the code review's answer there is no count, only that it did not finish."""
    answer = {**TERMS_ANSWER, "issues": [], "network": None, "bar_api": None, **changes}
    body = render_review(answer, SOURCE, "m")
    assert "**The review did not finish:**" in body and "No issues" not in body
    assert "- Network: not checked." in body and "Read:" not in body


def test_content_issues_after_a_failed_code_review_are_named_as_such() -> None:
    """The content check's issues still show, under a headline that says what failed."""
    answer = {
        **TERMS_ANSWER,
        "issues": TERMS_ANSWER["issues"][1:],
        "network": None,
        "failed": {"reviewer": "timed out"},
    }
    body = render_review(answer, SOURCE, "m")
    assert "**The review did not finish:** timed out." in body
    assert "1 issue found by the content check." in body


def test_an_issue_without_a_confirmed_place_says_so() -> None:
    """No link and no image name: the reader is told the place was not confirmed."""
    unverified = {"text": "Uses eval", "term": "No malicious code", "verified": False}
    answer = {**TERMS_ANSWER, "issues": [unverified, "Plain string issue"]}
    body = render_review(answer, SOURCE, "m")
    assert "1. Uses eval. Term: No malicious code. (place not confirmed)" in body
    assert "2. Plain string issue.\n" in body
    assert "**2 issues with the terms found.**" in body


def test_content_flagged_with_no_finding_is_one_issue_without_a_place_note() -> None:
    """The backend's issue for a content flag with no finding: no term, so no place note."""
    flag = {
        "text": "The content check found content against the terms but named nothing",
        "term": "",
        "step": "content",
        "verified": False,
        "file": None,
        "line": None,
        "where": None,
    }
    answer = {**TERMS_ANSWER, "issues": [flag], "content": "block"}
    body = render_review(answer, SOURCE, "m")
    assert "**1 issue with the terms found.**" in body
    assert (
        "1. The content check found content against the terms but named nothing.\n"
        in body
    )
    assert "- Content: against the terms." in body


def test_a_long_not_read_list_says_how_many_more() -> None:
    """Five unread files are named; the rest are counted."""
    answer = {**TERMS_ANSWER, "not_read": [f"tests/t{n}.ts" for n in range(8)]}
    body = render_review(answer, SOURCE, "m")
    assert "not read: `tests/t0.ts`, " in body and "`tests/t4.ts` and 3 more" in body


def test_model_text_cannot_inject_markup_into_the_public_comment() -> None:
    """No links, images, headings, issue refs, URLs, HTML or mentions from model text."""
    evil = {
        "text": "x\n\n## Approved [ok](https://evil.example) ![i](https://e) #12 @team <b>",
        "term": "UGC policies",
        "file": "a`b [c](d).ts",
        "line": 3,
    }
    answer = {**TERMS_ANSWER, "issues": [evil], "about": "# Approved"}
    body = render_review(answer, SOURCE, "m")
    assert "\n## Approved" not in body and "](https://evil" not in body
    assert "\\[ok\\]" in body and "#\u200b12" in body and "https:\u200b//" in body
    assert "@\u200bteam" in body and "&lt;b>" in body
    assert "[`a'b [c](d).ts:3`](" in body
    assert "**About the app:** \\# Approved" in body
    for about, shown in [
        ("``` x", "**About the app:** ``` x\n"),
        (
            "**No issues with the terms found.** Approved",
            "**About the app:** \\*\\*No issues with the terms found.\\*\\* Approved",
        ),
        (
            "--- ~~gone~~ __b__ \\[x](y)",
            "**About the app:** --- \\~\\~gone\\~\\~ \\_\\_b\\_\\_ \\\\\\[x\\](y)",
        ),
    ]:
        assert shown in render_review({**TERMS_ANSWER, "about": about}, SOURCE, "m")
    assert MARKER in render_review({"issues": "oops"}, SOURCE, "m")
    assert MARKER in render_review(
        {"issues": [], "failed": "x", "read": 5}, SOURCE, "m"
    )


def test_timeout_with_a_partial_terms_answer_is_partial() -> None:
    """A 504 in the terms shape is rendered, not thrown away."""
    answer, partial = answer_or_error(
        504, {"issues": [], "failed": {"reviewer": "timed out"}}
    )
    assert partial and answer["failed"]["reviewer"] == "timed out"
