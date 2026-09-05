# codegraph

**Ask your Python codebase what breaks if you change this.**

One file. No dependencies. Nothing but the standard library.

```bash
curl -O https://raw.githubusercontent.com/jedisolana/codegraph/main/codegraph.py
python3 codegraph.py build .
python3 codegraph.py impact _paid_ok
```

```
callers: boardofdirectors/server.Handler.do_POST, boardofdirectors/server._board,
         boardofdirectors/server._single, boardofdirectors/server._tier
sites:   boardofdirectors/server.py:201, boardofdirectors/server.py:250,
         boardofdirectors/server.py:279, boardofdirectors/server.py:632
blast:   5 functions could be affected
```

That is a real answer from a real codebase, not a mock-up. Four callers, the exact four lines
to open, and the transitive radius — before you touch anything.

## Why

`grep` finds the name. It cannot tell you that two files define a function called `digest` and
only one of them is the one you are about to break. Your editor's "find references" can, but it
needs a language server running, and it will not give you the *transitive* answer: who calls the
callers.

This is the question you actually have before an edit — **what depends on this** — answered in
one command, from a file you can copy into any repo.

## It tells you when it doesn't know

This is the part that matters, and it is why the tool is worth having rather than clever.

Every call edge carries a confidence label:

| label | meaning |
|---|---|
| `SELF-METHOD` | `self.helper()` — resolved inside the enclosing class |
| `TYPED` | `x = Foo(); x.method()` — resolved by local type inference |
| `QUALIFIED` | `thing.load()` where `thing` is a module you imported |
| `LOCAL` | a bare call to a function in the same module |
| `RESOLVED` | a bare call, and exactly one definition of that name exists |
| `AMBIGUOUS` | **several** definitions match — the candidates are listed, nothing is picked |
| `EXTERNAL` | a builtin, the stdlib, a library, or a method on an object it cannot type |

Most call-graph tools guess and hand you one answer. This one refuses. An `AMBIGUOUS` edge is
a real result: it means the question does not have a single answer, and a blast radius that
quietly picked one would be worse than no blast radius at all.

`stats` reports the resolution rate, so you know how much of the graph is solid:

```json
{
  "call_edges": 2580,
  "edge_confidence": {"QUALIFIED": 201, "LOCAL": 197, "SELF-METHOD": 78,
                      "RESOLVED": 46, "TYPED": 3, "AMBIGUOUS": 1, "EXTERNAL": 2054},
  "in_tree_resolution_rate": 0.844
}
```

## The commands

```
codegraph build [dir...]     build the graph (default: here); writes codegraph.json
codegraph impact <name>      callers + call sites + blast radius — the pre-edit view
codegraph callers <name>     who calls this
codegraph calls <name>       what this calls
codegraph blast <name>       transitive callers — what could break
codegraph sites <name>       every call site as file:line — the exact places to edit
codegraph where <name>       where a symbol is defined
codegraph find <substr>      fuzzy symbol search
codegraph path <from> <to>   a call path connecting two functions
codegraph deps <module>      a module's in-tree imports and importers
codegraph cycles             mutual import cycles (refactor smells)
codegraph stats              counts, resolution rate, never-called definitions
codegraph --selftest         seventeen ground-truth checks, several red-first
```

Queries rebuild automatically when a source file has changed, so you are never answered from a
stale snapshot. The rebuild is incremental — unchanged files are reused from a cache keyed by
modification time *and* by codegraph's own source hash, so editing the parser invalidates every
stale parse instead of silently reusing it.

## For an AI agent

An agent that edits code reads it as text and guesses at the consequences. Give it structure
instead:

```python
import codegraph
g = codegraph.load()
codegraph.impact(g, "spend_cap")   # {"callers": [...], "sites": [...], "blast": [...]}
```

`impact` before an edit is the difference between "I changed a function" and "I changed a
function that five others depend on, here are their line numbers." The output is small, exact,
and cheap — no model call, no network.

## What it cannot do

Static analysis, honestly labelled:

- **Python only.**
- **Dynamic dispatch defeats it** — `getattr(obj, name)()`, dispatch tables, monkeypatching,
  plugin registries. These land as `EXTERNAL`, which is the truthful answer.
- **Decorators that replace a function** are not followed.
- **Inheritance is not resolved.** `self.method()` resolves within the defining class, not up a
  base class chain.
- **Type inference is one line deep** — `x = Foo()` then `x.method()`. Nothing beyond that.

Everything it cannot resolve is labelled rather than guessed, so the limits are visible in the
output instead of hidden in it.

## Why not something else

- **`grep` / `ctags`** — finds names, not relationships. No transitive answer.
- **`pyan`, `code2flow`** — call graphs, but they want Graphviz and hand you a picture. This
  hands you an answer to a question, and installs nothing.
- **`pydeps`** — module imports only, not function calls.
- **`pycg`** — more rigorous, and a heavier dependency; worth it if you need academic precision.
- **A language server / SCIP indexer** — more accurate than this, and correspondingly large. If
  you already run one, use it. If you want an answer in a shell script or a CI job or an agent
  loop, that is what this is for.

## Install

The point is that you don't have to:

```bash
curl -O https://raw.githubusercontent.com/jedisolana/codegraph/main/codegraph.py
```

If you'd rather have it on your PATH:

```bash
pipx install git+https://github.com/jedisolana/codegraph
```

Python 3.9+. Tested on Linux, macOS and Windows.

## Proving itself

`python3 codegraph.py --selftest` builds small trees with known answers and checks them —
including **red-first controls** that prove the naive approach fails where this one does not:

- two modules both defining `digest`, and a query that must reach exactly one of them
- `open('x').write('y')` — a method on a file object, which must *not* resolve to a same-named
  function in your tree
- `from .thing import load` inside a package, next to a top-level `thing.py` — the trap that
  makes a lazy implementation return the wrong function with full confidence

The test suite adds the CLI, the on-disk contract, cache invalidation, corrupt-file recovery,
and codegraph reading its own source.

## Licence

MIT.
