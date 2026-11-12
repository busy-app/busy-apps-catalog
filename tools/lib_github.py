"""GitHub API adapter for publishing trusted catalog reports."""

import json
import re
from collections.abc import Mapping, Sequence

from tools.lib_http import HttpError, request

REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_RESPONSE = 16 * 1024 * 1024


class GitHubGateway:
    """Read pull-request state and write bot reports through GitHub's REST API."""

    def __init__(self, repository: str, token: str) -> None:
        """
        Store the repository and access token for requests to api.github.com.

        Repository validation prevents URLs or paths from redirecting credentials.
        """
        if not REPOSITORY.fullmatch(repository) or not token:
            raise ValueError("Invalid GitHub repository or token")
        self._repository = repository
        self._token = token

    def owner_login(self) -> str:
        """`owner.login` of this repository, with the capitalization GitHub uses."""
        data = self._request("GET", "")
        owner = data.get("owner") if isinstance(data, dict) else None
        login = owner.get("login") if isinstance(owner, dict) else None
        if not isinstance(login, str) or not login:
            raise ValueError("GitHub returned no repository owner")
        return login

    def _request(
        self, method: str, path: str, body: Mapping[str, object] | None = None
    ) -> object:
        """
        Call a fixed GitHub API path and parse the JSON response.

        Idempotent requests are retried by `lib_http` (5xx, 429, network errors).
        Errors expose the status code only and never include the token or response body.
        """
        try:
            reply = request(
                method,
                f"https://api.github.com/repos/{self._repository}"
                + (f"/{path}" if path else ""),
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self._token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json=body,
                max_bytes=MAX_RESPONSE,
            )
        except HttpError as error:
            if error.status is None:
                raise RuntimeError("GitHub API is unreachable") from None
            raise RuntimeError(f"GitHub API returned HTTP {error.status}") from None
        return json.loads(reply.content)

    def collaborator_permission(self, login: str) -> str:
        """
        The repository permission of a user: admin, maintain, write, triage, read or none.

        A user who is not a collaborator, or a login that cannot be one, is `none`.
        """
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", login):
            return "none"
        try:
            data = self._request("GET", f"collaborators/{login}/permission")
        except RuntimeError as error:
            if "HTTP 404" in str(error):
                return "none"
            raise
        value = data.get("permission") if isinstance(data, dict) else None
        return value if isinstance(value, str) else "none"

    def pull_head(self, number: int) -> str:
        """
        Read the current commit at the head of the pull request.

        An unexpected API response fails the report rather than assuming a SHA.
        """
        result = self._request("GET", f"pulls/{number}")
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("head"), dict)
            or not isinstance(result["head"].get("sha"), str)
        ):
            raise TypeError("Invalid pull request response")
        return result["head"]["sha"]

    def comments(self, number: int) -> Sequence[Mapping[str, object]]:
        """
        Read every page of comments for the requested pull request.

        A bounded page count prevents an unbounded reporting job.
        """
        result = []
        for page in range(1, 101):
            response = self._request(
                "GET", f"issues/{number}/comments?per_page=100&page={page}"
            )
            if not isinstance(response, list) or any(
                not isinstance(item, dict) for item in response
            ):
                raise ValueError("Invalid comment response")
            result.extend(response)
            if len(response) < 100:
                return result
        raise ValueError("Too many comment pages")

    def create_comment(self, number: int, body: str) -> None:
        """
        Create the first catalog checks comment on the pull request.

        The caller provides already escaped report text.
        """
        self._request("POST", f"issues/{number}/comments", {"body": body})

    def update_comment(self, comment_id: int, body: str) -> None:
        """
        Replace the body of an existing bot-owned comment.

        Ownership is verified by the calling report service.
        """
        self._request("PATCH", f"issues/comments/{comment_id}", {"body": body})

    def create_check(
        self,
        sha: str,
        conclusion: str,
        summary: str,
        annotations: Sequence[Mapping[str, object]] = (),
    ) -> None:
        """
        Publish the catalog-checks check run for the reviewed commit.

        The workflow token requires checks:write permission. GitHub accepts at most
        50 annotations per request, so the list is cut to that.
        """
        self._request(
            "POST",
            "check-runs",
            {
                "name": "catalog-checks",
                "head_sha": sha,
                "status": "completed",
                "conclusion": conclusion,
                "output": {
                    "title": "Catalog checks",
                    "summary": summary,
                    "annotations": list(annotations[:50]),
                },
            },
        )
