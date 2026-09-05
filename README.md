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
only one of them is the one you are about to break. Ask this for `digest` and it will not guess
either — it names both and waits for you to say which, because merging their callers into one
answer is how you end up "fixing" a caller of the other one. Your editor's "find references" can, but it
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
| `TYPED` | `x = Foo(); x.method()` — resolved by local type inference, and refused when two branches give `x` two types |
| `INHERITED` | `self.method()` where the method lives on a base class |
| `CLASS` | `Parent.method()` — the receiver is a class in this module |
| `QUALIFIED` | `thing.load()` where `thing` is a module you imported — and is not shadowed by a local name of its own |
| `LOCAL` | a bare call to a function in the same module |
| `RESOLVED` | a bare call, and exactly one definition of that name exists |
| `AMBIGUOUS` | **several** definitions match — the candidates are listed, nothing is picked |
| `EXTERNAL` | a builtin, the stdlib, a library, or a method on an object it cannot type |

Most call-graph tools guess and hand you one answer. This one refuses. An `AMBIGUOUS` edge is
a real result: it means the question does not have a single answer, and a blast radius that
quietly picked one would be worse than no blast radius at all.

`UNTYPED` is the one that matters. It means *a method call whose receiver could not be typed* —
`d.get()`, `x.run()`. The target might well be in your tree; the tool simply cannot tell. It is
deliberately not lumped in with `EXTERNAL`, because that would claim knowledge it does not have
and would flatter the resolution rate by shrinking the denominator.

`stats` reports that rate over **what was winnable** — resolved, plus ambiguous, plus the calls
it could not type. Builtins and library calls are excluded, since counting them would only
measure how much of the standard library you happen to use:

```json
{
  "call_edges": 2580,
  "call_sites": 3129,
  "edge_confidence": {"UNTYPED": 1261, "EXTERNAL": 421, "BUILTIN": 379, "QUALIFIED": 201,
                      "LOCAL": 197, "SELF-METHOD": 78, "RESOLVED": 39, "TYPED": 3,
                      "CLASS": 1, "AMBIGUOUS": 0},
  "resolved_to_one_def": 519,
  "could_have_been_resolved": 1780,
  "resolution_rate": 0.292
}
```

That 0.29 is a real number on a real codebase, and it is not flattering. Most of what it cannot
place are method calls on objects it has no type for — the honest ceiling of static analysis
this size. The alternative was a rate of 0.84 computed by leaving the hard cases out of the sum,
which is how a metric ends up meaning nothing.

## The commands

```
codegraph build [dir...]     build the graph (default: here); writes codegraph.json
codegraph impact <name>      callers + call sites + blast radius, and what it could not resolve
codegraph callers <name>     who calls this
codegraph calls <name>       what this calls
codegraph blast <name>       transitive callers — what could break
codegraph sites <name>       every call site as file:line — all of them, not one per caller
codegraph where <name>       where a symbol is defined
codegraph find <substr>      fuzzy symbol search
codegraph path <from> <to>   a call path connecting two functions
codegraph deps <module>      a module's in-tree imports and importers
codegraph cycles             import cycles of any length (refactor smells)
codegraph stats              counts, resolution rate, never-called definitions
codegraph --selftest         17 ground-truth checks, several of them red-first
```

**Exit codes**, because scripts and agents read them:

| code | meaning |
|---|---|
| `0` | answered — including a real function with no callers |
| `1` | this graph has never heard the name, or a search matched nothing |
| `2` | the name matches several definitions, or the command was malformed |

"Nothing depends on this" and "I do not know that name" are deliberately different answers. A
misspelling that returns success is how an agent talks itself into an unsafe edit.

Queries find the graph by walking up from where you are, the way git finds `.git`, so you can
ask from anywhere in the repository. They rebuild automatically when the tree has changed — a file edited, added, **or deleted**.
Deletion is the one people forget: removing a file changes nobody else's timestamp, so a
graph that only watched timestamps would go on answering about code that is gone. The rebuild is incremental — unchanged files are reused from a cache keyed by
modification time *and* by codegraph's own source hash, so editing the parser invalidates every
stale parse instead of silently reusing it. The graph carries that hash too: upgrade codegraph
and the next query rebuilds, rather than answering from the version you replaced.

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

The library refuses exactly what the command line refuses, through the same code:
`codegraph.Ambiguous` when several definitions answer to the name, carrying the candidates, and
`codegraph.Unknown` when this graph has never seen it. Neither is answered with an empty
result, because "nothing depends on this" is the one reply a misspelling must never get.

## What it cannot do

Static analysis, honestly labelled:

- **Python only.** A file it cannot parse — Python 2, a template, something half-written — is
  named on stderr and left out, never turned into an empty module in silence.
- **Dynamic dispatch defeats it** — `getattr(obj, name)()`, dispatch tables, monkeypatching,
  plugin registries. These land as `EXTERNAL`, which is the truthful answer.
- **Definition-time code counts as calls** — a default value, an annotation, a decorator all
  run beside the `def`, so they are edges from the enclosing scope.
- **A decorator is counted as a call** — `@register` is an edge from the enclosing scope, since
  that is where it runs. But a decorator that *replaces* the function with a different one is
  not followed through: calls to the decorated name still point at the original `def`.
- **Inheritance is resolved by name, not by import.** `self.method()` follows Python's own
  method order — C3 linearisation, so a diamond resolves where the interpreter resolves it —
  when the bases are classes it can see — same module, or a uniquely-named class anywhere in the tree.
  A base imported under an alias, or built by a metaclass, is not followed.
- **Type inference is one line deep** — `x = Foo()` then `x.method()`. Nothing beyond that.

Everything it cannot resolve is labelled rather than guessed, so the limits are visible in the
output instead of hidden in it.

## Why not something else

- **`grep` / `ctags`** — finds names, not relationships. No transitive answer, and it cannot
  tell two same-named functions apart.
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

`python3 codegraph.py --selftest` builds small trees with known answers and checks all 17 —
including **red-first controls** that prove the naive approach fails where this one does not:

- two modules both defining `digest`, and a query that must reach exactly one of them
- `open('x').write('y')` — a method on a file object, which must *not* resolve to a same-named
  function in your tree
- `from .thing import load` inside a package, next to a top-level `thing.py` — the trap that
  makes a lazy implementation return the wrong function with full confidence

The test suite adds 170 more: the CLI and its error messages, the on-disk contract, cache
invalidation, corrupt-file recovery, dangling symlinks and self-linked directories, inheritance
and cyclic class hierarchies, blast-radius completeness on a twelve-deep chain, import cycles three modules
long, a 1,200-deep import chain, decorators, redefined functions, overlapping
directory arguments, two trees whose folders share a name, three calls to one function
from one place, eight builds racing each other, files with a
byte-order mark, a symlink pointing back into the tree, a graph built
by an older copy of the tool, two functions that share a name, a misspelled
name that must not answer "nothing depends on this",
and every verb crossed with every state the graph can be in,
plus the graph's own invariants checked against real codebases,
eighteen syntactic positions a call can hide in,
and a local name shadowing an imported module,
a class, or a function of the same name,
and a diamond hierarchy checked against the interpreter's own MRO — and codegraph reading its own source.

## Licence

MIT.
