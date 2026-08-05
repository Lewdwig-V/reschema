import json
import time

from tools.dogfood.runners.base import AgentOutcome


class FakeRunner:
    """Scripted AgentRunner: the ledger state is written at spawn (the
    agent's whole scripted life), or never (hang) — kill() wakes wait().
    "alive": True keeps the agent running despite a written ledger, so the
    probe-ceiling guard (ordered after exited()) stays reachable."""

    def __init__(self, script: dict):
        self.script = script
        self.cfg = None
        self._killed = False

    def prepare(self, cfg):
        self.cfg = cfg
        self._killed = False

    def spawn(self, prompt):
        # ponytail: plan routed this via engine.TaskStore + a RESCHEMA_HOME env
        # mutation, but engine binds ROOT/TASKS at import time (engine.py) —
        # the env trick lands the ledger in the wrong root under xdist, and the
        # stub corpus has no canonicalizer sidecar. The slot contract is the
        # ledger path under run_root; write it directly.
        led = self.script.get("ledger")
        if led is not None:
            d = (
                self.cfg.run_root
                / ".reschema"
                / "tasks"
                / self.script["task_id"].replace("::", "__")
            )
            d.mkdir(parents=True, exist_ok=True)
            (d / "ledger.json").write_text(json.dumps(led))

    def exited(self):
        if self.script.get("alive"):
            return self._killed
        return self._killed or self.script.get("ledger") is not None

    def wait(self):
        while not self._killed:
            if self.script.get("ledger") is not None:
                return AgentOutcome(
                    exit_kind="eof", returncode=0, transcript_tail="fake"
                )
            time.sleep(0.05)
        return AgentOutcome(
            exit_kind="timeout", returncode=-9, transcript_tail="killed"
        )

    def kill(self):
        self._killed = True


class PreflightFakeRunner(FakeRunner):
    """FakeRunner variant whose optional preflight reports a dead endpoint
    (exercises run_slot's infra-error path without spawning)."""

    def preflight(self, cfg):
        raise RuntimeError("endpoint dead")
