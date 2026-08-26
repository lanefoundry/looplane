# Otter animation cadence

## Goal

Slow the looping otter thinking animation without changing response streaming or
reduced-motion behavior.

## Plan

- [completed] Inspect frame count, cadence, and existing timing tests.
- [completed] Choose a calmer loop rate and update the animation constant.
- [completed] Update focused tests and render/behavior verification.

## Baseline UI constraints

- Keep the animation local to the loading indicator.
- Preserve the static reduced-motion frame.
- Do not change layout or introduce new effects.

## Result

- Changed frame cadence from 0.14s to 0.20s: the six-frame loop slows from
  0.84s to 1.20s (about 43% slower).
- Kept the cadence at the Baseline UI 200ms interaction-feedback ceiling.
- Reduced-motion still uses the static otter frame with no automatic refresh.
- Ruff passed and all 36 TUI tests passed.
