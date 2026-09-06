# Changelog

## 0.1.0

First release.

### Constructing an object runs `__new__` too

The constructor edge exists because `impact __init__` on a class built in twenty places used to
answer "callers: (none)". `X()` runs `__new__` first, and that half was never recorded: 237
definitions in the standard library, 8 of them with a caller. A class defining only `__new__` —
a singleton, an immutable type, anything that interns its instances — reported that nothing
depends on the method that builds it.

Both are recorded now, not one or the other, because a class defining both runs both. Found
through the same inheritance order as `__init__`. 125 of those 237 now have a caller, 1,373
more edges resolve, and the resolution rate goes from 0.409 to 0.413 at no measurable cost.

### A renamed import was a different function

`from pkg import load` resolved and `from pkg import load as l` did not. When a package
re-exports a name, the chain that follows it to where the name really lives was walked under
the name written at the call site — so aliasing it meant no module along the chain had heard of
that name, the first hop failed, and the call came back `EXTERNAL`: the label that means "not
in your tree" about a function two directories away. Two spellings of one import, two different
answers, and the wrong one silent.

The chain is now followed under the name each module along it actually knows, and the name the
defining module uses is carried back — every hop is free to rename it again, and arriving at
the right module to ask for the wrong name resolves to nothing, which looks identical to a call
going outside the tree.

### Two calls were one edge even when they landed in different places

Call edges were deduplicated on (caller, receiver, name). A method holding
`super(A, self).run()` and `super(C, self).run()` collapsed into a single edge — neither super
writes a receiver, so the two looked identical. The survivor pointed at one target and took the
other's line number as one of its own call sites, so `callers` lost a caller and `sites` gained
a place where nothing is called. Both answers came back confident.

Two edges are the same relationship only when they resolve to the same place, so the key is now
everything an edge carries except where it was written. The list is written out rather than
derived — deriving it cost a fifth of the build on a large tree — and a test parses files
exercising every edge shape and fails if any field escapes it, because the failure mode of a
hand-written list is forgetting to add to it and the symptom is silence.

### Three more things the language runs on its own

`class Child(Base)` runs `Base.__init_subclass__` — looked up on the order *after* the new
class, the way `super()` is, so a class never calls its own. Building a dataclass runs
`__post_init__` from a generated `__init__` that is not in the graph at all; a class that
writes its own `__init__` is left alone, since it calls `__post_init__` in the open and
counting both would invent a caller. An f-string placeholder runs `__format__`, `{x!r}` runs
`__repr__`, and a class defining neither falls back to `__str__` the way `x += y` falls back
to `__add__`.

In the standard library those had 2 of 45, 5 of 25 and 3 of 29 definitions with a caller. They
now have 38, 22 and 4, and `__repr__` went from 40 to 59 of 391.

### Iteration counts however it is written

`for x in r` was recorded and `[x for x in r]` was not — the same operation, two spellings,
two different answers. The statement form turned out to be under half of it: the standard
library writes 11,571 `for` statements against 15,322 comprehension clauses, unpackings,
star-expansions, `yield from`s and augmented assignments, none of which produced an edge.

All of them do now, including the second and later clauses of a comprehension, which run in the
comprehension's own scope rather than the enclosing one.

`x += y` needed deciding rather than guessing: it runs `__iadd__` when the class defines one
and falls back to `__add__` when it does not, and which of those is a fact about the class, not
about the line being parsed. Both names are carried through resolution and the reachable one is
kept — `__add__` appears in nearly four times as many standard-library files as `__iadd__`, so
the fallback is the common case. When a class has both, only `__iadd__` is credited, because
only `__iadd__` runs.

### The language calls things the source never names

`with r:` runs `__enter__` and `__exit__`. `for x in r` runs `__iter__`. `len(r)` runs
`__len__`, `r[k] = v` runs `__setitem__`, `1 in r` runs `r`'s `__contains__`. Not one of them
is an `ast.Call` on the method, so none produced an edge — `impact __exit__` on a context
manager answered that changing it would break nothing. `unused` had annotated these
"(python calls this one)" all along, so the category was known and the edge still missing.

Statement forms, subscripts, operators, comparisons and the builtins that are a method in
disguise (`len`, `str`, `iter`, `next`, `hash`, `bool`, `abs`, …) now all resolve, through the
same inheritance order as a method call and with the same requirement: a receiver the file can
put a class to. Against the standard library that is 1,148 new edges, and definitions reached
this way that have a caller went from 45 to 353.

Where the class is known and simply does not have the method — `for row in rows` on a `list`
subclass — the edge is `EXTERNAL`, not "cannot tell". Recording it as unknown claimed the
target might be in the tree and pulled the resolution rate down with cases nobody could win;
labelled truthfully, the rate goes up rather than down.

### Reading a property is a call

`c.endpoint` on a `@property` runs a function, and there is nothing in the syntax to say so —
no `ast.Call` node exists, so the call visitor never saw one. Every property in a tree answered
`callers: (none)`, and `impact` on one said that changing it would break nothing. That is the
worst shape of wrong answer this tool can give, because it is the answer somebody deletes code
on: a method one line away, on the same annotated receiver, resolved perfectly.

Attribute reads are now counted wherever the receiver can be typed — `self`/`cls` inside a
class, or a local whose class is known — and resolved through the same inheritance order as a
method call, so a property on a mixin is found from a subclass that reads it. `cached_property`
and the `@x.setter` family count too.

Against the standard library that is 888 new edges and 310 definitions that no longer claim to
have no callers, verified against the source rather than counted. Whether a name is a property
cannot be known while a single file is being parsed, so every typed attribute read is recorded
and then discarded once all the definitions are in: 98% of them are ordinary fields. They are
dropped by name before the resolution pass rather than after it, which keeps the cost to
roughly 5% of peak memory and 6% of build time.

### Installing it

The distribution is **`jedi-codegraph`**. `codegraph` on PyPI belongs to an unrelated project,
and there is no hyphenated spelling to fall back on: PyPI compares a proposed name against the
existing ones with `-`, `_` and `.` removed, so every arrangement of those letters collides.
The command, the module and the import are all still `codegraph`.

Releases are cut by tagging. The workflow calls the same test matrix the branch runs rather
than copying it, refuses a tag that disagrees with the version, refuses a wheel that carries
the metadata but not the tool or its entry point, and uploads through Trusted Publishing - so
no API token exists to leak or rotate.

### What it does

- `impact <name>` — the pre-edit view: who calls it, every call site as `file:line`, and the
  transitive blast radius, in one command.
- `callers`, `calls`, `blast`, `sites`, `where`, `find`, `path`, `deps`, `cycles`, `stats`.
- One file, the standard library only, no install required. Copy it into a repo and run it.
- A built-in `--selftest`: 32 ground-truth checks, several of them red-first controls that
  prove the naive approach fails where this one does not.

### Two readers

The command line prints prose for a person. `--json` gives the same answers as data for
whatever is going to parse them — including the refusals, so a misspelling and a function with
nothing calling it stay distinguishable without matching on an English sentence. The prose form
of `impact` says the blast radius holds *five functions*; the JSON says which.

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
- **"I cannot tell" said about a list.** `UNTYPED` claims the receiver could not be typed and
  the target might be in your tree. For `rows.append(1)` that claim is false and provable —
  nothing in the tree is named `append` — so those are `EXTERNAL` now. Nine of every ten
  "cannot tell" edges on one real codebase were `.get()`, `.items()`, `.join()` and
  `.assertEqual()`. It applies only to method calls, where the name written is the target's;
  for a bare call on a local the name is the variable's and says nothing.
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
