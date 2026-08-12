"""opencode v1 adapter: writes the sandbox config, spawns `opencode run`."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import urllib.error
import urllib.request

from .base import AgentOutcome, RunnerConfig

TRANSCRIPT = "transcript.log"


class OpenCodeV1Runner:
    def __init__(self, binary: str = "opencode"):
        self.binary = binary
        self._p: subprocess.Popen | None = None
        self._cfg: RunnerConfig | None = None
        self._killed = False

    def preflight(self, cfg: RunnerConfig) -> dict:
        """Endpoint liveness + run-header facts; raises on a dead endpoint
        (slot maps that to a typed infra-error, no rep consumed).

        ponytail: the 10s per-request timeout assumes a warm endpoint —
        first weight-load on a cold server may exceed it, in which case
        the slot is (correctly, if flakily) recorded as infra-error."""
        base = (cfg.endpoint or "").rstrip("/")
        status, _ = self._post(base, "/models", None)
        status2, _ = self._post(
            base,
            "/chat/completions",
            {"model": cfg.model, "messages": [], "max_tokens": 1},
        )
        if status != 200 or status2 != 200:
            raise RuntimeError(f"endpoint unavailable: {status}/{status2}")
        # Ollama-shaped stacks publish their build at /api/version, NOT /v1/...
        _, ver = self._post(base.removesuffix("/v1"), "/api/version", None)
        return {
            "model": cfg.model,
            "endpoint": cfg.endpoint,
            "digest": ver.get("version") or "unknown",
        }

    def _post(self, base: str, path: str, payload) -> tuple[int, dict]:
        """JSON request to base+path; GET when payload is None, POST otherwise.
        Returns (status, decoded body or {}); 0 means transport-level dead."""
        req = urllib.request.Request(
            base + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:  # live endpoint, real status
            return e.code, {}
        except (OSError, ValueError):  # connection refused / bad json
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
                            "models": {cfg.model: {}},
                        }
                    },
                    # TOP-LEVEL switchboard: agent.<name>.tools would define an
                    # agent NAMED "tools" and leave the built-ins ON.
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
        self._killed = False
        # opencode MERGES config layers: a developer-global ~/.config/opencode
        # (MCP servers, plugins, tools) would silently survive alongside the
        # sandbox config. No env var disables the global layer — pin HOME/XDG
        # at a sandbox-private dir, plus OPENCODE_CONFIG as the belt.
        home = cfg.sandbox / "_home"
        home.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "OPENCODE_CONFIG": str(cfg.sandbox / "opencode.json"),
        }
        with open(cfg.sandbox / TRANSCRIPT, "wb") as out:  # child dup's the fd
            self._p = subprocess.Popen(
                [self.binary, "run", prompt],
                cwd=cfg.sandbox,
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group: kill reaches children
            )

    def wait(self) -> AgentOutcome:
        p = self._p
        rc = p.wait() if p else None
        tail = ""
        tp = self._cfg.sandbox / TRANSCRIPT if self._cfg else None
        if tp and tp.exists():
            # ponytail: full read; transcripts are overnight-large —
            # seek-from-end if tails ever hurt.
            tail = "\n".join(tp.read_text(errors="replace").splitlines()[-50:])
        if rc is None:
            kind = "error"  # never spawned
        elif rc == 0:
            kind = "eof"
        elif rc < 0:
            kind = "timeout" if self._killed else "error"
        else:
            kind = "exit"
        return AgentOutcome(exit_kind=kind, returncode=rc, transcript_tail=tail)

    def kill(self) -> None:
        if self._p and self._p.poll() is None:
            self._killed = True
            os.killpg(self._p.pid, signal.SIGKILL)

    def exited(self) -> bool:
        return self._p is not None and self._p.poll() is not None
