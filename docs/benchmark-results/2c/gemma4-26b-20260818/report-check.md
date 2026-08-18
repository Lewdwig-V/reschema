# 2C live-agent campaign — check

- records: 30 total (primed 15, unprimed 15), 6 accepted, 24 aborted, 0 infra-error (excluded from stats)
- φ median: -0.02957414635126115  IQR: 0.8991729610150985  (φ base: 5 rep(s) contributing)
- aborts by class: aborted: agent-exit ×16, aborted: priming-failed ×8
- unprimed trajectory: [0.028724639654239433, 0.0, 0.0] (flat: False)

## run header (first record)

- agent_exit: eof
- canonicalizer_version: 2.1
- digest: 0.32.9
- driver_revision: 40c5937
- endpoint: http://localhost:11434/v1
- manifest_sha256: c0647447316bc3c98fcbe123950e91605ab6ae4cf1849337114590556c9b6974
- model: gemma4:26b
- prompt_sha256: cd1a044af76a28655f2b6f7f48950de8f4497a1bcddab01dc8523fd2d9236152

## per-slot deltas

- slot 1: primed 0.000, unprimed 0.000, Δ 0.000
- slot 2: primed 0.000, unprimed 0.000, Δ 0.000

*reference-agent trajectories are instrument-wiring checks (protocol §4); these rows are the measurement.*
