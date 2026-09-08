# Changelog

## 0.1.0

First release.

### A return class that is another call's return class

`def client(app): return app.test_client()` — flask's own fixture, and 204 unresolved calls in
its test suite. The class is stated two functions away, by what `test_client` declares, and
reading return classes stopped at the first hop.

These facts depend on each other. A fixture's class can come from a call inside another
fixture, which cannot resolve until that fixture has a class, which needs the call resolved.
One pass in a fixed order cannot settle that, so the return classes, the fixture values and the
calls that rest on them now run in rounds until a round changes nothing — bounded, so two
functions that return each other stop rather than spin.

The name of a call is carried ALONGSIDE the class reading, never instead of it, which is the
order the two-line form already used: `return make()` reads as the class name "make" the same
way `x = make()` does, and that name usually belongs to a function, so what the call hands back
is read when the class reading names nothing.

### An imported name that is not a class

Found while building the above, and the worst class of bug this tool can have. Asking which
class a name means, for a name the module imported, answered with whatever definition of that
name the other module holds — function or class, unchecked.

`x = connect()` reads "connect" as a class name, the way every call on the right of an
assignment is read. When `connect` is a function with something nested inside it,
`app.connect.get` is a real id, so `x.get()` resolved to that nested function and was labelled
TYPED: the highest confidence this tool has, on an object that is what the function returned.

The importing module says outright what the name is. If it is not a class it is not a class,
and looking for a class of that name elsewhere in the tree would be a guess of exactly the kind
this refuses everywhere else.

306 more resolved calls across the three test repositories, none lost.

### A test's parameters are not unknowns

pytest fills them from fixtures, by name. The rule is written down and decidable from the
tree - the module's own fixtures, then `conftest.py` in its directory, then each directory
above it, first match winning - and this tool was reading those fixture bodies all along
without ever connecting a parameter to one.

In a clone of flask, `app` and `client` were 548 unresolved calls between them: a quarter of
everything that could resolve, from two three-line fixtures in `tests/conftest.py`. So `blast`
on a library function stopped at the library, and the question this tool exists to answer has
the tests in the answer or it has half of it.

Only where pytest itself would fill it in: a test function in a `test_*.py`, a method of a
`Test*` class, or a fixture, which receives fixtures too. A parameter the body rebinds is not
answered - the tool answers for a whole scope at once - and an annotated one already had its
type. A fixture that hands back another fixture takes that one's class, followed link by link.
Two fixtures of one name are an ORDER, not an ambiguity, so this never reports AMBIGUOUS; a
sibling directory's `conftest.py` is not on the path and is not read. The label is `FIXTURE`,
so an answer that came from pytest's rule can be told from one that came from the source.

297 more resolved calls on flask - 0.496 to 0.634 - and 1,073 on pandas, none lost.

Three of the new guards were tests that could not fail. The generator guard's test used a
function with no `return` in it, so it passed with the guard deleted. The rebinding guard's
test rebound to a call, which a different rule blocks first. And two conditions in the edge
test - one for an annotated receiver, one for `self` - could not be reached at all, because
both are already out of the parameter set; they read like safeguards and were removed.

### A function says what it returns without annotating it

`def make(): return Client()` then `c = make(); c.get()` — unresolved, while the identical
function with `-> Client` on it resolved. The annotation was never the evidence. The body is,
and that is the reading already trusted one scope down, where `c = Client()` types `c`.

A function's return class is now read from its returns when every one of them agrees, and
otherwise not at all. Two classes is not an answer; one readable return beside one this file
cannot name is not an answer either, because the caller can get either and the readable one
would be wrong on the other path. `return None` is the exception — a name cannot be called
through None, the same reason `Optional[Client]` is already read as a Client. Not for a
generator, which hands back a generator object rather than what it yields, and not for an
`async def`, whose caller holds a coroutine until `await` is read too.

### A method called on what a function returned

`make().go()`. The two-line form — `x = make()` then `x.go()` — resolved, and the one-liner
never could: a receiver that is a call had its name read only as a CLASS, which is why
`Leg(100).payoff()` worked and `make().go()` did not, on the identical expression shape. The
same name is now carried as a function too, read only when the class reading names nothing,
and answered by the code that already answers the two-line form. 13,321 calls in a clone of
pandas are written on a receiver that is a call.

Together the two are 681 more resolved calls on pandas, 101 on ansible and 4 on flask. Every
call edge was compared against the previous build: none was lost. The first comparison said 80
were, which was a dictionary keyed on `(file, callee, line)` over 22,443 call sites that share
one — the tool was right and the measurement was not.

### An imported singleton keeps its type

`from .globals import display`, then `display.warn()`. The type was known one module up, where
`display = Display()` is written out in full, and dropped at the import: local inference typed
that variable in the file that built it, and every other file using it got nothing.

The module's instance bindings are now carried with its imports and re-exports, so a
from-imported name is looked up in the module that made it, with the class resolved in *that*
module's scope — `display` means whatever `Display` meant where the object was made, not
whatever the caller happens to import. Aliases (`import display as d`) follow the original
name. A local of the same name still shadows it, a name bound to something that is not an
instance is still untyped, and an instance of a class outside the tree is not invented.

315 more resolved calls on a clone of ansible, 21 on flask — about a point on each. I had
estimated twenty points on flask, by counting the calls that *mention* such a name instead of
the ones a class can actually be found for. The rule is real and the estimate was not; the
measurement is what the README states.

### One answer for what a receiver is

`self.conn.__len__()` resolved and `len(self.conn)` did not — the same object, the same line of
reasoning, two answers. Instance-attribute types were taught to written method calls and to
nothing else, so `with self.conn`, `for x in self.conn`, `self.conn[0]` and
`self.cfg.endpoint` all went back to being blind on exactly the receivers that had just been
solved.

Every place that needs a receiver's class now asks one function, which knows the three shapes
this file can answer for: `self`/`cls`, a local whose class is known, and `self.<attribute>`.
An attribute the class never assigns still has no type, and `with` on it does not reach for
whichever class happens to define `__enter__`.

### An attribute on the instance has a class too

`self.db = Database()` in the constructor and `self.db.query()` in the method below it — how
most object-oriented Python is written, and the shape this tool's own description named as what
it could not resolve. 14,034 such call sites in the standard library, none of them answerable.

The type is written down in three places and none was read: the constructor call, an annotation
on the assignment, and a bare annotation in the class body. All three now are, scanned from the
whole class body before any method is visited — a method that uses an attribute is often
written above the `__init__` that sets it, and reading them as the methods go past would give
an answer that depended on the order somebody wrote the file in.

An attribute assigned two different classes gets no type: which one a call reaches depends on
which branch ran. 1,759 more edges resolve, `TYPED` grows by 30%, and the resolution rate goes
from 0.414 to 0.423 — the largest single move of the day.

The scan walks statement bodies rather than every node. Descending into expressions as well
walked most of the file a second time and cost a sixth of the build; assignments are statements,
so it never needed to.

### `Optional[Client]` is a Client

A subscripted annotation was skipped wholesale, on the sound reasoning that `Dict[str, Client]`
is a dict and reading `Client` out of it would resolve `d.get()` to a Client method. That rule
swept up the one subscript that is not a container: `Optional[Client]`, `Union[Client, None]`
and `Client | None` each say "a Client, or nothing at all", and a method called on one is a
Client's method with no second candidate.

It was the costliest annotation gap left. In an installed-packages corpus `X | None` is 414 of
the annotated parameters against 550 plain ones — two in five stated their type outright and
were not listened to. A string forward reference spelling out the same thing,
`"Optional[Client]"`, is now read too, where before only a single bare identifier was.

A union of two real classes is still not read. `Client | Server` does name a class the call
could reach, and choosing one is the guess this tool exists not to make.

### A class nested inside a class is still a type

`i = Outer.Inner()` then `i.method()`: the construction resolved and the method did not.
Resolving a dotted type name only ever read the part before the dot as a module, so
`Outer.Inner` found nothing, the variable was left untyped, and every call on it afterwards
came back unresolved. The constructor edge landed perfectly throughout, which is what made it
hard to see — the class was obviously known, and the next line was a blind spot.

A module-qualified `svc.Client()` still wins, since that reading is tried first; the nested
class is the fallback for when there is no module by that name.

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
change — a suite that never fails is not evidence of anything. The file admits 1,267 mutations,
and the count is checked by a test, because it was published as 208 here and 710 in the README
while the real number was neither.

A full pass has been run over an earlier version of the file and **killed every one, with no
survivors** — 954 by a failing test and two by a timeout. The file has grown since: an MCP
server, a shape reader, a saving footer and an incompleteness warning, a hundred and eighty-five
more sites, not yet covered by a pass of their own. The count above is a fact
about the file today; that result is a fact about the file as it was.
Two of them were killed by the timeout rather than by a failing test, because they make the
suite never finish rather than fail, and "hung" and "failed" are counted apart. It is re-run
whenever the file changes — the previous pass covered 841, and a result about an older
version of a file is not a result about this one.

It edits codegraph.py in place, so it refuses to start unless that file is committed, and it
verifies it byte for byte before exiting. A run that is killed rather than finished cannot do
that — `finally` does not run — and what it leaves behind is not one flipped comparison but the
whole file rewritten without its comments, so it now drops a marker on the way in and explains
itself on the way back.

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
