# Changelog

## 0.1.0

First release.

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
