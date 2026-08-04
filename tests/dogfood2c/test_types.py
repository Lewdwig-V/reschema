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
