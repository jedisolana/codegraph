# Changelog

## 0.1.0

First release.

### A list is as plainly stated as a class

`cmd = []` then `cmd.append('-u')`. The tool reads `c = Client()` and types `c`; a list display
says the type just as plainly and was read as nothing at all, so the call came back UNTYPED -
which claims the target might be somewhere in this repository.

It is the biggest single source of that claim. In a clone of ansible the unresolved calls are
led by `append` (845), `get` (840), `join` (531), `items` (421) and `update` (337), and the
name-wide test that would call them external never fires, because a repository that size has
its own class with a `get` on it somewhere.

Nothing is resolved by this - a builtin's method is not in your tree, which is what BUILTIN has
always meant. Three things keep it honest. The interpreter is asked whether the method really
belongs to that type, so `config = 'text'` then `config.dumps(1)` - broken code - is not called
a builtin. A module that can see its own `dict` or `list`, defined there or imported into it,
takes the name back. And that check is scoped to the module rather than the
tree, because one file defining a function called `set` should not make every `set()` in the
repository doubtful. A first version asked the whole repository and is measurably worse: 35
fewer correct BUILTIN labels on ansible as the file stands. It was a much larger difference when
first measured, before annotations were read as well — the reason to scope it is that a
tree-wide answer is wrong, not that the gap is big.

An annotation says the same thing and was read as neither: `rows: list`, `rows: list[str]`,
`t.List[str]`, or a `-> dict` on a function one call away. A subscript IS that container here,
which is the same sentence the CLASS reading refuses for the opposite and equally correct
reason - `list[Client]` is a list, and reading Client out of it would answer `d.get()` with
`Client.get`.

1,355 calls on ansible, 878 on pandas and 11 on flask stop being "cannot tell" and are named for
what they are. Nothing gained, nothing lost, and the rates move because the denominator does.

### An imported class is still a class receiver

`Helper.tag(1)`, where `Helper` came from an import. A class DEFINED in the file resolved -
that is what the `CLASS` label is - and the identical statement on an imported one did not,
which is the ordinary way a classmethod or a factory gets called. `AnsibleTagHelper.tag(...)`
alone is 81 unresolved calls in a clone of ansible.

The lookup that says which class an imported name means already existed, is already used for
annotations and for a class written out through a module, and answers for a real class and
nothing else - so an imported FUNCTION of that name gets no answer, which matters because
`lib.helper_fn.tag` can be a real id when `tag` is nested inside it. The name has to be one this
file imported and not one it rebinds; a bare name matched across the tree would be the guess
this refuses everywhere else.

514 more resolved calls on ansible, 520 on pandas, 1 on flask, none lost.

### `from . import app` is not always a submodule

Every name after `from .` was read as a module of its own. So `from . import app`, beside an
`app = Flask(__name__)` in the package's `__init__.py`, recorded the name as living in a module
that does not exist: the object's class was lost, every `@app.route(...)` in the file next to it
went unresolved, and a phantom module went into the import graph where `deps` could name it.
Written out as `from pkg import app`, the same import worked. It is flask's own examples, and
the last three call sites `impact Scaffold.route` could not resolve.

The absolute branch has always recorded the PACKAGE as the home and the submodule only as a
candidate, kept if a module of that id really exists. Both branches do now.

Underneath it, a second one. A sub-*package* imported that way bound nothing at all: a package's
id ends in `/__init__`, and the test for "does that module exist" compared against the bare
path, which never matches one. `from .. import _profiles` bound no module, so every class
inheriting through it lost its base, and 16 answers that looked like losses from the first fix
turned out to be this second one waiting underneath.

1,097 more resolved calls across the three test repositories - 869 on ansible, 226 on pandas,
2 on flask - and none lost. The two halves are not split further than that, because fixing the
first is what let the second show through.

A third edit went in with these and came back out. It looked right - the same normalisation
applied to the other table that carries submodule candidates - and changed nothing on any of
the three repositories and no test, because the first table already reaches that conclusion. An
edit that cannot be shown to do anything is a claim that it handles a case, and it does not.

### A base class imported under an alias

`from .sansio.blueprints import Blueprint as SansioBlueprint`, then
`class Blueprint(SansioBlueprint)` - flask's own shape, and the inheritance link was dropped.
Every method Blueprint gets from Scaffold was invisible, so `bp.route()` resolved to nothing on
a receiver the tool had typed perfectly.

A base is looked up by name and then checked by name, to stop it matching some other class that
happens to answer to the spelling. The check compared against the name written HERE, and an
alias is by definition a different name from the one the class was defined under - so the
lookup found the class and the check threw it away. The import already says which class the
alias means; the check only has to accept the name it means it by.

78 more resolved calls on flask, 15 on ansible. One answer changed: `super(ActionModule,
self).__init__()` in an ansible test plugin was reaching `ActionBase.__init__`, two classes up,
because the class in between was linked under an alias. It now reaches the one in between.

Found while checking something else. A relabelling pass was about to call `bp.route()` external
- on a method sitting in that very repository - and the sample that was meant to confirm the
relabelling was right is what caught it.

### A typed receiver whose class does not have the method

`UNTYPED` is a claim: the receiver could not be typed, so the target might be in this tree. For
`client.get()` in flask's own tests it is false. `client` is a `FlaskClient`, `get` is
werkzeug's, and the tool can prove no class it can see carries it.

The name-wide version of this already ran - a method no definition in the tree carries is
EXTERNAL, not a blind spot. This is the same test with better evidence: not "does anything
define it" but "does THIS class, or any base of it this tree can see". Only for a method call,
where the name written IS the target's name, and only when exactly one class answered to the
receiver - several means the tool DECLINED to type it, which is not evidence about anything.

It resolves nothing new. It moves 200 calls on flask, 220 on ansible and 1,564 on pandas out of
the "unsure" list `impact` prints - which is this tool sending an agent to look for something it
has already proved is not there - and out of a denominator of winnable calls they were never
winnable in. The published rates move with it, and the README says so where it states them.

### Two chains in one function are two questions

`x.clone().go()` and `y.clone().go()`, on two different classes. What `clone` returned was
looked up by NAME within the function, so two calls to `clone` reaching two definitions
cancelled each other and neither chain resolved.

The rule they cancelled under is right everywhere else: a name reaching two definitions from
one scope is not an answer. But a chain does not need the scope-wide answer. The call it is
written on is on the same line, and has already been resolved on its own.

Found from a real diff rather than from reasoning: resolving `mi.append(mi)` in a pandas test
made a pair with `index.append(index)` two lines down, and the `.get_loc()` on both stopped
resolving. The tool got more right and answered less. Both are back, and three more with them.
Where one line genuinely holds two of them, the scope-wide rule applies and still refuses.

### A class reached through a module

`pd.MultiIndex.from_product(...)`. The receiver is a class named in full: a module this file
imported, then a class that module holds or re-exports. `Parent.method()` resolved and this
never did, though it is the same statement with the class written out properly - and it is how
library code is called from outside, which is most of the calls in most test suites.
`from_tuples`, `from_arrays` and `from_product` alone are written this way 2,046 times in a
clone of pandas, and 1,417 of those were unresolved.

Everything needed was already here. The chain is recorded on the edge, and turning `pkg.Frame`
into the class it means - through a package's re-export, or a dotted module path - is the
lookup class annotations already use. It answers only for a real class, and only when exactly
one answers to the name.

2,081 more resolved calls on pandas and 72 on ansible. Two stopped resolving: a function that
calls `.append()` on both a MultiIndex and an Index, where resolving one of them made the pair
ambiguous. Two answers is not an answer, and that rule fired correctly on information it did
not have before.

### A parameter shadowing a module, in a dotted call

Found while building the above. `def use(pkg)` in a file that also does `import pkg`, then
`pkg.mod.func()` - resolved into the module and labelled `QUALIFIED`, this tool's highest
confidence, on a name that means whatever the caller passed.

The guard against exactly this has been here for a long time and only ever looked at `recv`,
the receiver when it is a single name. It is None for every dotted receiver, so `a.b.c()`
walked straight past it. The root of a chain is now asked the same question, which is readable
because an import is deliberately not counted as a shadow.

### The right side is evaluated first

`df = df.where(df > 0)`. The receiver on the right is the OLD `df`, typed a line earlier. The
visitor retyped the target and then walked the value, so that call read a name with no type yet
and went unresolved - and so did every call after it. Python evaluates the right side first,
and a name means what it meant a line earlier until the assignment completes.

A variable rebound from its own method is how a great deal of dataframe, query-builder and
string-handling code is written. In a clone of pandas, `df` alone was 1,012 unresolved calls
and 126 of those now resolve; across the whole repository it is 696, plus 7 on ansible.

Five edges stopped resolving, and all five were the old order reading the NEW type on the OLD
name - `left = pd.Series(JSONArray(left.values...))`, where the `left.values` inside the call is
the argument that was passed in, not the Series being built. Right by luck, and labelled
`TYPED` either way.

The rule this does not disturb: a name holding two different classes in one scope still has
neither. The tool answers for a whole scope at once, and taking whichever branch was walked
last is right half the time and certain both times.

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
and answered by the code that already answers the two-line form. 10,657 calls in a clone of
pandas are written directly on the result of another call.

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
change — a suite that never fails is not evidence of anything. The file admits 1,342 mutations,
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
