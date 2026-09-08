# codegraph

**Before you change a function, find out what else breaks.**

You are about to edit `_paid_ok` — the function that decides whether someone has paid. Who
calls it? Which exact lines do you have to open? What else depends on those?

Ask it:

```bash
pipx install jedi-codegraph
codegraph build .
codegraph impact _paid_ok
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

Read that as three answers:

- **callers** — four functions call it.
- **sites** — nine exact lines to open. One of those callers checks the gate five times, so it
  is five separate edits in one file.
- **blast** — 19 functions sit downstream. They call it, or call something that does.

Real output from a real codebase, not a mock-up. One command, before you touch anything.

**One file. No dependencies. Nothing but the Python standard library.** Copy `codegraph.py`
into any repo and it works.

## Why you can trust the answer

Any tool can print a list. The question is whether to believe it.

**It tells you when it doesn't know.** Every answer is labelled with how it was worked out —
certain, or a guess it refuses to make. If two files both define `digest`, it names both and
asks which you meant, instead of picking one. A tool that presents a guess as a fact is worse
than no tool, because you cannot tell which answers to double-check.
[See the labels](#it-tells-you-when-it-doesnt-know).

**Its own tests are built to fail.** Most projects ship tests that were green the day they were
written and could never have gone red. Here, several checks deliberately prove that the obvious
approach gives the *wrong* answer — so if this tool ever became that naive, the tests would
catch it.

It is also checked by breaking it on purpose: **867 small sabotages of its own code, one at a
time, and all 867 made a test fail.** No behaviour goes unwatched.
[How it is checked](#proving-itself).

## Why not just search for the name?

Because search finds the word, not the meaning.

Two files can both define a function called `digest`. Search shows you both. Only one of them
is the one you are about to break, and search cannot tell you which. Ask this for `digest` and it will not guess
either — it names both and waits for you to say which, because merging their callers into one
answer is how you end up "fixing" a caller of the other one. Your editor's "find references" can, but it
needs a language server running, and it will not give you the *transitive* answer: who calls the
callers.

This is the question you actually have before an edit — **what depends on this** — answered in
one command, from a file you can copy into any repo.

## It tells you when it doesn't know

This is the part that matters most.

Python is a language where you often cannot tell, just by reading, what a line of code will
call. So every answer comes with a label saying how it was worked out — and the ones it could
not work out are listed as exactly that, rather than quietly guessed:

| label | what it means | resolved? |
|---|---|---|
| `LOCAL` | a bare call to a function this scope can see — the same module, or a function it is nested inside | yes |
| `QUALIFIED` | `thing.load()`, where `thing` is a module this file actually imported | yes |
| `RE-EXPORT` | `flask.abort()`, where `abort` is not defined in `flask/__init__.py` but re-exported by it — the shape of nearly every library API | yes |
| `SELF-METHOD` | `self.helper()`, resolved inside the enclosing class | yes |
| `INHERITED` | `self.method()` or `super().method()`, where the method lives on a base class | yes |
| `CLASS` | `Parent.method()` or `pd.MultiIndex.from_product()` — the receiver is a class, written out: defined here, or held by a module this file imported | yes |
| `TYPED` | the receiver's class is known: `x = Foo()`, `x = svc.Foo()`, or an annotation that says so | yes |
| `CONSTRUCTOR` | `Client()` — the second, equally real edge, to the `__new__` and `__init__` it runs | yes |
| `FIXTURE` | a test's parameter, filled by the pytest fixture that name resolves to — the module's own, then `conftest.py` in its directory, then each directory above | yes |
| `AMBIGUOUS` | several definitions match; the candidates are listed and none is picked | no |
| `BUILTIN` | `len()`, `open()`, `sorted()` — and `cmd.append()` where the source wrote `cmd = []`, checked against the interpreter's own list of that type's methods | no |
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

**The language calls things the source never names.** `with r:` runs `__enter__` and
`__exit__`; `for x in r` runs `__iter__`; `len(r)` runs `__len__`; `r[k]`, `r[k] = v` and
`del r[k]` are three different methods; `r + 1` runs `__add__` and `1 in r` runs `r`'s
`__contains__` — the one case where the receiver is the operand on the right. All of these are
edges. In the standard library 2,780 definitions are reached only this way, and 98% of them
used to report no caller at all.

Defining `class Child(Base)` runs `Base.__init_subclass__`. Building a dataclass runs
`__post_init__`, from an `__init__` that is generated and so is nowhere in the graph. An
f-string placeholder runs `__format__`, or `__repr__` for `{x!r}`.

Iteration counts however it is written — a `for` statement, a comprehension, a generator
expression, `a, b = r`, `[*r]`, `yield from r`. And `x += y` runs `__iadd__` when the class has
one and `__add__` when it does not, which is decided against the class rather than guessed at
the line.

**Reading a property runs it**, and writes no parentheses doing so — `c.endpoint` is a call
with nothing in the syntax to say so. Attribute reads are counted wherever the receiver can be
typed, the same reach a method call has: `self` and `cls` inside a class, or a local whose
class is known, resolved through the same inheritance order. In the standard library that is
807 definitions, 331 of which really are read somewhere and used to report no callers at all.

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

Run on this repository, so you can reproduce it — `codegraph build . && codegraph stats`:

```json
{
  "call_edges": 6677,
  "call_sites": 8741,
  "edge_confidence": {"EXTERNAL": 3364, "INHERITED": 1079, "BUILTIN": 770, "SELF-METHOD": 606,
                      "QUALIFIED": 448, "LOCAL": 228, "UNTYPED": 174, "TYPED": 5,
                      "CONSTRUCTOR": 3, "AMBIGUOUS": 0},
  "resolved_to_one_def": 2369,
  "could_have_been_resolved": 2543,
  "resolution_rate": 0.932
}
```

Measured on CPython 3.14. Every figure above is identical on every supported version except
`call_sites`, which moves by one: a placeholder inside a **multi-line f-string** reported the
line the string *starts* on before Python 3.12, and its own real line from 3.12. Same code,
same edges — one of them is two places to look and the other is one, and the newer answer is
the more precise. A number that depends on the interpreter is worth saying out loud rather
than leaving somebody to find.

That 0.932 says: of the calls that could plausibly have gone to something in this codebase,
it placed 93%. It is not the sum being flattered — the rule is the opposite of the usual one.
A denominator that counts `list.append` and `str.strip` is not measuring how much the tool
resolved, it is measuring how much of Python you happen to use, and the same reasoning that
keeps builtins out keeps those out. What remains in it are the calls that genuinely might have
been yours.

The number this README first published was 0.29, over a denominator that swept in every
impossible call. Two things moved it: resolution bugs fixed with tests, and then that
denominator being made to mean something. Both directions are in `CHANGELOG.md`, with the
count of fabricated edges each one removed.

On other people's code, measured on a clone of each and built from its own root. These three
have been tracked from the beginning, so the `before` is where each started:

| repo | before | after |
|---|---|---|
| flask | 0.241 | **0.755** |
| ansible | 0.379 | **0.726** |
| pandas | 0.359 | **0.817** |

And the whole corpus it is measured against now — **fourteen codebases, 596,641 call edges,
156,835 definitions**. They were not chosen for flattering numbers; they were chosen to be
unalike, and the low two are here for the same reason as the high two:

| repo | rate | | repo | rate |
|---|---|---|---|---|
| rich | 0.869 | | attrs | 0.771 |
| click | 0.864 | | flask | 0.755 |
| starlette | 0.837 | | pydantic | 0.727 |
| black | 0.823 | | ansible | 0.726 |
| scrapy | 0.816 | | sqlalchemy | 0.702 |
| pandas | 0.817 | | celery | 0.596 |
| httpx | 0.801 | | stdlib | 0.577 |

Running it over code nobody aimed it at is what has found every real bug in it, and the number
that matters is the one that looks wrong. `httpx` first measured **0.353** against a field that
was otherwise above 0.72, and that gap was one line: `httpx/__init__.py` is twelve lines of
`from ._api import *`. The two at the bottom were checked the same way and are not that. `sqlalchemy` was the next one down and gave up a rule rather than a bug: it is the library that
made the fluent interface Python idiom, and every link in `q.filter(a).order_by(b).all()` is
annotated `-> Self`. That is PEP 673 and it names no class, so the chain stopped at the first
link — 510 methods in pandas are written that way and 261 in sqlalchemy. `Self` means the class
of the RECEIVER, which is not the class the method is written in the moment a subclass inherits
it, so where a subclass overrides the next call in the chain the answer is refused rather than
guessed. celery
is a dynamic task registry and unannotated fixtures; the standard library is old, barely
annotated, and spends its time on strings and dicts — which is the blind spot named further
down, showing up as a number.

It is also fast enough to ask casually, which is the point of a tool an agent talks to. Measured
on the same clones, on a laptop:

| repo | call edges | build, cold | build after one file changed | a query |
|---|---|---|---|---|
| pandas | 191,470 | 11.2s | 4.1s | 0.5s |
| sqlalchemy | 153,034 | 8.1s | 2.9s | 0.5s |
| stdlib | 62,552 | 4.2s | 1.2s | 0.2s |
| rich | 9,279 | 0.5s | — | — |

That is about seventeen thousand call edges a second to build, on the largest Python
repositories there are, and a query is mostly the cost of loading the graph back off disk. The
MCP server keeps the parsed graph for the life of the session, so an agent pays that once
rather than per question. "After one file changed" is the loop you are actually in: edit, ask,
edit — and nothing has to be rebuilt by hand, because a query notices the tree moved and
rebuilds what moved.

One more check does not measure the tool against a corpus but against the interpreter. The
README says a diamond lands where Python lands; that is checkable by asking Python. A hierarchy
with five leaves — `D(B, C)`, `E(C, B)`, an inherited one, an unrelated mixin, and the awkward
case where the first base does NOT define the method — is imported, Python is asked which class
actually owns the call, and codegraph is required to have said the same. A hand-written
expectation encodes what the author believed C3 does; this cannot drift from what Python does.
Replacing the linearisation with a depth-first walk — the bug that was there originally — turns
it red on exactly the case that bug got wrong.

What `from x import *` carries is checked the same way, because that rule was written from
reading the language reference and is exactly the kind of thing to get subtly wrong. The
package is imported, Python is asked which names actually arrived, and the tool has to resolve
through those and no others. It agrees on all five: a name `__all__` lists, one it leaves out,
an underscore name, a plain name from a module with no `__all__`, and an underscore name from
one. Asking also turned up a detail worth knowing — `__all__` naming something the module does
not define makes `import *` raise, so that shape cannot occur in code that runs.

The calls Python makes from SYNTAX are checked the same way: `x += y`, `with c`, `for r in
rows`, `len(x)`. Which method actually runs is a fact about the class, so Python is asked and
the tool has to agree. The interesting one is `+=`, which runs `__iadd__` when the class has
one and falls back to `__add__` when it does not — never both, because only one of them runs —
and a subclass inherits that choice from wherever the method really lives. Removing the
fallback, or looking for it only on the class itself rather than up its bases, each turn the
comparison red.

`super()` is checked both ways round, because half of it is a limit rather than an answer. For
the class at the bottom of a diamond the tool and the interpreter agree and that is the whole
answer. For a class in the MIDDLE the tool answers for the class the call is written in, which
is right for an instance of that class and wrong for one reached through the diamond — and
which of those it is depends on the object, not the file. The test records Python's own answer
beside it, `D().run()` producing `"DBCA"`, so the limit is written down as a fact rather than
an opinion about one.

Counting dots is checked that way too, and that one found a bug. `from ...top import x` inside
`pkg.sub.deep` means `pkg.top`; one dot more is beyond the top-level package and Python refuses
to import at all. The guard for that compared with `>` where it needed `>=`, so the too-deep
form resolved to a top-level module of the same name and was labelled `QUALIFIED` — the
confident kind of wrong, and the exact failure the guard's own comment said it had fixed. It
changes nothing on any repository measured, because code Python will not import does not exist
in one; it is a wrong answer to a question that can be asked, which is enough.

The last one is LEGB, which is implemented here by hand and is where a bare call gets its
meaning: the nearest enclosing FUNCTION that defines the name, then the module — and a class
body is not in that chain at all, so `helper()` inside a method is the module's function and
not the method sitting beside it. Each case in the fixture returns a different string, so the
interpreter says which definition actually ran and the tool has to have named that one.

Two more go the same way. A `@property` read RUNS code, and which code is a fact about the
class — an override wins, an inherited one is found up the bases — so Python is asked where the
property came from. And `Client()` runs `__new__` and then `__init__`, but only the ones that
exist: a class with just `__new__`, a class with just `__init__`, and a class with neither are
each asked of the interpreter, because an edge to a method that is not there puts a caller on
nothing.

Two checks run over all fourteen. **Every graph invariant holds** — every edge's source and
target a real node, every id under its module, every label consistent with whether the edge
resolved. And **every resolved call's target bears the name that was called**, with three
kinds of exception that all turn out correct: a subclass constructor reaching an inherited
`__init__`, an instance call landing on `__call__`, and `import ... as ...` aliases. No wrong
answers were found, which is the number worth caring about — a confidently wrong answer is
worse than a missing one, and the rate above cannot see them.

A rate has a denominator, and two of the changes below move it, so here are the counts as well
— how many calls are pinned to exactly one definition, and how many the tool answers "I cannot
tell" about. Neither column can be flattered by anything but resolving more.

Read the baselines carefully, because they are not the same one. The `before` above is the
figure that column has carried since the table was first published — the state before any of
this resolution work. The `before` here is the build immediately preceding the most recent
round of it, so this table is that round on its own:

| repo | pinned, before → after | cannot tell, before → after |
|---|---|---|
| flask | 1,045 → **1,471** | 1,112 → **477** |
| ansible | 17,553 → **20,120** | 11,309 → **7,584** |
| pandas | 96,759 → **102,318** | 31,635 → **23,892** |

Eleven rules and six bugs did that, and none of them is clever. A name a package **re-exports** —
`pandas/__init__.py` getting `DataFrame` from `core.api`, which gets it from `core.frame` — is
followed to where the definition actually is. And `import flask` is connected to the module id
`src/flask/__init__`, because a directory that holds packages and is not one is where Python
imports from, which is decidable from the tree. Before that second one, the first fired 38,162
times on pandas and *not once* on flask; nothing about flask was harder, its ids just had a
prefix on them.

The other two carry a type across a boundary it was being dropped at. A module-level
`display = Display()` types `display.warn()` in every function of that file, and a
`from .globals import display` in the next file over keeps that type instead of starting over.
Together those two are 336 more resolved calls on ansible and flask — about a point each. I
had estimated twenty points, by counting the calls that *mention* such a name rather than the
ones a class could actually be found for. The estimate was the thing that was wrong; the rule
does what it says, at a twentieth of the size.

The last two read what a function RETURNS. `def make(): return Client()` was unresolved while
`def make() -> Client:` was not, on the same function — but the annotation was never the
evidence, the body was, and that reading is already trusted one scope down where
`c = Client()` types `c`. Every return has to agree, `return None` excepted, and a generator is
not what it yields. Then `make().go()`, which is `x = make()` and `x.go()` written on one line:
the receiver's name was read as a class and never as a function whose return class was, by
then, already known. 10,657 calls in a clone of pandas are written directly on the result of
another call. Together: 681 more resolved calls on pandas, 101 on ansible, 4 on flask.

The seventh is the largest single one, and it reads pytest. A test function's parameters are
not unknowns — pytest fills them from fixtures, by name, under a scoping rule that is written
down and decidable from the tree: the module's own fixtures, then `conftest.py` in its
directory, then each directory above it, first match winning. In flask, `app` and `client` were
548 unresolved calls between them when this was built — a quarter of everything that could
resolve, and 560 before the rest of this round chipped at them — and both are
three-line fixtures in `tests/conftest.py` whose bodies this tool was already reading. It never
connected the parameter to them, so `blast` on a library function stopped at the library — and
"what breaks if I change this" has the tests in the answer or it has half of it.

Only functions pytest actually calls: a test function in a test file, a `Test*` class's method,
or a fixture, which receives fixtures too. Anything the body rebinds or annotates is left
alone. 297 more calls on flask, 1,073 on pandas.

The eighth is the one that made the others reach: a return class that is another call's return
class. `def client(app): return app.test_client()` — flask's own fixture — states its class two
functions away, and reading one hop stopped short of it. These facts depend on each other, so
they are settled in rounds until a round changes nothing, rather than in one pass in a fixed
order.

The bug was reading order. `df = df.where(df > 0)` names the OLD `df` on the right, typed a
line earlier — and the target was retyped before the value was ever walked, so that call read a
name with no type yet and went unresolved, taking every call after it along. Python evaluates
the right side first. A variable rebound from its own method is how a great deal of dataframe,
query-builder and string-handling code is written: 1,012 unresolved calls on `df` alone in a
clone of pandas, 126 of those now answered and 696 across the whole repository.

The ninth is a class written out in full through a module: `pd.MultiIndex.from_product(...)`.
`Parent.method()` resolved and this never did, though it is the same statement with the class
named properly — and it is how library code is called from outside, which is most calls in most
test suites. `from_tuples`, `from_arrays` and `from_product` alone are written that way 2,046
times in pandas, and 1,417 of those were unresolved. Everything needed was already recorded; nothing had ever asked.

The second bug came out of building that. A parameter called `pkg`, in a file that also imports
`pkg`, had `pkg.mod.func()` resolved into the module — `QUALIFIED`, the highest confidence
there is, on a name that means whatever the caller passed. The guard against exactly this
existed and only ever looked at a ONE-NAME receiver, so every dotted one walked past it.

Those last two were an accident worth keeping: a function calling `.append()` on two different
classes made the pair ambiguous the moment one of them resolved, and the chains written on both
stopped resolving. A chain does not need the scope-wide answer — the call it is written on is
right there, on the same line, already resolved on its own. Reading it from the line recovers
both and answers three more. Where one line genuinely holds two, the scope-wide rule is still
the right one.

The third bug was inheritance through an alias. `from .sansio.blueprints import Blueprint as
SansioBlueprint`, then `class Blueprint(SansioBlueprint)` — flask's own shape. A base is looked
up by name and then checked by name, to stop it matching some other class that answers to the
spelling; the check compared against the name written *here*, and an alias is by definition a
different name from the one the class was defined under. So the lookup found the class and the
check threw it away, and every method Blueprint inherits from Scaffold was invisible. 78 more
calls on flask, 15 on ansible, and one correction: a `super()` that was skipping a level.

Every one of these was checked edge by edge against the build before it, and the whole round
was then checked end to end against the build it started from: **8,558 calls gained across the
three repositories, and 6 lost.** Five of the six are answers the old reading order reached by
using the NEW type on the OLD name — right by luck, and labelled `TYPED` either way — and the
sixth is a `super()` that was reaching two classes up. That gained-minus-lost is 8,552, which
is exactly the change in the count table above; a running total added up from each step said
6,794, and adding up notes is not the same as measuring.

The last two repositories were added after the rest of this was done, to find out what it
still got wrong on code nobody had aimed it at. `rich` came out at 0.869 first time and taught
it nothing. `httpx` came out at **0.353**, and the reason was one line: `httpx/__init__.py` is
twelve lines of `from ._api import *`, and every caller then writes `httpx.get(...)`. The
re-export rule followed a name a package imports by name and had nothing to say about a star,
though the tool already reads a star for a bare call — `from turtle import *` then `home()`.
The same sentence one dot further along had never been asked. What a star carries is the
interpreter's rule and not a guess: `__all__` when the module states one, otherwise the names
it defines that do not begin with an underscore. 1,730 more resolved calls on httpx, none lost
anywhere.

The tenth rule is a class receiver the file imported. `Helper.tag(1)` where `Helper` is defined
in the same file resolved — that is the `CLASS` label — and the identical statement on an
imported one did not, which is the ordinary way a classmethod or a factory gets called. That is
1,035 calls across ansible and pandas, and `AnsibleTagHelper.tag(...)` alone is 81 of them.

The fourth bug was `from . import app`. Every name after `from .` was read as a module of its
own, so a name the package's `__init__.py` built — `app = Flask(__name__)`, and `from . import
app` in the file beside it — was recorded as living in a module that does not exist. Its class
was lost, every `@app.route(...)` next to it went unresolved, and a phantom module went into
the import graph where `deps` could name it. Underneath that, a sub-*package* imported the same
way bound nothing at all, because a package's id ends in `/__init__` and the test for "does
that module exist" never matched one, so every class inheriting through it had lost its base.
The two together are 1,097 more resolved calls — 869 on ansible, 226 on pandas, 2 on flask —
and none lost. They are not split further than that here, because they interact: fixing the
first is what let the second show through, and 16 answers that looked like losses from the
first turned out to be the second one waiting underneath.

**The last column also moved because the denominator did, and that is worth being plain
about.** `UNTYPED` is a claim — the receiver could not be typed, so the target might be yours —
and for `client.get()` in flask's tests it is false: `client` is a `FlaskClient`, `get` is
werkzeug's, and the tool can prove no class it can see carries it. The name-wide version of
this test already ran; this is the same test with better evidence, asking whether THIS class
has the method rather than whether anything does. It resolves nothing new. It moves 200 calls
on flask, 220 on ansible and 1,564 on pandas out of a denominator of winnable calls they were
never winnable in, and out of the "unsure" list `impact` prints — which is this tool sending an
agent to look for something it has already proved is not there. Resolved counts, unchanged by
it: flask 1,471, ansible 20,120, pandas 102,318.

The same is true of one more reading, and for the same reason. `cmd = []` states a type as
plainly as `c = Client()` does, and was read as nothing at all — so `cmd.append('-u')` came back
"could not tell", which is the biggest single source of that answer: in ansible the unresolved
calls are led by `append` (845), `get` (840), `join` (531), `items` (421) and `update` (337),
and the name-wide test that would call them external does not fire, because a repository that
size has its own class with a `get` on it somewhere. A list's `append` is not in anybody's tree.
The interpreter is asked whether the method really is that type's, so `config = 'text'` followed
by `config.dumps(1)` — broken code — is not called a builtin either; and a module that can see
its own `dict` or `list` takes the name back for that module only, because one file defining a
function called `set` should not make every `set()` in the repository doubtful. An annotation
says the same thing — `rows: list`, `rows: list[str]`, `t.List[str]`, or a `-> dict` one call
away — and `list[Client]` is read here as a list, which is the same sentence the CLASS reading
refuses for the opposite and equally correct reason: its contents are not its type.

There used to be one more label. `RESOLVED` meant "a bare call, and exactly one definition of
that name exists somewhere in the tree" — which is a coincidence, not a resolution. By the time
every real way a bare name reaches a definition had a rule of its own, it fired fifteen times
across an entire standard library and not once across 2,725 installed packages, and the fifteen
were wrong: `turtle` builds `up`, `down`, `left` and `right` at import time rather than defining
them, and those calls were being answered with functions in `_pyrepl`. A name this file neither
defines nor imports nor stars in is a library, something injected at runtime, or a mistake — and
saying `EXTERNAL` is the true answer to all three.

## "Nothing calls this" now has to earn it

Most tools will happily hand you a list of dead code. Then you delete something and the app
breaks, because a name can be reached in ways nothing spells out as a call.

This one used to get it wrong too. Pointed at its own source, it named 577 of its 734 functions
as uncalled, and every single one was wrong: 505 unittest methods, 46 fixtures, 22 `visit_*` methods of its own AST walker, a
callback handed to `signal.signal`, one handed to `subprocess`, an alias assigned to four
other names. A number that is wrong every time is not conservative, it is broken — and it was
the headline of `stats`, printed bare, where somebody either deletes live code or stops
believing the tool.

Every name it mentions without calling is now tracked, so the default list is the finding:

```
codegraph unused          → the names nothing calls and nothing names
codegraph unused --all    → all of them, each with the reason it is reached
```

| reason | what it means |
|---|---|
| *(none)* | nothing here calls it and nothing here names it. **This is the finding.** |
| `python calls this` | a dunder. Yours to write, Python's to call. |
| `named, not called` | a dispatch table, a callback argument, an alias — reached, just not written as a call |
| `inherited interface` | its class has a base outside the scanned roots, which may call it: `do_GET`, `generic_visit`, `setUp` |
| `looks dispatched` | the name follows a convention a framework dispatches on — `test` is the prefix **unittest itself** uses, so `testFoo` counts as well as `test_foo` |

Nothing is hidden — `--all` still prints every row. What changed is that the count means
something. On this repo it went from 577 to **0**, which is the true answer.

And the number is still only about **the roots you pointed it at**. A sibling package nobody
scanned calls plenty of these. `stats` names it `not_called_in_scanned_roots` for that reason.

## Before you commit: what did I just touch?

`impact` answers for a name you already know. The question *before* that one is which names you
have touched at all — and the answer is sitting in the diff you have not committed yet.

```bash
git diff | codegraph changed
```

```
app/auth.validate_user  (lines 40-44)
  callers: app/routes.login, app/routes.refresh, app/billing.charge
  blast:   14 function(s) downstream
```

It reads the diff on **stdin** rather than running git. This file starts no processes and
imports nothing outside the standard library — a property people rely on when they copy it into
their own repository, and not worth spending to save a pipe. A pipe also works with `hg`, `jj`,
a saved patch, and a pull request fetched by something else.

Three things come back, and the two that are not the main list are the point:

- **touched** — the functions your edits are inside, each with its callers and blast radius.
- **module level** — changed lines that sit outside every function. Those run on *import* and
  can reach anything in the file, so they are a bigger question, not a smaller one.
- **unknown files** — files this graph never read: brand new, or outside the roots you scanned.
  Answering "nothing depends on this" for a file it never opened is the one reply it must never
  give quietly.

It walks the hunk **bodies**, not just the `@@` headers. `git diff` prints three lines of
context either side by default and the header counts them, so trusting the header reports a
function as changed because a *neighbour* was — the first version of this named `impact` off the
back of an edit three lines away. Blank lines and comments are dropped too: they change no
behaviour, and calling a comment a module-level risk is alarming and untrue.

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
codegraph changed            read a diff on stdin; what those edits would break
codegraph map                what this codebase IS: the modules everything leans on, and
                             where execution starts. The first question in a new repository
codegraph shape <file>       every signature in a file and its line number, no bodies
codegraph mcp                serve all of it to a coding agent over MCP (stdin/stdout)
codegraph unused             definitions nothing calls AND nothing names
codegraph unused --all       ...plus the ones reached some other way, each with why
codegraph stats              counts, resolution rate, definitions nothing reaches
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

An AI editing your code reads it as text and guesses what else it affects. This replaces the
guess with an answer:

```python
import codegraph
g = codegraph.load()
codegraph.impact(g, "spend_cap")   # {"callers": [...], "sites": [...], "blast": [...]}
```

`impact` before an edit is the difference between "I changed a function" and "I changed a
function that five others depend on, here are their line numbers." The output is small, exact,
and cheap — no model call, no network.

### The MCP door an agent knocks on

That is the library. `codegraph mcp` is the same answers over **MCP** — line-delimited
JSON-RPC on stdin and stdout, so the agent asks rather than you. No dependency, no network, no
daemon.

| tool | when |
|---|---|
| `codegraph_repo_map` | first, in a repository you have not seen |
| `codegraph_shape` | instead of reading a file you only need the shape of |
| `codegraph_where`, `codegraph_find` | where is this, what is it called |
| `codegraph_callers`, `codegraph_calls` | who calls this, what does it call |
| `codegraph_blast` | before changing a shared function — transitive callers, not just direct |
| `codegraph_sites` | every call site, as file:line |
| `codegraph_path` | how does A reach B |
| `codegraph_changed` | after editing and **before saying the work is done** — hand it your `git diff` and it finds the names itself |

The handshake carries that same guidance, so a client shows it to the model once rather than
leaving each tool to be rediscovered mid-task.

**The answer to read carefully.** `nothing calls X` is not `X is unreachable`, and the reply
says which — a function reached through a dispatch table or a decorator has no callers this
graph can name, and deleting it still breaks the code.

**Large answers are capped and say so.** `blast` on a widely-used helper in pandas returned
11,200 lines — about 199,000 tokens, which does not fill an agent's context, it ends the
conversation. It comes back as 120 rows, the real count, the files they are in, and a `filter`
glob to narrow. An answer cut off without a word is one an agent reads as complete.

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

The counts are **in the graph file**, not only in `stats`. They used to be computed on
demand and stored nowhere, so the first integration written against `codegraph.json` read
`func_defs` off it, found nothing, and printed a confident zero — a gauge reading zero rather
than the truth. `json.load(open("codegraph.json"))["stats"]` is the whole answer now.

The library refuses exactly what the command line refuses, through the same code:
`codegraph.Ambiguous` when several definitions answer to the name, carrying the candidates, and
`codegraph.Unknown` when this graph has never seen it. Neither is answered with an empty
result, because "nothing depends on this" is the one reply a misspelling must never get.

## What it cannot do

This reads your code without running it, and some things are only decidable while running. Here
is every one of them, spelled out — because knowing where a tool stops is what makes the rest of
it usable:

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
- **A call made by syntax needs a receiver the file can type**, the same as a written one.
  `with open(p) as f` and `with self.lock` name no typed local, so neither is recorded. When
  the class *is* known and does not have the method, the edge is `EXTERNAL` rather than
  "cannot tell" — `for row in rows` on a `list` subclass runs code outside the tree.
- **An augmented assignment picks the method the interpreter would.** `x += y` resolves to
  `__iadd__` if the class has one and to `__add__` if it does not — never to both, since only
  one of them runs.
- **A reflected operator is not followed.** `a + b` records `a.__add__`; Python's fallback to
  `b.__radd__` happens only when the first returns `NotImplemented`, which is a runtime answer.
- **A property read needs a receiver the file can type.** `self.thing` and `c.thing` where `c`
  is annotated or was built here both resolve; a bare `x.thing` on an untyped `x` does not, the
  same way `x.method()` does not. Every typed attribute read is recorded while parsing and
  dropped afterwards unless the name turns out to be a property, since one file cannot know
  what another one defines.
- **A function passed by name is not a call** — `Thread(target=f)`, `sorted(key=f)`,
  `partial(f, 1)`. `f` is referenced, never invoked here. Those references are tracked, so `f`
  is not reported as unreached; it is listed under `unused --all` as *named, not called*.
- **`super()` resolves against the class it is written in.** Python's own order for that
  class - so a diamond lands where the interpreter lands. What static analysis cannot know is
  that `B.m`'s `super()` goes to `C` when `B` is reached through a `D(B, C)` instance.
- **A base class is found the way any other class name is** — defined in this module, or
  imported into it, before any tree-wide search; an alias (`from x import Base as B`) is
  followed, a dotted one (`pkg.base.Case`) is resolved through the module it names, and a base
  the package re-exports (`unittest.TestCase`, which lives in `unittest/case.py`) is followed
  to where it is defined. Method lookup then uses Python's own order, C3 linearisation, so a diamond resolves
  where the interpreter resolves it. What is not followed: a base built by a metaclass, a base
  that is a variable (`V = Generic[T]` then `class C(V)`), and a subscripted one — `Generic[T]`
  names `Generic`, and a subscript is not a name.
- **A mixin's `self` calls belong to whatever it is mixed into, which is not decided here.**
  `class BaseBytesTest:` declares no base and calls `self.assertEqual`; the file later writes
  `class BytesTest(BaseBytesTest, unittest.TestCase)` and `class ByteArrayTest(BaseBytesTest,
  unittest.TestCase)`. At the mixin's own definition there is no base to walk, and the answer
  depends on a combination made elsewhere - possibly more than one. Those calls stay
  unresolved rather than being attributed to a class the mixin never names. Measured: after
  dotted bases were fixed this is what remains of `assertEqual` on the standard library,
  2,527 of the original 15,872, and every one of them is this shape.
- **A re-export is followed to the definition, through however many packages.** `pd.DataFrame`
  reaches `pandas/core/frame.DataFrame` even though `pandas/__init__.py` gets it from
  `pandas.core.api`, which gets it from `core.frame` — labelled `RE-EXPORT`. It fires only when
  the receiver is a module this file imported and the name is one that module explicitly
  re-exports; a name it does not re-export is left unresolved rather than attached to something
  with the right name elsewhere in the tree. It works under a src-layout too: a directory that
  holds packages and is not one itself is where Python imports from, and that is decidable from
  the tree. Two roots offering the same name resolve to neither.
- **The saving footer only speaks where the baseline is real.** `shape --saved` replaces
  opening a file you were going to open and says so; `callers --saved` names the files the
  answers are in instead, because a caller list replaces a *search* and measuring it against
  whole files nobody would have read flatters the tool.
- **A module-level instance types the whole module.** `display = Display()` at the top of a
  file types `display.warning(...)` inside every function in it, which is how almost every
  program keeps a logger, a client or a registry. A local assignment or a parameter of the same
  name shadows it, because a function that rebinds the name is talking about something else.
- **Type inference reads what the source states, and nothing else.** `x = Foo()` and
  `x: Foo`; an attribute the class body assigns; a function's return class, whether annotated
  or plainly returned; a call made on the result of another call; a module-level instance; and
  a test's parameters, from the pytest fixture that name resolves to. A container annotation is
  not its contents, so `Dict[str, Client]` stays a dict. A name rebound to anything the tool
  cannot name loses its type rather than keeping the old one. And `with Foo() as c` is not
  assumed to give you a Foo, because `__enter__` may return anything at all.
- **An unannotated parameter is the blind spot that is left.** `def get_facts(module)` gets its
  value from whoever calls it, and this reads one function at a time. Typing it from what every
  call site passes was measured rather than assumed: on a clone of ansible, the arguments
  written out plainly enough to start that induction type 32 parameters and answer **two**
  calls, so it is not built. The honest answer stays `UNTYPED`, which is what that label is for.
- **A fixture is read only where pytest would fill one in** — a test function in a `test_*.py`,
  a `Test*` class's method, or another fixture. The lookup is pytest's own: the module, then
  `conftest.py` in its directory, then each directory above. A fixture supplied by a plugin is
  not in the tree and cannot be seen, and a parameter the body rebinds is not answered.

Everything it cannot resolve is labelled rather than guessed, so the limits are visible in the
output instead of hidden in it.

## How it compares

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
pipx install jedi-codegraph
```

The distribution is **`jedi-codegraph`**, not `codegraph` — that name belongs to somebody else
on PyPI, and PyPI reads hyphens as if they weren't there. The command it installs is
`codegraph`, and `pip install jedi_codegraph` reaches the same package.

It is still one file with no dependencies, so copying `codegraph.py` out of an installed
package and dropping it into a repository works exactly as well.

**The published version is 0.1.0 and predates the measurements above.** Those describe `main`,
which is a long way ahead of it — `CHANGELOG.md` lists what is in between under *Unreleased*.
Clone the repository, or take `codegraph.py` from it, if you want the numbers this README
states.

Python 3.9+. Tested on Linux, macOS and Windows.

## Proving itself

Passing tests prove nothing on their own — a test that could never fail is decoration. So this
is checked backwards: by breaking the tool on purpose and seeing whether the tests notice.
`tools/mutation.py` breaks the tool one small way at a time — flips a comparison, swaps an
`and` for an `or`, drops a `not`, moves a number by one — and runs the suite against each
change. Every one of them should make something go red, and one that does not is the
interesting output: it names a behaviour nothing is checking.

**The file admits 1,390 mutations.** A full pass killed every one of the 987 the file admitted
then, and the file has grown a long way since — an MCP server, a shape reader, a saving footer,
an incompleteness warning, and the rules and fixes above. Three samples have been drawn from it as it changed: 150, then 300, then 300 again, and every
one of the 750 was killed — one of them by hanging rather than failing, which is counted apart
because "hung" and "failed" are different facts. The most recent is two changes short of this
file, so it is not a result about this one: the number is a fact about this version, the
results are facts about older ones. The pass is re-run whenever the file changes, because a
result about an older version of a file is not a result about this one, and a count that
happens to match is not evidence that it is. A full pass is hours of work, so it is run deliberately
rather than on every push, and the count is checked by a test — it was published as 710 here
and 208 in the changelog while the file admitted 839, because a number written twice and
checked nowhere drifts in two directions. Two of them did not make the suite fail but made
it never finish — flip the comparison that ends a `while` — and those are caught by a timeout
and counted apart, because "hung" and "failed" are different facts.

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

The test suite adds 893 more. Grouped, because a list of every one of them stopped being
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

## Nothing private gets published

Reading a file carefully is not a safeguard. It is a habit, and it works right up until the once
it doesn't. Twice here, something private survived a careful read: a dead project's name survived every
review of the LICENCE, because the reviews were looking for forbidden words and years, and
`--help` still named where the tool came from after a full pass had been called clean. Both
were plain text in files nobody thought to question.

So it is a check that fails, not a habit. `tools/scrub.py` reads every file git tracks — which
is exactly what a push publishes — for secrets, home directories, real email addresses,
machine addresses, dates and assistant attribution. `--history` adds every commit message and
**every version of every file ever committed** — deleting a file does not remove it from the
history, and a scan of the tracked tree alone called this repository clean while two files of
working notes sat in earlier commits.

**The private-word list lives outside the repository** — `~/.config/codegraph/deny.txt`, or
wherever `CODEGRAPH_DENY` points. It was in `tools/` for a while with each word stored as a
sha256, on the reasoning that a hash is not a word. It is not much else either: these are short
dictionary words with no salt, so they come back by *guessing and checking* rather than by
reversing — twelve of them fell to a sixteen-word list in under a second, in a file whose own
comment said it existed to stop exactly that.

Hashing still earns its place: an accidentally shared list is not instantly readable, `--add
WORD` never writes the word down, and a hit reports the position rather than the text, because
a CI log on a public repository is public too. But that is obfuscation. The protection is that
the file is not published.

Fifteen of its sixteen tests **plant** the thing being looked for and prove the scan goes red.
A scrub that has never failed is not evidence that a tree is clean.

A test file that proves a key is caught has to contain a key, so those lines are exempted one
at a time, in the source, and `--quiet`'s companion `--fixtures` lists every one of them — a
whole-file exemption would have meant a real key in a test goes unseen for ever.

**And `--history` runs on every push, not only on a release.** It was wired to the publish
workflow alone, so two private words written into a docstring as *examples of what to keep out*
were caught by the tree scan, removed from the file, and left in every earlier version of it —
where anyone could read them with one command. Nobody noticed until somebody went looking,
weeks of commits later. Fixing a file is not fixing the history, and a check that runs rarely
finds things late.

**It runs before the commit exists, which is the only cheap moment.** The cleanup here was done
twice. Both times the tree had been scrubbed by reading it and the reading passed. The second
one cost a history rewrite, a force-push, and then deleting and recreating the repository —
because a rewrite does not make the host forget: the orphaned objects stay fetchable by id, and
a repository going public serves them to anyone who asks. So `.githooks/pre-commit` scans the
staged tree and `.githooks/commit-msg` scans the message, and both refuse rather than report.

```bash
python3 tools/scrub.py --install-hooks
```

One command rather than a line to remember, because the hooks live in the tree and git runs
them only when local config says so — and *present but not installed* is the shape every guard
here has failed in. A test checks it is actually on, and skips on CI, where nothing is
committed from.

Ten tests cover them, including the control that an ordinary commit still goes through — a
guard that blocks everything gets deleted the first week — and the named escape hatch,
`CODEGRAPH_ALLOW_PRIVATE=1`, because a guard with no way past it gets deleted the first time it
is wrong.

## Licence

MIT.

Built by [@jedisolana](https://x.com/jedisolana).
