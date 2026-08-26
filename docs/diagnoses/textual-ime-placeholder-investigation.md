# Textual IME / placeholder investigation

- [x] Locate installed Textual 8.2.8 Input source and event path
- [x] Check IME composition/preedit support
- [x] Find focused-placeholder override without subclass rewrite
- [x] Identify test approach and report findings

## Findings

- Textual 8.2.8 exposes `Key`, `Paste`, focus, and mouse input events, but no IME
  composition/preedit event or state. Its xterm parser receives committed terminal
  bytes and converts printable Unicode characters to `Key` events.
- `Input` renders its placeholder whenever `value` is empty. When focused, it then
  applies the cursor component style to the placeholder's first character. That is
  why Warp's terminal-owned preedit text is drawn over `M` while the app still shows
  the rest of `Message PCA...`.
- A placeholder-only `text-opacity: 0%` rule is insufficient because the cursor
  restyles the first placeholder character after the placeholder style is applied.
- CSS-only focused state that hides every placeholder glyph while retaining a block
  cursor:

  ```css
  #task:focus > .input--placeholder { text-opacity: 0%; }
  #task:focus > .input--cursor { color: $input-cursor-background; }
  ```

- Verified in a Textual `run_test`: the placeholder tail resolves to foreground ==
  background, and its first cursor-styled character also resolves to foreground ==
  background. No `Input` rewrite or subclass is needed.
- Automated test: focus `#task`, assert placeholder component `text_opacity == 0`,
  assert cursor component foreground equals background, press a committed CJK
  character and assert `Input.value`. Screenshot both focused-empty and committed
  states. Pilot cannot synthesize macOS/Warp preedit, so the development gate also
  needs one real-Warp manual screenshot while composition is active.
