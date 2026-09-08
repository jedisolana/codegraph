#!/usr/bin/env python3
"""codegraph - ask a Python codebase what breaks if you change this.

A local, dependency-free code graph built with the standard library's `ast`. No network, no
language server, no model. Nodes are modules, functions, classes and methods; edges are
defines, imports and calls.

Every call edge carries a CONFIDENCE, because the useful part is knowing when the answer is
solid. SELF-METHOD, INHERITED, CLASS, TYPED, QUALIFIED, LOCAL and CONSTRUCTOR each pin a call
to exactly one definition. AMBIGUOUS lists the candidates instead of choosing between them.
BUILTIN and EXTERNAL say the target is not here; UNTYPED says the receiver could not be typed
and the target might be. Nothing is guessed: a blast radius that quietly picked one of two
same-named functions would be worse than no blast radius at all.

  codegraph build [dir...]     build the graph (default: here); writes codegraph.json
  codegraph impact <name>      callers + call sites + blast radius, and what it could not resolve
  codegraph callers <name>     who calls this
  codegraph calls <name>       what this calls
  codegraph blast <name>       transitive callers - what could break if you change it
  codegraph sites <name>       every call site as file:line - all of them
  codegraph where <name>       where a symbol is defined
  codegraph find <substr>      fuzzy symbol search
  codegraph path <from> <to>   a call path connecting two functions
  codegraph deps <module>      a module's in-tree imports and importers
  codegraph cycles             import cycles of any length (refactor smells)
  codegraph symbols            every function and class defined here
  codegraph changed            read a diff on stdin; what those edits would break
  codegraph unused             definitions nothing calls AND nothing names
  codegraph unused --all       ...plus the ones reached some other way, each with why
  codegraph stats              counts, resolution rate, never-called definitions
  codegraph shape ... --saved  add a line saying what the answer replaced: `12 line(s)
                               here, instead of reading 1 file(s) whole (2,140 lines)`.
                               Opt-in, because scripts parse the current output; always on
                               over MCP, where an agent is deciding whether to open them.
  codegraph callers ... --saved  says instead where the answers are - the files and how
                               many. A caller list replaces a SEARCH, and measuring it
                               against whole files nobody would have read flatters the tool.
  codegraph mcp ... changed    the same `changed` question, asked by the agent: hand it a
                               diff and it says what those edits reach. It finds the names,
                               so nothing has to be named first.
  codegraph map                what this codebase IS: the modules everything leans on,
                               ranked by how many others import them, and where execution
                               starts. The first question in a repository you have not seen.
  codegraph shape <file>       every signature in a file and its line number, no bodies -
                               3,600 lines of source as 110 lines of what it offers
  codegraph mcp                serve the graph to a coding agent over MCP (stdin/stdout).
                               Every answer carries a WARNING when a file in the tree could
                               not be parsed, because an incomplete answer that looks
                               complete is the failure this tool exists to prevent.
                               The same answers, asked by the agent instead of by you:
                               callers, calls, blast, sites, where, find, path.
  codegraph --selftest         32 ground-truth checks, several of them red-first
  codegraph --help             this text
  --json                       any query, answered as JSON instead of prose - including
                               the refusals, and including the blast radius by name
  --only PAT / --exclude PAT   keep or drop results by module, glob-matched:
                               `--exclude 'tests/*'` is the one you will want first

Exit codes: 0 answered, 1 the name is unknown here or a search matched nothing, 2 the name
matches several definitions or the command was malformed.
"""
import ast
import builtins
import contextlib
import fnmatch
import glob
import hashlib
import json
import os
import sys
import threading
import time
from collections import Counter, defaultdict

_BUILTINS = set(dir(builtins))                              # next()/len()/sorted()/open()/... are builtins, never a same-named in-tree func (discard edges to built-ins; matters most cross-tree where a unique in-tree name shadows a builtin)

# The graph belongs next to the CODE, not next to this script. It used to be written beside
# codegraph.py, which is fine for one house and wrong for a tool people copy around: analyse
# someone's repo and the output landed in yours. Default to the working directory; both are
# overridable so a build server can put them wherever it likes.
HOME = os.environ.get("CODEGRAPH_ROOT") or os.getcwd()
OUT = os.environ.get("CODEGRAPH_OUT") or os.path.join(HOME, "codegraph.json")
# The cache lives beside the GRAPH, not beside the source. Keying it off HOME meant setting
# CODEGRAPH_OUT redirected the graph and left the cache writing into the directory that was
# unwritable in the first place - so the escape hatch this tool prints when a build fails did
# not actually work. Advice that has not been run is not advice.
CACHE = (os.environ.get("CODEGRAPH_CACHE")
         or os.path.join(os.path.dirname(OUT) or ".", "codegraph.cache.json"))          # per-file parse cache keyed by path + content digest (incremental build)
# The cache is namespaced by codegraph's OWN source hash: edit the parser and every stale parse
# is invalidated, so a change here can never silently reuse yesterday's extraction.
try:
    with open(os.path.abspath(__file__), "rb") as _fh:
        _VERSION = hashlib.sha256(_fh.read()).hexdigest()[:12]
except Exception:
    _VERSION = "0"


# `match` binds names through PATTERNS, not through Name(Store): `case [config]` and
# `case str() as config` each carry the name as a plain string on the pattern node. Built with
# getattr because these node types arrive in 3.10 and this file runs on 3.9.
_MATCH_BINDS = tuple(getattr(ast, n) for n in ("MatchAs", "MatchStar") if hasattr(ast, n))
_MATCH_MAP = getattr(ast, "MatchMapping", ())
# Exact node type -> what it binds. Everything absent from this table binds nothing, which is
# almost every node in a file, and the point is that finding that out costs one dict lookup.
_BINDS = {ast.Name: "name", ast.Import: "import", ast.ImportFrom: "import",
          ast.ExceptHandler: "except", ast.Global: "global", ast.Nonlocal: "global",
          ast.FunctionDef: "def", ast.AsyncFunctionDef: "def", ast.ClassDef: "def",
          ast.ListComp: "comp", ast.SetComp: "comp", ast.DictComp: "comp",
          ast.GeneratorExp: "comp"}
_BINDS.update({t: "match" for t in _MATCH_BINDS})
if _MATCH_MAP:
    _BINDS[_MATCH_MAP] = "matchmap"
_STORES = frozenset({ast.Store, ast.Del})


def _say(path):
    """A path to put in a message, shortened when that is possible and safe.

    `os.path.relpath` RAISES on Windows when the file and the current directory are on
    different drives - `ValueError: path is on mount 'C:', start on mount 'D:'`. Every skip
    message went through it, so on a machine with the code on one drive and the temp directory
    on another, a single unparseable file ended the whole build with a traceback. The one thing
    the skip path exists to prevent, on an entire platform, for two years of Python.
    """
    try:
        rel = os.path.relpath(path)
    except (ValueError, OSError):
        return path
    return rel if len(rel) < len(path) else path


def _bound_names(node):
    """Names this function binds in its OWN scope: parameters, assignments, loop targets,
    `with ... as`, `except ... as`, walruses, imports, and nested defs.

    Python binds for the whole scope, so a name assigned anywhere in a body shadows an outer
    one everywhere in it - which is why this is a pre-scan rather than something tracked as
    the visitor goes. `global` and `nonlocal` take a name back out again.

    Without this, a parameter called `helpers` in a module that also imports `helpers` made
    `helpers.run()` resolve to the imported module's function and label it QUALIFIED - the
    tool's highest confidence, on an answer that is simply wrong.
    """
    names, defined, freed = set(), set(), set()
    a = getattr(node, "args", None)                  # a Module has a body and no parameters
    if a is not None:
        for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg):
            if arg is not None:
                names.add(arg.arg)
    stack = list(node.body)
    while stack:
        n = stack.pop()
        # One dict lookup on the exact type, instead of a chain of isinstance calls that the
        # ninety-five per cent of nodes which bind nothing all had to walk to the bottom of.
        # This ran fifty-five million isinstance calls over a large tree, and was the single
        # most expensive thing in a build.
        kind = _BINDS.get(n.__class__)
        if kind is None:
            for f in n._fields:
                v = getattr(n, f, None)
                if type(v) is list:
                    stack.extend(x for x in v if isinstance(x, ast.AST))
                elif isinstance(v, ast.AST):
                    stack.append(v)
            continue
        if kind == "name":
            # A Name's only child is its ctx, a shared singleton with nothing in it. Names are
            # the commonest node there is, so not pushing that is worth saying out loud.
            if n.ctx.__class__ in _STORES:
                names.add(n.id)
            continue
        if kind == "comp":
            # A comprehension's loop variable is its OWN scope in Python 3 and does not leak
            # out. Collecting it here made `[r for config in rows]` shadow the imported module
            # `config` for the whole enclosing scope - and since the module gained a pre-scan of
            # its own, one such line at the top of a file silenced every config.x() call in it.
            # A walrus INSIDE a comprehension does bind outward, so the rest is still walked.
            for gen in n.generators:
                stack.append(gen.iter); stack.extend(gen.ifs)
            stack.extend(c for c in ast.iter_child_nodes(n)
                         if not isinstance(c, ast.comprehension))
            continue
        if kind == "def":
            defined.add(n.name)                   # the NAME binds here; the body is its own scope
            continue
        if kind == "import":
            # An import binds the name TO THE MODULE, which is the one binding resolution wants
            # to follow rather than be blocked by. Counting it as a shadow meant the deliberate
            # cycle-break - `def f(): import memory; return memory.recall()` - lost every call
            # edge through it, in a tool that recognises that idiom well enough to keep it out
            # of the cycle report.
            pass
        elif kind == "except" and n.name:
            names.add(n.name)
        elif kind == "match" and n.name:
            names.add(n.name)                     # case [config] / case str() as config
        elif kind == "matchmap" and n.rest:
            names.add(n.rest)                     # case {**rest}
        elif kind == "global":
            freed.update(n.names)                 # declared elsewhere; collected and removed last,
        # `ast.iter_child_nodes` is a generator wrapping `iter_fields`, which is another
        # generator with a try/except per field. Five million nodes pay for both. The fields
        # are read directly here; this collects a SET, so the order it sees them in is not
        # something anything depends on.
        for f in n._fields:                       # because the walk order is not source order
            v = getattr(n, f, None)
            if type(v) is list:
                stack.extend(x for x in v if isinstance(x, ast.AST))
            elif isinstance(v, ast.AST):
                stack.append(v)
    # (values, definitions). A receiver is shadowed by either; a BARE call is shadowed only by
    # a value, because a nested `def helper()` really is the helper that a bare helper() means.
    return names - freed, defined - freed


def _mro(cid, bases_of, cache, busy=None):
    """The order Python itself looks a method up in: C3 linearisation.

    A depth-first walk of the bases is right for a chain and wrong for a diamond. With
    `class D(B, C)` where B and C both derive from A, and both A and C define m, depth-first
    reaches A through B and stops - but Python's order is D, B, C, A, so C.m is what actually
    runs. The tool said A.m, labelled INHERITED, which is the confident kind of wrong.

    A cyclic hierarchy is illegal in Python and trivially expressible in a half-written file,
    so `busy` stops the recursion rather than the interpreter stopping it for us. An
    inconsistent hierarchy that C3 cannot linearise yields what has been ordered so far, which
    is better than nothing and honest about being partial.
    """
    if cid in cache:
        return cache[cid]
    busy = busy or set()
    if cid in busy:
        return [cid]
    busy = busy | {cid}
    bases = list(bases_of.get(cid, ()))
    seqs = [list(_mro(b, bases_of, cache, busy)) for b in bases]
    seqs = [s for s in seqs if s] + ([list(bases)] if bases else [])
    out = [cid]
    while seqs:
        for seq in seqs:
            head = seq[0]
            if not any(head in rest[1:] for rest in seqs):
                break
        else:
            break                                  # no good head: inconsistent, stop here
        out.append(head)
        for seq in seqs:
            if seq and seq[0] == head:
                del seq[0]
        seqs = [s for s in seqs if s]
    cache[cid] = out
    return out


class Unparseable(Exception):
    """A .py file the parser could not read. Reported, never swallowed."""


def _defs_and_calls(path, mod):
    """Extract definition nodes and call edges from one file via ast. Returns (defs, edges).
    defs: list of {id, kind, name, module, line}. edges: list of {src, callee, kind, line}."""
    try:
        # utf-8-SIG, because a byte-order mark is not a syntax error. Visual Studio and Notepad
        # write them, Python itself accepts them - and plain utf-8 left the mark as a stray
        # character at the top of the file, so ast.parse threw and the WHOLE FILE silently
        # became an empty module. Every function in it disappeared from every blast radius.
        with open(path, encoding="utf-8-sig", errors="replace") as _fh:   # a context manager, so
            src = _fh.read()                                              # a big tree does not
        tree = ast.parse(src, filename=path)                              # leak a handle per file
    except RecursionError as ex:
        # Which of the two recursion limits a generated file hits - the parser's or the
        # walker's - depends on the platform and the interpreter version, and the person
        # reading the message does not care. One sentence for one situation.
        raise Unparseable(f"{_say(path)}: too deeply nested to analyse "
                          f"(generated code?): {ex}") from ex
    except (SyntaxError, ValueError) as ex:
        # py2, a template, a half-written file. Skipping is right; skipping in SILENCE is not -
        # an empty module in the graph looks exactly like a file with nothing in it.
        raise Unparseable(f"{_say(path)}: could not parse: {ex}") from ex
    except OSError as ex:
        # A file the process cannot READ - restrictive permissions in a vendored directory, a
        # container running as a different user. One such file used to end the whole build on
        # a PermissionError traceback, which is the same mistake a dangling symlink once made:
        # a single awkward file is not a reason to refuse to analyse a codebase.
        raise Unparseable(f"{_say(path)}: could not read: {ex.strerror or ex}") from ex
    defs, edges, imports = [], [], []
    refs = set()          # every name this file MENTIONS without calling: a callback
                          # handed over, a dispatch table, an alias. Not a call, and
                          # not nothing either - see `unused`.
    aliases, fromimp, fromorig = {}, {}, {}                                   # module aliases (name->module) and from-imports (name->module) for import-aware call resolution
    # One name imported from two different modules - the try/except ImportError idiom. A plain
    # dict keeps the LAST binding, which for that idiom is the FALLBACK: the tool named
    # slow.parse as the definite target of parse() while fast.parse, the one that actually runs
    # when the import succeeds, showed no callers at all. Both are recorded, and a name with
    # two possible sources is answered the way every other ambiguity is.
    fromalt = defaultdict(list)
    submodules = {}                                             # name -> the module id it MIGHT be, confirmed in build()

    class V(ast.NodeVisitor):
        def visit(self, node):
            """`ast.NodeVisitor.visit` builds the string "visit_" + the class name and does a
            getattr with a default, for every one of five million nodes. The answer only ever
            depends on the node's TYPE, so it is worth looking up once per type."""
            try:
                fn = self._route[node.__class__]
            except KeyError:
                fn = self._route[node.__class__] = getattr(
                    self, "visit_" + node.__class__.__name__, self.generic_visit)
            return fn(node)

        def generic_visit(self, node):
            """The same fields in the same order as the version in ast, without the two
            generators it goes through to produce them."""
            for f in node._fields:
                v = getattr(node, f, None)
                if type(v) is list:
                    for item in v:
                        if isinstance(item, ast.AST):
                            self.visit(item)
                elif isinstance(v, ast.AST):
                    self.visit(v)

        def __init__(self):
            self._route = {}
            self.atypes = [{}]                                  # per-class attribute->class, so self.db.query() resolves
            self._call_funcs = set()                            # ids of Attribute nodes that ARE a call's func, so the reader below does not count `c.f()` twice
            self.scope = [mod]                                  # qualified-name stack: module -> class -> func
            self.owner = [mod]                                  # nearest ENCLOSING owner a call belongs to (module at bottom, so module-level calls are captured too)
            self.classes = []                                   # enclosing class ids, so self.method() resolves within the right class
            self.vtypes = [{}]                                  # per-scope var->ClassName from `x = Foo(...)`, so x.method() resolves to Foo.method (local type inference)
            # Beside it, and only a FALLBACK: var -> the name of the function that produced it.
            # `x = make()` looks like a constructor to the reading above, so the class name it
            # guesses is tried first and this is used when that names no class in the tree -
            # `make` may DECLARE what it returns, and a declared type is the source saying so.
            self.ctypes = [{}]
            # And the builtin type a name plainly holds: `cmd = []`, `env = {}`, `s = ''`. Same
            # scope rules, same "two answers is no answer" rule.
            self.ltypes = [{}]
            # The MODULE is a scope too, and it was the only one with no pre-scan: every
            # function got one and the file's own top level got an empty set. So
            # `for config in rows:` or `with open(p) as config:` at top level left config
            # looking like the imported module, and config.dumps() resolved into it, labelled
            # QUALIFIED - on a receiver that is a number, or a file.
            # Only VALUES, never the module's own defs: a top-level `class Parent` is the
            # definition Parent.make() is looking for, not a shadow of it.
            self.bound = [(_bound_names(tree)[0], set())]       # per-scope (values, defs) bound locally, which SHADOW an imported module or class of the same name
            self.rets = []                                      # per-function: the class each `return` hands back, so a function with no annotation still says what it returns
            self.yields = []                                    # the same for `yield`, which is what a pytest fixture hands its test
            self.pyparams = []                                  # per-function: parameters pytest itself fills in, so a test's arguments are not unknowns

        def _qual(self, name):
            return ".".join(self.scope + [name])

        def visit_Import(self, node):
            ml = len(self.owner) == 1                            # a top-level import (real dependency) vs one deferred inside a function (the deliberate cycle-break)
            for a in node.names:
                top = a.name.split(".")[0]
                imports.append({"src": mod, "callee": top, "kind": "IMPORT", "line": node.lineno, "module_level": ml})
                if "." in a.name:
                    # `import logging.handlers` imports BOTH. Only the package was recorded, so
                    # `deps logging/handlers` could not name the files that import it - the same
                    # hole `from pkg import sub` had, in the other import statement.
                    imports.append({"src": mod, "callee": a.name.replace(".", "/"),
                                    "kind": "IMPORT", "line": node.lineno, "module_level": ml})
                # `import pkg.mod as m` binds m to pkg/mod - NOT to pkg. Mapping it to the top
                # level meant m.func() looked for func in the package's __init__ and missed the
                # submodule entirely, which is how most package code is written.
                aliases[a.asname] = a.name.replace(".", "/") if a.asname else None
                if not a.asname:
                    aliases[top] = top                          # `import memory` -> the name binds to the top module
                aliases.pop(None, None)

        def visit_ImportFrom(self, node):
            ml = len(self.owner) == 1
            if node.level:
                # A RELATIVE import - the standard idiom inside any package, and previously the
                # tool's blind spot. `from . import x` was dropped on the floor (node.module is
                # None), so x.f() came back EXTERNAL. Worse, `from .x import f` had its dot
                # stripped and matched a TOP-LEVEL module of the same name, resolving the call to
                # the wrong function and labelling it QUALIFIED - a confident wrong answer, which
                # is the one thing this tool exists not to give.
                # Module ids are path-relative ("pkg/user"), so a relative import resolves by
                # walking up from this module's own package: one dot stays in it, each extra dot
                # climbs one more.
                base = mod.split("/")[:-1]
                if node.level - 1 > len(base):
                    # More dots than there is tree to climb. Python calls this "attempted
                    # relative import beyond top-level package" and refuses to import at all.
                    # The excess used to be discarded silently, so `from ... import thing` in
                    # pkg/up.py landed on the TOP-LEVEL thing.py and resolved thing.load() to
                    # it, labelled QUALIFIED. That is the exact wrong answer the relative-import
                    # work removed, still reachable by writing one dot too many.
                    return
                for _ in range(node.level - 1):
                    base = base[:-1]
                if node.module:                                  # from .thing import load / from ..pkg.mod import x
                    target = "/".join([*base, *node.module.split(".")])
                    imports.append({"src": mod, "callee": target, "kind": "IMPORT", "line": node.lineno, "module_level": ml})
                    for a in node.names:
                        _bind(fromimp, fromalt, a.asname or a.name, target)
                        if a.asname: fromorig[a.asname] = a.name
                        # the name may be a SUBMODULE, same as the absolute form - the two
                        # branches have to learn the same things or one of them lags behind
                        submodules[a.asname or a.name] = f"{target}/{a.name}"
                        imports.append({"src": mod, "callee": f"{target}/{a.name}",
                                        "kind": "IMPORT", "line": node.lineno,
                                        "module_level": ml, "maybe": True})
                else:
                    # `from . import thing` - the name may be a submodule, and may just as
                    # easily be something the package's `__init__.py` built. Every name was
                    # read as a module of its own, so `from . import app` beside
                    # `app = Flask(__name__)` recorded the name as living in `pkg/app`, a module
                    # that does not exist: the object's class was lost, every `@app.route(...)`
                    # next to it went unresolved, and a phantom module went into the import
                    # graph where `deps` could name it.
                    #
                    # The absolute branch has always recorded the PACKAGE as the home and the
                    # submodule only as a candidate, kept if a module of that id really exists.
                    # The two branches have to learn the same things or one of them lags behind.
                    home = "/".join(base)
                    for a in node.names:
                        name = a.asname or a.name
                        if home:
                            imports.append({"src": mod, "callee": home, "kind": "IMPORT",
                                            "line": node.lineno, "module_level": ml})
                        _bind(fromimp, fromalt, name, home)   # `from . import thing` also allows a bare thing() if it is a func
                        if a.asname: fromorig[a.asname] = a.name
                        target = "/".join([*base, a.name])
                        submodules[name] = target
                        imports.append({"src": mod, "callee": target, "kind": "IMPORT",
                                        "line": node.lineno, "module_level": ml, "maybe": True})
                return
            if node.module:
                top = node.module.split(".")[0]
                full = node.module.replace(".", "/")
                imports.append({"src": mod, "callee": top, "kind": "IMPORT", "line": node.lineno, "module_level": ml})
                if full != top:                                  # the package AND the module inside
                    imports.append({"src": mod, "callee": full, "kind": "IMPORT", "line": node.lineno, "module_level": ml})
                for a in node.names:
                    # The WHOLE path, not its first segment. `from b.svc import helper` recorded
                    # the module as "b", so the lookup for helper missed b/svc entirely and the
                    # call fell through to a tree-wide name match - which found a second helper
                    # elsewhere and answered AMBIGUOUS, in a file that had said which one it
                    # meant. The relative branch has always recorded the full path.
                    _bind(fromimp, fromalt, a.asname or a.name, full)   # `from memory import recall` -> bare recall() resolves into module memory
                    # `from operator import index as _index`: the module holds `index`, and the
                    # lookup was made with the LOCAL name. It found nothing, fell through to the
                    # tree-wide "one definition of that name" rule, and answered with an
                    # unrelated function in another package.
                    if a.asname: fromorig[a.asname] = a.name
                    # ...but the name might be a SUBMODULE rather than a function, and
                    # `from pkg import mod` then mod.func() is ordinary package code. Recorded
                    # as a candidate; build() keeps it only if that module really exists.
                    submodules[a.asname or a.name] = f"{node.module.replace('.', '/')}/{a.name}"
                    # `from ctypes import util` really does import ctypes.util. Recorded as a
                    # MAYBE, because the name is just as likely to be a function; build() keeps
                    # it only if a module of that id turns out to exist. Without it,
                    # `deps ctypes/util` could not name the files that import it.
                    imports.append({"src": mod, "callee": f"{full}/{a.name}", "kind": "IMPORT",
                                    "line": node.lineno, "module_level": ml, "maybe": True})

        def visit_ClassDef(self, node):
            self._decorators(node)
            qid = self._qual(node.name)
            # Base classes by NAME. Resolved to ids in build(), where the whole tree is known, so
            # `self.method()` can be found on a parent instead of giving up - which is the single
            # most common shape this used to miss.
            # `class Mine(logging.Handler)` is as ordinary as the bare form and was invisible:
            # only Names were collected, so a qualified base produced no parent at all and
            # every method inherited through it went unresolved. The dotted string is what the
            # class-name rule already understands.
            # A base whose name is a local VARIABLE is not a class this can follow.
            # `V = Visitor[List[int]]` then `class IntListVisitor(V)` is ordinary generic code,
            # and V is a value - it was being matched against every class in the tree and given
            # a parent in an unrelated test module. Calls have refused shadowed names since the
            # beginning; bases never asked.
            bases = []
            for b in node.bases:
                name = _annotated_class(b)
                if name and not any(name.split(".")[0] in v for v, _d in self.bound):
                    bases.append(name)
            if node.bases:
                # Writing `class Child(Base)` RUNS `Base.__init_subclass__(cls)`. Nothing at
                # the class statement writes that name, so 45 definitions in the standard
                # library had two callers between them. It is looked up the way `super()` is -
                # on the order AFTER the new class, never on the new class itself - which is
                # exactly what the super_of field already means here.
                edges.append({"src": self.owner[-1], "mod": mod, "callee": "__init_subclass__",
                              "recv": None, "method": True, "kind": "CALL", "syntax": True,
                              "super_of": self._qual(node.name),
                              "line": getattr(node, "lineno", 0)})
            defs.append({"id": qid, "kind": "class", "name": node.name, "module": mod,
                         "line": node.lineno, "bases": bases})
            self.scope.append(node.name); self.classes.append(qid)   # methods walk under this class scope
            # Scanned from the whole class body up front: a method that uses an attribute is
            # often written above the __init__ that assigns it.
            self.atypes.append(_self_attr_types(node))
            # A class body is a scope: `class A: config = 1` then config.dumps() in that body
            # is the attribute, not the imported module, and it used to resolve into the module.
            # A METHOD does not see class attributes, so the frame comes back off around every
            # nested def - which is exactly what Python's own lookup does with class scopes.
            self.bound.append(_bound_names(node))
            for c in node.body:
                if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    hidden = self.bound.pop()
                    self.visit(c)
                    self.bound.append(hidden)
                else:
                    self.visit(c)
            self.bound.pop()
            self.atypes.pop()
            self.classes.pop(); self.scope.pop()

        def _decorators(self, node):
            """A decorator is a call, made where the def sits, not inside it.

            `@register` above a function produced no edge at all, so callers_of("register")
            was empty and a decorator's blast radius was nothing - change it and the tool said
            nothing depended on it. Python decorators are everywhere, so this was a hole the
            size of the language. `@app.route("/x")` was half-visible: the inner route() call
            was never walked either, because decorator_list was simply not visited.
            """
            for d in node.decorator_list:
                self.visit(d)                        # any calls written INSIDE the expression
                if isinstance(d, ast.Name):          # @register -> register(fn)
                    edges.append({"src": self.owner[-1], "mod": mod, "callee": d.id, "recv": None,
                                  "method": False, "recv_root": None, "kind": "CALL",
                                  "line": getattr(d, "lineno", 0)})
                elif isinstance(d, ast.Attribute):   # @mod.thing -> mod.thing(fn)
                    root = d.value
                    while isinstance(root, ast.Attribute): root = root.value
                    edges.append({"src": self.owner[-1], "mod": mod, "callee": d.attr,
                                  "recv": d.value.id if isinstance(d.value, ast.Name) else None,
                                  "method": True,
                                  "recv_root": root.id if isinstance(root, ast.Name) else None,
                                  "kind": "CALL", "line": getattr(d, "lineno", 0)})

        def _signature(self, node):
            """Default values and annotations run where the def SITS, at definition time.

            The same hole decorators were in: _func walked node.body and nothing else, so
            `def f(x=make_default())` recorded no call at all - change make_default and the
            tool said nothing depended on it. Defaults are evaluated once, at import, in the
            enclosing scope; annotations too, unless postponed by `from __future__ import
            annotations`, and counting them costs nothing when they are.
            """
            args = node.args
            for d in [*args.defaults, *[k for k in args.kw_defaults if k is not None]]:
                self.visit(d)
            for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if a is not None and a.annotation is not None:
                    self.visit(a.annotation)
            if node.returns is not None:
                self.visit(node.returns)

        def _func(self, node):
            self._decorators(node)
            self._signature(node)
            qid = self._qual(node.name)
            d = {"id": qid, "kind": "func", "name": node.name, "module": mod, "line": node.lineno}
            if _is_property(node): d["prop"] = True
            # `def make() -> Client:` states the answer the inference was reaching around. Kept
            # as the NAME the annotation wrote; it is resolved later in this module's scope,
            # because that is where the name means something. A container - `list[Client]` - is
            # not its contents, and _annotated_class already refuses those.
            rt = _annotated_class(node.returns) if node.returns is not None else None
            if rt: d["returns"] = rt
            brt = _annotated_builtin(node.returns)
            if brt: d["returns_builtin"] = brt
            defs.append(d)
            self.scope.append(node.name); self.owner.append(qid)
            # A parameter's ANNOTATION is the type, stated outright. `def send(c: Client)` then
            # c.get() was UNTYPED - the tool inferring around an answer the source had written
            # down. *args and **kwargs are skipped: the annotation there describes the ELEMENTS.
            seeded = {}
            lseeded = {}
            for arg in [*getattr(node.args, "posonlyargs", []), *node.args.args,
                        *node.args.kwonlyargs]:
                cls = _annotated_class(arg.annotation)
                if cls: seeded[arg.arg] = cls
                bt = _annotated_builtin(arg.annotation)
                if bt: lseeded[arg.arg] = bt
            # A MODULE-LEVEL BINDING IS VISIBLE IN HERE - that is what module level means. So
            # `display = Display()` at the top of a file types `display.warning(...)` inside
            # every function in it, which is how almost every Python program keeps a logger, a
            # client, a registry or a console. Measured on ansible: 762 unresolved calls on
            # `display` alone, the largest named receiver in the tree, with `display =
            # Display()` sitting at the top of those same files.
            #
            # Names this function BINDS are left out, which is the whole risk: a local
            # assignment or a parameter of the same name is talking about something else, and
            # answering with the module's type there would be a confident wrong answer.
            shadowed = _bound_names(node)
            # `_bound_names` returns (bound, freed) - the second being names a `global` or
            # `nonlocal` hands back to the enclosing scope. Testing membership against the
            # TUPLE instead of the set shadowed nothing at all, which the two controls caught.
            binds = shadowed[0]
            outer = {k: v for k, v in self.vtypes[0].items()
                     if k not in binds and k not in seeded} if self.vtypes else {}
            self.ctypes.append({})
            louter = {k: v for k, v in self.ltypes[0].items()
                      if k not in binds and k not in seeded and k not in lseeded} if self.ltypes else {}
            self.ltypes.append({**louter, **lseeded})
            self.vtypes.append({**outer, **seeded})             # calls inside this body belong to qid; its own var-type scope
            self.bound.append(shadowed)
            self.rets.append([])
            self.yields.append([])
            # A TEST'S PARAMETERS ARE NOT UNKNOWNS. pytest fills them from fixtures, by name,
            # under a scoping rule that is written down and decidable from the tree. In a clone
            # of flask, `app` and `client` were 548 unresolved calls between them - a quarter of
            # everything that could resolve - and both are three-line fixtures in
            # `tests/conftest.py` whose bodies this tool was already reading.
            #
            # Only for functions pytest actually calls: a test function in a test file (and, in
            # a class, only a `Test*` one), or a fixture, which receives fixtures too. An
            # ordinary helper with a parameter of the same name is called by whoever wrote the
            # call, and is none of pytest's business.
            #
            # An annotated parameter is already typed, and a parameter the body REBINDS is not
            # reliably the fixture any more - this tool answers for a whole scope at once, so a
            # name that changes inside it is not answered.
            is_fixture = any(_dec_base(x) == "fixture" for x in node.decorator_list)
            collected = (node.name.startswith("test") and _is_test_module(mod)
                         and (not self.classes
                              or self.classes[-1].rsplit(".", 1)[-1].startswith("Test")))
            pyp = set()
            if is_fixture or collected:
                rebound = _bound_names(ast.Module(body=node.body, type_ignores=[]))[0]
                for arg in [*getattr(node.args, "posonlyargs", []), *node.args.args,
                            *node.args.kwonlyargs]:
                    if (arg.annotation is None and arg.arg not in ("self", "cls")
                            and arg.arg not in rebound):
                        pyp.add(arg.arg)
            self.pyparams.append(pyp)
            for c in node.body: self.visit(c)
            self.pyparams.pop()
            got = self.rets.pop(); gave = self.yields.pop()
            # WHAT THE BODY RETURNS, when there is no annotation to read. `def make(): return
            # Client()` was unresolved and `def make() -> Client:` was not, on the same
            # function - but the annotation was never the evidence, the body was, and this is
            # the reading already trusted one scope down where `c = Client()` types `c`.
            #
            # Every return has to agree. Two classes is not an answer, and one readable return
            # beside one this file cannot name is not either: the caller can get either, so
            # answering with the readable one is wrong on the other path. `return None` is the
            # exception, because a name cannot be called through None - the same reason
            # `Optional[Client]` is read as a Client.
            #
            # Not for a generator: `def rows(): yield Row()` hands back a generator object, and
            # typing the caller's variable as Row would put every loop's calls onto Row. Not
            # for an `async def` either - without a matching read of `await`, its caller holds
            # a coroutine.
            if (not rt and got and not isinstance(node, ast.AsyncFunctionDef)
                    and not _yields(node)):
                seen = {c for c in got if c != _RET_NONE}
                if len(seen) == 1 and None not in seen:
                    d["returns"] = seen.pop()
                # Carried ALONGSIDE, never instead. `return make()` reads as the class name
                # "make" here, exactly as `x = make()` does, and that name usually belongs to a
                # function rather than a class - so what the call HANDS BACK is carried too and
                # read when the class reading turns out to name nothing.
                c = _handoff_call(node)
                if c: d["returns_call"] = c
            if is_fixture:
                # What the fixture HANDS OVER - returned or yielded, since most are written as a
                # generator so teardown can follow the value. Not `returns`: a fixture that
                # yields is a generator function, and calling one directly gives a generator.
                val = rt
                if not val:
                    seen = {c for c in [*got, *gave] if c is not _RET_NONE}
                    if len(seen) == 1 and None not in seen: val = seen.pop()
                if val:
                    d["fixture"] = val
                else:
                    # A FIXTURE THAT HANDS BACK ANOTHER FIXTURE. `def ready(client): ...;
                    # return client` - the value has a class and this function is not where it
                    # is written. The name is carried and followed after every module is read,
                    # because which `client` it means depends on where this file sits.
                    names = set()
                    for h in _own_handoffs(node):
                        v = h.value
                        if v is None or (isinstance(v, ast.Constant) and v.value is None):
                            continue
                        names.add(v.id if isinstance(v, ast.Name) else None)
                    if len(names) == 1:
                        nm = names.pop()
                        if nm in pyp: d["fixture_alias"] = nm
                if not d.get("fixture_alias"):                  # inside `is_fixture`
                    # `def client(app): return app.test_client()` - flask's own shape, and 204
                    # unresolved calls in its own test suite. Settled in the rounds below, once
                    # `app` has a class of its own.
                    c = _handoff_call(node)
                    if c: d["fixture_call"] = c
            self.bound.pop(); self.vtypes.pop(); self.ctypes.pop(); self.ltypes.pop()
            self.owner.pop(); self.scope.pop()

        def visit_Return(self, node):
            if self.rets: self.rets[-1].append(self._returned_class(node.value))
            self.generic_visit(node)

        def visit_Yield(self, node):
            if self.yields: self.yields[-1].append(self._returned_class(node.value))
            self.generic_visit(node)

        def _returned_class(self, v):
            """The class name a returned expression has, `_RET_NONE` for a return that hands
            back nothing, and None for one this file cannot read."""
            if v is None or (isinstance(v, ast.Constant) and v.value is None):
                return _RET_NONE
            cls = _called_class(v)
            if cls: return cls
            if isinstance(v, ast.Name): return self.vtypes[-1].get(v.id)
            return None

        def _comp(self, node):
            """A comprehension shadows INSIDE itself, and nowhere else. The first iterable is
            evaluated in the enclosing scope, which is where its names still mean what they
            meant a line earlier."""
            if node.generators:
                # A list comprehension iterates exactly as a for statement does. Only the
                # statement form was recorded, which is under half of it: the standard library
                # writes 11,571 `for` statements and 3,330 comprehension clauses.
                self._syntax_call(node.generators[0].iter, "__iter__",
                                  getattr(node, "lineno", 0))
                self.visit(node.generators[0].iter)
            names = set()
            for gen in node.generators:
                for sub in ast.walk(gen.target):
                    if isinstance(sub, ast.Name): names.add(sub.id)
            self.bound.append((names, set()))
            self.vtypes.append({k: v for k, v in self.vtypes[-1].items() if k not in names})
            self.ctypes.append({k: v for k, v in self.ctypes[-1].items() if k not in names})
            for i, gen in enumerate(node.generators):
                if i:
                    self._syntax_call(gen.iter, "__iter__", getattr(node, "lineno", 0))
                    self.visit(gen.iter)
                for cond in gen.ifs: self.visit(cond)
            for part in ((node.key, node.value) if isinstance(node, ast.DictComp)
                         else (node.elt,)):
                self.visit(part)
            self.bound.pop(); self.vtypes.pop(); self.ctypes.pop()

        visit_ListComp = visit_SetComp = visit_GeneratorExp = visit_DictComp = _comp

        def visit_Lambda(self, node):
            """A lambda's parameters are a scope, and they were not one at all.
            `lambda config: config.dumps(x)` read config as the imported module and resolved
            the call into it at the tool's highest confidence - on a receiver that is whatever
            the caller passes in."""
            for d in (*node.args.defaults, *[k for k in node.args.kw_defaults if k]):
                self.visit(d)                                    # defaults run in the ENCLOSING scope
            params = {arg.arg for arg in (*node.args.posonlyargs, *node.args.args,
                                          *node.args.kwonlyargs, node.args.vararg,
                                          node.args.kwarg) if arg is not None}
            for sub in ast.walk(node.body):                      # lambda: (c := Foo()) binds c
                if isinstance(sub, ast.NamedExpr) and isinstance(sub.target, ast.Name):
                    params.add(sub.target.id)
            self.bound.append((params, set()))
            self.vtypes.append({k: v for k, v in self.vtypes[-1].items() if k not in params})
            self.ctypes.append({k: v for k, v in self.ctypes[-1].items() if k not in params})
            self.visit(node.body)
            self.bound.pop(); self.vtypes.pop(); self.ctypes.pop()

        def visit_FunctionDef(self, node): self._func(node)
        def visit_AsyncFunctionDef(self, node): self._func(node)

        def _retype(self, name, cls, call=None, lit=None):
            """cls is a class name, or None for "rebound to something I cannot name"."""
            # The same two-answers rule, applied to the fallback as well: a variable assigned
            # from two different functions has no declared type either.
            if name in self.ctypes[-1] and self.ctypes[-1][name] != call:
                self.ctypes[-1][name] = None
            else:
                self.ctypes[-1][name] = call
            if name in self.ltypes[-1] and self.ltypes[-1][name] != lit:
                self.ltypes[-1][name] = None
            else:
                self.ltypes[-1][name] = lit
            if name in self.vtypes[-1] and self.vtypes[-1][name] != cls:
                # `if c: x = Alpha() else: x = Beta()` then x.go() used to pick whichever
                # branch was walked last and label it TYPED - right half the time, and
                # certain both times. Two answers is not a type.
                self.vtypes[-1][name] = None
            else:
                self.vtypes[-1][name] = cls                                       # x = Foo(...) -> x is a Foo (resolved to a class in build())

        def _unpacks(self, node):
            """`a, b = r` iterates r. A plain `a = r` does not."""
            if any(isinstance(tg, (ast.Tuple, ast.List)) for tg in node.targets):
                self._syntax_call(node.value, "__iter__", getattr(node, "lineno", 0))

        def visit_Assign(self, node):
            self._unpacks(node)
            # EVERY target, and every kind of value. `a = b = Client()` bound neither, because
            # the check wanted exactly one target. Worse, `x = Foo()` followed by
            # `x = load_config()` left x a Foo: a rebinding to anything that was not another
            # class call was invisible, so x.go() was answered with Foo.go, confidently. The
            # one value that says nothing is None - a name cannot be called through it, so the
            # code has to rebind before using it, and `x = None` above an `x = Foo()` is how
            # half of Python initialises an optional.
            # THE RIGHT SIDE FIRST, which is the order Python evaluates in. `df = df.where(...)`
            # names the OLD df on the right - typed a line earlier - and the target was retyped
            # before the value was ever walked, so that call read a name with no type yet and
            # went unresolved, taking every call after it with it. A variable rebound from its
            # own method is how a great deal of dataframe, query-builder and string code is
            # written: 1,012 unresolved calls on `df` alone in a clone of pandas, 126 of them
            # answered by reading the two sides in the order the interpreter does.
            self.visit(node.value)
            if not (isinstance(node.value, ast.Constant) and node.value.value is None):
                cls = _called_class(node.value)
                # Carried ALONGSIDE, not instead: `x = make()` is indistinguishable from a
                # constructor here, so the class reading is tried first and this answers when it
                # names nothing.
                call = _returning_call(node.value)
                lit = _literal_type(node.value)
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name): self._retype(tgt.id, cls, call, lit)
                    elif isinstance(tgt, (ast.Tuple, ast.List)):  # a, b = f() - two unknowns
                        for el in tgt.elts:
                            if isinstance(el, ast.Name): self._retype(el.id, None, None)
            for tgt in node.targets:
                self.visit(tgt)

        def visit_AnnAssign(self, node):
            """`c: Client = make()` - the annotation names the class the inference could not
            reach through the call."""
            if node.value is not None:
                self.visit(node.value)                           # the right side first, as above
            if isinstance(node.target, ast.Name):
                cls = _annotated_class(node.annotation)
                if cls is None: cls = _called_class(node.value)
                if cls or node.value is not None: self._retype(node.target.id, cls)
            self.visit(node.target)
            if node.annotation is not None: self.visit(node.annotation)

        def _receiver_type(self, node):
            """What class a receiver expression has, when this file can say so.

            Three shapes and no more: `self`/`cls` inside a class, a local whose class is
            known, and `self.<attribute>` whose class the class body states. Everything that
            resolves a receiver goes through here, because the alternative is what happened -
            written calls learned about instance attributes and `with self.conn`, `len(self.x)`
            and `self.cfg.endpoint` did not, so the same object was typed on one line and not
            the next.
            """
            if isinstance(node, ast.Name):
                if node.id in ("self", "cls") and self.classes:
                    return ("encl_class", self.classes[-1])
                cls = self.vtypes[-1].get(node.id)
                return ("recv_type", cls) if cls else None
            if _is_self_attr(node):
                cls = self.atypes[-1].get(node.attr)
                return ("recv_type", cls) if cls else None
            return None

        def _syntax_call(self, recv_node, name, line, alt=None):
            """Record a call the language makes and the source never writes.

            Same reach as a written method call and no more: `self`/`cls` inside a class, or a
            local whose class is known. `with open(p) as f` and `with self.lock` name no typed
            local, so they are not recorded - the receiver has to be something this file can
            put a class to.
            """
            if not self.owner: return
            got = self._receiver_type(recv_node)
            if not got: return
            edge = {"src": self.owner[-1], "mod": mod, "callee": name,
                    "recv": recv_node.id if isinstance(recv_node, ast.Name) else None,
                    "method": True, "kind": "CALL", "syntax": True, "line": line}
            if alt: edge["alt"] = alt
            edge[got[0]] = got[1]
            edges.append(edge)

        def _with(self, node, enter, exit_):
            for item in node.items:
                self._syntax_call(item.context_expr, enter, getattr(node, "lineno", 0))
                self._syntax_call(item.context_expr, exit_, getattr(node, "lineno", 0))
            self.generic_visit(node)

        def visit_With(self, node): self._with(node, "__enter__", "__exit__")
        def visit_AsyncWith(self, node): self._with(node, "__aenter__", "__aexit__")

        def visit_For(self, node):
            self._syntax_call(node.iter, "__iter__", getattr(node, "lineno", 0))
            self.generic_visit(node)

        def visit_AsyncFor(self, node):
            self._syntax_call(node.iter, "__aiter__", getattr(node, "lineno", 0))
            self.generic_visit(node)

        def visit_FormattedValue(self, node):
            """`f"{x}"` runs `x.__format__('')`, and `!r` and `!s` run `__repr__` and
            `__str__` instead. A class that defines no `__format__` inherits object's, which
            calls `__str__` - the same fallback shape as `__iadd__`, and decided the same way,
            against the class rather than at the line. The standard library writes 6,960 of
            these placeholders, and `__repr__` is its most-defined method of all."""
            name = {114: "__repr__", 115: "__str__"}.get(node.conversion)
            line = getattr(node, "lineno", 0)
            if name: self._syntax_call(node.value, name, line)
            elif node.conversion in (-1, None):
                self._syntax_call(node.value, "__format__", line, alt="__str__")
            self.generic_visit(node)

        def visit_AugAssign(self, node):
            """`x += y` runs `__iadd__` when the class has one and falls back to `__add__`
            when it does not - and which of the two is a fact about the class, not about this
            line. Both names are carried and the one the interpreter would reach is kept once
            every definition is known. In the standard library `__add__` appears in nearly four
            times as many files as `__iadd__`, so the fallback is the common case, not the
            corner."""
            base = _BINOP_METHOD.get(type(node.op))
            if base:
                self._syntax_call(node.target, "__i" + base[2:], getattr(node, "lineno", 0),
                                  alt=base)
            self.generic_visit(node)

        def visit_YieldFrom(self, node):
            self._syntax_call(node.value, "__iter__", getattr(node, "lineno", 0))
            self.generic_visit(node)

        def visit_Starred(self, node):
            """`[*r]` and `f(*r)` iterate r. `a, *b = r` does not - there the star is a
            target being assigned to, and the iteration belongs to the assignment below."""
            if isinstance(node.ctx, ast.Load):
                self._syntax_call(node.value, "__iter__", getattr(node, "lineno", 0))
            self.generic_visit(node)

        def visit_Subscript(self, node):
            """`d[k]`, `d[k] = v` and `del d[k]` are three different methods."""
            name = {ast.Store: "__setitem__", ast.Del: "__delitem__"}.get(
                type(node.ctx), "__getitem__")
            self._syntax_call(node.value, name, getattr(node, "lineno", 0))
            self.generic_visit(node)

        def visit_BinOp(self, node):
            """The LEFT operand's method is the one Python tries first. It falls back to the
            right operand's reflected form (`__radd__`) when the left returns NotImplemented,
            which is a runtime answer and not recorded here."""
            name = _BINOP_METHOD.get(type(node.op))
            if name: self._syntax_call(node.left, name, getattr(node, "lineno", 0))
            self.generic_visit(node)

        def visit_UnaryOp(self, node):
            name = _UNARY_METHOD.get(type(node.op))
            if name: self._syntax_call(node.operand, name, getattr(node, "lineno", 0))
            self.generic_visit(node)

        def visit_Compare(self, node):
            """`a in b` runs b.__contains__, not a's - the receiver is the operand on the
            RIGHT, which is the one place in this table where the order flips."""
            left, line = node.left, getattr(node, "lineno", 0)
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    self._syntax_call(right, "__contains__", line)
                else:
                    name = _COMPARE_METHOD.get(type(op))
                    if name: self._syntax_call(left, name, line)
                left = right
            self.generic_visit(node)

        def visit_Name(self, node):
            """A name that is read but not called: `signal.signal(SIGINT, on_signal)`,
            `CARDS = [_options_card]`, `visit_ListComp = _comp`.

            Python reaches a function this way as surely as by calling it, and the tool had
            no record of it at all - so `unused` called every callback and every table entry
            dead. A call whose target could not be resolved is deliberately NOT a mention:
            that is a different fact, and folding it in here would quietly shorten the one
            list somebody reads to find real dead code.
            """
            if isinstance(node.ctx, ast.Load) and id(node) not in self._call_funcs:
                refs.add(node.id)
            self.generic_visit(node)

        def visit_Attribute(self, node):
            """Reading a property RUNS it, and writes no parentheses doing so.

            Nothing here is an ast.Call, so visit_Call never saw it: every @property in a tree
            reported "callers: (none)", and `impact` on one answered that changing it would
            break nothing - the tool's most dangerous shape of wrong answer, because it is the
            answer somebody deletes code on. A method one line away, on the same annotated
            receiver, resolved perfectly.

            Only a receiver this file can actually type is recorded - `self`/`cls` inside a
            class, or a local whose class is known - which is the same reach the method case
            has. An untyped `x.y` would be a guess, and there are millions of them; a module
            attribute is not a property at all. Edges that turn out not to point at a property
            are dropped once every definition is known.
            """
            if isinstance(node.ctx, ast.Load) and id(node) not in self._call_funcs:
                refs.add(node.attr)          # `obj.handler` in a table reaches `handler`
            if (isinstance(node.ctx, ast.Load) and self.owner
                    and id(node) not in self._call_funcs):
                got = self._receiver_type(node.value)
                if got:
                    edge = {"src": self.owner[-1], "mod": mod, "callee": node.attr,
                            "recv": node.value.id if isinstance(node.value, ast.Name) else None,
                            "method": True, "kind": "CALL", "attr_read": True,
                            "line": getattr(node, "lineno", 0)}
                    edge[got[0]] = got[1]
                    edges.append(edge)
            self.generic_visit(node)

        def visit_Call(self, node):
            fn = node.func
            if isinstance(fn, (ast.Attribute, ast.Name)): self._call_funcs.add(id(fn))
            recv = None; method = False
            recv_root = recv_path = None
            if isinstance(fn, ast.Name): callee = fn.id                          # a BARE call foo() - may be local/imported
            elif isinstance(fn, ast.Attribute):
                callee = fn.attr; method = True                                  # a METHOD call X.attr() - only resolves via a known module or self, else external
                recv = fn.value.id if isinstance(fn.value, ast.Name) else None   # the `memory` in memory.recall(); None when the receiver is open(...)/a[0]/x.y (still a method call, never bare)
                root = fn.value                                  # and the ROOT of a dotted chain:
                while isinstance(root, ast.Attribute): root = root.value   # the `os` in os.path.join()
                recv_root = root.id if isinstance(root, ast.Name) else None
                parts, cur = [], fn.value                        # and the WHOLE chain, so that
                while isinstance(cur, ast.Attribute):            # pkg.mod.func() can find pkg/mod
                    parts.append(cur.attr); cur = cur.value
                if isinstance(cur, ast.Name):
                    parts.append(cur.id)
                    recv_path = "/".join(reversed(parts))
                else:
                    recv_path = None
            else: callee = None
            if callee in ("import_module", "__import__") and node.args:
                # `import_module("curses.textpad")` is an import, written as a call. The name
                # is right there as a constant, and a plugin loader or a test that reaches for
                # a module this way is doing exactly what an import statement does - so `deps`
                # and `cycles` should see it. Only a literal counts: a variable is a runtime
                # decision and nobody can read it from here.
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    target = first.value.replace(".", "/")
                    if target and not target.startswith("/"):
                        imports.append({"src": mod, "callee": target, "kind": "IMPORT",
                                        "line": node.lineno, "module_level": len(self.owner) == 1,
                                        "maybe": True})
            # len(x) IS x.__len__(), and str(x) is x.__str__(). The builtin is written; the
            # method it runs is not, so the definition looked uncalled.
            if (isinstance(fn, ast.Name) and fn.id in _BUILTIN_METHOD and node.args
                    and not any(fn.id in v for v, _ in self.bound)):
                self._syntax_call(node.args[0], _BUILTIN_METHOD[fn.id],
                                  getattr(node, "lineno", 0))
            if callee:                                          # attribute this call to its NEAREST enclosing owner (a def, or the module if top-level)
                # The module is carried, not re-derived. It used to be recovered by splitting
                # the qualified id on its first dot - which is wrong the moment a DIRECTORY has
                # a dot in its name (my.pkg, v1.2, django-3.2): "my.pkg/user.go" read as "my".
                # Import-aware resolution then failed silently for every file in such a folder,
                # and `sites` pointed at "my.py", a file that does not exist.
                edge = {"src": self.owner[-1], "mod": mod, "callee": callee, "recv": recv, "method": method,
                        "recv_root": recv_root if isinstance(fn, ast.Attribute) else None,
                        "recv_path": recv_path,
                        # a receiver bound in any enclosing scope is NOT the imported module
                        # or the class of that name - it is whatever the local name holds
                        "recv_local": bool(recv) and any(recv in v or recv in d
                                                         for v, d in self.bound),
                        # And the same question for the ROOT of a dotted chain. `recv_local` is
                        # about a one-name receiver and is False for every `a.b.c()`, so a
                        # parameter called `pkg` in a file that also imports `pkg` had
                        # `pkg.mod.func()` resolved into the module - QUALIFIED, the highest
                        # confidence there is, on a name that means whatever the caller passed.
                        # An import is not a shadow, which is what makes this readable at all.
                        "recv_root_local": bool(recv_root) and any(recv_root in v or recv_root in d
                                                                   for v, d in self.bound),
                        # a BARE call to a name holding a value is not the module-level function
                        # of that name either. Nested defs are excluded: `def g()` then `g()`
                        # inside the same function really is that g.
                        "callee_local": (not isinstance(fn, ast.Attribute)
                                         and any(callee in v for v, _ in self.bound)),
                        "kind": "CALL", "line": getattr(node, "lineno", 0)}
                if (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Call)
                        and isinstance(fn.value.func, ast.Name) and fn.value.func.id == "super"
                        and self.classes):
                    # super().run() names no target and yet has exactly one: the next class
                    # after this one in the interpreter's own order. It used to land as
                    # UNTYPED, so `impact Base.run` reported no callers for a method every
                    # subclass overrides and then calls - the one shape where the caller
                    # cannot mention the callee by name.
                    edge["super_of"] = self.classes[-1]
                    a0 = fn.value.args[0] if fn.value.args else None
                    if isinstance(a0, ast.Name):                 # super(Base, self).run()
                        edge["super_from"] = a0.id
                if recv in ("self", "cls") and self.classes: edge["encl_class"] = self.classes[-1]   # self.method() -> resolve inside this class
                elif recv and self.vtypes[-1].get(recv):
                    edge["recv_type"] = self.vtypes[-1][recv]    # x.method() where x = Foo() -> Foo.method
                if recv and recv not in ("self", "cls") and self.ctypes[-1].get(recv):
                    # The fallback, carried whether or not a class name was guessed above.
                    # Read only if that guess turns out to name nothing.
                    edge["recv_call"] = self.ctypes[-1][recv]
                elif (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Call)
                      and not edge.get("super_of") and _called_class(fn.value)):
                    # `Leg("stock", 100).payoff(110)` - the class is written at the call site,
                    # in the same expression, and was read only when it went through a variable
                    # first. So `x = Leg(...)` then `x.payoff()` resolved and the one-liner did
                    # not, and `unused` then reported `Leg.payoff` as called by nothing while
                    # three lines called it. Found by running this tool over somebody else's
                    # codebase; the standard library writes the shape 944 times.
                    edge["recv_type"] = _called_class(fn.value)
                    # ...and the same name carried as a FUNCTION, read only if that class
                    # reading turns out to name nothing. `make().go()` and `x = make()` then
                    # `x.go()` are one expression written two ways, and only the two-line form
                    # was ever answered: the one-liner's receiver was read as a class name and
                    # never as a function whose return class is already known. 10,657 calls in
                    # a clone of pandas are written directly on the result of another call.
                    edge["recv_call"] = _returning_call(fn.value)
                elif (isinstance(fn, ast.Attribute) and _is_self_attr(fn.value)
                      and self.atypes[-1].get(fn.value.attr)):
                    # self.db.query() - a collaborator held on the instance, which is how most
                    # object-oriented Python is written and was the one shape named in this
                    # tool's own description of its blind spot.
                    edge["recv_type"] = self.atypes[-1][fn.value.attr]
                if recv and recv not in ("self", "cls") and self.ltypes[-1].get(recv):
                    # The receiver plainly holds a builtin. Carried, not resolved: whether this
                    # tree defines its own class of that name is not knowable from one file.
                    edge["builtin_recv"] = self.ltypes[-1][recv]
                if recv and self.pyparams and recv in self.pyparams[-1]:
                    # Carried, not resolved: WHICH fixture this name means depends on where the
                    # file sits, which is not knowable until every module has been read.
                    #
                    # No guard here against an annotated or rebound receiver, deliberately.
                    # Both are already out of `pyparams`, so a second test for them is a
                    # condition nothing can reach - it reads like a safeguard and is one more
                    # line that no test can ever fail on.
                    edge["fixture_param"] = recv
                if not method and isinstance(self.vtypes[-1].get(callee), str):
                    # c(1) where c is a Client. Calling an INSTANCE runs its __call__, and the
                    # call site never writes that name - the same shape as __init__, and the
                    # same answer it used to give: "callers: (none)" for a method being called
                    # two lines away. Callable classes are ordinary Python: decorators written
                    # as classes, handlers, anything with state and one obvious verb.
                    edge["invoke_type"] = self.vtypes[-1][callee]
                edges.append(edge)
            self.generic_visit(node)                            # recurse into args/keywords (which may hold more calls)

    walker = V()
    try:
        walker.visit(tree)
    except RecursionError as ex:
        # Machine-generated Python nests deeper than the interpreter's own stack: a 30,000-term
        # constant, a giant literal table, a generated parser. ast.parse survives files like
        # that; WALKING the tree does not. One such file ended the entire build on a traceback
        # thousands of frames long - every other file in the tree lost its answers to it, which
        # is the same mistake a dangling symlink and an unreadable file each made once. Named
        # on stderr and left out, like every other file this cannot read.
        raise Unparseable(f"{_say(path)}: too deeply nested to analyse "
                          f"(generated code?): {ex}") from ex
    # One EDGE per (caller, receiver, name) - that is one relationship, and it keeps the graph
    # the right size. But it used to keep only the first line and drop the rest, so a function
    # calling helper() on lines 6, 7 and 8 produced a single site: line 6. `sites` promises
    # "every call site - the exact places to edit", and refactoring from a list that is
    # missing two of three is how you break a codebase with a tool bought to prevent that.
    # The relationship is deduplicated; the places are all kept.
    # The key is EVERY field except the line, and it has to be: it decides which edges are
    # the same relationship, and an edge carries a dozen fields that decide where it resolves.
    # Keyed on (caller, receiver, name) alone, one method holding `super(A, self).run()` and
    # `super(C, self).run()` produced a single edge - two different targets, and the survivor
    # took the other one's line number as one of its own call sites. So the tool lost a caller
    # and invented a site, in the same breath, and said nothing.
    #
    # Listed rather than derived, because sorting a dict per edge costs a fifth of the build
    # on a large tree. The failure mode of a list is forgetting to add to it, and the symptom
    # of that is silence - so a test parses a file exercising every edge shape and fails if any
    # field escapes this tuple.
    seen = {}
    for e in edges:
        # An attribute read is identified by four fields, not eighteen. It can only ever
        # resolve through `encl_class` or `recv_type` - it is emitted for no other receiver -
        # so the rest of the identity tuple is eighteen pointers of nothing, allocated for
        # forty thousand candidates of which nine hundred survive. Measured on the standard
        # library, three runs each: 1019MB peak to 859MB, with `nodes`, `calls` and `imports`
        # byte-identical either way.
        k = (ATTR_IDENTITY_MARK, *(e.get(f) for f in ATTR_IDENTITY)) if e.get("attr_read") \
            else tuple(e.get(f) for f in EDGE_IDENTITY)
        if k in seen:
            seen[k]["lines"].append(e["line"])
        else:
            e["lines"] = [e["line"]]
            seen[k] = e
    for e in seen.values():
        e["lines"] = sorted(set(e["lines"]))
        e["line"] = e["lines"][0]                    # the first, for anything reading one line
    # MODULE-LEVEL INSTANCES, by name. `display = Display()` at the top of a file is a type
    # that other modules import - `from ansible.cli import display` - and it was lost the
    # moment it crossed a file. Carried out of the parse so resolution can use it, the same way
    # aliases and from-imports already are.
    singletons = {k: v for k, v in walker.vtypes[0].items() if v}
    # WHAT A STAR CARRIES OUT OF THIS MODULE. `from .x import *` brings `__all__` when the
    # module states one, and otherwise every name it defines that does not start with an
    # underscore - the interpreter's own rule. Reading it is the difference between following
    # a re-export and guessing at one.
    exported = None
    for n in tree.body:
        tgt = (n.targets[0] if isinstance(n, ast.Assign) and len(n.targets) == 1
               else getattr(n, "target", None) if isinstance(n, ast.AnnAssign) else None)
        if isinstance(tgt, ast.Name) and tgt.id == "__all__" and isinstance(
                getattr(n, "value", None), (ast.List, ast.Tuple)):
            names = [x.value for x in n.value.elts
                     if isinstance(x, ast.Constant) and isinstance(x.value, str)]
            if len(names) == len(n.value.elts):      # a computed entry means we cannot say
                exported = sorted(set(names))
    return (defs, list(seen.values()), imports, aliases, fromimp,
            {k: v for k, v in fromalt.items() if len(v) > 1}, fromorig, submodules,
            sorted(refs), singletons, exported)


# Two call edges are the same relationship only when they would resolve to the same place.
# Everything an edge carries except WHERE IT WAS WRITTEN therefore belongs in its identity:
# keyed on (caller, receiver, name) alone, one method holding `super(A, self).run()` and
# `super(C, self).run()` collapsed into a single edge - the tool lost one caller and handed the
# survivor the other one's line number as a call site, silently.
# What identifies an ATTRIBUTE READ. Kept apart from the tuple below because those edges are
# emitted in bulk and discarded in bulk, and because they reach exactly two resolution paths.
# Guarded by a test: any field an attr_read edge can carry has to appear here.
ATTR_IDENTITY = ("src", "callee", "recv", "encl_class", "recv_type")
ATTR_IDENTITY_MARK = "\0attr"        # so a short key can never collide with a long one

EDGE_IDENTITY = ("src", "mod", "callee", "recv", "method", "kind", "recv_root", "recv_path",
                 "builtin_recv",
                 "recv_local", "recv_root_local", "callee_local", "encl_class", "recv_type",
                 "recv_call", "invoke_type", "super_of", "super_from", "alt", "syntax",
                 "attr_read")

# Python runs these from SYNTAX. Nothing at the call site writes the name, so a call graph
# built from ast.Call nodes cannot see any of them: in the standard library 2,780 definitions
# are reached this way and 98% of them recorded no caller at all.
_BINOP_METHOD = {ast.Add: "__add__", ast.Sub: "__sub__", ast.Mult: "__mul__",
                 ast.Div: "__truediv__", ast.FloorDiv: "__floordiv__", ast.Mod: "__mod__",
                 ast.Pow: "__pow__", ast.LShift: "__lshift__", ast.RShift: "__rshift__",
                 ast.BitAnd: "__and__", ast.BitOr: "__or__", ast.BitXor: "__xor__",
                 ast.MatMult: "__matmul__"}
_UNARY_METHOD = {ast.USub: "__neg__", ast.UAdd: "__pos__", ast.Invert: "__invert__"}
_COMPARE_METHOD = {ast.Eq: "__eq__", ast.NotEq: "__ne__", ast.Lt: "__lt__", ast.LtE: "__le__",
                   ast.Gt: "__gt__", ast.GtE: "__ge__"}
# a builtin whose whole job is to call one method on its argument
_BUILTIN_METHOD = {"len": "__len__", "iter": "__iter__", "next": "__next__", "str": "__str__",
                   "repr": "__repr__", "hash": "__hash__", "bool": "__bool__",
                   "abs": "__abs__", "format": "__format__", "reversed": "__reversed__",
                   "dir": "__dir__", "round": "__round__"}

# `@property`, `@cached_property`, and the `@x.setter/.getter/.deleter` that go with them.
# Anything decorated with one of these is reached by READING an attribute, never by writing
# parentheses, so no ast.Call node exists at the place it runs.
_PROPERTY_DECORATORS = frozenset({"property", "cached_property", "setter", "getter", "deleter"})


def _is_property(node):
    """Is this def reached by attribute access rather than by a call?"""
    for d in node.decorator_list:
        n = d.func if isinstance(d, ast.Call) else d            # @foo(...) as well as @foo
        if isinstance(n, ast.Name) and n.id in _PROPERTY_DECORATORS: return True
        if isinstance(n, ast.Attribute) and n.attr in _PROPERTY_DECORATORS: return True
    return False


def _is_none_annotation(node):
    """`None` in an annotation, written either way."""
    return ((isinstance(node, ast.Constant) and node.value is None)
            or (isinstance(node, ast.Name) and node.id == "None"))


def _union_parts(ann):
    """Flatten `A | B | None` into its operands."""
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        return _union_parts(ann.left) + _union_parts(ann.right)
    return [ann]


def _returning_call(value):
    """`x = make()` -> "make", or None when the value is not a plain call.

    A deferred type: which function `make` names is not decidable in this file, so the NAME is
    carried and the answer looked up after resolution, when the call it belongs to has been
    resolved like any other.
    """
    if not isinstance(value, ast.Call):
        return None
    fn = value.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr                           # svc.make() - the resolver matches the callee
    return None


def _annotated_class(ann):
    """The class an annotation names, when it names one plainly.

    `c: Client` and `c: "Client"` (a forward reference, and what every annotation becomes under
    `from __future__ import annotations`) both say exactly which class this is - the source
    stating the answer the tool was inferring around. A SUBSCRIPT usually does not:
    `Dict[str, Client]` is a dict, and reading Client out of it would resolve d.get() to a
    Client method.

    `Optional[Client]`, `Union[Client, None]` and `Client | None` are the exception, and they
    are not a container: each says "a Client, or nothing at all". A method called on one is a
    Client's method, and there is no second class it could belong to. Reading none of them cost
    more than any other annotation gap - in an installed-packages corpus `X | None` is 414 of
    the annotated parameters against 550 plain ones, so two in five said outright what they
    were and were not listened to.

    A union of two real classes stays unread. `Client | Server` does name a class the call
    could reach, and picking one of them is the guess this tool exists not to make.
    """
    if isinstance(ann, ast.Name):
        return ann.id
    if isinstance(ann, ast.Attribute):
        # The WHOLE dotted path, not one level of it. `pkg.base.Case` read as a single
        # attribute on a single name matched nothing, so a class in a sub-package was not a
        # base at all and its methods were never in the lookup - and a base written this way
        # is ordinary in any package deeper than one level.
        parts, cur = [ann.attr], ann.value
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr); cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))               # svc.Client - a module and a class
        return None                                        # a call, a subscript: not a name
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        text = ann.value.strip()
        if text.isidentifier():
            return text
        try:                                               # c: "Optional[Client]"
            return _annotated_class(ast.parse(text, mode="eval").body)
        except (SyntaxError, ValueError, RecursionError):
            return None
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        real = [p for p in _union_parts(ann) if not _is_none_annotation(p)]
        return _annotated_class(real[0]) if len(real) == 1 else None
    if isinstance(ann, ast.Subscript):
        v = ann.value
        nm = v.id if isinstance(v, ast.Name) else (v.attr if isinstance(v, ast.Attribute) else None)
        if nm == "Optional":
            return _annotated_class(ann.slice)
        if nm == "Union":
            elts = ann.slice.elts if isinstance(ann.slice, ast.Tuple) else [ann.slice]
            real = [p for p in elts if not _is_none_annotation(p)]
            return _annotated_class(real[0]) if len(real) == 1 else None
    return None


def _is_self_attr(node):
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id in ("self", "cls"))


def _self_attr_types(cls_node):
    """attribute name -> the class it holds, for one class body.

    `self.db = Database()` in `__init__` and `self.db.query()` in the method below it is the
    ordinary shape of object-oriented Python, and `self.a.b()` was this tool's own stated blind
    spot: 14,034 such call sites in the standard library, none of them resolvable. The type is
    written down in three places and none was being read - the constructor call, an annotation
    on the assignment, and a bare annotation in the class body.

    Read from the WHOLE class body before any method is visited, because a method that uses an
    attribute may be written above the `__init__` that sets it.

    An attribute assigned two different classes gets none: which one a call reaches depends on
    which branch ran, and answering that with the first one seen is a guess.
    """
    out, dead = {}, set()

    def note(name, cls):
        if name in dead: return
        if name in out and out[name] != cls: dead.add(name); out.pop(name, None)
        else: out[name] = cls

    # Only where a STATEMENT can be. An assignment is a statement, so descending into every
    # expression as well walks most of the file a second time for nothing - it cost a sixth of
    # the build on a large tree.
    BODIES = ("body", "orelse", "finalbody", "handlers", "cases")

    def walk(node):
        for field in BODIES:
            for child in getattr(node, field, None) or ():
                if isinstance(child, ast.ClassDef):
                    continue                               # its `self` is not this one
                if isinstance(child, ast.AnnAssign) and _is_self_attr(child.target):
                    c = _annotated_class(child.annotation) or _called_class(child.value)
                    if c: note(child.target.attr, c)
                elif isinstance(child, ast.Assign):
                    c = _called_class(child.value)
                    if c:
                        for tg in child.targets:
                            if _is_self_attr(tg): note(tg.attr, c)
                walk(child)

    for stmt in cls_node.body:                             # class body: `db: Database`
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            c = _annotated_class(stmt.annotation)
            if c: note(stmt.target.id, c)
    for stmt in cls_node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            walk(stmt)
    return out


_RET_NONE = object()          # a return that hands back nothing: it disqualifies nothing


def _dec_base(dec):
    """The bare name of a decorator, however it was written: `@fixture`, `@pytest.fixture`,
    `@pytest.fixture(scope="module")` and `@pytest_asyncio.fixture` all give "fixture"."""
    while isinstance(dec, ast.Call):
        dec = dec.func
    if isinstance(dec, ast.Attribute):
        return dec.attr
    return dec.id if isinstance(dec, ast.Name) else None


def _is_test_module(mod):
    """pytest's own collection rule: a file named `test_*.py` or `*_test.py`."""
    base = mod.rsplit("/", 1)[-1]
    return base.startswith("test_") or base.endswith("_test")


def _own_handoffs(node):
    """Every `return` and `yield` this function itself makes, skipping any written inside a
    function nested in it - those hand back to their own caller."""
    for c in ast.iter_child_nodes(node):
        if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(c, (ast.Return, ast.Yield, ast.YieldFrom)):
            yield c
        yield from _own_handoffs(c)


def _handoff_call(node):
    """The one call this function hands back, when every `return` and `yield` in it hands back
    a call of the same name. The NAME only: which function it is cannot be decided here, so it
    is looked up after resolution, like `x = make()` already is."""
    names = set()
    for h in _own_handoffs(node):
        v = h.value
        if v is None or (isinstance(v, ast.Constant) and v.value is None):
            continue
        names.add(_returning_call(v) if isinstance(v, ast.Call) else None)
    return names.pop() if len(names) == 1 and None not in names else None


def _yields(node):
    """Does this function body contain a `yield` of its OWN - not one belonging to a function
    written inside it. A generator returns a generator object, so its returns say nothing about
    what the caller holds."""
    for c in ast.iter_child_nodes(node):
        if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(c, (ast.Yield, ast.YieldFrom)) or _yields(c):
            return True
    return False


_LITERAL_NODE = {ast.List: "list", ast.Dict: "dict", ast.Set: "set", ast.Tuple: "tuple",
                 ast.ListComp: "list", ast.DictComp: "dict", ast.SetComp: "set",
                 ast.JoinedStr: "str"}
_BUILTIN_CTOR = frozenset({"list", "dict", "set", "tuple", "str", "int", "float", "bool",
                           "bytes", "frozenset"})


def _is_builtin_method(recv_type, callee, shadowed):
    """Is `callee` really a method of that builtin type.

    The interpreter is asked, not assumed: `config = 'an attribute'` then `config.dumps(1)` has
    a receiver that is plainly a str and a method str does not have - broken code, and answering
    BUILTIN would say the interpreter provides something it does not.

    And not at all if the calling module can SEE something of that name - a class called `dict`
    defined there, a function called `list` imported into it - because `x = list()` is then that
    thing's result and the tool has no business being certain about it. Scoped to the module,
    not the tree: one file defining a function called `set` does not make every `set()` in the
    repository doubtful, and a tree-wide test cost 811 correct answers on ansible for that.

    Only reached for a method call: `builtin_recv` is set in one place, and only when the
    receiver is a single name, which a bare call never has."""
    if not recv_type or recv_type in shadowed:
        return False
    return hasattr(getattr(builtins, recv_type, None), callee)


_TYPING_ALIAS = {"List": "list", "Dict": "dict", "Set": "set", "Tuple": "tuple",
                 "FrozenSet": "frozenset", "Text": "str", "ByteString": "bytes"}


def _annotated_builtin(ann):
    """The builtin type an annotation plainly names. `rows: list` and `rows: list[str]` are both
    a list - unlike `list[Client]`, where the CONTENTS are not the type, which is why the CLASS
    reading refuses a subscript and this one does not. `t.List[str]` is the same sentence in the
    spelling a generation of Python was written in."""
    if ann is None:
        return None
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):   # a forward reference
        try:
            ann = ast.parse(ann.value, mode="eval").body
        except SyntaxError:
            return None
    if isinstance(ann, ast.Subscript):
        ann = ann.value
    name = ann.attr if isinstance(ann, ast.Attribute) else (ann.id if isinstance(ann, ast.Name) else None)
    if not name:
        return None
    name = _TYPING_ALIAS.get(name, name)
    return name if name in _BUILTIN_CTOR else None


def _literal_type(value):
    """The builtin type a right-hand side plainly is. `cmd = []` says list as plainly as
    `c = Client()` says Client, and was read as nothing at all - so `cmd.append(...)` came back
    UNTYPED, which claims the target might be in your tree. In a clone of ansible the unresolved
    calls are led by append, get, join, items and update.

    Only what the source writes out: a display, a constant, a comprehension, or a call to a
    builtin that can only return its own type. Nothing inferred from a name."""
    if value is None:
        return None
    t = _LITERAL_NODE.get(value.__class__)
    if t:
        return t
    if isinstance(value, ast.Constant):
        n = type(value.value).__name__
        return n if n in ("str", "int", "float", "bool", "bytes") else None
    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id in _BUILTIN_CTOR):
        return value.func.id
    return None


def _called_class(value):
    """The class name a `x = ...` right-hand side constructs, as written.

    `Client()` gives "Client" and `svc.Client()` gives "svc.Client" - the qualified form is how
    package code constructs things (`import svc` then `svc.Client()`), and it used to give no
    type at all, so the very next line's c.get() was a blind spot in the most ordinary shape
    Python has.
    """
    if not isinstance(value, ast.Call):
        return None
    fn = value.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
        return f"{fn.value.id}.{fn.attr}"
    return None


def _bind(table, alts, name, target):
    """Record `from X import name`, remembering a second, different X for the same name."""
    if name in table and table[name] != target:
        alts.setdefault(name, [table[name]])
    if name in alts and target not in alts[name]:
        alts[name].append(target)
    table[name] = target


def _root_labels(dirs):
    """A SHORT but unique label for each root, used to prefix module ids in a multi-tree build.

    One trailing segment is enough almost always, and catastrophic when it is not: two roots
    ending in the same name collapse into one namespace. Widen the label until the set is
    unique, and fall back to the full path if even that is not enough.
    """
    parts = [d.rstrip(os.sep).split(os.sep) for d in dirs]
    for n in range(1, max((len(p) for p in parts), default=1) + 1):
        labels = ["/".join([x for x in p[-n:] if x]) for p in parts]
        if len(set(labels)) == len(labels) and all(labels):
            return dict(zip(dirs, labels))
    return {d: d.strip(os.sep).replace(os.sep, "/") for d in dirs}


class BadPath(Exception):
    """A path that cannot be analysed. Raised rather than quietly producing an empty graph."""


def build(dirs=None, write=True):
    dirs = dirs or [HOME]
    # A typo'd path used to build an empty graph and exit 0 - success, zero modules, and no
    # hint that the answer to every later query would be "(none)".
    checked = []
    for d in dirs:
        d = os.path.abspath(os.path.expanduser(d))
        if os.path.isdir(d):
            checked.append(d)
        elif os.path.isfile(d) and d.endswith(".py"):
            # A call graph of one file is nearly useless, so the file's DIRECTORY is what gets
            # analysed. That is a bigger scope than was asked for - on a monorepo, much bigger -
            # and it used to happen in silence, with the comment here claiming a one-file tree.
            here = os.path.dirname(d) or "."
            print(f"note: {os.path.basename(d)} is a file - building its directory {here}",
                  file=sys.stderr)
            checked.append(here)
        elif os.path.exists(d):
            raise BadPath(f"{d} is not a directory or a .py file")
        else:
            raise BadPath(f"{d} does not exist")
    # `build . .` indexed everything twice, and `build . ./sub` counted the nested file under
    # two different ids - both silently inflating the graph and double-counting in stats.
    # Shortest path first, then drop anything already inside a kept root.
    unique = []
    for d in sorted(dict.fromkeys(checked), key=len):
        if not any(d == u or d.startswith(u + os.sep) for u in unique):
            unique.append(d)
    dirs = unique
    multiroot = len(dirs) > 1                                   # several trees at once -> qualify module ids by root so same-named modules (api/models vs worker/models) don't collide
    # The root label has to be UNIQUE, not merely short. It was the last path segment, so
    # `build /a/proj /b/proj` gave both trees the label "proj" and every file in them the same
    # id - two unrelated codebases silently merged into one module, with functions from both
    # hanging off it. Take as many trailing segments as it takes to tell the roots apart.
    labels = _root_labels(dirs) if multiroot else {}
    pairs = []                                                  # (path, rootdir, rootname) - RECURSIVE + noise-pruned; module ids are path-relative so nested same-named files don't collide either
    # One FILE, however many paths reach it. A symlink to a file inside the same tree used to
    # index it twice - two modules with identical function names - which made every call to
    # them AMBIGUOUS and emptied the blast radius of everything in the linked file. Nothing is
    # lost by indexing once: it is the same content, reachable under the first path walked.
    seen_files = {}                                             # realpath -> the entry we keep
    for d in dirs:
        root = labels.get(d) or os.path.basename(d.rstrip(os.sep)) or "root"
        for dp, dns, fns in os.walk(d):
            dns[:] = [x for x in dns if not _prune_dir(dp, x)]   # one pruned walk: skip noise, hidden, embedded interpreters, venv roots
            for fn in sorted(fns):
                if not fn.endswith(".py"): continue
                full = os.path.join(dp, fn)
                try:
                    real = os.path.realpath(full)
                except OSError:
                    real = full
                if real in seen_files:
                    # A second path to a file already indexed. Keep the REAL one: naming the
                    # module after the symlink instead made `callers_of(real.shared)` empty,
                    # which is the same silent gap in a nicer disguise.
                    #
                    # Ask whether THIS path is a link - do not compare abspath to realpath. On
                    # macOS a temp dir sits under /var, which is itself a link to /private/var,
                    # so the two never match and the test would silently never fire. Comparing
                    # a resolved path against an unresolved one is the trap; this sidesteps it.
                    if not os.path.islink(full):
                        seen_files[real] = (full, d, root)
                    continue
                seen_files[real] = (full, d, root)
    pairs = list(seen_files.values())                           # walk order, one entry per file
    cache = {}                                                  # INCREMENTAL: reuse a file's parse when its bytes are unchanged (single-tree only; multiroot ids differ by mode so it parses fresh)
    if write and not multiroot:
        try:
            with open(CACHE, encoding="utf-8") as _fh:
                _c = json.load(_fh)
            cache = _c.get("files", {}) if _c.get("_v") == _VERSION else {}   # discard the whole cache if codegraph's code changed (versioned namespace)
        except Exception: cache = {}
    nodes, calls, imports, unreadable = [], [], [], []
    mentioned = set()            # every name the tree names without calling; see `unused`
    stamps = {}                                                  # path -> [size, digest], the graph's own record of what it read
    mod_alias, mod_from, mod_root, mod_sub, mod_alt = {}, {}, {}, {}, {}
    mod_single = {}                                            # module -> {name: class name} for `x = Foo()` at top level
    mod_orig = {}                                                # local alias -> the name the module actually defines
    mod_all = {}                                                 # module -> its __all__, when it states one
    newcache = {}
    for path, d, root in pairs:
        rel = os.path.relpath(path, d)[:-3].replace(os.sep, "/")   # 'memory' for a top-level file, 'sub/mod' for a nested one (no .py) - path-relative id, no nested collision
        base = os.path.basename(rel)
        mid = f"{root}/{rel}" if multiroot else rel             # SINGLE flat tree: bare 'memory' (unchanged). Nested: 'sub/mod'. Multi-tree: 'root/...'
        nodes.append({"id": mid, "kind": "module", "name": base, "module": mid, "line": 0,
                      "file": path})            # the REAL file. A module id is not a path: in a
                                                # multi-root build it starts with a synthetic
                                                # label, and `sites` was printing places that do
                                                # not exist - or, worse, that do and are wrong.
        try:
            os.stat(path)
        except OSError:
            # A DANGLING SYMLINK - a link left behind after a move, which every real repo
            # eventually has. os.walk lists it as a file and stat() then raises, which used to
            # kill the whole build with a traceback. One broken link is not a reason to refuse
            # to analyse a codebase; skip it and carry on.
            nodes.pop()                                          # drop the module node just added
            continue
        try:
            stamp = _stamp(path)                       # size + a digest of the bytes; see _stamp
        except OSError:
            # THERE, and unreadable - a mode-000 file, a permissions mistake, a file held
            # open elsewhere. Not the same as a dangling symlink, which is not a file at all.
            # This one gets named in `unreadable` by the parser below, so it must not be
            # dropped here; it just cannot be stamped, and so cannot be cached either.
            stamp = None
        else:
            stamps[path] = stamp
        # POP, not get: this file's old parse is finished with the moment it is copied, and
        # the copy goes into newcache. Holding both tables whole is where a build peaks - the
        # entries are released one at a time now, so the old table shrinks as the new one grows
        # instead of the two standing at full size together.
        c = cache.pop(path, None)
        if c and stamp is not None and c.get("stamp") == stamp:                       # unchanged file -> reuse cached parse (deep-copied so resolution can't leak back into the cache)
            # A shallow copy PER EDGE, not a deep one. Resolution writes dst/confidence/
            # candidates onto these dicts, and those writes must not reach the cache that is
            # about to be written back - but every value it touches is a scalar, and the only
            # nested value an edge carries is its list of line numbers, which nothing mutates
            # after parsing. copy.deepcopy walked six million objects to protect against a
            # write that does not happen; on a large tree that was a third of a warm build.
            d, e, im, al, fi = (c["defs"], [dict(x) for x in c["calls"]],
                                c["imports"], c["aliases"], c["fromimp"])
            sub = c.get("submodules", {}); alt = c.get("fromalt", {})
            orig = c.get("fromorig", {}); mentions = c.get("refs", [])
            singles = c.get("singletons", {}); exported = c.get("exported")
        else:
            try:
                d, e, im, al, fi, alt, orig, sub, mentions, singles, exported = \
                    _defs_and_calls(path, mid)
            except Unparseable as ex:
                unreadable.append(str(ex))
                nodes.pop()                                      # not a module we can describe
                continue
        if not multiroot and stamp is not None:
            newcache[path] = {"stamp": stamp, "defs": d, "calls": [dict(x) for x in e], "imports": im,
                              "aliases": al, "fromimp": fi, "fromalt": alt,
                              "fromorig": orig, "submodules": sub, "refs": mentions,
                              "singletons": singles, "exported": exported}
        nodes += d; calls += e; imports += im; mentioned.update(mentions)
        mod_alias[mid] = al; mod_from[mid] = fi; mod_root[mid] = root; mod_sub[mid] = sub
        mod_alt[mid] = alt; mod_orig[mid] = orig; mod_single[mid] = singles
        if exported is not None: mod_all[mid] = exported
    # The cache read off disk has done its job: every file has either been reused from it or
    # reparsed, and the answers are in `newcache` now. Holding it through resolution and two
    # JSON writes is a second copy of the whole tree's parse for nothing - on a large one that
    # is hundreds of megabytes still resident at the moment of peak use.
    # Ninety-eight per cent of the attribute reads recorded during parsing are ordinary field
    # access - `self.count`, `self._buf` - and were recorded at all only because whether a name
    # belongs to a property is not knowable while one file is being read. It is knowable now,
    # and the cheap half of the answer is the NAME: an attribute that nothing in the tree
    # defines as a property is not one, and does not need an MRO walk to say so. This runs
    # before the resolution pass rather than after it, which is the difference between carrying
    # forty thousand dead edges through the expensive half of the build and carrying hundreds.
    prop_names = {n["name"] for n in nodes if n.get("prop")}
    calls = [e for e in calls if not e.get("attr_read") or e["callee"] in prop_names]
    cache = None
    if multiroot:                                              # remap import aliases to the SAME-ROOT module id, so within-tree imports resolve to the right tree
        allmods = {n["id"] for n in nodes if n["kind"] == "module"}   # the ids known at this point
        qual = lambda r, b: (f"{r}/{b}" if f"{r}/{b}" in allmods else b)
        for mid in list(mod_alias):
            r = mod_root[mid]
            # a relative import already resolved to a full id (it was built from this module's
            # own id, root included), so qual() must leave it alone - it does, because
            # "<root>/<already-rooted-id>" is never a real module.
            mod_alias[mid] = {k: qual(r, v) for k, v in mod_alias[mid].items()}
            mod_from[mid] = {k: qual(r, v) for k, v in mod_from[mid].items()}
            mod_alt[mid] = {k: [qual(r, x) for x in v] for k, v in mod_alt[mid].items()}
    allmods = {n["id"] for n in nodes if n["kind"] == "module"}
    # A package is a directory, and its module id is `pkg/__init__` - so an import that names
    # `pkg` has to be pointed at the file that actually holds its code. Without this,
    # `from pkg import helper` resolved to nothing at all, because there is no module `pkg`.
    # SOURCE ROOTS. A directory that holds packages and is not one itself - no `__init__.py` of
    # its own - is what Python treats as a place to import from, and `src` is the convention
    # with a name rather than a rule. Without this, `import flask` never met the module id
    # `src/flask/__init__`, so every rule beginning "the receiver is a module this file
    # imported" fired not at all under that layout: 38,162 re-export resolutions on pandas,
    # which is flat, and none on flask, which is not.
    roots = set()
    for mid in allmods:
        if not mid.endswith("/__init__"):
            continue
        parent = mid[: -len("/__init__")].rpartition("/")[0]
        if parent and f"{parent}/__init__" not in allmods:
            roots.add(parent)

    def _real_module(name):
        # The PACKAGE first. Python's own finder looks for pkg/__init__.py before pkg.py in the
        # same directory, so when a repo holds both, `import thing` is the package - and this
        # used to answer with the file.
        pkg = f"{name}/__init__"
        if pkg in allmods:
            return pkg
        if name in allmods:
            return name
        # Then under each source root. AMBIGUITY IS REFUSED: two roots both offering `thing` -
        # a vendored copy is a real thing to find - resolve to neither, which is what this tool
        # does everywhere else it cannot tell.
        under = [f"{r}/{name}/__init__" for r in roots if f"{r}/{name}/__init__" in allmods]
        under += [f"{r}/{name}" for r in roots if f"{r}/{name}" in allmods]
        return under[0] if len(under) == 1 else name

    for table in (mod_alias, mod_from):
        for mid in table:
            table[mid] = {k: _real_module(v) for k, v in table[mid].items()}
    # `from pkg import direct` where `pkg/direct` is a MODULE binds a module, not a name -
    # which is what Python does, and what makes `direct.direct_thing()` a qualified call
    # rather than an untyped receiver. It was recorded only as a name imported from `pkg`, so
    # every call through it was unresolved.
    for mid, names in mod_from.items():
        for name, target in names.items():
            base = target[: -len("/__init__")] if target.endswith("/__init__") else target
            sub = f"{base}/{name}"
            if sub in allmods and name not in mod_alias.get(mid, {}):
                mod_alias.setdefault(mid, {})[name] = sub
    imports = [i for i in imports if not i.get("maybe") or i["callee"] in allmods]
    for i in imports:
        i.pop("maybe", None)
        # `import sysconfig` records "sysconfig"; the module's id is "sysconfig/__init__". The
        # resolver normalised that and the IMPORT TABLE did not, so the edge pointed at a name
        # no node has - and `deps sysconfig/__init__` answered "importers: (none)" for a package
        # half the tree imports, while `cycles` could not see a loop that runs through any
        # package's __init__.
        i["callee"] = _real_module(i["callee"])
    for mid in mod_alt:
        mod_alt[mid] = {k: [_real_module(x) for x in v] for k, v in mod_alt[mid].items()}
    # `from pkg import mod` where pkg/mod is a real module: the name is a MODULE alias, so
    # mod.func() resolves. Confirmed here, where the whole module list is finally known.
    for mid, cand in mod_sub.items():
        for name, target in cand.items():
            # A PACKAGE is a submodule too, and its id ends in `/__init__` - so this test for
            # "does that module exist" never matched one. `from .. import _profiles`, where
            # `_profiles` is a package, bound nothing at all, and every class inheriting through
            # it lost its base. Ansible writes that shape 869 times.
            real = _real_module(target)
            if real in allmods:
                mod_alias.setdefault(mid, {}).setdefault(name, real)
    # RE-EXPORTS. `pkg/__init__` does `from .thing import load`, and another module does
    # `from pkg import load`. The name is not defined in pkg/__init__ at all, so the import
    # pointed at a module that does not have it. It happened to resolve anyway whenever the
    # name was unique in the tree - which is luck, and stops being luck the moment two modules
    # define it. Follow the chain to where the name actually lives, with a hop limit because a
    # circular re-export is expressible even if it would not import.
    defined_in = {(n["module"], n["name"]) for n in nodes if n["kind"] in ("func", "class")}
    # Follow it under the name the OTHER module knows it by, not the one written here.
    # `from pkg import load as l` re-exported through a package walked the chain looking for
    # `l`, which no module along it has ever heard of, so the hop failed on the first step and
    # the call came back EXTERNAL. Aliasing an import is 3.7% of the from-imports in the
    # standard library, and the unaliased form worked, so the two spellings of one import gave
    # different answers - and the failing one failed silently.
    for mid in mod_from:
        for name, target in list(mod_from[mid].items()):
            real = mod_orig.get(mid, {}).get(name, name)
            hops, seen_hops = 0, set()
            while (target, real) not in defined_in and target in mod_from and hops < 8:
                nxt = mod_from[target].get(real)
                if not nxt or nxt in seen_hops: break
                # each module along the chain may have renamed it again on the way through
                seen_hops.add(nxt)
                real = mod_orig.get(target, {}).get(real, real)
                target = nxt; hops += 1
            mod_from[mid][name] = target
            # And under the name the DEFINING module uses, which is not necessarily the one
            # this module imported: every hop is free to rename it again. Following the chain
            # to the right module and then asking it for the wrong name resolves to nothing,
            # which looks exactly like a call to something outside the tree.
            if real != name: mod_orig.setdefault(mid, {})[name] = real
    by_name = defaultdict(list); by_modname = {}; def_ids = set()
    # A bare name written in one scope can only reach a definition at MODULE level. A class
    # defined inside a function is not visible outside it - `def holder(): class Inner: ...`
    # then Inner() in another function is a NameError - and yet the tree-wide fallback offered
    # it, resolving the call and then typing the variable from it: two confident wrong answers
    # from a name Python would refuse to look up at all.
    public = defaultdict(list)
    cls_ids = defaultdict(dict)                                  # module -> {ClassName: class id}
    for n in nodes:
        if n["kind"] in ("func", "class"):
            by_name[n["name"]].append(n["id"]); def_ids.add(n["id"])
            if n["id"] == n["module"] + "." + n["name"]:         # MODULE-LEVEL only. A (module, name)
                by_modname[(n["module"], n["name"])] = n["id"]   # dict cannot hold two `inner`s or a
                public[n["name"]].append(n["id"])                # method and a function sharing a name;
                if n["kind"] == "class":                         # whichever parsed last silently won.
                    cls_ids[n["module"]][n["name"]] = n["id"]
    kind_of = {n["id"]: n["kind"] for n in nodes}                # for walking a call's scope chain

    def _star_sources(mod):
        """The modules a `from ... import *` in `mod` brings names from."""
        if "*" not in mod_from.get(mod, {}):
            return ()
        return mod_alt.get(mod, {}).get("*") or [mod_from[mod]["*"]]

    def _star_carries(mod, name):
        """Would `import *` from `mod` bring `name` out. `__all__` decides when the module
        states one; otherwise every name that does not begin with an underscore."""
        declared = mod_all.get(mod)
        return name in declared if declared is not None else not name.startswith("_")

    def _classes_named(rt, srcmod, scope=None):
        """Which class a type name means HERE: the one this scope defines, then the one this
        module defines or imported, before any tree-wide search. A qualified `svc.Client` says
        outright where to look."""
        if scope:
            # A class defined INSIDE the function doing the calling. Round thirty-three stopped
            # OTHER functions reaching a nested class and never taught the owning one that it
            # could - so pdfminer, which defines `class Parser` inside main() and builds one two
            # lines later, had that variable typed as Pillow's Parser. Class bodies are skipped
            # climbing out, the same way a bare name skips them.
            s_ = scope
            while s_ != srcmod and "." in s_:
                if kind_of.get(s_) != "class":
                    cand = s_ + "." + rt
                    if kind_of.get(cand) == "class":
                        return [cand]
                s_ = s_.rsplit(".", 1)[0]
        if "." in rt:
            who, cname = rt.rsplit(".", 1)
            where_ = mod_alias.get(srcmod, {}).get(who) or mod_sub.get(srcmod, {}).get(who)
            if where_ is None and "." in who:
                # `import pkg.base` binds only `pkg`; `pkg.base` is an attribute of it, so the
                # dotted module was in no table and `class B(pkg.base.Case)` had no base at
                # all. Map the ROOT through the aliases and treat the rest as the path under
                # it, which is what the interpreter does.
                root, rest = who.split(".", 1)
                top = mod_alias.get(srcmod, {}).get(root) or mod_sub.get(srcmod, {}).get(root)
                if top:
                    top = top[: -len("/__init__")] if top.endswith("/__init__") else top
                    cand = f"{top}/{rest.replace('.', '/')}"
                    where_ = cand if cand in allmods else _real_module(cand)
            cid = by_modname.get((where_, cname)) if where_ else None
            if cid is None and where_:
                # The name is not defined in that module, but the module may re-export it -
                # `unittest/__init__.py` does `from .case import TestCase`, and TestCase lives
                # in unittest/case.py. Every `self.assertEqual` in every test class in the
                # standard library went unresolved on exactly this: 15,872 edges, led by the
                # single most common class statement in Python. The chain was already followed
                # for `from pkg import Case`; it was never consulted for `import pkg` then
                # `pkg.Case`.
                onward = mod_from.get(where_, {}).get(cname)
                if onward:
                    real_name = mod_orig.get(where_, {}).get(cname, cname)
                    cid = by_modname.get((onward, real_name))
                else:
                    # ...or the package re-exports it with a STAR, which is how a great many
                    # of them do it: `pkg/__init__.py` is a column of `from .x import *`, and
                    # `pkg.Thing()` then names a class the file itself does not define.
                    star = [by_modname[(m, cname)] for m in _star_sources(where_)
                            if (m, cname) in by_modname and _star_carries(m, cname)]
                    if len(star) == 1: cid = star[0]
            if cid and kind_of.get(cid) == "class":
                return [cid]
            # `Outer.Inner()`: the part before the dot is a CLASS in this module, not a module.
            # Only the module reading was tried, so a variable built from a nested class got no
            # type at all and every method call on it after that went unresolved - while the
            # construction itself resolved perfectly, which is what made it look fine.
            outer = cls_ids.get(srcmod, {}).get(who)
            nested = f"{outer}.{cname}" if outer else None
            return [nested] if nested and kind_of.get(nested) == "class" else []
        real = mod_orig.get(srcmod, {}).get(rt, rt)              # from svc import Client as C
        here = cls_ids.get(srcmod, {}).get(rt)
        if here:
            return [here]
        imported = (by_modname.get((mod_from.get(srcmod, {}).get(rt), real))
                    or by_modname.get((mod_sub.get(srcmod, {}).get(rt), real)))
        if imported:
            # WHATEVER THE OTHER MODULE HOLDS UNDER THAT NAME - and it was taken without ever
            # asking whether it is a class. `x = connect()` reads "connect" as a class name,
            # the way every call on the right of an assignment is read; when `connect` is a
            # function with something nested in it, `app.connect.get` is a real id, so
            # `x.get()` resolved to a nested function and was labelled TYPED. The highest
            # confidence this tool has, on an object that is what the function returned.
            #
            # The importing module says outright what the name is. If it is not a class it is
            # not a class, and looking for one of that name elsewhere in the tree would be a
            # guess of exactly the kind this refuses everywhere else.
            return [imported] if kind_of.get(imported) == "class" else []
        if rt in mod_from.get(srcmod, ()) or "*" in mod_from.get(srcmod, ()):
            return []                    # imported from somewhere named, and not found there:
                                         # the same rule bare calls follow. `from io import
                                         # BytesIO` was answered with _pyio.BytesIO - the
                                         # pure-Python twin, not the class that actually runs.
        return [i for i in public.get(rt, []) if kind_of.get(i) == "class"]

    bases_of = {}                                                # class id -> base class ids
    for n in nodes:
        if n["kind"] == "class" and n.get("bases"):
            resolved = []
            for b in n["bases"]:
                # The SAME rule every other class name goes through. A base used to be matched
                # by name across the whole tree with no test of whether this file had ever
                # heard of it - so pandas' `class _BytesTarFile(io.BytesIO)` was given pip's
                # vendored msgpack BytesIO as a parent, and `class CMakeExtension(Extension)`
                # inherited from cryptography's x509 Extension. Both wrong, both confident, and
                # both then feeding the method lookup, super() and the constructor edges.
                want = b.rsplit(".", 1)[-1]              # `core.Handler` is named Handler
                # ...and an ALIAS is a different name from the one the class was defined under.
                # `from .sansio.blueprints import Blueprint as SansioBlueprint`, then
                # `class Blueprint(SansioBlueprint)` - flask's own shape - found the class and
                # then threw it away, because "Blueprint" is not "SansioBlueprint". Every method
                # that Blueprint gets from Scaffold was invisible, so `bp.route()` resolved to
                # nothing on a receiver typed perfectly. The import already says which class the
                # alias means; this only has to accept the name it means it BY.
                real = mod_orig.get(n["module"], {}).get(want, want).rsplit(".", 1)[-1]
                hits = [c for c in _classes_named(b, n["module"], n["id"].rsplit(".", 1)[0])
                        if c.split(".")[-1] in (want, real)]
                if len(hits) == 1: resolved.append(hits[0])
            bases_of[n["id"]] = resolved
    imported_in = {m: set(mod_alias.get(m, ())) | set(mod_from.get(m, ())) | set(mod_sub.get(m, ()))
                   for m in set(mod_alias) | set(mod_from) | set(mod_sub)}   # names each module actually imported
    mro_cache = {}                                                # linearisation is reused across edges
    # Anything a module can SEE under a builtin's name - defined there, or imported into it -
    # takes that name back for that module, and the tool stops being certain there.
    defined_here = defaultdict(set)
    for n in nodes:
        if n["kind"] in ("class", "func"):
            defined_here[n["module"]].add(n["id"].rsplit(".", 1)[-1])
    for e in calls:                                               # resolve each call to a SPECIFIC definition, import-aware (highest confidence first)
        srcmod = e.get("mod") or e["src"].split(".")[0]; recv = e.get("recv"); callee = e["callee"]; method = e.get("method"); dst = None; conf = None
        # DID ANYTHING PUT A CLASS TO THE RECEIVER? Not the same question as whether a target
        # was found. `self.render()` in a class that inherits from a library base has a known
        # receiver and no findable method, and calling that UNTYPED - "could not be typed, so
        # the target might be yours" - is a claim the tool can disprove. It is what `impact`
        # prints as "unsure", which is the tool telling an agent to go and look.
        knew_class = False
        ec = e.get("encl_class")
        if ec and (ec + "." + callee) in def_ids:                # self.method()/cls.method() -> the method in the ENCLOSING class (exact scope)
            dst = ec + "." + callee; conf = "SELF-METHOD"
        elif ec:
            knew_class = True
            # INHERITED: self.method() where the method lives on a base class. Looked up in
            # Python's own order - C3, not depth-first - because those differ exactly where a
            # diamond makes the answer interesting.
            for b in _mro(ec, bases_of, mro_cache)[1:]:          # [0] is ec itself, handled above
                if (b + "." + callee) in def_ids:
                    dst = b + "." + callee; conf = "INHERITED"; break
        if not dst and e.get("super_of"):
            # The same C3 order used for INHERITED, entered one place further along: super()
            # deliberately skips the class it is written in. An explicit super(Base, self)
            # starts after Base instead, when Base is a class this graph can see.
            chain = _mro(e["super_of"], bases_of, mro_cache)
            start = cls_ids.get(srcmod, {}).get(e.get("super_from")) or e["super_of"]
            try: i = chain.index(start)
            except ValueError: i = 0
            for b in chain[i + 1:]:
                if (b + "." + callee) in def_ids:
                    dst = b + "." + callee; conf = "INHERITED"; break
        if (not dst and method and recv and not e.get("recv_local")
                and recv in cls_ids.get(srcmod, {})):
            # ClassName.method() written out in full - a classmethod call, or an explicit
            # unbound call. The receiver is a class in this module, not an unknown object.
            cand = cls_ids[srcmod][recv] + "." + callee
            if cand in def_ids: dst = cand; conf = "CLASS"
        if (not dst and method and recv and not e.get("recv_local")
                and recv not in cls_ids.get(srcmod, {})
                and recv in imported_in.get(srcmod, ())):
            # ...and the same statement on a class this file IMPORTED, which is the ordinary
            # way a classmethod or a factory gets called. `AnsibleTagHelper.tag(...)` alone is
            # 81 unresolved calls in a clone of ansible. The lookup that says which class an
            # imported name means is the one annotations already use; it answers for a real
            # class and nothing else, so an imported FUNCTION of that name gets no answer here.
            #
            # The name has to be one this file imported and not one it rebinds. A bare name
            # matched across the tree would be the guess this refuses everywhere else.
            cands = _classes_named(recv, srcmod, e["src"])
            knew_class = knew_class or len(cands) == 1
            if len(cands) == 1:
                for b in _mro(cands[0], bases_of, mro_cache):
                    if (b + "." + callee) in def_ids:
                        dst = b + "." + callee; conf = "CLASS"; break
        if (not dst and method and e.get("recv_path") and "/" in e["recv_path"]
                and not e.get("recv_root_local")
                and e.get("recv_root") in imported_in.get(srcmod, ())):
            # A CLASS NAMED IN FULL, THROUGH A MODULE. `pd.MultiIndex.from_product(...)` - the
            # receiver is a module this file imported and then a class that module holds or
            # re-exports. `Parent.method()` resolved and this never did, though it is the same
            # statement with the class written out properly, and it is how library code is
            # called from outside: `from_tuples`, `from_arrays` and `from_product` alone are
            # 1,417 calls written this way in a clone of pandas, every one unresolved.
            #
            # Everything needed was already here. The chain is on the edge, and turning
            # `pkg.Frame` into the class it means - through a package's re-export, or a dotted
            # module path - is the lookup class annotations already use, which answers only for
            # a real class and only when exactly one answers to the name.
            #
            # The chain has to START with a name this file imported. `recv_local` cannot say
            # so here - it is about `recv`, which is None for any dotted receiver - so a
            # parameter called `pkg` would have been read as the module `pkg`, which is the
            # confidently-wrong answer this tool has been bitten by before.
            #
            # The `"/" in recv_path` test above is a cheap pre-filter, not a safeguard: a
            # one-name receiver names no class here anyway. It keeps a lookup off a hot path.
            cands = _classes_named(e["recv_path"].replace("/", "."), srcmod, e["src"])
            knew_class = knew_class or len(cands) == 1
            for c in cands:
                for b in _mro(c, bases_of, mro_cache):
                    if (b + "." + callee) in def_ids:
                        dst = b + "." + callee; conf = "CLASS"; break
                if dst: break
        it = e.get("invoke_type")
        if not dst and it:
            hits = []
            cands = _classes_named(it, srcmod, e["src"])
            for c in cands:
                for b in _mro(c, bases_of, mro_cache):           # inherited __call__ counts,
                    if (b + ".__call__") in def_ids:             # the same way an inherited
                        hits.append(b + ".__call__"); break      # __init__ does
            if len(hits) == 1: dst = hits[0]; conf = "TYPED"
            elif len(hits) > 1: conf = "AMBIGUOUS"; e["candidates"] = sorted(hits)
        rt = e.get("recv_type")
        if not dst and rt:                                       # x.method() where `x = Foo()` (local type inference) -> Foo.method
            # WHICH class called Foo? The one this module can see - defined here, or imported
            # here - before any tree-wide search. It used to take whichever id sorted first,
            # with no ambiguity test at all: a file writing `from b.svc import Client` had
            # c.get() resolved into a/svc.Client, one line after the Client() call itself was
            # labelled AMBIGUOUS. The tool contradicted itself inside a single function.
            # Through the MRO, the way `Client()` already finds an inherited __init__ and
            # `c(1)` an inherited __call__. Only the exact class was looked at here, so
            # `x = Sub(); x.method()` found nothing whenever the method lived on the parent -
            # which in ordinary class hierarchies is most of them.
            hits = []
            cands = _classes_named(rt, srcmod, e["src"])
            # Exactly one, deliberately: several classes answering to the name means the tool
            # DECLINED to type the receiver, which is not evidence about where the method is.
            knew_class = knew_class or len(cands) == 1
            for c in cands:
                for b in _mro(c, bases_of, mro_cache):
                    if (b + "." + callee) in def_ids:
                        hits.append(b + "." + callee); break
            if len(hits) == 1: dst = hits[0]; conf = "TYPED"
            elif len(hits) > 1:
                # Several classes answer to that name and nothing here says which. Same rule
                # as a bare call: list them, pick none.
                conf = "AMBIGUOUS"; e["candidates"] = sorted(set(hits))
        if (not dst and method and recv and not e.get("recv_local")
                and recv in mod_alias.get(srcmod, {})):        # memory.recall() where `memory` is an imported module -> resolve to memory.recall
            target_mod = mod_alias[srcmod][recv]
            dst = by_modname.get((target_mod, callee)); conf = "QUALIFIED" if dst else conf
            if not dst:
                # A NAME THE PACKAGE RE-EXPORTS. `flask.abort(404)` reaches a function
                # `__init__.py` does not define - it re-exports it, `from .helpers import abort
                # as abort` - which is the shape of nearly every Python library API. Twenty
                # calls in a clone of flask, all unresolved, with everything needed to resolve
                # them already computed and already carried here.
                #
                # As careful as the rule it extends: the receiver has to be a module this file
                # imported, and the name has to be one that module EXPLICITLY re-exports. A
                # name it does not re-export stays unresolved rather than being attached to
                # something with the right name elsewhere in the tree.
                came_from = mod_from.get(target_mod, {}).get(callee)
                if came_from:
                    dst = by_modname.get((came_from, callee))
                    conf = "RE-EXPORT" if dst else conf
                elif "*" in mod_from.get(target_mod, {}):
                    # A PACKAGE THAT RE-EXPORTS WITH A STAR. `httpx/__init__.py` is twelve lines
                    # of `from ._api import *`, and every caller then writes `httpx.get(...)`:
                    # 744 unresolved calls in a clone of httpx, which is most of what its tests
                    # do. The rule above follows a name a package imports BY NAME and had
                    # nothing to say about a star - though the tool already reads a star for a
                    # BARE call, `from turtle import *` then `home()`. The same sentence one dot
                    # further along was never asked.
                    #
                    # What a star carries is the interpreter's rule, not a guess: `__all__` when
                    # the source states one, and otherwise the names it defines that do not
                    # begin with an underscore. Two sources offering the name answer neither.
                    hits = [by_modname[(m, callee)]
                            for m in _star_sources(target_mod)
                            if (m, callee) in by_modname and _star_carries(m, callee)]
                    if len(hits) == 1: dst = hits[0]; conf = "RE-EXPORT"
                    elif len(hits) > 1: conf = "AMBIGUOUS"; e["candidates"] = sorted(set(hits))
        if (not dst and method and recv and not e.get("recv_local")
                and recv in mod_from.get(srcmod, {})):
            # AN IMPORTED SINGLETON. `from .globals import display` then `display.warn()`.
            # The type was known - one module up, where `display = Display()` is written out
            # in full - and thrown away at the import. Local inference already typed the same
            # variable in the module that built it; every OTHER file using it got nothing.
            # In a clone of flask that is 422 calls, a fifth of everything it calls.
            #
            # The name has to be one the home module really binds to an instance, and the
            # class is looked up in THAT module's scope, not the caller's - `display` means
            # whatever Display meant where the object was made.
            home = mod_from[srcmod][recv]
            cname = mod_single.get(home, {}).get(mod_orig.get(srcmod, {}).get(recv, recv))
            if cname:
                hits = []
                for c in _classes_named(cname, home):
                    for b in _mro(c, bases_of, mro_cache):
                        if (b + "." + callee) in def_ids:
                            hits.append(b + "." + callee); break
                if len(hits) == 1: dst = hits[0]; conf = "TYPED"
                elif len(hits) > 1: conf = "AMBIGUOUS"; e["candidates"] = sorted(set(hits))
        if (not dst and method and not e.get("recv_local")
                and not e.get("recv_root_local")
                and e.get("recv_path") in allmods
                and e.get("recv_root") in imported_in.get(srcmod, ())):
            # pkg.mod.func() where pkg/mod is ours - but ONLY if this module imported the name
            # the chain starts with. Without that test, any dotted call whose receiver happened
            # to spell an in-tree module id resolved into that module: a file holding
            # `config = C()` and calling config.load() was recorded as calling load() in a
            # config.py it never imported, labelled QUALIFIED. In Python a name you did not
            # import is not that module - it is whatever the name holds, or a NameError.
            dst = by_modname.get((e["recv_path"], callee)); conf = "QUALIFIED" if dst else conf
        if not dst and not method and e.get("callee_local"):
            conf = "UNTYPED"                                   # a local name holding something
        if not dst and not method and not conf:
            # LEGB, the order Python itself uses: a bare call sees its own scope first, then
            # each ENCLOSING FUNCTION, then the module. Two functions each defining a helper
            # called `inner` - or `wrapper`, in any two decorators - are different functions,
            # and the (module, name) lookup below could only hold one of them. outer() was
            # recorded as calling other()'s inner, labelled LOCAL, and the blast radius of
            # anything inner touched stopped one hop short of the truth.
            # Class bodies are skipped climbing out: a bare name inside a method does not see
            # its sibling methods, which is why `helper()` in a method is a NameError.
            scope = e["src"]
            while scope != srcmod and "." in scope:
                if kind_of.get(scope) != "class":
                    cand = scope + "." + callee
                    if cand in def_ids:
                        dst = cand; conf = "LOCAL"; break
                scope = scope.rsplit(".", 1)[0]
        if (not dst and not method and not conf and callee not in mod_from.get(srcmod, {})
                and "*" in mod_from.get(srcmod, {})):
            # `from turtle import *` and then a bare home(). The star was bound as a name
            # spelled "*", which nothing ever calls, so the name fell through to the tree-wide
            # "one definition of that name" rule - which across the standard library answered
            # turtledemo's home() with a function in _pyrepl. The star names a module; ask it.
            # ...and only the names the star actually carries: `__all__` when the source
            # states one, otherwise the names that do not begin with an underscore. This path
            # asked only "does that module define it", so a name `__all__` leaves out was
            # answered with a function that is not in the importing module at all. One rule,
            # both here and for the qualified form.
            hits = [by_modname[(m, callee)] for m in _star_sources(srcmod)
                    if (m, callee) in by_modname and _star_carries(m, callee)]
            if len(hits) == 1: dst = hits[0]; conf = "QUALIFIED"
            elif len(hits) > 1: conf = "AMBIGUOUS"; e["candidates"] = sorted(hits)
        if not dst and not method and not conf and callee in mod_from.get(srcmod, {}):     # BARE recall() under `from memory import recall`
            real = mod_orig.get(srcmod, {}).get(callee, callee)      # `as` renames it here only
            alts = mod_alt.get(srcmod, {}).get(callee)
            hits = ([by_modname[(m, real)] for m in alts if (m, real) in by_modname]
                    if alts else [])
            if len(hits) > 1:
                # Imported from two places under one name. Which one runs depends on the
                # machine, so neither is the answer - the candidates are.
                conf = "AMBIGUOUS"; e["candidates"] = sorted(hits)
            else:
                dst = by_modname.get((mod_from[srcmod][callee], real)); conf = "QUALIFIED" if dst else conf
        if not dst and not method and not conf and by_modname.get((srcmod, callee)):       # a BARE call to a function in the SAME module
            dst = by_modname[(srcmod, callee)]; conf = "LOCAL"
        if not dst and not method and not conf and callee in mod_from.get(srcmod, {}):
            # The file said where this name came from and the module does not appear to define
            # it - an out-of-tree module, a C accelerator behind a pure-Python twin, a re-export
            # this could not follow. Whatever the reason, the answer is not "some other module
            # in the tree that happens to have one function of that name": that rule used to
            # fire here and answer `from time import sleep` with asyncio/tasks.sleep.
            conf = "EXTERNAL"
        if not dst and not method and not conf and "*" in mod_from.get(srcmod, {}):
            # A file that star-imports has named where its loose names come from. turtledemo
            # does `from turtle import *` and calls up(), mode(), mainloop() - none of which
            # turtle defines statically, because it builds them at import time - and the
            # tree-wide rule answered with _pyrepl.commands.up and statistics.mode. A star is a
            # statement about provenance even when the tool cannot see through it.
            conf = "EXTERNAL"
        if not dst and not method and not conf:                  # a BARE call
            # Nothing left to try. The name is not defined in this scope or any enclosing one,
            # not imported, not starred in, and not a builtin - so in Python it is a library,
            # something a framework injected, or a NameError. It used to be matched across the
            # whole tree instead: "exactly one definition of that name exists somewhere" is a
            # coincidence, not a resolution, and by the time every real case had a rule of its
            # own it fired FIFTEEN times over an entire standard library and zero times over
            # 2,725 installed packages - turtle's up(), down(), left() and right(), which turtle
            # builds at import time and does not define, answered with _pyrepl.commands.
            conf = "BUILTIN" if callee in _BUILTINS else "EXTERNAL"
        if not dst and conf is None:
            rr = e.get("recv_root")
            if rr and mod_alias.get(srcmod, {}).get(rr) not in (None, *allmods):
                # os.path.join(), json.load() - the chain is rooted in a module you imported
                # that is not one of ours. Knowably external, not a blind spot.
                conf = "EXTERNAL"
            else:
                # A METHOD call whose receiver could not be typed: d.get(), x.run(), self.a.b().
                # This is the tool's real blind spot, and it is NOT the same as external - the
                # target might well be in your tree. Naming it separately is what makes the
                # resolution rate mean something: it is the denominator of what was winnable.
                #
                # Unless the receiver plainly holds a builtin. `cmd = []` then `cmd.append(...)`
                # runs list.append, which is not in anybody's tree - the one exception being a
                # repository that defines its own class of that name, where the tool has no
                # business being certain.
                conf = "BUILTIN" if _is_builtin_method(
                    e.get("builtin_recv"), callee,
                    defined_here[srcmod] | imported_in.get(srcmod, set())) else "UNTYPED"
        e["dst"] = dst; e["confidence"] = conf
        # Only for a METHOD call, where the name written IS the target's name - the same limit
        # the name-wide rule keeps. `p = Plain(); p()` is a bare call on a typed local whose
        # class has no `__call__`: broken code, not a library's method, and nothing about where
        # a target lives.
        #
        # Every branch above that types a receiver is a method call already, so no test can
        # tell this condition from its absence today. It is kept because it states the rule: a
        # branch added later that types the receiver of a BARE call would otherwise start
        # relabelling silently, and a wrong EXTERNAL is a claim the tool cannot take back.
        if not dst and knew_class and method: e["typed_miss"] = True
    # UNTYPED is a claim: the receiver could not be typed, so the target MIGHT be in this tree.
    # For a call like `rows.append(x)` or `text.strip()` that claim is false and the tool can
    # prove it - no definition anywhere in the tree carries that name, so the target is not
    # here, and EXTERNAL is what it is. On one real codebase nine in ten "cannot tell" edges
    # were `.get()`, `.items()`, `.join()` and `.assertEqual()`, none of which the tree defines.
    # This is not the resolution rate being flattered by dropping hard cases from the sum; it
    # is impossible cases leaving a denominator that was never theirs to be in.
    # Only for a METHOD call, where the name written IS the target's name. For a bare call on
    # a local - `handler = pick(); handler()` - the name is the variable's, and says nothing
    # about what it holds, which may very well be in this tree.
    for e in calls:
        if (e["confidence"] == "UNTYPED" and e.get("method")
                and e["callee"] not in by_name):
            e["confidence"] = "EXTERNAL"
    # `x += y` runs `__iadd__` when the class defines one and falls back to `__add__` when it
    # does not. Which name the interpreter reaches is a fact about the class, and the class is
    # only settled once every file has been read - so both names travelled this far and the
    # reachable one is chosen here, before anything gets labelled external for missing a method
    # it was never going to have.
    for e in calls:
        if not e.get("alt") or e.get("dst"): continue
        want, ec = e["alt"], e.get("encl_class")
        if ec: cands = [ec]
        elif e.get("recv_type"): cands = _classes_named(e["recv_type"], e["mod"], e["src"])
        else: continue
        hits = []
        for c in cands:
            for b in _mro(c, bases_of, mro_cache):
                if (b + "." + want) in def_ids:
                    hits.append(b + "." + want); break
        if len(hits) == 1:
            e["dst"], e["callee"] = hits[0], want
            e["confidence"] = ("SELF-METHOD" if ec and hits[0] == ec + "." + want
                               else "INHERITED" if ec else "TYPED")

    # A call the language makes is only recorded when the receiver's class is known, so an
    # unresolved one is not the usual "could not tell what this is". The class was named and
    # its inheritance walked; the method is not on it. `for row in rows` where `rows` subclasses
    # `list` runs list.__iter__, which is outside the tree - EXTERNAL, exactly what it means.
    # Left as UNTYPED these would claim the target MIGHT be here and drag the resolution rate
    # down with cases that were never winnable.
    for e in calls:
        if e.get("syntax") and e["confidence"] == "UNTYPED":
            e["confidence"] = "EXTERNAL"
    # An attribute read was recorded wherever the receiver could be typed, because whether the
    # name belongs to a property is not knowable until every file has been parsed. Now it is:
    # anything that did not land on a property was an ordinary attribute - a field, a constant,
    # a bound method passed around - and is not a call.
    prop_ids = {n["id"] for n in nodes if n.get("prop")}
    calls = [e for e in calls if not e.get("attr_read") or e.get("dst") in prop_ids]
    # Constructing an object RUNS its __init__, and that edge was missing. `impact __init__`
    # on a class built in twenty places answered "callers: (none), blast: 0" with exit code 0 -
    # the tool's one unforgivable answer, given to the single most commonly edited method in
    # Python. The class edge stays (Client() does construct a Client); this adds the second,
    # equally real edge to the code that actually runs, found through the same C3 order the
    # interpreter uses, so a subclass without its own __init__ points at the one it inherits.
    class_ids = {n["id"] for n in nodes if n["kind"] == "class"}
    ctor = []
    for e in calls:
        if e.get("dst") in class_ids:
            # Constructing also runs `__new__`, BEFORE `__init__` and independently of it -
            # not an either/or, since a class defining both runs both. This half was missed:
            # 237 definitions in the standard library, 8 of them with a caller. A class that
            # defines only `__new__` - a singleton, an immutable type, anything interning its
            # instances - answered "nothing depends on this" about the method that builds it.
            for c in _mro(e["dst"], bases_of, mro_cache):
                if (c + ".__new__") in def_ids:
                    ctor.append({**e, "dst": c + ".__new__", "confidence": "CONSTRUCTOR"})
                    break
            for c in _mro(e["dst"], bases_of, mro_cache):
                if (c + ".__init__") in def_ids:
                    ctor.append({**e, "dst": c + ".__init__", "confidence": "CONSTRUCTOR"})
                    break
            else:
                # No `__init__` anywhere in the order, and yet the class takes arguments: a
                # dataclass, whose `__init__` is generated and so is not in this graph. What IS
                # here is `__post_init__`, which that generated code calls - and which reported
                # five callers across twenty-five definitions. Only when there is no explicit
                # `__init__`: a class that writes its own calls `__post_init__` in the open,
                # and counting it twice would invent a caller.
                for c in _mro(e["dst"], bases_of, mro_cache):
                    if (c + ".__post_init__") in def_ids:
                        ctor.append({**e, "dst": c + ".__post_init__",
                                     "confidence": "CONSTRUCTOR"})
                        break
    calls.extend(ctor)
    # A function defined twice in one module - the `try: from fast import x / except: def x`
    # pattern - produced TWO nodes sharing one id. Python binds the last one, so that is the
    # definition; the earlier lines are recorded as shadowed rather than thrown away, because
    # "this name is defined three times" is worth knowing.
    live = {}
    for n in nodes:
        prev = live.get(n["id"])
        if prev is not None and prev.get("line") != n.get("line"):
            n = {**n, "shadows": [*prev.get("shadows", []), prev["line"]]}
        live[n["id"]] = n
    nodes = list(live.values())
    nodes.sort(key=lambda n: (n["kind"], n["id"]))              # DETERMINISTIC output: byte-reproducible across runs regardless of os.walk order -> two builds are diffable
    calls.sort(key=lambda e: (e["src"], e.get("recv") or "", e["callee"], e.get("line", 0)))
    imports.sort(key=lambda e: (e["src"], e["callee"], e.get("line", 0)))
    # The source files this graph was built from. Staleness used to be "is any file newer than
    # the graph", which cannot see a DELETION: removing a file changes nobody's mtime, so the
    # graph kept answering about code that was gone - `where` would send you to a deleted file.
    # Stamped with codegraph's OWN source hash. The parse cache had this from the start; the
    # graph did not, so upgrading the tool and querying an unchanged tree served the previous
    # version's answers - every resolution fix invisible until somebody happened to edit a file.
    # The graph carries what a QUESTION needs; the machinery that answered it stays behind.
    # An edge accumulates a dozen working fields on the way to a target - whether the receiver
    # was a local, the root of a dotted chain, the enclosing class, the inferred type - and not
    # one of them is read again once the target is known. They were being written to disk,
    # loaded back on every query, and held in memory through both serialisations.
    # `method` is a fact about the CALL SITE - was it written `f()` or `x.f()` - not machinery
    # from resolving it, and without it nothing downstream can tell the two apart. `recv` does
    # not answer that: it is None both for a bare call and for `get_thing().f()`.
    # WHAT A CALL HANDS BACK, settled last and all together, because these facts depend on one
    # another. `def session(...) -> Estimate` says outright what `est = cost.session(...)` is,
    # and ignoring it left `est.human()` unresolved - a method that runs three times in the same
    # program reported as called by nothing. A fixture's class can come from a call inside
    # another fixture, which cannot resolve until THAT fixture has a class. One pass in a fixed
    # order cannot settle that, so these run in rounds until a round changes nothing.
    #
    # Nothing here resolves a call from scratch. Each round reuses answers already worked out:
    # the call is an ordinary edge that has been pointed at a definition, and this reads that
    # definition's return class, resolves the NAME in the module the function was defined in -
    # where the name means something - and walks the MRO like every other typed receiver.
    returns_of = {n["id"]: n["returns"] for n in nodes if n.get("returns")}
    returns_builtin = {n["id"]: n["returns_builtin"] for n in nodes if n.get("returns_builtin")}
    # A recorded class name that names no class in this tree is not an answer, so those fall
    # through to what the call hands back. This is the same "tried first, fallen back from"
    # order the two-line form uses, one level up.
    ret_pending = {}
    mod_of_def = {n["id"]: n["module"] for n in nodes}
    fixtures = {}
    for n in nodes:
        if n.get("fixture"):
            fixtures.setdefault(n["module"], {})[n["name"]] = (n["fixture"], n["module"], n["id"])
    fix_pending = []

    def _names_a_class(cls_name, where, scope):
        return bool(cls_name) and len(_classes_named(cls_name, where, scope)) == 1

    for n in nodes:
        if n.get("returns_call") and not _names_a_class(n.get("returns"), n["module"], n["id"]):
            ret_pending[n["id"]] = n["returns_call"]
        if ((n.get("fixture_alias") or n.get("fixture_call"))
                and not _names_a_class(n.get("fixture"), n["module"], n["id"])):
            fix_pending.append(n)

    def _fixture_chain(home):
        """Where pytest looks, in the order it looks: this module, then `conftest.py` in its
        directory, then each directory above it."""
        parts = home.split("/")[:-1]
        chain = [home]
        while True:
            chain.append("/".join([*parts, "conftest"]) if parts else "conftest")
            if not parts:
                break
            parts = parts[:-1]
        return chain

    def _visible_fixture(home, name):
        for m in _fixture_chain(home):
            got = fixtures.get(m, {}).get(name)
            if got:
                return got
        return None

    def _method_on(cls_name, where, scope, callee, e=None):
        """The definition `callee` names on a class called `cls_name`, read in `where`'s
        scope. One class or none: several answering to the name is not an answer.

        When the class was found and the method was not, that is recorded on the edge - it is
        the difference between "could not type the receiver" and "typed it, and the method is
        not in this tree"."""
        hits = _classes_named(cls_name, where, scope)
        if len(hits) != 1:
            return None
        for c in _mro(hits[0], bases_of, mro_cache):
            cand = f"{c}.{callee}"
            if cand in def_ids:
                return cand
        if e is not None:
            e["typed_miss"] = True
        return None

    for _round in range(6):
        changed = False
        # Which definition a name reached from a given scope. Two different ones is not an
        # answer, the same way a bare call with two candidates is not.
        resolved_in = {}
        # And the same thing per LINE. A chain does not need the scope-wide answer: the call it
        # is written on is right there, on the same line, and has been resolved on its own.
        # Without this, `mi.append(mi).get_loc(...)` and `index.append(index).get_loc(...)` two
        # lines apart cancelled each other - the tool got more right about `append` and answered
        # LESS about the chains. Where one line holds two of them, the scope-wide rule is the
        # right one and still applies.
        resolved_at = {}
        for e in calls:
            if e.get("dst"):
                k = (e["src"], e["callee"])
                resolved_in[k] = None if k in resolved_in and resolved_in[k] != e["dst"] \
                    else e["dst"]
                for ln in e.get("lines") or [e.get("line")]:
                    a = (e["src"], e["callee"], ln)
                    resolved_at[a] = None if a in resolved_at and resolved_at[a] != e["dst"] \
                        else e["dst"]

        # A return class that is another call's return class. `def open_client(): return
        # connect().session()` states it two functions away.
        for nid, callee in list(ret_pending.items()):
            tgt = resolved_in.get((nid, callee))
            if tgt and tgt in returns_of:
                returns_of[nid] = returns_of[tgt]
                mod_of_def[nid] = mod_of_def.get(tgt, mod_of_def.get(nid, ""))
                del ret_pending[nid]
                changed = True

        # A fixture whose value is another fixture's, or another call's.
        rest = []
        for n in fix_pending:
            got = None
            if n.get("fixture_alias"):
                got = _visible_fixture(n["module"], n["fixture_alias"])
            elif n.get("fixture_call"):
                tgt = resolved_in.get((n["id"], n["fixture_call"]))
                if tgt and tgt in returns_of:
                    got = (returns_of[tgt], mod_of_def.get(tgt, ""), tgt)
            if got:
                n["fixture"] = got[0]
                fixtures.setdefault(n["module"], {})[n["name"]] = got   # replaces a name that named nothing
                changed = True
            else:
                rest.append(n)
        fix_pending = rest

        # `est = cost.session(...)` then `est.human()`, and `make().go()` written on one line.
        for e in calls:
            if e.get("dst") or not e.get("recv_call"):
                continue
            here = (e["src"], e["recv_call"], e.get("line"))
            target = resolved_at[here] if here in resolved_at \
                else resolved_in.get((e["src"], e["recv_call"]))
            if not target:
                continue
            if target in returns_builtin and not e.get("builtin_recv"):
                # `d = make()` where `make() -> dict`. The same reading as `d = {}`, one call
                # away, and settled here because it needs the call resolved first.
                e["builtin_recv"] = returns_builtin[target]
                changed = True
            if target not in returns_of:
                continue
            cand = _method_on(returns_of[target], mod_of_def.get(target, ""), target,
                              e["callee"], e)
            if cand:
                e["dst"] = cand
                e["confidence"] = "TYPED"
                changed = True

        # WHAT PYTEST PUTS IN A TEST'S ARGUMENTS. The lookup is pytest's own and no more, so
        # two fixtures of one name are an ORDER, not an ambiguity - which is why this never
        # reports AMBIGUOUS - and a sibling directory's conftest is not on the path.
        for e in calls:
            if e.get("dst") or not e.get("fixture_param"):
                continue
            found = _visible_fixture(e.get("mod") or "", e["fixture_param"])
            if not found:
                continue
            cand = _method_on(found[0], found[1], found[2], e["callee"], e)
            if cand:
                e["dst"] = cand
                e["confidence"] = "FIXTURE"
                changed = True

        if not changed:
            break

    # A RECEIVER THAT WAS TYPED, AND A METHOD THAT IS NOT HERE. The name-wide version of this
    # already runs above: a method no definition in the tree carries is EXTERNAL, not a blind
    # spot. This is the same test with better evidence - not "does anything define it" but
    # "does THIS class, or any base of it this tree can see". Every `client.get()` in flask's
    # test suite is werkzeug's; 161 calls on `self` alone in a clone of ansible are a library
    # base's. Left as UNTYPED they are counted as winnable and printed by `impact` as "unsure",
    # which is this tool sending an agent to look for something it has already proved is not
    # there.
    for e in calls:
        if e["confidence"] != "UNTYPED":
            continue
        if e.get("typed_miss"):
            e["confidence"] = "EXTERNAL"
        elif e.get("builtin_recv"):
            # Settled in the rounds above rather than at the call site: a builtin handed back by
            # a function this file called.
            m = e.get("mod") or ""
            if _is_builtin_method(e["builtin_recv"], e["callee"],
                                  defined_here[m] | imported_in.get(m, set())):
                e["confidence"] = "BUILTIN"

    keep = ("src", "dst", "callee", "confidence", "recv", "method", "mod", "line", "lines",
            "candidates")
    calls = [{k: e[k] for k in keep if k in e} for e in calls]
    graph = {"version": _VERSION,
             "nodes": nodes, "calls": calls, "imports": imports, "dirs": sorted(dirs),
             "mentioned": sorted(mentioned),
             "sources": {p: stamps[p] for p in sorted(stamps)}, "unreadable": sorted(unreadable)}
    # The counts, IN the file. They were printed by `stats` and stored nowhere, so anything
    # reading the graph had to recompute them - and the first integration written against it
    # read `func_defs` off the JSON, found nothing, and printed a confident zero. A gauge that
    # reads zero rather than the truth is worse than no gauge. One line, and that whole class
    # of bug goes away for everyone downstream.
    graph["stats"] = stats(graph)
    if write:
        for target, what in ((CACHE, "cache"), (OUT, "graph")):
            try:
                _jwrite({"_v": _VERSION, "files": newcache} if what == "cache" else graph, target)
                if what == "cache":
                    newcache = {}      # written; the graph is serialised next and wants the room
            except OSError as ex:
                if what == "cache":
                    # The cache is an OPTIMISATION. Losing it costs a second on the next build
                    # and nothing else, so it must never be the reason an answer does not
                    # arrive - which is what it was: eight concurrent builds on Windows, and
                    # the one that lost the race to the cache file exited 1 with no graph.
                    print(f"note: could not write the cache to {target} "
                          f"({ex.strerror or ex}) - the next build will be slower",
                          file=sys.stderr)
                    continue
                # A read-only checkout, a container mount, someone else's repository. The
                # analysis itself worked; only the writing failed, and there is somewhere else
                # to put it. Name the path that ACTUALLY failed - reporting the graph's
                # directory when the cache was the problem sends people to the wrong place.
                raise BadPath(f"cannot write the {what} to {target}: {ex.strerror or ex}\n"
                              f"  set CODEGRAPH_OUT to a writable path") from ex
    return graph


def _selftest():
    """Prove import-aware resolution disambiguates same-named functions - the hardening that makes blast-radius
    trustworthy - with a RED-FIRST control showing the naive name-match conflates."""
    import shutil
    import tempfile

    def _w(path, body):                                      # write a fixture file, closing it
        with open(path, "w", encoding="utf-8") as fh: fh.write(body)
    d = tempfile.mkdtemp()
    _w(os.path.join(d, "alpha.py"), "def digest():\n    return 1\ndef write():\n    return 9\n")
    _w(os.path.join(d, "beta.py"), "def digest():\n    return 2\n")
    _w(os.path.join(d, "caller.py"),
        "import alpha\ndef run():\n    return alpha.digest()\n"
        "class Box:\n    def helper(self):\n        return 5\n    def use(self):\n        return self.helper()\n"
        "def w():\n    open('x').write('y')\n"                    # a .write() on a file object - must NOT resolve to alpha.write
        "def t():\n    b = Box()\n    return b.helper()\n")       # b = Box(); b.helper() -> must resolve to caller.Box.helper (TYPED)
    os.makedirs(os.path.join(d, "sub")); _w(os.path.join(d, "sub", "nested.py"), "def deep():\n    return 7\n")   # a NESTED module (recursion)
    os.makedirs(os.path.join(d, "__pycache__")); _w(os.path.join(d, "__pycache__", "j.py"), "x = 1\n")           # noise dir - must be pruned
    g = build([d], write=False)
    edge = [e for e in g["calls"] if e["src"] == "caller.run" and e["callee"] == "digest"]
    ok1 = len(edge) == 1 and edge[0].get("dst") == "alpha.digest" and edge[0]["confidence"] == "QUALIFIED"
    ba, bb = blast_radius(g, "alpha.digest"), blast_radius(g, "beta.digest")
    ok2 = "caller.run" in ba and "caller.run" not in bb
    naive = sorted({e["src"] for e in g["calls"] if e["callee"] == "digest"})   # the v0 name-only method
    ok3 = "caller.run" in naive                                                  # naive cannot tell WHICH digest -> conflates -> proves the hardening earns its keep
    sm = [e for e in g["calls"] if e["src"] == "caller.Box.use" and e["callee"] == "helper"]
    ok4 = len(sm) == 1 and sm[0].get("dst") == "caller.Box.helper" and sm[0]["confidence"] == "SELF-METHOD"
    we = [e for e in g["calls"] if e["src"] == "caller.w" and e["callee"] == "write"]
    ok5 = len(we) == 1 and we[0].get("dst") is None and we[0]["confidence"] == "UNTYPED"   # a method on a value we cannot type - and NOT the in-tree alpha.write
    ok6 = any(loc.startswith("caller.py:") for loc, c in sites(g, "alpha.digest") if c == "caller.run")
    ok7 = path(g, "caller.run", "alpha.digest") == ["caller.run", "alpha.digest"]
    imp = impact(g, "alpha.digest")
    ok8 = "caller.run" in imp["callers"] and any(l.startswith("caller.py:") for l, _ in imp["sites"]) and "caller.run" in imp["blast"]
    tt = [e for e in g["calls"] if e["src"] == "caller.t" and e["callee"] == "helper"]
    ok9 = len(tt) == 1 and tt[0].get("dst") == "caller.Box.helper" and tt[0]["confidence"] == "TYPED"   # b = Box(); b.helper()
    r1, r2 = os.path.join(d, "t1"), os.path.join(d, "t2"); os.makedirs(r1); os.makedirs(r2)
    _w(os.path.join(r1, "dup.py"), "def only_a():\n    return 1\n")
    _w(os.path.join(r2, "dup.py"), "def only_b():\n    return 2\n")
    gm = build([r1, r2], write=False)
    ok10 = {n["id"] for n in gm["nodes"] if n["kind"] == "module" and n["name"] == "dup"} == {"t1/dup", "t2/dup"}   # two same-named modules across trees get DISTINCT ids (no collision)
    ok11 = any(n["id"] == "sub/nested" for n in g["nodes"]) and not any("__pycache__" in n["module"] for n in g["nodes"])   # recursion finds sub/nested; __pycache__ pruned
    cd2 = tempfile.mkdtemp()
    _w(os.path.join(cd2, "ma.py"), "import mb\ndef f(): return mb.g()\n")
    _w(os.path.join(cd2, "mb.py"), "def g():\n    import ma\n    return ma.f()\n")   # LOCAL import back = the deliberate cycle-break, NOT a cycle
    _w(os.path.join(cd2, "mc.py"), "import md\ndef h(): return 1\n")
    _w(os.path.join(cd2, "md.py"), "import mc\ndef i(): return 1\n")               # module-level both ways = a REAL cycle
    cyc = set(cycles(build([cd2], write=False)))
    ok12 = ("mc", "md") in cyc and ("ma", "mb") not in cyc
    shutil.rmtree(cd2, ignore_errors=True)
    # RELATIVE IMPORTS - the standard package idiom. `from . import x` used to vanish entirely,
    # and `from .x import f` had its dot stripped and matched a TOP-LEVEL module of the same
    # name: a wrong answer wearing the QUALIFIED label. The trap is built deliberately here -
    # a top-level `thing.py` sitting beside a package that has its own `thing.py`.
    rd = tempfile.mkdtemp()
    os.makedirs(os.path.join(rd, "pkg", "deep"))
    _w(os.path.join(rd, "thing.py"), "def load():\n    return 'TOP LEVEL - the wrong one'\n")
    _w(os.path.join(rd, "pkg", "__init__.py"), "")
    _w(os.path.join(rd, "pkg", "thing.py"), "def load():\n    return 'the package one'\n")
    _w(os.path.join(rd, "pkg", "user.py"),
        "from . import thing\nfrom .thing import load\n"
        "def a():\n    return thing.load()\n"
        "def b():\n    return load()\n")
    # ...and two ways to reach that same top-level thing.py WRONGLY: a receiver nobody
    # imported, and one dot more than there is tree to climb.
    _w(os.path.join(rd, "pkg", "loose.py"), "def d():\n    return thing.load()\n")
    _w(os.path.join(rd, "pkg", "toofar.py"),
        "from ... import thing\ndef e():\n    return thing.load()\n")
    _w(os.path.join(rd, "pkg", "deep", "__init__.py"), "")
    _w(os.path.join(rd, "pkg", "deep", "down.py"),
        "from .. import thing\ndef c():\n    return thing.load()\n")     # two dots: climb to pkg
    gr = build([rd], write=False)
    def _dst(src, callee):
        m = [e for e in gr["calls"] if e["src"] == src and e["callee"] == callee]
        return (m[0].get("dst"), m[0]["confidence"]) if len(m) == 1 else (None, "?")
    ok13 = _dst("pkg/user.a", "load") == ("pkg/thing.load", "QUALIFIED")     # from . import thing
    ok14 = _dst("pkg/user.b", "load") == ("pkg/thing.load", "QUALIFIED")     # from .thing import load
    ok15 = _dst("pkg/deep/down.c", "load") == ("pkg/thing.load", "QUALIFIED")  # from .. import thing
    ok16 = all(_dst(src, "load")[0] != "thing.load" for src in ("pkg/user.a", "pkg/user.b", "pkg/deep/down.c"))
    ok17 = ("pkg/user", "pkg/thing") in {(i["src"], i["callee"]) for i in gr["imports"]}
    ok18a = _dst("pkg/loose.d", "load") == (None, "UNTYPED")       # never imported: not that module
    ok18b = _dst("pkg/toofar.e", "load") == (None, "UNTYPED")      # one dot too many: no target
    shutil.rmtree(rd, ignore_errors=True)
    # CONSTRUCTORS, and the half-qualified name people use to ask about one. `impact __init__`
    # answered "callers: (none), blast: 0" for a class constructed all over a tree, because
    # nobody writes the word __init__ at a call site - they write Client(). Mid() runs the
    # __init__ it inherits, so the edge follows the same C3 order the interpreter does.
    kd = tempfile.mkdtemp()
    _w(os.path.join(kd, "k.py"),
        "class Base:\n    def __init__(self, a):\n        self.a = a\n"
        "class Mid(Base):\n    pass\n"
        "def build():\n    return Mid(1)\n")
    gk = build([kd], write=False)
    ok18 = callers_of(gk, "k.Base.__init__") == ["k.build"]
    ok19 = (callers_of(gk, "Base.__init__") == ["k.build"]
            and [i for i, _ in where(gk, "Base.__init__")] == ["k.Base.__init__"])
    ok20 = sorted({e["src"] for e in gk["calls"] if e["callee"] == "__init__"}) == []
    shutil.rmtree(kd, ignore_errors=True)
    # SCOPES. Two functions in one module can share a name - `inner`, or `wrapper` inside any
    # two decorators. Resolution went through a (module, name) dict, which holds one of them.
    nd = tempfile.mkdtemp()
    _w(os.path.join(nd, "n.py"),
        "def outer():\n    def inner():\n        return 1\n    return inner()\n"
        "def other():\n    def inner():\n        return 2\n    return inner()\n")
    gn = build([nd], write=False)
    ok21 = ({e["src"]: e.get("dst") for e in gn["calls"] if e["callee"] == "inner"}
            == {"n.outer": "n.outer.inner", "n.other": "n.other.inner"})
    shutil.rmtree(nd, ignore_errors=True)
    # super().run() - the one call shape whose caller cannot mention the callee by name, and
    # whose target is nevertheless exact: the next class in the interpreter's own order.
    sd = tempfile.mkdtemp()
    _w(os.path.join(sd, "s.py"),
        "class Base:\n    def run(self):\n        return 1\n"
        "class Kid(Base):\n    def run(self):\n        return super().run() + 1\n")
    gs = build([sd], write=False)
    ok22 = callers_of(gs, "s.Base.run") == ["s.Kid.run"]
    shutil.rmtree(sd, ignore_errors=True)
    # TWO classes of one name, and an import that says which. `x = Client()` then x.get() used
    # to take whichever id sorted first - resolving into a/svc while the file said b/svc.
    td = tempfile.mkdtemp()
    for _p, _tag in (("a", "A"), ("b", "B")):
        os.makedirs(os.path.join(td, _p))
        _w(os.path.join(td, _p, "__init__.py"), "")
        _w(os.path.join(td, _p, "svc.py"),
            f"class Client:\n    def get(self):\n        return '{_tag}'\n")
    _w(os.path.join(td, "app.py"),
        "from b.svc import Client\ndef go():\n    c = Client()\n    return c.get()\n")
    gt = build([td], write=False)
    _ge = [e for e in gt["calls"] if e["src"] == "app.go" and e["callee"] == "get"]
    ok23 = (len(_ge) == 1 and _ge[0].get("dst") == "b/svc.Client.get"
            and callers_of(gt, "a/svc.Client.get") == [])
    shutil.rmtree(td, ignore_errors=True)
    # The try/except ImportError idiom: one name, two sources. A dict keeps the last, which is
    # the FALLBACK - so the tool used to name slow.parse and leave fast.parse with no callers.
    cd3 = tempfile.mkdtemp()
    _w(os.path.join(cd3, "fast.py"), "def parse():\n    return 'FAST'\n")
    _w(os.path.join(cd3, "slow.py"), "def parse():\n    return 'slow'\n")
    _w(os.path.join(cd3, "app.py"),
        "try:\n    from fast import parse\nexcept ImportError:\n    from slow import parse\n"
        "def go():\n    return parse()\n")
    gc3 = build([cd3], write=False)
    _pe = [e for e in gc3["calls"] if e["src"] == "app.go"]
    ok24 = (len(_pe) == 1 and _pe[0].get("dst") is None
            and _pe[0].get("candidates") == ["fast.parse", "slow.parse"])
    shutil.rmtree(cd3, ignore_errors=True)
    # ANNOTATIONS. A parameter is the one place no assignment exists to read, and the one place
    # modern Python writes the type down. `def send(c: Client)` then c.get() was UNTYPED.
    ad = tempfile.mkdtemp()
    _w(os.path.join(ad, "svc.py"), "class Client:\n    def get(self):\n        return 1\n")
    _w(os.path.join(ad, "app.py"),
        "from typing import Dict\nfrom svc import Client\n"
        "def send(c: Client):\n    return c.get()\n"
        "def keyed(d: Dict[str, Client]):\n    return d.get('k')\n")
    ga = build([ad], write=False)
    _ae = {e["src"]: e.get("dst") for e in ga["calls"] if e["callee"] == "get"}
    ok25 = _ae.get("app.send") == "svc.Client.get" and _ae.get("app.keyed") is None
    shutil.rmtree(ad, ignore_errors=True)
    # SCOPE, from the other side: a class defined inside a function is a NameError anywhere
    # else, and the tree-wide fallback used to hand it out - resolving the call and then typing
    # the variable from it. `import svc` then svc.Client() is the shape that DOES have an
    # answer, and had none.
    nd2 = tempfile.mkdtemp()
    _w(os.path.join(nd2, "svc.py"), "class Client:\n    def get(self):\n        return 1\n")
    _w(os.path.join(nd2, "app.py"),
        "import svc\n"
        "def holder():\n    class Inner:\n        def run(self):\n            return 1\n"
        "    return Inner()\n"
        "def far():\n    i = Inner()\n    return i.run()\n"
        "def near():\n    c = svc.Client()\n    return c.get()\n")
    gn2 = build([nd2], write=False)
    _by = {(e["src"], e["callee"]): e.get("dst") for e in gn2["calls"]}
    ok26 = (_by.get(("app.far", "Inner")) is None and _by.get(("app.far", "run")) is None
            and _by.get(("app.holder", "Inner")) == "app.holder.Inner"
            and _by.get(("app.near", "get")) == "svc.Client.get")
    shutil.rmtree(nd2, ignore_errors=True)
    # SCOPES nobody had pre-scanned: a lambda's parameters, and the file's own top level. And
    # the opposite mistake in the same machinery - a DEFERRED import counted as a name
    # shadowing the module, so the cycle-break idiom lost every call edge through it.
    ld = tempfile.mkdtemp()
    _w(os.path.join(ld, "config.py"), "def dumps(x):\n    return 1\n")
    _w(os.path.join(ld, "app.py"),
        "import config\n"
        "handler = lambda config: config.dumps(1)\n"
        "def deferred():\n    import config as later\n    return later.dumps(2)\n")
    gl = build([ld], write=False)
    _le = {e["src"]: e.get("dst") for e in gl["calls"] if e["callee"] == "dumps"}
    ok27 = _le.get("app") is None and _le.get("app.deferred") == "config.dumps"
    shutil.rmtree(ld, ignore_errors=True)
    # A COMPREHENSION's loop variable is its own scope: it shadows inside, and does not exist
    # outside. Collected as a name bound in the enclosing scope, one line at the top of a file
    # silenced every call through that name in the whole file.
    cd4 = tempfile.mkdtemp()
    _w(os.path.join(cd4, "config.py"), "def dumps(x):\n    return 1\n")
    _w(os.path.join(cd4, "app.py"),
        "import config\n"
        "INSIDE = [config.dumps(r) for config in [[1]]]\n"
        "def after():\n    return config.dumps(2)\n")
    gc4 = build([cd4], write=False)
    _ce = {e["src"]: e.get("dst") for e in gc4["calls"] if e["callee"] == "dumps"}
    ok28 = _ce.get("app") is None and _ce.get("app.after") == "config.dumps"
    shutil.rmtree(cd4, ignore_errors=True)
    # WHERE A NAME CAME FROM. `from time import sleep` says it outright and the answer is "not
    # here" - the tree-wide "one definition of that name" rule used to fire anyway. And
    # `from ops import index as _index` asks the module for the name IT defines, not the local
    # alias, which is the same mistake pointing the other way.
    fd = tempfile.mkdtemp()
    _w(os.path.join(fd, "sched.py"), "def sleep(n):\n    return 'the wrong answer'\n")
    _w(os.path.join(fd, "ops.py"), "def index(x):\n    return 1\n")
    _w(os.path.join(fd, "far.py"), "def _index(x):\n    return 'the other wrong answer'\n")
    _w(os.path.join(fd, "app.py"),
        "from time import sleep\nfrom ops import index as _index\n"
        "def go():\n    return sleep(1) + _index(2)\n")
    gf = build([fd], write=False)
    _fe = {e["callee"]: e.get("dst") for e in gf["calls"] if e["src"] == "app.go"}
    ok29 = _fe.get("sleep") is None and _fe.get("_index") == "ops.index"
    shutil.rmtree(fd, ignore_errors=True)
    # A STAR import is a real binding. Bound as a name spelled "*" - which nothing ever calls -
    # a bare home() fell through to the tree-wide "one definition of that name" rule instead.
    sd2 = tempfile.mkdtemp()
    _w(os.path.join(sd2, "turtle.py"), "def home():\n    return 'the right one'\n")
    _w(os.path.join(sd2, "commands.py"), "def home():\n    return 'a different module'\n")
    _w(os.path.join(sd2, "dance.py"), "from turtle import *\ndef main():\n    return home()\n")
    gs2 = build([sd2], write=False)
    ok30 = callers_of(gs2, "turtle.home") == ["dance.main"] and callers_of(gs2, "commands.home") == []
    shutil.rmtree(sd2, ignore_errors=True)
    shutil.rmtree(d, ignore_errors=True)
    print(f"  resolves alpha.digest specifically (QUALIFIED, not ambiguous): {ok1}")
    print(f"  blast-radius trustworthy (caller.run in alpha's radius, NOT beta's): {ok2}")
    print(f"  RED-FIRST control - naive name-match conflates (would wrongly blame beta.digest): {ok3}")
    print(f"  self.helper() resolves inside its class -> caller.Box.helper (SELF-METHOD): {ok4}")
    print(f"  RED-FIRST - open(..).write() stays UNTYPED, not falsely -> alpha.write: {ok5}")
    print(f"  sites() finds the exact call site (caller.py:line) of alpha.digest: {ok6}")
    print(f"  path() traces caller.run -> alpha.digest: {ok7}")
    print(f"  impact() gives callers + sites + blast in one pre-edit view: {ok8}")
    print(f"  LOCAL TYPE INFERENCE - b = Box(); b.helper() resolves to caller.Box.helper (TYPED): {ok9}")
    print(f"  MULTIROOT - same-named modules across trees get distinct ids (t1/dup, t2/dup), no collision: {ok10}")
    print(f"  RECURSION + PRUNE - finds nested sub/nested, prunes __pycache__: {ok11}")
    print(f"  CYCLES - a real module-level cycle is flagged, a LOCAL import (the cycle-break) is NOT: {ok12}")
    print(f"  RELATIVE `from . import thing` resolves into the package: {ok13}")
    print(f"  RELATIVE `from .thing import load` resolves into the package: {ok14}")
    print(f"  RELATIVE `from .. import thing` climbs one level correctly: {ok15}")
    print(f"  RED-FIRST - none of them resolve to the same-named TOP-LEVEL module (the old wrong answer): {ok16}")
    print(f"  a relative import produces a real IMPORT edge, so deps/cycles see packages: {ok17}")
    print(f"  CONSTRUCTOR - Mid(1) is a caller of the Base.__init__ it inherits: {ok18}")
    print(f"  a half-qualified Base.__init__ means that one, for callers and for where: {ok19}")
    print(f"  RED-FIRST - name matching finds NO caller of __init__; nobody writes it: {ok20}")
    print(f"  SCOPES - two nested helpers both named inner stay two functions: {ok21}")
    print(f"  super().run() is a real caller of the base method it reaches: {ok22}")
    print(f"  RED-FIRST - thing.load() with no import of thing resolves to NOTHING: {ok18a}")
    print(f"  RED-FIRST - one dot too many climbs out of the tree, not onto thing.py: {ok18b}")
    print(f"  x = Client(); x.get() resolves to the Client this file IMPORTED: {ok23}")
    print(f"  one name imported from two modules names BOTH, picks neither: {ok24}")
    print(f"  an annotated parameter is a type, and Dict[str, Client] is NOT one: {ok25}")
    print(f"  RED-FIRST - a class nested in a function is not offered to the tree: {ok26}")
    print(f"  a lambda parameter shadows, and a deferred import still resolves: {ok27}")
    print(f"  a comprehension variable shadows inside it and nowhere else: {ok28}")
    print(f"  RED-FIRST - an out-of-tree import stays out, and `as` keeps the real name: {ok29}")
    print(f"  `from turtle import *` then home() is turtle's home, not some other: {ok30}")
    ok = (ok1 and ok2 and ok3 and ok4 and ok5 and ok6 and ok7 and ok8 and ok9 and ok10 and ok11 and ok12
          and ok13 and ok14 and ok15 and ok16 and ok17 and ok18 and ok19 and ok20 and ok21 and ok22 and ok18a and ok18b and ok23 and ok24 and ok25 and ok26 and ok27 and ok28 and ok29 and ok30)
    print("SELFTEST", "GREEN" if ok else "RED")
    return 0 if ok else 1


_PRUNE = {"__pycache__", ".git", ".venv", "venv", "env", "virtualenv", "node_modules", ".tox", ".mypy_cache", ".pytest_cache", "build", "dist", "site-packages", "uvpy", ".uvpy"}


def _prune_dir(parent, name):
    """True if a subdir should be skipped - noise, hidden, an embedded interpreter, or a virtualenv root (never index installed Python)."""
    if name in _PRUNE or name.startswith(".") or name.startswith(("cpython-", "python-", "pypy-")): return True
    sub = os.path.join(parent, name)
    try:
        if os.path.exists(os.path.join(sub, "pyvenv.cfg")): return True                              # a virtualenv root
        if os.path.isdir(os.path.join(sub, "bin")) and glob.glob(os.path.join(sub, "bin", "python*")): return True
    except OSError: pass
    return False


def _stamp(path):
    """What the graph records about a file it read: its size, and a digest of its bytes.

    It used to be [mtime, size], and an edit can hold both of those still. Rename a function
    to another of the same length and the size does not move; `git checkout`, `cp -p`,
    `rsync -t` and a container layer all put the old timestamp back on purpose, and an
    automated edit lands inside the same second anyway. The graph then went on answering
    about a function that no longer existed and denying the one that did - with a success
    code, and at the exact moment somebody was about to edit. That is the one failure this
    tool cannot have.

    So it reads the bytes. Reading every file in a tree costs about a twentieth of a second
    where the parse costs three, and no arrangement of timestamps can defeat it. blake2b
    rather than sha256 because nothing here is defending against an adversary - only against
    a clock that did not move.
    """
    with open(path, "rb") as fh:                    # OSError here is a dangling symlink, and
        b = fh.read()                               # both callers already skip those
    return [len(b), hashlib.blake2b(b, digest_size=16).hexdigest()]


def _stamps(paths):
    """{path: stamp} for many files. A file that cannot be read is left out rather than raising.

    THIS WAS PARALLEL FOR AN HOUR AND IS NOT ANY MORE. Deciding whether the graph is current
    costs far more than the answer it guards - on a clone of ansible, 2.7ms to answer and over
    a hundred to check - and a thread pool over the hashing benchmarked at 5.5x on that tree.

    In the real path it was worth nothing: 120ms against 114ms serial, measured three times.
    The benchmark had read the files cold. Here they are always warm, because the build or the
    previous query has just read every one of them, so the work is CPU-bound hashing of small
    buffers and threads have nothing to overlap.

    Kept as a function because gathering the paths first and stamping them second is clearer
    than doing both in one walk. The pool is gone: code that changes nothing is code that will
    be wrong later with nobody noticing.
    """
    out = {}
    for p in paths:
        with contextlib.suppress(OSError):
            out[p] = _stamp(p)
    return out


def _is_stale(g):
    """True if the graph no longer describes what is on disk.

    Changed content is only half of it. A file that was DELETED leaves every other file
    untouched, so a per-file test alone said "fresh" and the graph went on answering about
    code that no longer existed. The set of files is compared too, which catches additions
    and deletions in the same pass.

    What is compared per file is `_stamp` - size and a digest - so an edit that holds the
    clock still is not mistaken for no edit at all.
    """
    if g.get("version") != _VERSION:
        return True                            # a different codegraph built this; its answers
                                               # are that version's, not this one's
    if not os.path.exists(OUT): return True
    known = g.get("sources")
    if not isinstance(known, dict): return True   # a graph from before stamps: rebuild once
    todo = set()                                  # every file worth stamping, gathered first
    winner = {}                                   # realpath -> the path the BUILD would keep
    for d in g.get("dirs", [HOME]):
        for dp, dns, fns in os.walk(d):
            dns[:] = [x for x in dns if not _prune_dir(dp, x)]
            for fn in fns:
                if not fn.endswith(".py"): continue
                full = os.path.join(dp, fn)
                # A SYMLINK TO A FILE THIS TREE ALREADY HOLDS. The build skips it on purpose -
                # naming the module after the link left `callers_of` on the real one empty -
                # and this walk counted it as a file it had never seen. So the two disagreed
                # for ever: stale, rebuild, the rebuild skips the link, stale again. Every
                # query rebuilt the entire graph, silently, and the only symptom was that it
                # felt slow. Ten of them in a clone of ansible: 1.30s a query against 0.09s.
                #
                # Exactly the failure the paragraph below describes for a DANGLING link, one
                # step along, and the same cure: skip what the build skips, or the two do not
                # agree about what the tree contains.
                real = os.path.realpath(full)
                if real in winner:
                    if os.path.islink(full):
                        continue                  # the build keeps the other one
                    todo.discard(winner[real])    # ...and this is the other one
                winner[real] = full
                todo.add(full)
    # A DANGLING SYMLINK, which every long-lived repo has one of, is dropped by `_stamps`
    # rather than raising. This used to return True - "something changed" - so a single broken
    # link meant every query rebuilt the whole graph, for ever, in silence. The build already
    # skips these; the freshness check has to skip the same ones or the two disagree about what
    # the tree even contains.
    seen = _stamps(sorted(todo))
    # An exact comparison of content, not of clocks. A file restored from a backup, a
    # checkout, cp -p, rsync -t or a container layer keeps the timestamp it had, so it lands
    # OLDER than the graph while holding different code - and a test built on timestamps
    # called that fresh, whichever way round it was written. The graph then answered about
    # functions that no longer exist and denied ones that do, with a success code.
    return seen != {k: list(v) for k, v in known.items()}


def _jwrite(obj, path):
    """Write json so that an interrupted or concurrent write cannot leave a corrupt file.

    The temp name carries the process AND thread id. It used to be a fixed `path + ".tmp"`,
    which is not atomic between processes at all: two builds of the same tree wrote the same
    temp file, the first renamed it away, and the second's rename found nothing. Measured: six
    of eight concurrent builds died with FileNotFoundError. An agent that runs this before
    every edit, a CI matrix, or two terminals in one repo all hit it immediately.

    The rename is atomic; the CONTENT has to be on disk before it, or a crash can leave a
    valid-looking file full of nothing.
    """
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        # `json.dump(obj, fh)` and `fh.write(json.dumps(obj))` produce identical bytes and are
        # not the same speed: dump streams through the PURE-PYTHON encoder, because the C one
        # is only reachable from the one-shot path that dumps() takes. On a large graph that is
        # five times the work for the same output.
        text = json.dumps(obj, ensure_ascii=False)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # POSIX renames over an open file without complaint. WINDOWS does not: if any other
        # process has the destination open - a second build, an editor, a reader mid-query -
        # replace fails with "Access is denied", and the message that reached the user blamed
        # their permissions on a directory they could plainly write to. It is a moment, not a
        # refusal, so it is worth waiting out.
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.02)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)                                      # never leave a stray temp behind
        raise

def _find_graph(start=None):
    """The nearest graph at or above `start`, the way git looks upward for .git.

    Without this, building at the repository root and then cd-ing into a package meant every
    query answered "no graph yet" - and the obvious next move, `build .` inside the package,
    quietly produces a PARTIAL graph plus a second graph file, so from then on the answers
    exclude the rest of the repository without ever saying so. An explicit CODEGRAPH_OUT is
    always obeyed; `build` still writes exactly where you point it.
    """
    if os.path.exists(OUT) or os.environ.get("CODEGRAPH_OUT"):
        # The configured location wins whenever it actually holds a graph. Walking up
        # unconditionally broke the library contract - set codegraph.OUT, call load(), and it
        # would answer from whatever graph happened to sit in a parent directory instead.
        # The walk is for the CLI case only: no graph here, so look where the repo root is.
        return OUT
    here = os.path.abspath(start or os.getcwd())
    d = here
    while True:
        cand = os.path.join(d, "codegraph.json")
        if os.path.exists(cand) and _covers(cand, here):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            return OUT                                # reached the filesystem root; no graph
        d = parent


def _covers(graph_path, here):
    """Is this graph ABOUT the place we are standing?

    Walking upward is right - build at the repository root, ask from a package inside it - and
    nothing checked that what it found described where you were. Asked about pandas from a
    directory with no graph, it answered `no symbol named isna` in no time at all, from a graph
    two levels up describing a different tree entirely.

    For a person that is unlikely. For an agent it is the worst kind of wrong answer: fast,
    confident, and about somebody else's code.

    A graph that cannot be read does not cover anything - a corrupt file one directory up must
    not shadow a good one further up, and `load` has its own, better complaint for the case
    where that was the only candidate.
    """
    try:
        with open(graph_path, encoding="utf-8") as fh:
            dirs = json.load(fh).get("dirs") or []
    except (OSError, ValueError):
        return False
    for d in dirs:
        root = os.path.realpath(d)
        p = os.path.realpath(here)
        if p == root or p.startswith(root + os.sep):
            return True
    return False


def load(fresh=True):
    """Load the graph. If `fresh` and any source file changed since the last build, rebuild first (incremental,
    so it's fast) - every query is answered against the CURRENT code, never a stale snapshot."""
    global OUT, CACHE
    found = _find_graph()
    if found != OUT:                                  # answer from the graph that owns this tree
        OUT = found
        CACHE = os.path.join(os.path.dirname(found), "codegraph.cache.json")
    try:
        with open(OUT, encoding="utf-8") as _fh:
            g = json.load(_fh)
    except FileNotFoundError:
        sys.exit("no graph yet - run: codegraph build <path>")
    except (json.JSONDecodeError, ValueError):
        sys.exit(f"graph {OUT} is corrupt (interrupted build?) - run: codegraph build <path> to rebuild")
    if not (isinstance(g, dict) and all(isinstance(g.get(k), list)
                                        for k in ("nodes", "calls", "imports"))):
        # Valid JSON is not the same as a graph. A file left by some other tool, a bad merge,
        # or someone's codegraph.json from a different project parses perfectly and then every
        # query dies on a traceback - `[]` has no .get, and the first thing asked of it is
        # g.get("version"). The parse has been guarded since the beginning; the SHAPE never was.
        sys.exit(f"{OUT} is not a codegraph graph - run: codegraph build <path> to rebuild")
    if fresh and _is_stale(g):
        try:
            g = build(g.get("dirs"))
        except BadPath as e:
            # TWO different situations raise this, and saying the wrong one sends somebody
            # looking for a directory that has not moved. A read-only checkout answered
            # "the tree this graph was built from is gone: cannot write the graph ... Permission
            # denied" - the first clause false, the rest true, in one sentence.
            if "cannot write" in str(e):
                # The analysis worked. Only the writing failed, and the answers are still good;
                # they just cost a rebuild every time until there is somewhere to put them.
                sys.exit(f"{e}\n"
                         f"  the code is fine and so is the graph - this is a read-only place "
                         f"to keep it")
            # The tree this graph describes has been moved or deleted. Rebuilding is the right
            # instinct and it cannot succeed - but a traceback is not an answer, and the graph
            # on disk is now a description of somewhere that is not there. Say which.
            sys.exit(f"the tree this graph was built from is gone: {e}\n"
                     f"  build somewhere that exists, or delete {OUT}")
    return g


def _module_of(node_id):
    """The module an id belongs to: everything before the first dot after the last slash."""
    head, _, tail = node_id.rpartition("/")
    return (head + "/" if head else "") + tail.split(".")[0]


def _files(g):
    """module id -> the file to open, relative to the directory the GRAPH lives in.

    `sites` promises the exact places to edit, and it was building them out of module ids -
    fine for a single tree, where an id really is a path, and wrong the moment there are two
    roots: ids are prefixed with a label then, so the answer named a file that does not exist.
    Anchored to the roots the graph was built from - their common parent - so the same query
    gives the same answer wherever it is asked from. For one tree that is the tree itself, which
    is what these paths have always been relative to.
    """
    dirs = [d for d in g.get("dirs") or [] if d]
    try:
        base = os.path.commonpath(dirs) if dirs else None
    except ValueError:
        base = None                              # roots on different Windows drives
    out = {}
    for n in g["nodes"]:
        if n["kind"] != "module":
            continue
        f = n.get("file")
        if f and base:
            with contextlib.suppress(ValueError):    # a different Windows drive: absolute it is
                f = os.path.relpath(f, base).replace(os.sep, "/")
        out[n["id"]] = f or (n["id"] + ".py")
    return out


def _ids_matching(g, name):
    """The definitions a query name refers to: an exact id, else every id it is a dotted TAIL of.

    `Base.__init__` is the natural way to say which __init__ you mean, and it used to match
    nothing: only whole ids (`pkg.mod.Base.__init__`) and bare names counted. Matching nothing
    was not the bug - the bug is what happened next. The name fell through to "called here but
    not defined here" and the answer was assembled from every edge whose callee was `__init__`,
    so asking about Base's constructor returned the caller of a different class's, under a note
    saying Base.__init__ is not defined in a graph that defines it. Exit code 0.

    An exact id wins outright, so a tree containing both `a.b.f` and `x.a.b.f` still answers
    `a.b.f` with the one you named rather than calling it ambiguous.
    """
    defs = [n for n in g["nodes"] if n["kind"] in ("func", "class")]
    exact = [n["id"] for n in defs if n["id"] == name]
    if exact:
        return exact
    # Both separators count as a boundary: ids nest directories with "/" and symbols with ".",
    # so `mod.f` has to reach `pkg/deep/mod.f` the same way `Base.__init__` reaches
    # `h.Base.__init__`. Never a bare endswith - that would let `init` match `__init__`.
    tails = ("." + name, "/" + name)
    return [n["id"] for n in defs if n["id"].endswith(tails)]


class Unknown(Exception):
    """A name this graph has never seen: not defined here, and not called here either.

    Raised rather than answered with an empty result. "Nothing depends on this" and "I have
    never heard of that" are different facts, and a misspelling that returns the first is how
    an agent talks itself into an unsafe edit. The command line learned this a round ago; the
    library - the door an agent actually uses - kept returning the plausible empty answer.
    """


class Ambiguous(Exception):
    """A bare name that several definitions answer to.

    Raised rather than merged. The CLI learned to refuse this first, and the library - which is
    the door the README tells an agent to use - went on quietly unioning the callers of two
    different functions. Fixing one door and not the other is not fixing it.
    """

    def __init__(self, name, candidates):
        self.name = name
        self.candidates = sorted(candidates)
        super().__init__(f"{name!r} names {len(self.candidates)} definitions: "
                         + ", ".join(self.candidates))


def _one(g, target):
    """The definitions a query may act on: never merged, never invented.

    Raises Ambiguous for a name several definitions answer to, and Unknown for one the graph
    has never seen. A name that is only CALLED here - json.loads - is neither: asking where it
    is called is a fair question, so it passes through and the caller's fallback answers it.
    """
    kind, ids = _describe(g, target)
    if kind == "unknown":
        raise Unknown(f"nothing named {target!r} is defined or called in this graph")
    if len(ids) > 1:
        raise Ambiguous(target, ids)
    return ids


def _targets(g, target):
    """The definitions this name refers to. Empty when nothing here defines it.

    A dotted string used to be taken as a real id without checking, so `impact typo.name`
    answered "no callers" with a success code - and a caller cannot tell that from a function
    nothing calls. A qualified id has to exist to count.
    """
    return _ids_matching(g, target)


def _describe(g, name):
    """How this graph knows the name: 'defined' here, only 'called' here, or 'unknown'.

    Three different facts that all used to print "(none)" and exit 0. For a script - or an
    agent deciding whether an edit is safe - "nothing depends on it" and "I have never heard
    of it" have to be different answers, because one of them means you typed it wrong.
    """
    ids = _targets(g, name)
    if ids:
        return "defined", ids
    if any(n["kind"] == "module" and n["id"] == name for n in g["nodes"]):
        return "module", []                      # in the graph, just not a function or a class
    bare = name.split(".")[-1]
    if any(e["callee"] == bare for e in g["calls"]):
        return "called", []                      # called here, defined elsewhere (stdlib, a library)
    return "unknown", []


def callers_of(g, target):
    """function ids that call `target`. When `target` resolves to a definition, follow the resolved edges -
    so a query for pulse.digest returns only its real callers, NOT callers of the same-named court.digest.
    Falls back to bare-name matching only for external/unknown targets."""
    ids = set(_one(g, target))
    if ids:
        return sorted({e["src"] for e in g["calls"] if e.get("dst") in ids})
    name = target.split(".")[-1]
    return sorted({e["src"] for e in g["calls"] if e["callee"] == name})


def calls_from(g, node_id):
    """The resolved in-tree calls made by node_id (the specific definitions it reaches)."""
    for i in _one(g, node_id):
        return sorted({e["dst"] for e in g["calls"] if e["src"] == i and e.get("dst")})
    return sorted({e["dst"] for e in g["calls"] if e["src"] == node_id and e.get("dst")})


def _callers_index(g):
    """dst -> the set of ids that call it, built once.

    blast_radius used to call callers_of() per node, and callers_of() scans every edge, so a
    wide radius was O(nodes x edges). One pass instead.
    """
    idx = defaultdict(set)
    for e in g["calls"]:
        if e.get("dst"): idx[e["dst"]].add(e["src"])
    return idx


def blast_radius(g, target, max_hops=None):
    """Transitive callers of `target` - what could break if you change it. COMPLETE by default.

    It used to stop after six hops and say nothing about it: a twelve-deep call chain reported
    six of eleven callers as though that were the answer, and the five it dropped were the ones
    furthest from the change - exactly the ones you would not think to check yourself. A
    truncated blast radius is worse than none, because it is trusted. The graph is finite and
    `seen` guarantees termination, so the cap bought nothing. Pass max_hops to bound it on
    purpose; then the answer is partial because you asked for it to be.
    """
    idx = _callers_index(g)
    frontier, seen, hops = set(callers_of(g, target)), set(), 0
    while frontier and (max_hops is None or hops < max_hops):
        seen |= frontier
        nxt = set()
        for caller_id in frontier: nxt |= idx.get(caller_id, set())
        frontier = nxt - seen; hops += 1
    return sorted(seen)


def where(g, name):
    """where a symbol is DEFINED: (id, file:line) for each def matching name exactly or by its bare name."""
    # The same matcher every other verb uses, so `where` and `callers` cannot disagree about
    # what a name means. It used to strip to the bare name, so `where Base.__init__` listed
    # Own.__init__ as well - a definition that is not the one you asked about.
    want = set(_ids_matching(g, name))
    files = _files(g)
    out = []
    for n in g["nodes"]:
        if n["id"] in want:
            where_ = f"{files.get(n['module'], n['module'] + '.py')}:{n['line']}"
            if n.get("shadows"):     # the live definition, plus the lines it overrides
                where_ += " (shadows " + ", ".join(f"line {ln}" for ln in n["shadows"]) + ")"
            out.append((n["id"], where_))
    return sorted(out)


def find(g, substr):
    """fuzzy symbol search - every function/class whose name CONTAINS substr (replaces broad greps for a name)."""
    s = substr.lower()
    files = _files(g)
    return sorted((n["id"], f"{files.get(n['module'], n['module'] + '.py')}:{n['line']}")
                  for n in g["nodes"] if n["kind"] in ("func", "class") and s in n["name"].lower())


def sites(g, target):
    """every CALL SITE of target as (file:line, caller) - the exact places to edit when you change it (the
    REFACTOR helper). Uses resolved edges when target is a known def; else the bare callee name."""
    ids = set(_one(g, target)); name = target.split(".")[-1]
    mod_of = {n["id"]: n["module"] for n in g["nodes"]}
    files = _files(g)
    out = set()
    for e in g["calls"]:
        if (e.get("dst") in ids) if ids else (e["callee"] == name):
            m = e.get("mod") or mod_of.get(e["src"]) or e["src"].split(".")[0]
            for ln in e.get("lines") or [e["line"]]:
                out.add((f"{files.get(m, m + '.py')}:{ln}", e["src"]))
    return sorted(out)


def path(g, src, dst, max_hops=None):
    """A call path from src to dst over resolved edges, or [] if there is genuinely none.

    Also uncapped now. It stopped at eight hops and returned [] - which prints as "(no path)",
    i.e. absence of evidence reported as evidence of absence. Breadth-first over a finite graph
    with a visited set terminates by itself, and the first path found is a shortest one.
    """
    import collections
    fwd = {}
    for e in g["calls"]:
        if e.get("dst"): fwd.setdefault(e["src"], []).append(e["dst"])
    goals = set(_one(g, dst) or [dst])
    for s in (_one(g, src) or [src]):
        if s in goals: return [s]            # the path from a function to itself is that
                                             # function. "(no path)" reads as "unconnected".
        q = collections.deque([[s]]); seen = {s}
        while q:
            p = q.popleft()
            for nxt in fwd.get(p[-1], []):
                if nxt in goals: return p + [nxt]
                if nxt not in seen and (max_hops is None or len(p) < max_hops):
                    seen.add(nxt); q.append(p + [nxt])
    return []


def module_deps(g, mod):
    """(imports, importers) for a module: in-tree modules it imports, and in-tree modules that import it."""
    intree = {n["id"] for n in g["nodes"] if n["kind"] == "module"}
    if mod not in intree:
        # ([], []) for a module that does not exist reads exactly like a module with no
        # dependencies, which is a far more reassuring fact than the truth. The command line
        # has refused this since round six; the library went on returning the comfortable pair.
        raise Unknown(f"no module {mod!r} in this graph")
    imports = sorted({e["callee"] for e in g["imports"] if e["src"] == mod and e["callee"] in intree})
    importers = sorted({e["src"] for e in g["imports"] if e["callee"] == mod and e["src"] in intree})
    return imports, importers


def cycles(g):
    """Module-level import cycles among in-tree modules, of ANY length.

    It used to look only for MUTUAL pairs - A imports B and B imports A - and report nothing
    for a -> b -> c -> a. That is the cycle that actually survives in a codebase, because a
    mutual pair is obvious the moment you write it and a three-way loop is not. Reporting
    "(none)" for a real cycle is the failure this tool is supposed to avoid.

    Each group returned is a set of modules that can all reach each other: a strongly connected
    component. A deferred import (inside a function) is the deliberate cycle-break, so it is not
    counted - flagging it would call the fix a smell.
    """
    intree = {n["id"] for n in g["nodes"] if n["kind"] == "module"}
    imp = defaultdict(set)
    for e in g["imports"]:
        if e.get("module_level", True) and e["src"] in intree and e["callee"] in intree:
            imp[e["src"]].add(e["callee"])       # a SELF-import included: `deps` reported the
                                                 # module as its own importer while this said
                                                 # "(none)", and two verbs cannot disagree about
                                                 # whether an edge is there
    # Tarjan, iterative: a recursive walk blows the stack on a deep import chain, and a big
    # repo is exactly where you want this to work.
    index, low, on, stack, out = {}, {}, set(), [], []
    counter = [0]
    for root in sorted(imp):
        if root in index: continue
        work = [(root, iter(sorted(imp.get(root, ()))))]
        index[root] = low[root] = counter[0]; counter[0] += 1
        stack.append(root); on.add(root)
        while work:
            node, it = work[-1]
            nxt = next(it, None)
            if nxt is None:
                work.pop()
                if work: low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    comp = []
                    while True:
                        w = stack.pop(); on.discard(w); comp.append(w)
                        if w == node: break
                    if len(comp) > 1 or node in imp.get(node, ()):
                        out.append(tuple(sorted(comp)))      # size one counts when it imports
                                                             # itself - a real loop, and usually
                                                             # a leftover line nobody meant
            elif nxt not in index:
                index[nxt] = low[nxt] = counter[0]; counter[0] += 1
                stack.append(nxt); on.add(nxt)
                work.append((nxt, iter(sorted(imp.get(nxt, ())))))
            elif nxt in on:
                low[node] = min(low[node], index[nxt])
    return sorted(out)


def impact(g, name):
    """the PRE-EDIT SAFETY view of a function - everything you need before you change it, in one shot:
    its direct callers, every call SITE (file:line), and the transitive blast radius. This is the self-edit
    checklist: read these before touching `name`."""
    _one(g, name)                                    # refuse unknown and ambiguous names FIRST,
                                                     # before three empty lists look like an answer
    # Call sites that USE this name and could not be pinned to any definition. `impact start`
    # reported no callers at all while Engine().start() sat in another file - honest about
    # each edge, and false reassurance as an answer, which is the one thing this view must
    # never give. They are not callers; they are places the question is still open.
    bare = name.split(".")[-1]
    mod_of = {n["id"]: n["module"] for n in g["nodes"]}
    unresolved = sorted({(f"{e.get('mod') or mod_of.get(e['src'], '?')}.py:{ln}", e["src"])
                         for e in g["calls"] if e["callee"] == bare and not e.get("dst")
                         for ln in (e.get("lines") or [e["line"]])})
    return {"callers": callers_of(g, name), "sites": sites(g, name),
            "blast": blast_radius(g, name), "unresolved": unresolved}


def _diff_lines(diff):
    """A unified diff -> {path: {line numbers touched in the NEW file}}.

    The BODY is walked, not just the headers. `git diff` prints three lines of context either
    side by default and the hunk header counts them, so trusting the header alone reports a
    function as changed because a neighbour was - run against this repository, the first
    version named `impact` off the back of an edit three lines away. A tool for deciding what
    to re-read must not pad the list.

    Only `+` lines count. A `-` line is not in the new file at all, so it must not advance the
    counter either, or every line after a deletion is attributed to the wrong function.

    Works with `git diff`, `git diff -U0`, `hg diff`, a saved patch, or a pull request fetched
    by something else, because all of them are this format.
    """
    out, path, ln = {}, None, None
    old_left = new_left = 0                     # how much of the current hunk BODY is unread
    for line in diff.splitlines():
        if old_left > 0 or new_left > 0:
            # Inside a hunk body, and the counts in the @@ header say how long that is. A line
            # here is CONTENT whatever it starts with: an added line reading `++ x` renders as
            # `+++ x`, which the first version read as a file header and threw everything away.
            # Diff a patch file, a stored diff, or documentation containing a diff example, and
            # that is the ordinary case rather than a strange one.
            if line.startswith("+"):
                if ln is not None:
                    out[path].add(ln)
                    ln += 1
                new_left -= 1
            elif line.startswith("-"):
                old_left -= 1                   # not in the new file; do not advance
            elif line.startswith("\\"):
                pass                            # "\ No newline at end of file"
            else:
                if ln is not None:
                    ln += 1                     # context, including the empty line
                old_left -= 1
                new_left -= 1
            continue
        if line.startswith("+++ "):
            p = line[4:].split("\t")[0].strip()
            ln = None
            if p == "/dev/null":
                path = None                     # the file was deleted; there is nothing to read
                continue
            path = p[2:] if p.startswith(("a/", "b/")) else p
            out.setdefault(path, set())
        elif path is not None and line.startswith("---"):
            continue                            # the old-file header, and never a body line
        elif path is not None and line.startswith("@@"):
            # `@@ -12,3 +14,5 @@` - the third field is the NEW file's range, and the count is
            # optional, meaning one. Split rather than a regex: this file imports nothing it
            # can avoid, and a header is three fields separated by spaces.
            parts = line.split()
            if len(parts) < 3 or not parts[2].startswith("+"):
                continue
            span = parts[2][1:].split(",")
            try:
                start = int(span[0])
                count = int(span[1]) if len(span) > 1 else 1
            except ValueError:
                ln = None
                continue                        # not a hunk header after all
            ln = start
            new_left = count
            # The old-file span, for knowing where the body ends. `@@ -12,3 +14,5 @@` - the
            # second field, count optional and meaning one.
            old_span = parts[1][1:].split(",") if parts[1].startswith("-") else ["0", "0"]
            try:
                old_left = int(old_span[1]) if len(old_span) > 1 else 1
            except ValueError:
                old_left = 0
    return {k: v for k, v in out.items() if v}


def _enclosing_def(path, lines):
    """The INNERMOST function definition containing each line, as {line: def-line}.

    Innermost, so a change inside a nested helper is attributed to the helper rather than to
    everything it happens to sit inside. A line that no function contains is left out - module
    level is a different answer, not a missing one.

    The range starts at the first DECORATOR, not at the `def`. `@app.route("/pay")` is the most
    consequential line in a web handler and it sits above the def, so a range that began at the
    def called editing it a module-level risk: alarming, and less useful than naming the
    function it belongs to. The node itself still reports the `def` line, which is what
    somebody wants to open.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError, RecursionError):
        return {}
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", None) or node.lineno
        start = min([node.lineno] + [d.lineno for d in node.decorator_list
                                     if hasattr(d, "lineno")])
        for ln in lines:
            if start <= ln <= end:
                prev = found.get(ln)
                # A later, deeper definition wins: both enclose the line, and the one that
                # starts last is the one written closest to it. Compared on the DEF line, which
                # is what gets stored - two nested defs cannot share one.
                if prev is None or node.lineno > prev:
                    found[ln] = node.lineno
    return found


def _ranges(numbers):
    """[1,2,3,7,9,10] -> "1-3, 7, 9-10". A function edited in one place produced a row of
    thirty-five line numbers, which is not something anybody reads."""
    nums = sorted(numbers)
    if not nums:
        return ""
    out, start, prev = [], nums[0], nums[0]
    for n in nums[1:] + [None]:
        if n == prev + 1:
            prev = n
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    return ", ".join(out)


def _is_code(path, line_no):
    """Whether a line carries anything that runs. A blank line and a comment do not.

    Read once per file by the caller's loop over a handful of lines; opening the file again per
    line would be silly on a large diff, but the numbers here are the lines somebody edited,
    not the lines in the file.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            for i, text in enumerate(fh, 1):
                if i == line_no:
                    stripped = text.strip()
                    return bool(stripped) and not stripped.startswith("#")
    except OSError:
        return True                             # unreadable: assume it matters, do not drop it
    return False


def changed(g, diff, root="."):
    """What the edits in a diff would reach, without anybody naming a function first.

    `impact` answers for a name you already know; this finds the names. It reads a diff rather
    than running git, because this file starts no processes and imports nothing outside the
    standard library - a property people rely on when they copy it into their own repository,
    and not worth spending to save a pipe.

    The file it reads is the one ON DISK, which is the NEW side of the diff - true for
    uncommitted work, and the thing to know if you ever feed it a diff of something else.

    Three lists come back, and the two that are not `touched` are the point. A changed line at
    module level runs on import and can reach anything in the file; a changed file the graph
    never read has no answer at all. Folding either into "nothing depends on this" would be the
    one reply that must never be given quietly.
    """
    by_file = {}
    for n in g["nodes"]:
        if n["kind"] == "module" and n.get("file"):
            by_file[os.path.realpath(n["file"])] = n["id"]
    defs_by_line = {}
    for n in g["nodes"]:
        if n["kind"] == "func":
            defs_by_line[(n["module"], n["line"])] = n["id"]

    touched, module_level, unknown = {}, [], []
    for rel, lines in sorted(_diff_lines(diff).items()):
        if not rel.endswith(".py"):
            continue                            # a README is not a call graph question
        full = os.path.realpath(os.path.join(root, rel))
        mid = by_file.get(full)
        if mid is None:
            unknown.append(rel)                 # new, unscanned, or outside the roots
            continue
        # A blank line or a pure comment changes no behaviour, so it is neither a touched
        # function nor a module-level risk. It used to fall through to "module level, runs on
        # import, can reach anything in the file" - which is alarming, and false about a
        # comment. A trailing comment is not in the AST at all, so it has no enclosing
        # function to be attributed to either.
        lines = {ln for ln in lines if _is_code(full, ln)}
        enclosing = _enclosing_def(full, lines)
        for ln in sorted(lines):
            def_line = enclosing.get(ln)
            node_id = defs_by_line.get((mid, def_line)) if def_line else None
            if node_id is None:
                module_level.append(f"{rel}:{ln}")
            else:
                touched.setdefault(node_id, set()).add(ln)

    answers = []
    for node_id in sorted(touched):
        answers.append({"id": node_id, "lines": sorted(touched[node_id]),
                        "callers": callers_of(g, node_id),
                        "blast": blast_radius(g, node_id),
                        "sites": sites(g, node_id)})
    return {"touched": answers, "module_level": module_level, "unknown_files": unknown}


def symbols(g):
    """Every function and class the tree defines, as (id, file:line, kind).

    There was no way to ask what is IN a codebase. `find` needs a substring and refuses a
    blank one - correctly, since an unset shell variable must not match everything - so
    surveying a tree meant reading codegraph.json by hand. Which is what I did, repeatedly,
    while trying to rank this project's functions by how far a change to each would reach.
    """
    files = _files(g)
    return sorted((n["id"], f"{files.get(n['module'], n['module'] + '.py')}:{n['line']}", n["kind"])
                  for n in g["nodes"] if n["kind"] in ("func", "class"))


# Names Python itself reaches without anyone writing the call: a test runner finds `test_*`
# and its fixtures, an ast.NodeVisitor dispatches to `visit_*`, http.server calls `do_GET`,
# and a callback convention gives you `on_*` and `handle_*`. Conventions, not language rules,
# so this list is a reason to LABEL a name rather than to hide it.
_DISPATCHED_EXACT = frozenset((
    "setUp", "tearDown", "setUpClass", "tearDownClass", "setUpModule", "tearDownModule",
    "runTest", "generic_visit", "main", "handle", "run",
))
# `test`, not `test_`. unittest.TestLoader().testMethodPrefix IS "test", so every `testFoo`
# in a TestCase is collected and run exactly like every `test_foo` - and the camelCase spelling
# is what most of the standard library uses. Matching only the underscored form offered 1,316
# methods that run on every CI job as safe to delete: a third of the whole list, and the exact
# false positive this was rewritten to stop, surviving in the half of the convention nobody
# checked against the library itself. A test pins it to unittest's own value now.
_DISPATCHED_PREFIX = ("visit_", "test", "do_", "on_", "handle_")


def _dispatched_by_convention(name):
    return name in _DISPATCHED_EXACT or name.startswith(_DISPATCHED_PREFIX)


def unused(g):
    """Every function definition nothing in this tree calls, each with the reason it is here.

    This number used to be a lie by omission. Run against this repo's own source it named 577
    of 734 functions, and not one of them was dead: 505 unittest test methods, 46 fixtures, 22
    `visit_*` methods of its own AST walker, a callback handed to `signal.signal`, one handed
    to `subprocess`, an alias assigned to four other names, and a `generic_visit` override.
    Printed as a bare count in `stats`, that is a number somebody either deletes live code on
    or stops believing - and both are worse than saying nothing.

    So each row now carries WHY, which is the same honesty the edge labels already have:

      ""                    nothing here calls it and nothing here names it. The finding.
      "python calls this"   a dunder. Yours to write, Python's to call.
      "named, not called"   the tree mentions the name without calling it - a dispatch table,
                            a callback argument, an alias. Reached, just not written as a call.
      "inherited interface" its class has a base this tree cannot see, so the method may be
                            something that base calls: do_GET, generic_visit, setUp.
      "looks dispatched"    the name follows a convention a framework dispatches on.

    Nothing is filtered out. A shorter list would be a different lie.
    """
    called = {e["dst"] for e in g["calls"] if e.get("dst")}
    mentioned = set(g.get("mentioned") or ())
    files = _files(g)
    # A class whose base is not a class in this tree may be handed its methods by that base -
    # and inheritance is transitive, so the question is too. `class Fake(server.Handler)` where
    # `server.Handler(http.server.SimpleHTTPRequestHandler)` has a base that IS in the tree, so
    # asking only about its own bases said it was not foreign, and every override of a method
    # the external GRANDparent calls came back as reached by nothing.
    known = {n["name"] for n in g["nodes"] if n["kind"] == "class"}
    parents = {}
    for n in g["nodes"]:
        if n["kind"] == "class":
            parents.setdefault(n["name"], set()).update(
                b.split(".")[-1] for b in (n.get("bases") or ()))

    def _inherits_from_outside(name, seen):
        if name in seen:
            return False                        # a cycle in the bases is expressible; stop
        seen.add(name)
        return any(base not in known or _inherits_from_outside(base, seen)
                   for base in parents.get(name, ()))

    foreign_names = {nm for nm in parents if _inherits_from_outside(nm, set())}
    foreign = {n["id"] for n in g["nodes"]
               if n["kind"] == "class" and n["name"] in foreign_names}
    out = []
    for n in g["nodes"]:
        if n["kind"] != "func" or n["id"] in called:
            continue
        name = n["name"]
        owner = n["id"].rsplit(".", 1)[0]
        if name.startswith("__") and name.endswith("__"):
            why = "python calls this"
        elif name in mentioned:
            why = "named, not called"
        elif owner in foreign:
            why = "inherited interface"
        elif _dispatched_by_convention(name):
            why = "looks dispatched"
        else:
            why = ""
        out.append((n["id"], f"{files.get(n['module'], n['module'] + '.py')}:{n['line']}", why))
    return sorted(out)


def stats(g):
    kinds = defaultdict(int)
    for n in g["nodes"]: kinds[n["kind"]] += 1
    conf = defaultdict(int)
    for e in g["calls"]: conf[e.get("confidence", "?")] += 1
    defs = [n["id"] for n in g["nodes"] if n["kind"] == "func"]
    # Split, not summed. "Nothing calls this" was reported as one number and was wrong 100% of
    # the time on this repo's own source, because almost every name on it was reached some way
    # other than a written call. `unused` says which is which; this counts the same two piles.
    rows = unused(g)
    unreachable = [i for i, _w, why in rows if not why]
    by_name = len(rows) - len(unreachable)
    # Calls resolved to ONE definition. This used to add up three named labels by hand, so
    # SELF-METHOD and TYPED edges - which resolve to exactly one def - went uncounted, and the
    # rate was understated. Worse, it was a list that had to be edited every time a label was
    # added: INHERITED and CLASS would have been silently missing too. Ask the edge instead.
    specific = sum(1 for e in g["calls"] if e.get("dst"))
    # The rate is over what was WINNABLE. Counting builtins and stdlib in the denominator
    # measures how much of the standard library you happen to use; leaving out the untyped
    # method calls makes it tautologically 1.0, since almost everything else resolves by
    # definition. Winnable = resolved + ambiguous + the method calls it could not type.
    winnable = specific + conf["AMBIGUOUS"] + conf["UNTYPED"]
    # Edges are relationships; sites are places in the source. They are different numbers, and
    # reporting only the first hid the fact that one edge can stand for a dozen call sites.
    sites_total = sum(len(e.get("lines") or [e.get("line")]) for e in g["calls"])
    return {"nodes": dict(kinds), "unreadable_files": len(g.get("unreadable", [])),
            "call_edges": len(g["calls"]), "call_sites": sites_total,
            "edge_confidence": dict(conf),
            "resolved_to_one_def": specific,
            "could_have_been_resolved": winnable,
            "resolution_rate": round(specific / max(winnable, 1), 3),
            "func_defs": len(defs),
            # Named for what it can actually know. "in tree" read as "anywhere", and the
            # scanned roots are not anywhere - a sibling package nobody pointed this at calls
            # plenty of these.
            "not_called_in_scanned_roots": len(unreachable),
            "uncalled_but_reachable_by_name": by_name}


def _explain(g, exc):
    """Print an Ambiguous the way a person needs to see it: both names, and where they live."""
    where_ = {n["id"]: f"{n['module']}.py:{n['line']}" for n in g["nodes"]}
    print(f"{exc.name!r} names {len(exc.candidates)} definitions - say which:", file=sys.stderr)
    for i in exc.candidates:
        print(f"    {i}  {where_.get(i, '')}", file=sys.stderr)


def _located(g, ids):
    """Each id with where it is defined, for a refusal that has to be machine-readable."""
    at = {n["id"]: f"{_files(g).get(n['module'], n['module'] + '.py')}:{n['line']}"
          for n in g["nodes"] if n["id"] in set(ids)}
    return [(i, at.get(i, "?")) for i in sorted(ids)]


def _emit(obj):
    """One shape for every answer: what was asked, and what came back."""
    print(json.dumps(obj, indent=2))


def _sig(node):
    """One definition's signature, written the way the source writes it.

    Rebuilt from the tree rather than sliced out of the text, because a signature can span
    lines, carry comments between them, and end in the middle of one.
    """
    a = node.args
    parts = []
    pos = list(a.posonlyargs) + list(a.args)
    defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    for i, (arg, default) in enumerate(zip(pos, defaults)):
        text = arg.arg
        if arg.annotation is not None:
            text += ": " + ast.unparse(arg.annotation)
        if default is not None:
            text += (" = " if arg.annotation is not None else "=") + ast.unparse(default)
        parts.append(text)
        if a.posonlyargs and i == len(a.posonlyargs) - 1:
            parts.append("/")
    if a.vararg is not None:
        parts.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        parts.append("*")                            # the bare star: what follows is keyword-only
    for arg, default in zip(a.kwonlyargs, a.kw_defaults):
        text = arg.arg
        if arg.annotation is not None:
            text += ": " + ast.unparse(arg.annotation)
        if default is not None:
            text += (" = " if arg.annotation is not None else "=") + ast.unparse(default)
        parts.append(text)
    if a.kwarg is not None:
        parts.append("**" + a.kwarg.arg)
    out = f"{node.name}({', '.join(parts)})"
    if node.returns is not None:
        out += " -> " + ast.unparse(node.returns)
    return out


def shape(path):
    """What a file offers, without the code in between.

    An agent asked to change one function reads the whole file to find it: a 900-line module
    costs 900 lines of context to learn six signatures. This is the signatures and their line
    numbers, and nothing else.

    Parsed on demand rather than stored in the graph, so the answer is right even when the
    graph is stale - and reading one file is cheaper than the staleness check would be.

    Decorators are kept, because they change how a thing is CALLED: `size` behind `@property`
    is an attribute, and calling it as a method is exactly the mistake an agent working from a
    signature list would make.
    """
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src, filename=path)
    lines = []

    def walk(body, depth):
        for node in body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            pad = "  " * depth
            for dec in node.decorator_list:
                lines.append(f"{node.lineno:>6}  {pad}@{ast.unparse(dec)}")
            if isinstance(node, ast.ClassDef):
                bases = ", ".join(ast.unparse(b) for b in node.bases)
                head = f"class {node.name}({bases})" if bases else f"class {node.name}"
            else:
                kw = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
                head = kw + _sig(node)
            lines.append(f"{node.lineno:>6}  {pad}{head}")
            walk(node.body, depth + 1)

    walk(tree.body, 0)
    return _count_lines(src), lines


def _demote_test(node_id):
    """0 for the product, 1 for the suite. Orientation starts with what the code IS for.

    `_testing` and `_test_decorators` count too: on a clone of pandas the most-imported module
    in the whole tree was `pandas/_testing/__init__`, at 844 importers, and what imports it 844
    times is the test suite. Matched on a component CONTAINING `test` rather than equalling it,
    which is wide enough for those and narrow enough to leave `latest` and `contest` alone,
    because those are checked as whole components rather than as substrings of the path.

    Spelled without a regular expression because this file does not import `re` - it parses
    Python with `ast`, which is the point of it.
    """
    parts = node_id.replace("\\", "/").split("/")
    for p in parts:
        bare = p.strip("_")
        if bare in ("test", "tests", "testing", "conftest") or bare.startswith(("test_", "tests_")):
            return 1
    return 0


def repo_map(g, limit=12):
    """What this codebase IS, in one screen.

    The first question anybody asks about a tree they have not seen, and the one this tool
    could not answer. `stats` reports on the GRAPH - node counts, edge confidence, resolution
    rate - and every number in it is about how well the tool did its job rather than about the
    code.

    Ranked by how many modules import a module, because that is the thing the graph already
    knows and it is what "leans on" means. The output says so: a list in an order nobody
    explained is a list somebody has to reverse-engineer.
    """
    mods = [n for n in g["nodes"] if n["kind"] == "module"]
    ids = {m["id"] for m in mods}
    importers = defaultdict(set)
    for e in g["imports"]:
        # Counted from the PRODUCT. On a clone of pandas the most-imported module in the tree
        # was its testing helper, at 844 importers, and what imports it 844 times is the test
        # suite - true, and useless to somebody asking what the codebase IS. Import count alone
        # is a weak proxy for importance in any repository whose tests outnumber its code,
        # which is every well-tested one.
        if e["callee"] in ids and not _demote_test(e["src"]):   # stdlib is not the map either
            importers[e["callee"]].add(e["src"])
    called = Counter(e["callee"] for e in g["calls"])
    by_mod = defaultdict(list)
    for n in g["nodes"]:
        if n["kind"] in ("func", "class"):
            by_mod[n["module"]].append(n)
    out = ["ranked by how many NON-TEST modules in this tree import them:"]
    # A module that DEFINES nothing is not what anything leans on, whatever the import count
    # says. An empty `__init__.py` collects an import from every module in its package and so
    # ranked first - the top line of the map, pointing at a file with nothing in it.
    ranked = sorted((m for m in mods if by_mod.get(m["id"])),
                    key=lambda m: (_demote_test(m["id"]), -len(importers[m["id"]]), m["id"]))
    for m in ranked[:limit]:
        n_in = len(importers[m["id"]])
        defs = sorted(by_mod.get(m["id"], []), key=lambda d: -called.get(d["name"], 0))
        busiest = ", ".join(d["name"] for d in defs[:3])
        out.append(f"  {n_in:>3} importer(s)  {m['id']}   [{busiest}]")
    if len(ranked) > limit:
        out.append(f"  ...and {len(ranked) - limit} more module(s)")
    # WHERE IT STARTS. A definition nothing in the tree calls is either an entry point or dead,
    # and `unused` already tells you which; here it is the shortlist worth reading first.
    names_called = {e["callee"] for e in g["calls"]}
    starters = ("main", "run", "cli", "start", "serve", "app")

    def module_level(n):
        # `tests/test_cli.test_appgroup_app_context.cli` is a function defined INSIDE a test
        # function, and it topped this list on a clone of flask while flask's own
        # `src/flask/cli.main` was absent. A closure called by the function that made it is not
        # where anything starts.
        return "." not in n["id"][len(n["module"]) + 1:]

    found = [n for n in g["nodes"]
             if n["kind"] == "func" and n["name"] in starters
             and n["name"] not in names_called and module_level(n)]
    # A `main` in a test module is a real thing to find and is not what somebody opening an
    # unfamiliar repository wants first.
    entries = [n["id"] for n in sorted(found, key=lambda n: (_demote_test(n["id"]), n["id"]))]
    if entries:
        out.append("")
        out.append("nothing in this tree calls these, so this is where it starts:")
        out += [f"  {e}" for e in entries[:8]]
    return out


# ------------------------------------------------------------------ a door an agent can knock on
#
# Everything above this line is a thing a PERSON runs. An agent has to shell out for it and then
# parse prose, which is the difference between a tool used a few times a day and one leaned on
# constantly.
#
# MCP is the door: line-delimited JSON-RPC over stdin and stdout. No dependency, no network, no
# daemon - the same graph, asked a different way. stdout IS the protocol, so nothing here may
# print anything that is not a reply.

# The MCP handshake carries a protocol version, and the spec's identifiers are shaped like
# dates. This one is not a date about this repository - nothing here was written on it, and it
# changes when the protocol does, not when somebody sits down at a keyboard. It lives in one
# named constant so the no-dates rule can make a single narrow exception it can see, rather
# than a general one it cannot.
# What a client shows the model once, at the top of a session. A tool an agent does not think
# to call is a tool that does not exist, and ten good descriptions do not say which to reach for
# first. It is spent from the model's context every session, so it buys the ORDER and the one
# rule that matters, and leaves everything else to the tools' own descriptions.
MCP_INSTRUCTIONS = (
    "codegraph answers structural questions about the PYTHON in this repository, from a graph "
    "it builds by parsing it. It reads no other language.\n"
    "\n"
    "In a repository you have not seen, ask codegraph_repo_map first: it names the modules "
    "everything leans on and where execution starts.\n"
    "Before reading a file you only need the shape of, ask codegraph_shape - signatures and "
    "line numbers, no bodies.\n"
    "Before changing a shared function, ask codegraph_blast: it is the transitive callers, not "
    "just the direct ones.\n"
    "After editing and BEFORE saying the work is done, hand codegraph_changed your `git diff`. "
    "It finds the names itself, so you do not have to know what to ask about.\n"
    "\n"
    "One answer to read carefully: `nothing calls X` is not the same as `X is unreachable`. "
    "The reply says which, because a function reached through a dispatch table or a decorator "
    "has no callers this graph can name and deleting it still breaks the code."
)

MCP_PROTOCOL = "2024-11-05"  # scrub: fixture -- a protocol identifier, not a date

MCP_TOOLS = {
    "codegraph_callers": ("who calls this function or class, by exact id",
                          "name", lambda g, t, _: callers_of(g, t)),
    "codegraph_calls": ("what this function calls",
                        "name", lambda g, t, _: calls_from(g, t)),
    "codegraph_blast": ("everything that could break if you change this - transitive callers, "
                        "not just direct ones. Ask this BEFORE editing a shared function",
                        "name", lambda g, t, _: blast_radius(g, t)),
    "codegraph_sites": ("every call site as file:line - all of them, not a sample",
                        "name", lambda g, t, _: [f"{loc}  {via}" for loc, via in sites(g, t)]),
    "codegraph_where": ("where a symbol is defined, as file:line",
                        "name", lambda g, t, _: [f"{i}  {loc}" for i, loc in where(g, t)]),
    "codegraph_find": ("fuzzy search for a symbol when you only know part of the name",
                       "name", lambda g, _t, raw: find(g, raw)),
    "codegraph_path": ("a call path connecting two functions, if one exists",
                       "name", None),
    "codegraph_shape": ("what a file offers - every signature and its line number, without the "
                        "bodies. Ask this INSTEAD of reading a file you only need the shape of",
                        "name", None),
    "codegraph_repo_map": ("what this codebase is: the modules everything leans on, ranked by "
                           "how many others import them, and where execution starts. Ask this "
                           "FIRST in a repository you have not seen",
                           None, None),
    "codegraph_changed": ("give it a unified diff and it says what those edits would break - "
                          "it finds the names, so you do not have to know what to ask about. "
                          "Ask this straight after editing, BEFORE saying the work is done",
                          "diff", None),
}


def _shape_target(g, raw):
    """A path if it is one, otherwise the file a module id names.

    An agent that has been reading graph output has ids, not paths, and one that has been
    reading a diff has paths. Both are the obvious thing to type.
    """
    if os.path.isfile(raw):
        return raw
    for n in g["nodes"]:
        if n["kind"] == "module" and n["id"] in (raw, raw.replace(".", "/")):
            return n["file"]
    return None


def _count_lines(text):
    """How many lines a file has. Counting the newlines and adding one is right for a file whose
    last line has no newline after it, and wrong for every file an editor saved - which is
    nearly all of them, so nearly every line count this tool published was one too many. `shape`
    called a two-line file three lines, and the saving footer took its baseline from the same
    number, which turned a one-line answer into a saving."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _lines_of(paths):
    """(files sized, total lines). A file that cannot be read is left OUT rather than guessed.

    A graph can outlive the files it names, and an estimate built on a file that is gone is a
    wrong number. A smaller baseline is the honest answer.
    """
    n = total = 0
    for p in dict.fromkeys(paths):
        try:
            with open(p, "rb") as fh:
                total += _count_lines(fh.read().decode("utf-8", "replace"))
            n += 1
        except OSError:
            continue
    return n, total


def _files_for(g, ids):
    """The files somebody would have opened to learn what this answer told them."""
    by_id = {n["id"]: n.get("file") for n in g["nodes"]}
    out = []
    for i in ids:
        f = by_id.get(i) or by_id.get(_module_of(i))
        if f:
            out.append(f)
    return out


def _unread_warning(g):
    """What this answer could not see.

    The graph is never STALE - every query rebuilds it if the tree moved. The gap is next door
    and worse: a file that will not parse is skipped, the build records it, the command line
    prints `skipped ...` to stderr, and over MCP it vanished entirely. So an agent halfway
    through an edit asks who calls a function, gets a complete-looking list, and never learns
    that one file was not read - which is exactly where the caller it is about to break would be.

    It matters MOST when the answer is empty: "no callers" and "no callers, and one file was
    not read" are different facts, and somebody acts on the first by deleting something.
    """
    bad = list(g.get("unreadable") or [])
    if not bad:
        return ""                                    # on every answer it would be wallpaper
    shown = [str(b).split(":")[0] for b in bad[:3]]
    more = f", and {len(bad) - 3} more" if len(bad) > 3 else ""
    return (f"\n\nWARNING: {len(bad)} file(s) in this tree could not be parsed and were not "
            f"read, so this answer may be incomplete: {', '.join(shown)}{more}")


MCP_MAX_ROWS = 120               # measured: see `_capped`


NARROW_WITH_FILTER = ("That is most of this tree, which is the answer: it is used everywhere. "
                      "To see a part of it, ask again with `filter` - a glob over the id or "
                      "its module, like `pkg/thing*` or `*/api/*`.")
NARROW_THE_SEARCH = ("A substring that matches this much is not a search. Ask again with a "
                     "longer one, or use `codegraph_where` if you already know the name.")
NARROW_THE_DIFF = ("That diff touches most of the tree. Ask about a part of it, or about one "
                   "file at a time.")


def _capped(rows, how=NARROW_WITH_FILTER):
    """At most `MCP_MAX_ROWS` of them, and it says when it cut.

    `blast` on `find_stack_level` in a clone of pandas returns 11,200 lines - 795,039
    characters, near enough 199,000 tokens. That does not fill an agent's context, it ends the
    conversation. A person has `--only`, `--exclude` and a pipe to `head`; an agent gets
    whatever comes back, in one message, and cannot recover from it.

    Silence would be worse than the flood. An answer cut off without a word is one an agent
    reads as complete, and `120 of 11,200` is a different fact from `120`. So the count is
    first, the way to narrow is last, and the names are in between - because when a list is
    that long, `this function is used everywhere` is what it actually means.
    """
    rows = [str(r) for r in rows]
    if len(rows) <= MCP_MAX_ROWS:
        return "\n".join(rows)
    head = f"{len(rows)} results, showing the first {MCP_MAX_ROWS}:"
    tail = "\n\n" + how
    return "\n".join([head] + rows[:MCP_MAX_ROWS]) + tail


def _where_they_live(g, ids):
    """Which files the answers are in - a fact, not a comparison.

    The saving footer belongs where the baseline is real: `shape` replaces opening a file you
    were going to open. A list of callers replaces a search, and comparing it to reading whole
    files nobody would have read is a claim that flatters the tool.
    """
    files = {os.path.basename(p) for p in _files_for(g, ids)}
    if not files:
        return ""
    shown = ", ".join(sorted(files)[:4])
    more = f", +{len(files) - 4} more" if len(files) > 4 else ""
    return f"\n\n{len(ids)} result(s), in {len(files)} file(s): {shown}{more}"


def _saving(answer_lines, paths):
    """One line saying what this replaced, with its baseline written down.

    codegraph has always reported its resolution rate - a number about itself - and never the
    number a person cares about: how much reading this answer stood in for. An agent with no
    way to know that asking was cheaper than opening the files opens the files anyway.

    The baseline is an ASSUMPTION - that you would otherwise have read those files whole - so
    it is stated rather than implied. A saving with no stated baseline is a marketing number.
    """
    files, total = _lines_of(paths)
    if not files or total <= answer_lines:
        return ""                                    # nothing honest to claim
    return (f"\n\n{answer_lines} line(s) here, instead of reading {files} file(s) whole "
            f"({total} lines).")


def _why_not_called(g, target):
    """The sentence that goes after `nothing calls this`.

    `unused` already works this out for the whole tree and gives each row a reason: a dunder
    Python calls, a name the tree mentions without calling, a method whose base class this tree
    cannot see. Read here for one definition, because "nothing calls it" and "nothing reaches
    it" are different facts and only one of them means it is safe to delete.
    """
    # BEFORE anything else: is the bare name the callee of calls this graph could not resolve?
    # Asked flask who calls `abort`, it said "nothing names it either" while twenty edges in
    # the same graph named it - `flask.abort(404)`, reaching a function `__init__.py`
    # re-exports, which is the shape of nearly every Python library API there is. The limit is
    # fair; the sentence was not, and an agent reading it deletes what the library exports.
    bare = target.rsplit(".", 1)[-1]
    loose = [e for e in g["calls"] if e.get("callee") == bare and e.get("src") != target]
    if loose:
        who = ", ".join(sorted({e["src"] for e in loose})[:3])
        recv = next((e["recv"] for e in loose if e.get("recv")), None)
        through = f" through `{recv}.{bare}`" if recv else ""
        return (f"But `{bare}` is called {len(loose)} time(s){through} by calls this graph "
                f"could not resolve to a definition - {who}. Read this as unresolved, not as "
                "unreached.")
    for node_id, _where, reason in unused(g):
        if node_id == target:
            if not reason:
                return ("Nothing in this tree names it either, so as far as this graph can "
                        "see it is unreached.")
            return (f"It is still reached some other way: {reason}. Deleting it would break "
                    "whatever does that.")
    # Not in `unused` at all means something DOES reach it - a base class, a decorator - and
    # the honest answer is that this graph cannot name the caller rather than that there is none.
    return ("Something reaches it that is not a call this graph can name, so do not read this "
            "as unreached.")


def _mcp_result(text):
    return {"content": [{"type": "text", "text": text}]}


_SERVED = None                   # the graph this server process has already read


def _served():
    """The graph, parsed once for the life of the server rather than once per question.

    19MB of JSON on a clone of ansible, 86ms to read, against an answer that costs 2.7ms. A
    command pays that once and exits, which is why it was never worth noticing; a server is the
    whole point of the door, and an agent asking twenty questions paid it twenty times.

    What is NOT cached is whether it is still true. The code changes under the agent constantly
    - that is the entire reason it is asking - so freshness is decided again on every question,
    and the parse is skipped only while what it parsed is still current. When it is not,
    `load` rebuilds and the new graph is what gets kept.
    """
    global _SERVED
    if _SERVED is None or _is_stale(_SERVED):
        _SERVED = load()
    return _SERVED


def _mcp_call(tool, args):
    """Run one tool and return its MCP result.

    Every failure comes back as TEXT rather than a protocol error, because a protocol error is
    something the agent reports to the user and stops on, and "no graph yet, run build" is
    something it can act on by itself.
    """
    if tool not in MCP_TOOLS:
        return None
    try:
        g = _served()
    except SystemExit as e:
        # `load` exits the process when there is no graph. That is right for a command and
        # fatal for a server, and the message it exits with is the one the agent needs.
        return _mcp_result(str(e) or "no graph yet - run: codegraph build <path>")
    if tool == "codegraph_repo_map":
        return _mcp_result("\n".join(repo_map(g)) + _unread_warning(g))
    if tool == "codegraph_changed":
        diff = args.get("diff") or ""
        if not diff.strip():
            return _mcp_result("this tool needs a `diff` - the output of `git diff`, or "
                               "`git diff --cached` for staged work")
        got = changed(g, diff)
        out = []
        for t in got["touched"]:
            who = ", ".join(t["callers"][:6]) or "(none)"
            more = f", +{len(t['callers']) - 6} more" if len(t["callers"]) > 6 else ""
            out.append(f"{t['id']}  (line{'s' if len(t['lines']) > 1 else ''} "
                       f"{_ranges(t['lines'])})")
            out.append(f"  callers: {who}{more}")
            out.append(f"  blast:   {len(t['blast'])} function(s) downstream")
        if not got["touched"]:
            out.append("no changed line is inside a function this graph knows")
        out = _capped(out, NARROW_THE_DIFF).splitlines()
        # The two that are NOT `touched` are the point, and over MCP silence is the only way
        # they could be lost - the command line prints them on stderr, which nothing here has.
        if got["module_level"]:
            out.append(f"\n{len(got['module_level'])} changed line(s) at MODULE LEVEL, which "
                       "run on import and can reach anything in the file: "
                       + ", ".join(got["module_level"][:6]))
        if got["unknown_files"]:
            out.append(f"\n{len(got['unknown_files'])} changed file(s) this graph NEVER READ, "
                       "so there is no answer for them at all: "
                       + ", ".join(got["unknown_files"][:6]))
        return _mcp_result("\n".join(out) + _unread_warning(g))
    raw = (args.get("name") or args.get("query") or "").strip()
    if not raw:
        return _mcp_result("this tool needs a `name`")
    if tool == "codegraph_path":
        to = (args.get("to") or "").strip()
        if not to:
            return _mcp_result("codegraph_path needs `name` and `to`")
        got = path(g, raw, to)
        return _mcp_result(" -> ".join(got) if got else f"no call path from {raw} to {to}")
    if tool == "codegraph_shape":
        found = _shape_target(g, raw)
        if found is None:
            return _mcp_result(f"no file or module named {raw!r}")
        try:
            total, out = shape(found)
        except (OSError, SyntaxError, ValueError) as e:
            return _mcp_result(f"cannot read {raw}: {e}")
        head = f"{raw}  -  {len(out)} definition(s) of {total} lines"
        body = "\n".join([head] + out) if out else head + "\n(no definitions)"
        return _mcp_result(body + _saving(len(out) + 1, [found]) + _unread_warning(g))
    if tool == "codegraph_find":
        # `find` returns (id, file:line) PAIRS. Joining them as strings worked only while it
        # found nothing, which is exactly what every test of it had asked for - and a match
        # with no location is a match nobody can open.
        # Capped like the rest. `find` returned early with its own text and never reached the
        # cap, so a one-letter query on a clone of pandas came back with 3,668,564 characters -
        # about 917,000 tokens, four and a half times worse than the answer that motivated
        # capping anything. Fixing one path and not the others is how a cap becomes decoration.
        found = find(g, raw)
        if not found:
            return _mcp_result(f"nothing matching {raw!r}")
        return _mcp_result(_capped([f"{i}  {loc}" for i, loc in found], NARROW_THE_SEARCH))
    # `_describe` is the CLI's own answer to "how does this graph know that name", and the
    # three ways it can fail are three different things to tell an agent. Never "(none)": an
    # agent that reads an empty answer for a misspelled name concludes nothing depends on it,
    # which is the exact wrong conclusion and the one this tool exists to prevent.
    kind, hits = _describe(g, raw)
    if kind == "unknown":
        near = find(g, raw)[:5]
        hint = ("  Did you mean: " + ", ".join(near)) if near else ""
        return _mcp_result(f"no symbol named {raw!r} in this graph.{hint}"
                           + _unread_warning(g))
    if kind == "called":
        return _mcp_result(f"{raw!r} is called here but defined outside this tree "
                           "(the standard library, or a package you installed), so this graph "
                           "has nothing to say about its callers.")
    if kind == "module":
        return _mcp_result(f"{raw!r} is a module, not a function or a class. "
                           "For a module ask about its imports instead.")
    if len(hits) > 1:
        return _mcp_result(f"{raw!r} is ambiguous - it names {len(hits)} definitions, and "
                           "merging them would overstate the answer. Ask again with one of "
                           "these exact ids:\n" + "\n".join(hits))
    target = hits[0]
    out = MCP_TOOLS[tool][2](g, target, raw)
    keep = (args.get("filter") or "").strip()
    if keep and out:
        # The same idea as the command line's `--only`, which has existed for months and had no
        # door handle. Matched against the id and against its module, because both are what
        # somebody reaches for.
        narrowed = [i for i in out
                    if fnmatch.fnmatch(str(i), keep)
                    or fnmatch.fnmatch(_module_of(str(i)), keep)]
        if not narrowed:
            # NOT "nothing calls this". A filter that excluded everything is a fact about the
            # filter, and saying the other thing here would be this tool's worst answer given
            # for a reason that has nothing to do with the code.
            return _mcp_result(f"{len(out)} result(s), and none of them match filter "
                               f"{keep!r}. The ids look like `pkg/module.function`, so a glob "
                               "over the module is usually what you want: `pkg/thing*`."
                               + _unread_warning(g))
        out = narrowed
    if not out:
        # THE MOST DANGEROUS ANSWER THIS TOOL CAN GIVE. A function reached through a dispatch
        # table and one that is genuinely dead both had nothing calling them, and both got
        # `(nothing for ...)` - so an agent cleaning up dead code deletes the dispatched one
        # and breaks the table that names it.
        #
        # The command line has always known the difference: `unused --all` prints the reason.
        # The door threw it away, which is this tool's own founding failure arriving through
        # its newest part.
        return _mcp_result(f"nothing calls {target}. " + _why_not_called(g, target)
                           + _unread_warning(g))
    # NO SAVING CLAIMED HERE, and that is a correction of my own work from this morning. The
    # footer said `4 line(s) here, instead of reading 1 file(s) whole (3921 lines)` - and
    # nobody learns who calls a function by reading a 3,921-line file end to end. They grep,
    # and grep shows them four lines. The number was true and the sentence was false, which is
    # the thing `a saving with no stated baseline is a marketing number` was written to stop.
    #
    # What IS true and worth saying: where the answers live, so the next step is one file away.
    return _mcp_result(_capped(out) + _where_they_live(g, out) + _unread_warning(g))


def _mcp_serve(stream_in=None, stream_out=None):
    """Speak MCP until stdin closes.

    A malformed line is skipped rather than fatal: a client that writes half a line must not
    end the session, because the agent has no way to tell a crashed server from a slow one.
    """
    stream_in = stream_in or sys.stdin
    stream_out = stream_out or sys.stdout

    def send(msg):
        stream_out.write(json.dumps(msg) + "\n")
        stream_out.flush()

    def schema(n):
        if n == "codegraph_path":
            return ({"name": {"type": "string"}, "to": {"type": "string"}}, ["name", "to"])
        if n == "codegraph_repo_map":
            return ({}, [])                          # it asks about the whole tree, not a name
        if n == "codegraph_changed":
            return ({"diff": {"type": "string"}}, ["diff"])
        if n in ("codegraph_blast", "codegraph_callers", "codegraph_calls", "codegraph_sites"):
            return ({"name": {"type": "string"},
                     "filter": {"type": "string",
                                "description": "optional glob over the id or its module, to "
                                               "narrow a very large answer"}}, ["name"])
        return ({"name": {"type": "string"}}, ["name"])

    tools = []
    for n, (d, _arg, _fn) in MCP_TOOLS.items():
        props, required = schema(n)
        tools.append({"name": n, "description": d,
                      "inputSchema": {"type": "object", "properties": props,
                                      "required": required}})

    for line in stream_in:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (ValueError, TypeError):
            continue                                 # not our sentence; wait for the next one
        if not isinstance(msg, dict):
            continue
        mid = msg.get("id")
        method = msg.get("method") or ""
        if mid is None:
            continue                                 # a notification takes no reply, and
                                                     # answering one hangs some clients
        try:
            _mcp_dispatch(send, mid, method, msg, tools)
        except Exception as e:                       # a server outlives its own bugs
            # AROUND THE WHOLE MESSAGE, not around the part I happened to be thinking about.
            # The guard for a failing TOOL wrapped the tool call, and `params` arriving as a
            # string crashed above it - `"nonsense".get("name")` raises before anything guarded
            # is reached, and the process died mid-session. An agent cannot tell a dead server
            # from a slow one, so it waits, and the session is over.
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32603,
                            "message": f"{method or 'request'} failed: "
                                       f"{type(e).__name__}: {e}"}})
    return 0


def _mcp_dispatch(send, mid, method, msg, tools):
    """One message, answered. Every exception in here is caught by the caller."""
    if True:
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"protocolVersion": MCP_PROTOCOL,
                             "capabilities": {"tools": {}},
                             "instructions": MCP_INSTRUCTIONS,
                             "serverInfo": {"name": "codegraph",
                                            "version": str(_VERSION)}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            try:
                got = _mcp_call(params.get("name"), params.get("arguments") or {})
            except Exception as e:                   # a server outlives its own bugs
                # One bad answer must cost ONE answer. An exception used to escape this loop
                # and end the process mid-session, so the next question got no reply at all -
                # and an agent cannot tell a dead server from a slow one. It found out the
                # hard way: `codegraph_find` joined tuples as strings and took the session
                # down with it.
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32603,
                                "message": f"{params.get('name')} failed: "
                                           f"{type(e).__name__}: {e}"}})
                return
            if got is None:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32602,
                                "message": f"no such tool: {params.get('name')!r}"}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": got})
        else:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": f"unknown method: {method!r}"}})


def _one_target(g, name, as_json=False):
    """(target, exit code). The target is None once the problem has been explained.

    Three failures a script has to tell apart, and they used to be one: 2 means you were vague
    and must choose, 1 means this graph has never heard the name. Answering "(none)" and
    exiting 0 for a typo is how an agent concludes nothing depends on a function it misspelled.

    A bare name that matches several definitions used to be MERGED silently: `impact digest`
    with a pulse.digest and a court.digest reported both callers and a blast radius of two,
    when the function being changed has one. That is the precise failure this tool exists to
    prevent - the README's opening argument is that grep cannot tell two same-named functions
    apart - reachable by typing the obvious thing, since nobody types a qualified id first.
    Worse than overstating: you go and "fix" a caller of the other function.
    """
    kind, _ = _describe(g, name)
    if kind == "module":
        # It IS in the graph - saying "never heard of it" was simply false, and unhelpful in
        # the same breath, since the command they wanted is one word away.
        if as_json:
            _emit({"error": "not a function", "name": name, "detail": "it is a module",
                   "try": f"codegraph deps {name}"})
        else:
            print(f"{name!r} is a module, not a function - try: codegraph deps {name}",
                  file=sys.stderr)
        return None, 1
    if kind == "unknown":
        # The refusals are answers too, and an agent should not have to read English to get
        # them. A misspelling and a function with no callers are the two things that must
        # never look alike, and telling them apart from prose means matching on a sentence.
        if as_json:
            _emit({"error": "unknown name", "name": name,
                   "detail": "nothing of that name is defined or called in this graph"})
        else:
            print(f"nothing named {name!r} is defined or called in this graph", file=sys.stderr)
        return None, 1
    if kind == "called":
        # A legitimate question - "where do we call json.load" - but say that it is what is
        # being answered, so an empty result is not mistaken for a function with no callers.
        print(f"note: {name!r} is called here but not defined here", file=sys.stderr)
        return name, 0
    try:
        ids = _one(g, name)
    except Ambiguous as exc:
        if as_json:
            _emit({"error": "ambiguous", "name": name,
                   "detail": f"{len(exc.candidates)} definitions answer to that name",
                   "candidates": [{"id": i, "at": loc} for i, loc in _located(g, exc.candidates)]})
        else:
            _explain(g, exc)
        return None, 2
    return ids[0], 0


def _main(argv=None):
    """The CLI, as a function so `pip install` can expose it as a console script -- and so the
    tests can drive it in-process instead of shelling out."""
    a = list(sys.argv[1:] if argv is None else argv)
    # --anywhere, because that is how people type it. The prose below is for a person; this
    # gives the same answers to whatever is going to parse them.
    as_json = "--json" in a
    a = [x for x in a if x != "--json"]
    # Opt-in on the command line, because every script anybody has written parses the current
    # output. Default only over MCP, where the reader is an agent deciding whether to open the
    # files anyway - which is the decision this number exists to inform.
    saved = "--saved" in a
    a = [x for x in a if x != "--saved"]
    show_all = "--all" in a          # `unused --all`: also the names that carry a reason
    a = [x for x in a if x != "--all"]
    # --only and --exclude, because the first real use of `unused` on a shipped repository
    # returned 337 lines of which 293 were test methods. unittest calls those by reflection, so
    # every one of them looks dead, and a list that is seven-eighths noise is one nobody reads
    # twice. Patterns are globs over the module id: `tests/*`, `*test*`, `boardofdirectors/*`.
    keep_pat, drop_pat = [], []
    rest = []
    it = iter(a)
    for tok in it:
        if tok in ("--only", "--exclude"):
            try: pat = next(it)
            except StopIteration:
                print(f"usage: codegraph ... {tok} <pattern>", file=sys.stderr); return 2
            (keep_pat if tok == "--only" else drop_pat).append(pat)
        else:
            rest.append(tok)
    a = rest

    def wanted(what):
        """True if this id survives --only and --exclude.

        Matched against the FULL id and against its module, because both are things a person
        reaches for: `tests/*` is a corner of the tree, `*.V.*` is a class inside one file.
        Matching only the module meant the second silently matched nothing - which I found by
        typing it and getting back the exact list I was trying to filter out.
        """
        names = (what, _module_of(what))
        if keep_pat and not any(fnmatch.fnmatch(n, p) for n in names for p in keep_pat):
            return False
        return not any(fnmatch.fnmatch(n, p) for n in names for p in drop_pat)

    def keep_ids(ids):
        return [i for i in ids if wanted(i)]
    # Every query verb takes a name. Forgetting it used to be an IndexError traceback - the
    # first thing a new user sees when they type a command from memory.
    NEEDS = {"callers": 1, "calls": 1, "blast": 1, "where": 1, "find": 1, "sites": 1,
             "impact": 1, "deps": 1, "path": 2}      # "unused" and "cycles" take no name
    if a and a[0] in NEEDS and (len(a) - 1 < NEEDS[a[0]]
                                or not all(x.strip() for x in a[1:1 + NEEDS[a[0]]])):
        # A BLANK argument is a missing one. `codegraph find "$NAME"` with NAME unset used to
        # print every symbol in the codebase and exit 0 - the empty pattern that matches
        # everything, reported as a successful search.
        what = {"path": "<from> <to>", "deps": "<module>", "find": "<substring>"}.get(a[0], "<name>")
        print(f"usage: codegraph {a[0]} {what}", file=sys.stderr)
        return 2
    if a and a[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0                                     # a help request is not an error, and
                                                     # `codegraph --help || echo failed` said
                                                     # failed because this used to exit 2
    if not a:
        # A bare invocation used to BUILD - walking whatever directory you happened to be
        # standing in and writing two files into it. Someone typing `codegraph` in their home
        # directory to see what it does deserves the usage, not a silent traversal of
        # everything they own.
        print(__doc__)
        return 0
    if a[0] == "--selftest": return _selftest()
    if a[0] == "mcp": return _mcp_serve()
    if a[0] == "map":
        g = load()
        out = repo_map(g)
        if as_json: _emit({"query": "map", "results": out})
        else: print("\n".join(out))
        return 0
    if a[0] == "shape":
        if len(a) < 2 or not a[1].strip():
            print("usage: codegraph shape <file or module>", file=sys.stderr)
            return 2
        target = a[1] if os.path.isfile(a[1]) else _shape_target(load(), a[1])
        if target is None:
            print(f"no file or module named {a[1]!r}")
            return 1
        try:
            total, out = shape(target)
        except OSError as e:
            print(f"cannot read {a[1]}: {e}")
            return 1
        except SyntaxError as e:
            # Not a traceback: a file that will not parse is a normal thing to meet halfway
            # through an edit, and the line it stopped at is the useful part.
            print(f"cannot parse {a[1]}: {e.msg} at line {e.lineno}")
            return 1
        if as_json:
            _emit({"query": "shape", "target": a[1], "lines": total, "definitions": out})
        else:
            print(f"{a[1]}  -  {len(out)} definition(s) of {total} lines")
            print("\n".join(out) if out else "(no definitions)")
            # The one query the saving footer was built for, and the one place on the command
            # line it did not appear: `shape` replaces opening a file you were going to open,
            # which is the only baseline honest enough to state. It has always been on over
            # MCP; `--help` described it here and nothing printed it.
            if saved:
                note = _saving(len(out) + 1, [target])
                if note: print(note.strip())
        return 0
    if a[0] == "build":
        try:
            g = build(a[1:] or None)
        except BadPath as e:
            print(f"cannot build: {e}", file=sys.stderr)
            return 1
        for bad in g.get("unreadable", []):
            print(f"skipped {bad}", file=sys.stderr)   # the reason carries the detail: a
                                                       # syntax error and a permission error
                                                       # are not the same complaint
        if not [n for n in g["nodes"] if n["kind"] == "module"]:
            # "no .py files found" directly contradicted the skip lines printed just above it
            # when the files were there and simply would not parse. Two different problems,
            # and only one of them is solved by pointing at a different directory.
            bad = len(g.get("unreadable", []))
            where_ = ", ".join(g["dirs"])
            print(f"found {bad} .py file(s) under {where_}, none of which could be read"
                  if bad else f"no .py files found under {where_}", file=sys.stderr)
            return 1
        print(json.dumps(stats(g), indent=2))
    elif a[0] in ("callers", "calls"):
        g = load()
        t, rc = _one_target(g, a[1], as_json)
        if t is None: return rc
        found = keep_ids(callers_of(g, t) if a[0] == "callers" else calls_from(g, t))
        if as_json: _emit({"query": a[0], "target": t[0] if isinstance(t, list) else t,
                           "results": found})
        else:
            print("\n".join(found) or "(none)")
            if saved and found:
                print(_where_they_live(g, found).strip())
    elif a[0] == "blast":
        g = load()
        t, rc = _one_target(g, a[1], as_json)
        if t is None: return rc
        found = keep_ids(blast_radius(g, t))
        if as_json: _emit({"query": "blast", "target": t[0] if isinstance(t, list) else t,
                           "results": found})
        else: print("\n".join(found) or "(none)")
    elif a[0] == "where":
        g = load()
        hits = [(i, loc) for i, loc in where(g, a[1]) if wanted(i)]
        if not hits and _describe(g, a[1])[0] == "module":
            # Every other verb explains this; `where` used to answer "(not found)" about a
            # module sitting right there in the graph.
            print(f"{a[1]!r} is a module, not a function - try: codegraph deps {a[1]}",
                  file=sys.stderr)
            return 1
        if as_json: _emit({"query": "where", "results": [{"id": i, "at": loc} for i, loc in hits]})
        else: print("\n".join(f"{i}  {loc}" for i, loc in hits) or "(not found)")
        if not hits: return 1                    # a search that matched nothing, like grep
    elif a[0] == "find":
        hits = [(i, loc) for i, loc in find(load(), a[1]) if wanted(i)]
        if as_json: _emit({"query": "find", "results": [{"id": i, "at": loc} for i, loc in hits]})
        else: print("\n".join(f"{i}  {loc}" for i, loc in hits) or "(none)")
        if not hits: return 1
    elif a[0] == "sites":
        g = load()
        t, rc = _one_target(g, a[1], as_json)
        if t is None: return rc
        found = [(loc, c) for loc, c in sites(g, t) if wanted(c)]
        if as_json: _emit({"query": "sites", "target": t[0] if isinstance(t, list) else t,
                           "results": [{"at": loc, "caller": c} for loc, c in found]})
        else: print("\n".join(f"{loc}  {c}" for loc, c in found) or "(none)")
    elif a[0] == "path":
        g = load()
        for end in (a[1], a[2]):                     # both endpoints, same rules as every verb
            t, rc = _one_target(g, end, as_json)
            if t is None: return rc
        p = path(g, a[1], a[2])
        if as_json: _emit({"query": "path", "from": a[1], "to": a[2], "results": p})
        else: print(" -> ".join(p) if p else "(no path)")
    elif a[0] == "deps":
        g = load()
        if not any(n["kind"] == "module" and n["id"] == a[1] for n in g["nodes"]):
            # "(none)/(none)" for a module that does not exist reads exactly like a module
            # with no dependencies, which is a different and much more reassuring fact.
            print(f"no module {a[1]!r} in the graph", file=sys.stderr)
            return 1
        im, imp = module_deps(g, a[1])
        if as_json: _emit({"query": "deps", "module": a[1], "imports": im, "importers": imp})
        else:
            print("imports:   " + (", ".join(im) or "(none)"))
            print("importers: " + (", ".join(imp) or "(none)"))
    elif a[0] == "cycles":
        cy = cycles(load())
        if as_json: _emit({"query": "cycles", "results": [list(group) for group in cy]})
        else:
            # A one-module group is a module that imports ITSELF. Printed as a bare name it
            # read like a cycle with one participant, which is not a thing.
            print("\n".join(" <-> ".join(group if len(group) > 1 else (*group, "itself"))
                            for group in cy) or "(none)")
    elif a[0] == "impact":
        g = load()
        t, rc = _one_target(g, a[1], as_json)
        if t is None: return rc
        im = impact(g, t)
        im = {"callers": keep_ids(im["callers"]),
              "sites": [(loc, c) for loc, c in im["sites"] if wanted(c)],
              "blast": keep_ids(im["blast"]),
              "unresolved": [(loc, c) for loc, c in im["unresolved"] if wanted(c)]}
        if as_json:
            # The prose says how MANY the blast radius holds; an agent asking what breaks needs
            # to be told WHICH, and the library has always returned them. Nothing is truncated
            # here either - the four-site preview below is a courtesy to a human reader, and a
            # courtesy is the wrong thing to hand a parser.
            _emit({"query": "impact", "target": t[0] if isinstance(t, list) else t,
                   "callers": im["callers"],
                   "sites": [{"at": loc, "caller": c} for loc, c in im["sites"]],
                   "blast": im["blast"],
                   "unresolved": [{"at": loc, "caller": c} for loc, c in im["unresolved"]]})
            return 0
        # One per line. Comma-joining seven callers and fourteen sites produced a wrapped
        # wall of text that nobody reads, which is what it looked like the first time this was
        # used on a real repository rather than a fixture.
        name = t[0] if isinstance(t, list) else t
        print(f"callers of {name}:")
        for c in im["callers"] or ["(none)"]: print(f"  {c}")
        print("sites:")
        for loc, c in im["sites"]: print(f"  {loc}  {c}")
        if not im["sites"]: print("  (none)")
        n = len(im["blast"])
        print(f"blast:   {n} function{'' if n == 1 else 's'} could be affected"
              + (f"   (codegraph blast {name} to list them)" if n else ""))
        if im["unresolved"]:
            u = im["unresolved"]
            one = len(u) == 1
            print(f"unsure:  {len(u)} call site{'' if one else 's'} {'uses' if one else 'use'} "
                  f"this name and could not be resolved - {', '.join(loc for loc, _ in u[:4])}"
                  + (" ..." if len(u) > 4 else ""))
    elif a[0] == "symbols":
        rows = [r for r in symbols(load()) if wanted(r[0])]
        if as_json:
            _emit({"query": "symbols",
                   "results": [{"id": i, "at": loc, "kind": k} for i, loc, k in rows]})
        else:
            print("\n".join(f"{i}  {loc}" for i, loc, _k in rows) or "(none)")
    elif a[0] == "changed":
        # The diff comes in on STDIN. Nothing here runs git - this file starts no processes -
        # and a pipe works with hg, jj, a saved patch, or a pull request fetched by something
        # else, which shelling out to one tool would not.
        diff = sys.stdin.read() if not sys.stdin.isatty() else ""
        if not diff.strip():
            print("nothing on stdin - pipe a diff in:\n"
                  "  git diff | codegraph changed\n"
                  "  git diff --cached | codegraph changed", file=sys.stderr)
            return 2
        got = changed(load(), diff)
        if as_json:
            _emit({"query": "changed", **got})
        else:
            for t in got["touched"]:
                if not wanted(t["id"]):
                    continue
                print(f"{t['id']}  (line{'s' if len(t['lines']) > 1 else ''} "
                      f"{_ranges(t['lines'])})")
                who = t["callers"]
                shown = ", ".join(who[:6]) + (f", +{len(who) - 6} more" if len(who) > 6 else "")
                print("  callers: " + (shown or "(none)"))
                print(f"  blast:   {len(t['blast'])} function(s) downstream")
            if not got["touched"]:
                print("(no changed line is inside a function this graph knows)")
            sys.stdout.flush()
            if got["module_level"]:
                print(f"\n{len(got['module_level'])} changed line(s) at module level, which run "
                      f"on import and can\nreach anything in the file: "
                      f"{', '.join(got['module_level'][:6])}", file=sys.stderr)
            if got["unknown_files"]:
                print(f"\n{len(got['unknown_files'])} changed file(s) this graph never read, so "
                      f"there is no answer for\nthem at all: "
                      f"{', '.join(got['unknown_files'][:6])}", file=sys.stderr)
    elif a[0] == "unused":
        rows = [r for r in unused(load()) if wanted(r[0])]
        # By default the list is the FINDING - the names nothing calls and nothing names.
        # `--all` puts back the ones that carry a reason, which is what it used to print and
        # what made it 577 rows long here, none of them dead.
        shown = rows if show_all else [r for r in rows if not r[2]]
        if as_json:
            _emit({"query": "unused", "shown": "all" if show_all else "unreached",
                   "results": [{"id": i, "at": loc, "reached_by": why or None}
                               for i, loc, why in rows]})
        else:
            print("\n".join(f"{i}  {loc}" + (f"   ({why})" if why else "")
                             for i, loc, why in shown) or "(none)")
            sys.stdout.flush()       # or the caveat lands above the list it is about, since
                                     # stderr is unbuffered and stdout is not when piped
            held = len(rows) - len(shown)
            if shown:
                print(f"\n{len(shown)} definition(s) nothing here calls and nothing here names. "
                      f"Still not\nproof of dead: this only saw the roots it was pointed at.",
                      file=sys.stderr)
            if held:
                print(f"{held} more are reached some way other than a written call "
                      f"(--all to see them).", file=sys.stderr)
    elif a[0] == "stats": print(json.dumps(stats(load()), indent=2))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main())
