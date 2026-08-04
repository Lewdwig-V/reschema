"""Driver-side types + the harness-adapter interface (spec §runners/base)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class SlotSpec:
    """One live-agent run of one corpus slot in one condition."""

    family: str  # seed name, e.g. "rot13"
    condition: str  # "primed" | "unprimed"
    slot: str  # e.g. "gcc-O1-sym"
    slot_index: int  # 0..2 position in the chain
    rep: int
    task_id: str  # "<family>::<slot>"

    @property
    def slot_id(self) -> str:
        return f"{self.family}-{self.condition}-{self.slot}-r{self.rep}"

    @property
    def result_stem(self) -> str:
        """SINGLE owner of the result-file naming rule: primed chains share
        slot_id across their 3 slots, so later slots disambiguate by index."""
        return (
            f"{self.slot_id}-s{self.slot_index}"
            if self.condition == "primed"
            else self.slot_id
        )


@dataclass
class RunnerConfig:
    model: str
    endpoint: str | None  # OpenAI-compatible base URL (run-header evidence)
    sandbox: Path  # empty session cwd (agent cannot see the repo)
    run_root: Path  # slot's RESCHEMA_HOME
    mcp_server_args: list[str] = field(default_factory=list)
    max_turns: int | None = None


@dataclass
class AgentOutcome:
    exit_kind: str  # "eof" | "exit" | "timeout" | "error"
    returncode: int | None
    transcript_tail: str  # last ~50 lines


class AgentRunner(Protocol):
    """Harness adapter surface. Core code sees nothing harness-shaped."""

    def prepare(self, cfg: RunnerConfig) -> None: ...
    def spawn(self, prompt: str) -> None: ...
    def wait(self) -> AgentOutcome: ...  # must return promptly after kill()
    def kill(self) -> None: ...
    def exited(self) -> bool: ...  # agent process finished (conservative False ok)
