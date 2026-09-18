"""Driver-side types + the harness-adapter interface (spec §runners/base)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class SlotSpec:
    """One live-agent run of one corpus slot in one condition."""

    family: str  # seed name, e.g. "rot13"
    condition: str  # "primed" | "unprimed"
    slot: str  # e.g. "gcc-O1-sym"
    slot_index: int  # chain position; grouped same-task branches need distinct indices
    rep: int
    task_id: str  # "<family>::<slot>"
    # Explicit lineage within (family, condition, rep). Grouped calls preserve
    # judge state across sequential branches/resumes; independent trials need
    # a different group, repetition, or campaign directory. Task ids stay fixed.
    state_group: str | None = None

    @property
    def slot_id(self) -> str:
        return f"{self.family}-{self.condition}-{self.slot}-r{self.rep}"

    @property
    def result_stem(self) -> str:
        """Stable per-run identity, distinct from the shared judge root.

        Grouped siblings of the same task use distinct slot_index values;
        resuming the same spec replaces only that branch's record/transcript.
        Legacy campaign names remain unchanged.
        """
        if self.state_group is not None:
            return f"{self.slot_id}-g{self._group_key}-s{self.slot_index}"
        return (
            f"{self.slot_id}-s{self.slot_index}"
            if self.condition == "primed"
            else self.slot_id
        )

    @property
    def _group_key(self) -> str:
        # Hash the structured identity, not delimiter-joined labels: group
        # labels are opaque (including slashes), and rep is part of the trial.
        identity = json.dumps(
            [self.family, self.condition, self.rep, self.state_group],
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode()).hexdigest()

    @property
    def state_root_id(self) -> str:
        """Filesystem identity of the slot's RESCHEMA_HOME.

        Default behavior stays protocol-shaped: primed chains reuse one root
        across their slots, unprimed runs stay memory-cold per slot. Richer
        comparison harnesses can override that with an explicit group key.
        """
        if self.state_group is not None:
            # The -g suffix cannot alias legacy roots, which end in -r<rep>.
            return f"{self.family}-{self.condition}-r{self.rep}-g{self._group_key}"
        return (
            f"{self.family}-primed-r{self.rep}"
            if self.condition == "primed"
            else self.slot_id
        )


@dataclass
class RunnerConfig:
    model: str
    endpoint: str | None  # OpenAI-compatible base URL (run-header evidence)
    sandbox: Path  # empty session cwd (agent cannot see the repo)
    run_root: Path  # slot's RESCHEMA_HOME
    # Transcript file inside the sandbox. Primed chains SHARE their sandbox
    # across slots, so run_slot names it per slot (transcript-<stem>.log) —
    # a one-name default would have each slot truncate the previous session
    # (#94). The default keeps direct runner users' old shape.
    transcript: str = "transcript.log"


@dataclass
class AgentOutcome:
    exit_kind: str  # "eof" | "exit" | "timeout" | "error"
    returncode: int | None
    transcript_tail: str  # last ~50 lines


class AgentRunner(Protocol):
    """Harness adapter surface. Core code sees nothing harness-shaped.

    Optional member: `preflight(cfg) -> dict` (run-header evidence); slot.py
    reaches it via getattr — adapters with endpoints implement it, absence
    means skipped."""

    def prepare(self, cfg: RunnerConfig) -> None: ...
    def spawn(self, prompt: str) -> None: ...
    def wait(self) -> AgentOutcome: ...  # must return promptly after kill()
    def kill(self) -> None: ...
    def exited(self) -> bool: ...  # agent process finished (conservative False ok)
