# Runtime-reported model and `/model` menu

## Outcome

Rivumi must distinguish an automatic model selection policy from the concrete model reported by the
active runtime. The header and `/status` should show the actual model once known, without deriving it
from assistant prose. Typing `/model` with no trailing space must immediately show choices.

## Tasks

- [x] Trace Claude SDK system/result metadata for a trustworthy model identifier.
- [x] Add a provider-neutral runtime-model event and propagate it to the TUI.
- [x] Verify exact `/model`, `/model `, prefix filtering, selection, and no-space input events.
- [x] Run focused and full regression checks.

## Status

Complete.
