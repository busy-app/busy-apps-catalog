"""The e2e secrets check: Access secrets are optional, but only as a pair."""

ID = "CF_ACCESS_CLIENT_ID"
SECRET = "CF_ACCESS_CLIENT_SECRET"
OTHER = ["GITHUB_TOKEN", "R2_KEY"]


def problem(names):
    from e2e.domain import access_secret_problem

    return access_secret_problem(names)


def test_neither_is_fine() -> None:
    """Without the Access secrets there is no problem."""
    assert problem(OTHER) is None
    assert problem([]) is None


def test_both_is_fine() -> None:
    """Both present: no problem."""
    assert problem([*OTHER, ID, SECRET]) is None


def test_only_id_names_the_missing_secret() -> None:
    """Only the id: the message names the secret."""
    message = problem([*OTHER, ID])
    assert message and SECRET in message


def test_only_secret_names_the_missing_id() -> None:
    """Only the secret: the message names the id."""
    message = problem([*OTHER, SECRET])
    assert message and "CLIENT_ID" in message


def test_accepts_any_iterable() -> None:
    """A generator or a set works as well as a list."""
    assert problem(name for name in [ID]) is not None
    assert problem({ID, SECRET}) is None
