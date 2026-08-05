"""#76: sessionfinish deletes ONLY the exact root conftest captured at import
(_OWN_ROOT) — never a path a user happened to point RESCHEMA_HOME at."""

import conftest


def test_sessionfinish_removes_the_owned_worker_root(tmp_path, monkeypatch):
    # fake root in a shape-mismatched name ON PURPOSE: identity, not the
    # dirname, must be what makes it eligible for deletion
    own = tmp_path / "owned-root"
    (own / ".reschema").mkdir(parents=True)
    (own / ".reschema" / "state").write_text("x")
    monkeypatch.setattr(conftest, "_OWN_ROOT", str(own))
    monkeypatch.setenv("RESCHEMA_HOME", str(own))  # as conftest import sets it
    conftest.pytest_sessionfinish(session=object(), exitstatus=0)
    assert not own.exists()


def test_sessionfinish_never_touches_a_user_pointed_home(tmp_path, monkeypatch):
    own = tmp_path / "owned-root"
    own.mkdir()
    user_home = tmp_path / "my-real-data"
    user_home.mkdir()
    monkeypatch.setattr(conftest, "_OWN_ROOT", str(own))
    monkeypatch.setenv("RESCHEMA_HOME", str(user_home))  # user path != own root
    conftest.pytest_sessionfinish(session=object(), exitstatus=0)
    assert user_home.exists()
