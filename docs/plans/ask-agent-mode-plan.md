# Ask and Agent modes

- [x] Add a cancellable external Ask runner with no repository input.
- [x] Make external runtimes start in Ask; keep looplane Agent in Agent.
- [x] Keep a bounded process-local transcript and reset it on runtime/model/mode changes.
- [x] Preserve clean-repo, approval, patch, and verification gates in Agent mode.
- [x] Add dirty-repo Ask, routing, transcript, cancellation, and regression tests.
- [x] Run full gates and independent review.

This intermediate mode was superseded by M11's unified conversation and tool-boundary approvals.
