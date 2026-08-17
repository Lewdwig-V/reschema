# 2C live-agent campaign — rot13

- records: 30 total (primed 15, unprimed 15), 19 accepted, 11 aborted, 0 infra-error (excluded from stats)
- φ median: -0.46335060944285406  IQR: 3.0459907223680047  (φ base: 5 rep(s) contributing)
- aborts by class: aborted: agent-exit ×7, aborted: priming-failed ×4
- unprimed trajectory: [0.6703200460356393, 0.4723665527410147, 0.7408182206817179] (flat: False)

## run header (first record)

- agent_exit: eof
- canonicalizer_version: 2.1
- digest: 0.32.9
- driver_revision: f67db6d
- endpoint: http://localhost:11434/v1
- manifest_sha256: c0647447316bc3c98fcbe123950e91605ab6ae4cf1849337114590556c9b6974
- model: gemma4:26b
- prompt_sha256: 797261d7750a8f6ca504b0ec70bd9fb2187ef97c7c741771402175f7964f6207

## per-slot deltas

- slot 1: primed 0.000, unprimed 0.472, Δ -0.472
- slot 2: primed 0.000, unprimed 0.741, Δ -0.741

*reference-agent trajectories are instrument-wiring checks (protocol §4); these rows are the measurement.*
