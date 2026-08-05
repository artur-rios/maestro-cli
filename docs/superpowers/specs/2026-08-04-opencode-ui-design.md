# A frame for the shell, in the style of opencode

## The problem

The shell already draws through rich: a themed console, an ASCII wordmark, panels,
tables, a progress view and a bordered input box. What it lacks is a *frame*.
Output is printed at whatever width the terminal happens to be, with each helper
supplying its own two-space indent, so nothing shares an edge. The banner, the
status bar and the input box are three different widths. The result reads as a
sequence of unrelated prints rather than one interface.

opencode solves this with a fixed content column, a dim gutter rail down its left
side, and the status line pinned at the bottom just above the input. This design
adopts that arrangement.

## The frame

One width governs everything:

    frame_width() = min(terminal_width, 100)

The divider, the gutter rail, the status bar and the input box all use it, so
every element shares a left and a right edge.

    <blank>
        ___    ____        ____             __
       /   |  /  _/       / __ \_________  / /_
      / /| |  / /        / / / / ___/ ___/ __ \
     / ___ |_/ /        / /_/ / /  / /__/ / / /
    /_/  |_/___/        \____/_/   \___/_/ /_/
      AI AGENT ORCHESTRATOR  v2.0.0
      ────────────────────────────────────────────
      ▌ 7f2a1c  merged    3/3   12m  add rate limiting
      ▌ a91b04  running   1/4    2m  fix the flaky test
    <blank>
      claude codex opencode   my-api   3 runs   1 awaiting approval
      ╭─ type a task, @prompt or /help ───────────╮
      │ >
      ╰───────────────────────────────────────────╯

Three bands: a header that does not move, a content area in the rail, and a
footer that sits directly above the cursor.

## The rail

`Rail` is a renderable in `render.py` implementing `__rich_console__`. It renders
its child through `console.render_lines()` at `frame_width() - 4` and emits the
gutter before each produced line.

Wrapping at the segment level rather than the string level is what makes this
work for every renderable the shell already prints. A `Table` re-wraps its
columns to the narrowed width instead of overflowing past the gutter; a `Panel`
draws its own border inside the rail; multi-line `Text` gets the gutter on every
wrapped line, not just the first.

`render.out()` applies the rail itself, so no call site changes. The helpers
(`success`, `warn`, `error`, `info`, `muted`, `hint`, `field`) drop their
hardcoded two-space indents, because the gutter now supplies the left margin.

### When the rail is off

The rail draws only when `console.is_terminal`. Piping (`orchestrator runs > f`)
and the test suite must keep receiving clean, unprefixed text; a gutter in a
redirected file would break every downstream consumer. `render.py` already
branches on `console.is_terminal` for the box-drawing fallback, so this follows
an established rule rather than inventing one.

Raw agent output is also exempt. `logview.drain()` writes a subprocess's bytes
straight to `sys.stdout`, ANSI and all. That stream belongs to the agent's own
CLI, and prefixing it per line would corrupt the escape sequences it contains.
It stays raw.

## Header and footer

`repl.py` currently draws the banner and the status bar as one block at the top.
They separate:

- `header()` — blank line, banner, divider. Redrawn after each screen clear.
- `footer()` — blank line, status bar. Drawn every turn, immediately above the
  input box.

Ordering in the loop matters. `footer()` runs *before* `take_output()` starts
recording the turn, so the status bar is never captured and therefore never
replayed into the content area on the next redraw. Recording begins on a clean
slate with the footer already on screen.

`output_room()` accounts for the two lines the footer costs, so replayed output
still cannot push the banner off the top of the screen.

## What does not change

No business logic is touched. `commands.py`, `parser.py`, `runner.py`,
`store.py`, `models.py`, `backends.py`, `config.py`, `gitops.py` and
`settings.py` keep every function, signature and behaviour they have. Every
click subcommand, every slash command and every option continues to work
unchanged: `cli.py` is not modified beyond its module docstring.

Changes are confined to:

| File | Change |
|---|---|
| `render.py` | `Rail`, `frame_width()`, `divider()`, rail-aware `out()`, indents removed, shared box width |
| `repl.py` | header/footer split, loop ordering, `output_room()` |
| `prompts.py` | input box left margin aligned to the rail |
| `progress.py` | `Live` frame wrapped in the rail, indents removed |
| `logview.py` | indents removed |
| `commands.py` | one stray indent in a `muted()` call |
| `tests/test_render.py` | new coverage for the rail, the divider and the shared width |

## Consequences accepted

**The rail costs four columns.** `backend_table` has five columns and is already
tight at 100. It renders at its natural width rather than being squeezed into
96, so a wide table may extend past the frame's right edge instead of being
mangled. Legibility beats alignment for that one table.

**The footer costs two lines per turn.** That is the price of the opencode
arrangement, and `output_room()` absorbs it.

## Testing

Existing tests render through consoles built with `force_terminal=False`, so the
rail is off and their assertions are unaffected — the 456 currently passing tests
must stay green, which is the main guard against regression.

New tests turn the rail on explicitly and cover:

- the gutter appears on every line of a multi-line renderable, not just the first
- a table re-wraps inside the rail rather than overflowing it
- no gutter is emitted when the console is not a terminal
- the divider, the status bar and the input box edges are all `frame_width()` wide
- `frame_width()` caps at 100 and shrinks with a narrow terminal
