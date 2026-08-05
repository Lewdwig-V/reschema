"""opencode v1 adapter: writes the sandbox config, spawns `opencode run`."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import urllib.request

from .base import AgentOutcome, RunnerConfig

TRANSCRIPT = "transcript.log"


class OpenCodeV1Runner:
    def __init__(self, binary: str = "opencode"):
        self.binary = binary
        self._p: subprocess.Popen | None = None
        self._cfg: RunnerConfig | None = None

    def preflight(self, cfg: RunnerConfig) -> dict:
        """Endpoint liveness + run-header facts; raises on a dead endpoint
        (slot maps that to a typed infra-error, no rep consumed)."""
        base = (cfg.endpoint or "").rstrip("/")
        status, models = self._post(base, "/models", None)
        status2, _ = self._post(
            base, "/chat/completions", {"max_tokens": 1, "model": cfg.model}
        )
        if status != 200 or status2 != 200:
            raise RuntimeError(f"endpoint unavailable: {status}/{status2}")
        return {
            "model": cfg.model,
            "endpoint": cfg.endpoint,
            "digest": models.get("version", "unknown"),
        }

    def _post(self, base: str, path: str, payload) -> tuple[int, dict]:
        """urllib json POST to base+path; (status, decoded body or {})."""
        req = urllib.request.Request(
            base + path,
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except (OSError, ValueError):  # net down / bad json: status 0
            return 0, {}

    def prepare(self, cfg: RunnerConfig) -> None:
        (cfg.sandbox).mkdir(parents=True, exist_ok=True)
        (cfg.sandbox / "opencode.json").write_text(
            json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    "model": f"local/{cfg.model}",
                    "provider": {
                        "local": {
                            "npm": "@ai-sdk/openai-compatible",
                            "options": {"baseURL": cfg.endpoint},
                        }
                    },
                    "agent": {
                        "tools": {
                            "bash": False,
                            "edit": False,
                            "write": False,
                            "read": False,
                            "glob": False,
                            "grep": False,
                            "webfetch": False,
                            "task": False,
                            "skill": False,
                            "todowrite": False,
                            "question": False,
                            "reschema_*": True,
                        }
                    },
                    "mcp": {
                        "reschema": {
                            "type": "local",
                            "command": [
                                "uv",
                                "run",
                                "python",
                                "-m",
                                "reschema.mcp.server",
                            ],
                            "environment": {"RESCHEMA_HOME": str(cfg.run_root)},
                        }
                    },
                },
                indent=2,
            )
        )
        self._cfg = cfg

    def spawn(self, prompt: str) -> None:
        cfg = self._cfg
        with open(cfg.sandbox / TRANSCRIPT, "wb") as out:  # child dup's the fd
            self._p = subprocess.Popen(
                [self.binary, "run", prompt],
                cwd=cfg.sandbox,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group: kill reaches children
            )

    def wait(self) -> AgentOutcome:
        p = self._p
        rc = p.wait() if p else None
        tail = ""
        tp = self._cfg.sandbox / TRANSCRIPT
        if tp.exists():
            tail = "\n".join(tp.read_text(errors="replace").splitlines()[-50:])
        kind = "eof" if rc == 0 else ("timeout" if rc and rc < 0 else "exit")
        return AgentOutcome(exit_kind=kind, returncode=rc, transcript_tail=tail)

    def kill(self) -> None:
        if self._p and self._p.poll() is None:
            os.killpg(self._p.pid, signal.SIGKILL)

    def exited(self) -> bool:
        return self._p is not None and self._p.poll() is not None
