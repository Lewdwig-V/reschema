"""#76: sessionfinish deletes ONLY the mkdtemp worker roots conftest itself made."""

from conftest import _worker_root_eligible_for_cleanup


def test_own_xdist_mkdtemp_roots_are_eligible():
    for n in ("gw0", "gw7", "gw31"):
        assert _worker_root_eligible_for_cleanup(f"/tmp/reschema-{n}-abc123xy")


def test_user_set_homes_are_never_eligible():
    for p in (
        "/home/u/proj/.reschema",
        "/tmp/reschema-data",  # right prefix family, not a worker root
        "/var/tmp/reschema",
        "/tmp",
    ):
        assert not _worker_root_eligible_for_cleanup(p)
