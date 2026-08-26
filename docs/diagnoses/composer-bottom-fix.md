# Composer bottom layout fix

## Subject and job

Rivumi is a full-screen coding-agent terminal for developers. Its single layout job is to keep the
conversation readable while the next-input composer remains predictably available at the bottom.

## Design plan

- Keep the existing otter-terminal palette, typography, and transcript action language unchanged.
- Make the transcript the only flexible-height region (`1fr`).
- Keep the top bar, status strip, and composer content-sized in normal vertical flow.
- Sparse content begins at the top of the transcript; the blank flexible area belongs between the
  transcript content and bottom controls, never below the composer.
- Long content continues to scroll inside the transcript without moving the composer.

## Verification

- [x] Wide and narrow layout assertions pin the composer to the workspace bottom.
- [x] Sparse transcript and long-history scroll-follow tests pass.
- [x] Inline approval remains contained and keyboard-focusable.
- [x] Deterministic wide and narrow empty-start screenshots are visually inspected.
- [x] Full test, lint, format, lock, diff, and build checks pass.

## Status

Complete.
