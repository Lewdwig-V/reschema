"""Agent-declared param specs: how to generate/marshal/compare args."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Param:
    name: str
    kind: str  # "i32" | "buffer_i32" | "cstring"
    direction: str = "in"  # "in" | "out" | "in_out"
    length_param: str | None = None
    range: tuple[int, int] = (-100, 100)
    # "i32" | "void" — function-level, carried on the first param; void = mem-only compare
    ret: str = "i32"

    @classmethod
    def from_json(cls, d: dict) -> Param:
        r = d.get("range", [-100, 100])
        return cls(
            d["name"],
            d["kind"],
            d.get("direction", "in"),
            d.get("length_param"),
            (r[0], r[1]),
            d.get("ret", "i32"),
        )
