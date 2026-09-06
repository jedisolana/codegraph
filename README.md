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
         boardofdirectors/server.py:279, boardofdirectors/server.py:632,
         boardofdirectors/server.py:635, boardofdirectors/server.py:665,
         boardofdirectors/server.py:679, boardofdirectors/server.py:697,
         boardofdirectors/server.py:712
blast:   19 functions could be affected
```

That is a real answer from a real codebase, not a mock-up. Four callers, the nine exact lines
to open — a caller that checks a gate five times is five places to edit — and the transitive
radius, before you touch anything.

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

| label | what it means | resolved? |
|---|---|---|
| `LOCAL` | a bare call to a function this scope can see — the same module, or a function it is nested inside | yes |
| `QUALIFIED` | `thing.load()`, where `thing` is a module this file actually imported | yes |
| `SELF-METHOD` | `self.helper()`, resolved inside the enclosing class | yes |
| `INHERITED` | `self.method()` or `super().method()`, where the method lives on a base class | yes |
| `CLASS` | `Parent.method()` — the receiver is a class in this module | yes |
| `TYPED` | the receiver's class is known: `x = Foo()`, `x = svc.Foo()`, or an annotation that says so | yes |
| `CONSTRUCTOR` | `Client()` — the second, equally real edge, to the `__init__` it runs | yes |
| `AMBIGUOUS` | several definitions match; the candidates are listed and none is picked | no |
| `BUILTIN` | `len()`, `open()`, `sorted()` — certainly not yours | no |
| `EXTERNAL` | a library, the stdlib, or a method whose name nothing in your tree defines | no |
| `UNTYPED` | the receiver could not be typed, and the target might be yours | no |

Two of those deserve the detail the table cannot hold.

**`TYPED`** decides *which* `Foo` by what the file defines or imports, never by a tree-wide
name search. It looks the method up the inheritance order, so an inherited one is found and an
override wins. It refuses when two branches give `x` two types, when a later line rebinds it to
something it cannot name, and when two classes answer to the name. Calling an instance —
`c(1)` where `c` is a `Client` — resolves to that class's `__call__`, since the call site never
writes the name.

**`CONSTRUCTOR` and `INHERITED`** exist because of the same problem: some calls never mention
what they run. `Client()` runs an `__init__`; `super().save()` runs a parent's `save`; `c(1)`
runs a `__call__`. Without those edges the tool answers "nothing depends on this" about the
most-edited method in Python.

Most call-graph tools guess and hand you one answer. This one refuses. An `AMBIGUOUS` edge is
a real result: it means the question does not have a single answer, and a blast radius that
quietly picked one would be worse than no blast radius at all.

`UNTYPED` is the one that matters, and it is a claim rather than a shrug: *the receiver could
not be typed, and the target might be in your tree.* `x.run()` where something of yours defines
a `run`. It is deliberately not lumped in with `EXTERNAL`, because that would claim knowledge it
does not have.

The claim has to be true, though. `rows.append(1)` and `text.strip()` are not calls it failed
to place — **nothing in your tree is named `append` or `strip`**, so the target cannot be here,
and the tool can prove that rather than guess it. Those are `EXTERNAL`. On one real codebase
nine of every ten "cannot tell" edges were `.get()`, `.items()`, `.join()` and `.assertEqual()`.

`stats` reports that rate over **what was winnable** — resolved, plus ambiguous, plus the calls
it could not type. Builtins and library calls are excluded, since counting them would only
measure how much of the standard library you happen to use:

```json
{
  "call_edges": 2694,
  "call_sites": 3590,
  "edge_confidence": {"EXTERNAL": 1347, "QUALIFIED": 536, "BUILTIN": 389, "LOCAL": 197,
                      "SELF-METHOD": 78, "UNTYPED": 76, "CONSTRUCTOR": 57, "INHERITED": 10,
                      "TYPED": 3, "CLASS": 1, "AMBIGUOUS": 0},
  "resolved_to_one_def": 882,
  "could_have_been_resolved": 958,
  "resolution_rate": 0.921
}
```

That 0.921 says: of the calls that could plausibly have gone to something in this codebase,
it placed 92%. It is not the sum being flattered — the rule is the opposite of the usual one.
A denominator that counts `list.append` and `str.strip` is not measuring how much the tool
resolved, it is measuring how much of Python you happen to use, and the same reasoning that
keeps builtins out keeps those out. What remains in it are the calls that genuinely might have
been yours.

The number this README first published was 0.29, over a denominator that swept in every
impossible call. Two things moved it: resolution bugs fixed with tests, and then that
denominator being made to mean something. Both directions are in `CHANGELOG.md`, with the
count of fabricated edges each one removed.

There used to be one more label. `RESOLVED` meant "a bare call, and exactly one definition of
that name exists somewhere in the tree" — which is a coincidence, not a resolution. By the time
every real way a bare name reaches a definition had a rule of its own, it fired fifteen times
across an entire standard library and not once across 2,725 installed packages, and the fifteen
were wrong: `turtle` builds `up`, `down`, `left` and `right` at import time rather than defining
them, and those calls were being answered with functions in `_pyrepl`. A name this file neither
defines nor imports nor stars in is a library, something injected at runtime, or a mistake — and
saying `EXTERNAL` is the true answer to all three.

## The commands

```
codegraph build [dir...]     build the graph (default: here); writes codegraph.json
codegraph impact <name>      callers + call sites + blast radius, and what it could not resolve
codegraph callers <name>     who calls this
codegraph calls <name>       what this calls
codegraph blast <name>       transitive callers — what could break
codegraph sites <name>       every call site as file:line, relative to what you built — all of them
codegraph where <name>       where a symbol is defined
codegraph find <substr>      fuzzy symbol search
codegraph path <from> <to>   a call path connecting two functions
codegraph deps <module>      a module's in-tree imports and importers, `import_module("x")` included
codegraph cycles             import cycles of any length, including a module importing itself
codegraph symbols            every function and class defined here
codegraph unused             every definition nothing here calls — read the caveat
codegraph stats              counts, resolution rate, never-called definitions
codegraph --selftest         32 ground-truth checks, several of them red-first
codegraph --help             the same list; a bare `codegraph` prints it too
--json                       any query, answered as data instead of prose
--only PAT / --exclude PAT   keep or drop results — a glob over the id or its module,
                             so `--exclude 'tests/*'` and `--exclude '*.Handler.*'` both work
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
The graph records the size and timestamp of every file it read and compares them exactly, rather
than asking whether anything is newer than itself: a file restored from a backup, a checkout or a
container layer arrives *older* than the graph while holding different code.
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

From a shell, add `--json` and every verb answers in data rather than prose — **including the
refusals**, so a misspelling and a function with nothing calling it stay distinguishable
without matching on an English sentence:

```bash
$ codegraph impact leaf --json
{"query": "impact", "target": "app.leaf", "callers": ["app.mid"],
 "sites": [{"at": "app.py:4", "caller": "app.mid"}], "blast": ["app.mid", "app.top"],
 "unresolved": []}

$ codegraph callers digest --json        # exit 2
{"error": "ambiguous", "name": "digest", "detail": "2 definitions answer to that name",
 "candidates": [{"id": "pulse.digest", "at": "pulse.py:9"}, ...]}
```

The prose form of `impact` says the blast radius holds *five functions*; the JSON says
**which**. That gap existed because the prose was written for a person reading it and nothing
was checking the other reader.

The library refuses exactly what the command line refuses, through the same code:
`codegraph.Ambiguous` when several definitions answer to the name, carrying the candidates, and
`codegraph.Unknown` when this graph has never seen it. Neither is answered with an empty
result, because "nothing depends on this" is the one reply a misspelling must never get.

## What it cannot do

Static analysis, honestly labelled:

- **Python only.** A file it cannot parse — Python 2, a template, something half-written, or a
  generated one nested deeper than the interpreter's own stack — is named on stderr and left
  out, never turned into an empty module in silence.
- **A conditional import has no single answer.** `try: from fast import parse / except:
  from slow import parse` binds one name from two modules, and which one runs depends on the
  machine. Both are listed as candidates; neither is chosen.
- **Dynamic dispatch defeats it** — `getattr(obj, name)()`, dispatch tables, monkeypatching,
  plugin registries. These land as `EXTERNAL`, which is the truthful answer.
- **Definition-time code counts as calls** — a default value, an annotation, a decorator all
  run beside the `def`, so they are edges from the enclosing scope.
- **A decorator is counted as a call** — `@register` is an edge from the enclosing scope, since
  that is where it runs. But a decorator that *replaces* the function with a different one is
  not followed through: calls to the decorated name still point at the original `def`.
- **`super()` resolves against the class it is written in.** Python's own order for that
  class - so a diamond lands where the interpreter lands. What static analysis cannot know is
  that `B.m`'s `super()` goes to `C` when `B` is reached through a `D(B, C)` instance.
- **A base class is found the way any other class name is** — defined in this module, or
  imported into it, before any tree-wide search; an alias (`from x import Base as B`) is
  followed. Method lookup then uses Python's own order, C3 linearisation, so a diamond resolves
  where the interpreter resolves it. What is not followed: a base built by a metaclass, a base
  that is a variable (`V = Generic[T]` then `class C(V)`), and a subscripted one — `Generic[T]`
  names `Generic`, and a subscript is not a name.
- **Type inference is one line deep** — `x = Foo()` then `x.method()`, plus annotations, which
  say it outright: a parameter's, and a variable's. A container annotation is not its contents,
  so `Dict[str, Client]` stays a dict. A name rebound to anything the tool cannot name loses its
  type rather than keeping the old one. Nothing beyond that: no return types, no attributes,
  and `with Foo() as c` is not assumed to give you a Foo, because `__enter__` may return
  anything at all.

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

On PyPI it will be `jedi-codegraph`, not `codegraph` — that name belongs to somebody else, and
PyPI reads hyphens as if they weren't there. The command is `codegraph` either way.

Python 3.9+. Tested on Linux, macOS and Windows.

## Proving itself

A suite that never fails is not evidence of anything, so it is checked the other way round.
`tools/mutation.py` breaks the tool one small way at a time — flips a comparison, swaps an
`and` for an `or`, drops a `not`, moves a number by one — and runs the suite against each
change. Every one of them should make something go red. **All 710 mutations the file admits:
710 caught, none survived.** Two of them do not make the suite fail but make it never finish —
flip the comparison that ends a `while` — and those are caught by a timeout and counted apart,
because "hung" and "failed" are different facts.

Which name shadows which is the question everything else rests on, so it is not only
checked against fixtures: `symtable` is CPython's own scope analysis, and on the versions
where its model matches this one, the two are compared scope by scope over the running
interpreter's standard library. Fifteen thousand scopes, nothing missed.

`python3 codegraph.py --selftest` builds small trees with known answers and checks all 32 —
including **red-first controls** that prove the naive approach fails where this one does not:

- two modules both defining `digest`, and a query that must reach exactly one of them
- `open('x').write('y')` — a method on a file object, which must *not* resolve to a same-named
  function in your tree
- `from .thing import load` inside a package, next to a top-level `thing.py` — the trap that
  makes a lazy implementation return the wrong function with full confidence
- `Mid(1)` where `Mid` inherits its `__init__` — and the control showing that matching on the
  name `__init__` finds no caller at all, because no call site contains the word
- two functions in one module that each define a helper called `inner`, which a lookup keyed on
  (module, name) can only tell apart by luck
- `super().run()`, whose caller never writes the name of what it calls
- `thing.load()` in a file that never imported `thing`, next to a `thing.py` that would have
  answered — and the same trap reached by writing one dot too many in a relative import
- two classes called `Client`, one import naming which, and a method call that has to land on
  the one the file imported
- `try: from fast import parse / except ImportError: from slow import parse` — one name, two
  sources, and a dict that could only remember the fallback
- `def send(c: Client)` beside `def keyed(d: Dict[str, Client])`, where one of the two says
  what the receiver is and the other does not
- a class defined inside a function, which a second function cannot name — beside
  `svc.Client()`, which any function that imported `svc` can
- `lambda config: config.dumps(x)` in a file that imports a module called `config`, beside a
  deferred `import config` inside a function, which is the same name meaning the opposite thing
- `[config.dumps(r) for config in rows]` on one line and `config.dumps(2)` on the next, where
  the same name is the loop variable and then the module again
- `from time import sleep` in a tree that happens to contain a `sleep` of its own, and
  `from ops import index as _index`, where the module holds `index` and the file says `_index`
- `from turtle import *` followed by a bare `home()`, next to another module that also has one

The test suite adds 406 more. Grouped, because a list of every one of them stopped being
readable a long time before it stopped growing:

- **Python's own rules**, which are where the wrong answers come from: what shadows what — a
  parameter, a lambda's argument, a comprehension variable, a class attribute, a `match`
  capture, a loop target at the top of a file; which `Client` an import means when two files
  define one; `super()` through a diamond, checked against the interpreter's own order;
  `from x import y as z`; a star import; a conditional import that has two answers; a base
  class that is really a variable.
- **Calls that never write the name they call** — a constructor, an inherited method, a
  `__call__`, a `super()`. Each of those once returned "nothing depends on this" about code
  that is called constantly.
- **Trees that fight back** — dangling symlinks, a symlink into the tree, a self-linked
  directory, an unreadable file, a read-only directory, a byte-order mark, a file too deeply
  nested for the interpreter to walk, folders with dots in their names, two trees whose
  folders share a name, eight builds racing each other.
- **The graph on disk** — cache invalidation by size *and* timestamp, a file restored from a
  backup with the timestamp it used to have, deletion (which changes nobody's mtime), a graph
  built by an older copy of the tool, an interrupted write, valid JSON that is not a graph,
  and a second build that has to reach the same graph as the first.
- **Both doors** — every verb crossed with every state the graph can be in, the library
  refusing exactly what the command line refuses, exit codes, and a misspelled name that must
  never answer "nothing depends on this".
- **Its own claims** — the counts in this file, the example above, the promises in
  `SECURITY.md` (no network, no execution, nothing but the standard library), the label table
  further up, and every shipped file checked for a private origin story.

## Licence

MIT.

Built by [@jedisolana](https://x.com/jedisolana).
