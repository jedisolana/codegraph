# Changelog

## 0.1.0

First release.

### What it does

- `impact <name>` — the pre-edit view: who calls it, every call site as `file:line`, and the
  transitive blast radius, in one command.
- `callers`, `calls`, `blast`, `sites`, `where`, `find`, `path`, `deps`, `cycles`, `stats`.
- One file, the standard library only, no install required. Copy it into a repo and run it.
- A built-in `--selftest`: 32 ground-truth checks, several of them red-first controls that
  prove the naive approach fails where this one does not.

### The part that matters

Every call edge carries a confidence label, and the tool refuses rather than guesses. An
ambiguous name lists its candidates and picks none; a call it cannot place says so. The
resolution rate is reported over what was *winnable* — resolved, plus ambiguous, plus the
method calls it could not type — because a rate that leaves the hard cases out of the sum is a
measure of how easy the questions were.

### How the tests are checked

`tools/mutation.py` breaks the tool one small way at a time and runs the suite against each
change — a suite that never fails is not evidence of anything. 208 mutations, all caught. It
edits the file in place, so it refuses to start on a dirty tree and verifies the file byte for
byte before it exits.

### Correctness work before release

Every fix below carries a test that fails without it. These are the ones worth naming, because
they are the answers a call graph must never give:

- **"Nothing depends on this"** for code that is called constantly — a class's `__init__`, an
  instance's `__call__`, a method reached through `super()`. None of those call sites contain
  the name of what they call.
- **The wrong same-named thing.** Two nested helpers called `inner`; a bare call reaching a
  method it cannot see; a class name resolved to the wrong one of two; `from b.svc import
  Client` answered with `a/svc.Client`.
- **A name the file never mentions.** A bare call used to be matched against every definition
  in the tree and resolved whenever exactly one had that name — a coincidence, not a
  resolution. Every real way a bare name reaches a definition has its own rule now (this
  scope, an enclosing one, this module, an import, a star import, a builtin), so the guess was
  removed. It was answering `turtle`'s `up()` and `down()` — which turtle builds at import
  time rather than defining — with functions in `_pyrepl`.
- **Names the file had already accounted for.** A `from time import sleep` answered with an
  unrelated `sleep` in your tree; `from ops import index as _index` looked up under the alias;
  a star import treated as a name spelled `*`; a lambda parameter, a comprehension variable, a
  class attribute and a `match` capture all read as the module they shadowed.
- **A graph that trusted the clock.** Freshness asked "is anything newer than the graph", which
  misses every file restored from a backup, a checkout or a container layer — it now records
  the size and timestamp of each file it read and compares them exactly.
- **Crashes on ordinary trees**: a generated file too deeply nested to walk, a dangling
  symlink, an unreadable file, a read-only directory, a `codegraph.json` that is valid JSON and
  not a graph.

The last of those came from pointing it at the whole Python standard library — 1,849 modules,
236,000 call edges — and checking one thing it cannot fake: a call that crosses into another
module should land somewhere that module's file actually imports. Thirteen thousand did not.
That number is now about eleven hundred, and most of what remains is re-export chains.
