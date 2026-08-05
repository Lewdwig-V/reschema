from tools.dogfood.runners.base import AgentOutcome, SlotSpec


def test_slot_spec_shapes():
    s = SlotSpec(
        family="rot13",
        condition="primed",
        slot="gcc-O0-sym",
        slot_index=0,
        rep=1,
        task_id="rot13::gcc-O0-sym",
    )
    assert s.condition in ("primed", "unprimed")
    assert s.slot in s.task_id


def test_agent_outcome_kinds():
    o = AgentOutcome(exit_kind="eof", returncode=0, transcript_tail="done")
    assert o.exit_kind in ("eof", "exit", "timeout", "error")


def test_slot_result_name_naming():
    def spec(condition: str, slot_index: int) -> SlotSpec:
        return SlotSpec(
            family="rot13",
            condition=condition,
            slot="gcc-O0-sym",
            slot_index=slot_index,
            rep=1,
            task_id="rot13::gcc-O0-sym",
        )

    p = spec("primed", 0)
    assert p.slot_id == "rot13-primed-gcc-O0-sym-r1"
    assert p.result_stem == p.slot_id + "-s0"

    u = spec("unprimed", 1)
    assert u.slot_id == "rot13-unprimed-gcc-O0-sym-r1"
    assert u.result_stem == u.slot_id
