# Otter terminal animation reference check

Checked: 2026-08-22

## Question

Can a compact one-line ASCII/kaomoji loader be immediately recognizable as an
otter, and what do existing web examples do?

## Source inventory

| Source | Reading level | What it establishes | Limitation |
|---|---|---|---|
| [Unicode Emoji 12.0 list](https://unicode.org/emoji/charts-12.0/emoji-list.html) | Primary | U+1F9A6 is officially named `otter` | Glyph artwork varies by terminal/platform |
| [Unicode full emoji list](https://unicode.org/emoji/charts-12.0/full-emoji-list.html) | Primary | Shows the dedicated otter pictograph across vendor columns | Does not define terminal cell width |
| [EmojiCombos otter](https://emojicombos.com/otter) | Secondary/reference collection | Compact online otter combinations overwhelmingly use the dedicated `🦦` emoji; face-only kaomoji are generic mammal faces | User-submitted collection, not an authoritative design source |
| [EmojiCombos otter text art](https://emojicombos.com/otter-text-art) | Secondary/reference collection | Larger text art can add a body silhouette, but is unsuitable for a one-line status | Search labels are noisy and some entries are unrelated |

No cited conclusion depends on a search-result snippet alone. GitHub code search
also found Dwarf Fortress-style otter sprite metadata, but not a reusable,
compact one-line otter ASCII silhouette.

## Conclusion

A face such as `(•ᴥ•)` or `ʕ•ﻌ•ʔ` is not recognizably an otter. At one-line
spinner size, the visually reliable option is the dedicated Unicode otter
character `🦦`. Originality should come from the animation timing and the
surrounding bubble, shell, pebble, or water frames rather than pretending a
generic mammal face is otter-specific.

## Recommended original loader

Reserve a fixed indicator field and animate only the characters after the otter:

```text
🦦      Thinking…
🦦  ·   Thinking…
🦦  o   Thinking…
🦦  O   Thinking…
🦦  °   Thinking…
🦦      Thinking…
```

Implementation requirements:

- Measure the rendered width with the same `wcwidth` logic as the TUI and pad
  every frame to an identical cell width.
- Keep `Thinking…` static so only the tiny indicator moves.
- Provide an ASCII fallback such as `o>  ` when the terminal cannot render
  U+1F9A6, clearly treating it as a fallback rather than an otter drawing.
- Reduced-motion mode shows a static `🦦` plus the textual status.

