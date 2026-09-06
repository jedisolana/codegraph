#!/usr/bin/env python3
"""codegraph - ask a Python codebase what breaks if you change this.

A local, dependency-free code graph built with the standard library's `ast`. No network, no
language server, no model. Nodes are modules, functions, classes and methods; edges are
defines, imports and calls.

Every call edge carries a CONFIDENCE, because the useful part is knowing when the answer is
solid. SELF-METHOD, INHERITED, CLASS, TYPED, QUALIFIED, LOCAL and CONSTRUCTOR each pin a call
to exactly one definition. AMBIGUOUS lists the candidates instead of choosing between them.
BUILTIN and EXTERNAL say the target is not here; UNTYPED says the receiver could not be typed
and the target might be. Nothing is
guessed: a blast radius that quietly picked one of two same-named functions would be worse
than no blast radius at all.

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
  codegraph unused             every definition nothing here calls - read the caveat
  codegraph stats              counts, resolution rate, never-called definitions
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
from collections import defaultdict

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
         or os.path.join(os.path.dirname(OUT) or ".", "codegraph.cache.json"))          # per-file parse cache keyed by path+mtime (incremental build)
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
            self._call_funcs = set()                            # ids of Attribute nodes that ARE a call's func, so the reader below does not count `c.f()` twice
            self.scope = [mod]                                  # qualified-name stack: module -> class -> func
            self.owner = [mod]                                  # nearest ENCLOSING owner a call belongs to (module at bottom, so module-level calls are captured too)
            self.classes = []                                   # enclosing class ids, so self.method() resolves within the right class
            self.vtypes = [{}]                                  # per-scope var->ClassName from `x = Foo(...)`, so x.method() resolves to Foo.method (local type inference)
            # The MODULE is a scope too, and it was the only one with no pre-scan: every
            # function got one and the file's own top level got an empty set. So
            # `for config in rows:` or `with open(p) as config:` at top level left config
            # looking like the imported module, and config.dumps() resolved into it, labelled
            # QUALIFIED - on a receiver that is a number, or a file.
            # Only VALUES, never the module's own defs: a top-level `class Parent` is the
            # definition Parent.make() is looking for, not a shadow of it.
            self.bound = [(_bound_names(tree)[0], set())]       # per-scope (values, defs) bound locally, which SHADOW an imported module or class of the same name

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
                else:                                            # from . import thing - each NAME is itself a module
                    for a in node.names:
                        target = "/".join([*base, a.name])
                        imports.append({"src": mod, "callee": target, "kind": "IMPORT", "line": node.lineno, "module_level": ml})
                        aliases[a.asname or a.name] = target
                        _bind(fromimp, fromalt, a.asname or a.name, target)   # `from . import thing` also allows a bare thing() if it is a func
                        if a.asname: fromorig[a.asname] = a.name
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
            defs.append({"id": qid, "kind": "class", "name": node.name, "module": mod,
                         "line": node.lineno, "bases": bases})
            self.scope.append(node.name); self.classes.append(qid)   # methods walk under this class scope
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
            defs.append(d)
            self.scope.append(node.name); self.owner.append(qid)
            # A parameter's ANNOTATION is the type, stated outright. `def send(c: Client)` then
            # c.get() was UNTYPED - the tool inferring around an answer the source had written
            # down. *args and **kwargs are skipped: the annotation there describes the ELEMENTS.
            seeded = {}
            for arg in [*getattr(node.args, "posonlyargs", []), *node.args.args,
                        *node.args.kwonlyargs]:
                cls = _annotated_class(arg.annotation)
                if cls: seeded[arg.arg] = cls
            self.vtypes.append(seeded)                          # calls inside this body belong to qid; its own var-type scope
            self.bound.append(_bound_names(node))
            for c in node.body: self.visit(c)
            self.bound.pop(); self.vtypes.pop(); self.owner.pop(); self.scope.pop()

        def _comp(self, node):
            """A comprehension shadows INSIDE itself, and nowhere else. The first iterable is
            evaluated in the enclosing scope, which is where its names still mean what they
            meant a line earlier."""
            if node.generators:
                self.visit(node.generators[0].iter)
            names = set()
            for gen in node.generators:
                for sub in ast.walk(gen.target):
                    if isinstance(sub, ast.Name): names.add(sub.id)
            self.bound.append((names, set()))
            self.vtypes.append({k: v for k, v in self.vtypes[-1].items() if k not in names})
            for i, gen in enumerate(node.generators):
                if i: self.visit(gen.iter)
                for cond in gen.ifs: self.visit(cond)
            for part in ((node.key, node.value) if isinstance(node, ast.DictComp)
                         else (node.elt,)):
                self.visit(part)
            self.bound.pop(); self.vtypes.pop()

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
            self.visit(node.body)
            self.bound.pop(); self.vtypes.pop()

        def visit_FunctionDef(self, node): self._func(node)
        def visit_AsyncFunctionDef(self, node): self._func(node)

        def _retype(self, name, cls):
            """cls is a class name, or None for "rebound to something I cannot name"."""
            if name in self.vtypes[-1] and self.vtypes[-1][name] != cls:
                # `if c: x = Alpha() else: x = Beta()` then x.go() used to pick whichever
                # branch was walked last and label it TYPED - right half the time, and
                # certain both times. Two answers is not a type.
                self.vtypes[-1][name] = None
            else:
                self.vtypes[-1][name] = cls                                       # x = Foo(...) -> x is a Foo (resolved to a class in build())

        def visit_Assign(self, node):
            # EVERY target, and every kind of value. `a = b = Client()` bound neither, because
            # the check wanted exactly one target. Worse, `x = Foo()` followed by
            # `x = load_config()` left x a Foo: a rebinding to anything that was not another
            # class call was invisible, so x.go() was answered with Foo.go, confidently. The
            # one value that says nothing is None - a name cannot be called through it, so the
            # code has to rebind before using it, and `x = None` above an `x = Foo()` is how
            # half of Python initialises an optional.
            if isinstance(node.value, ast.Constant) and node.value.value is None:
                return self.generic_visit(node)
            cls = _called_class(node.value)
            for tgt in node.targets:
                if isinstance(tgt, ast.Name): self._retype(tgt.id, cls)
                elif isinstance(tgt, (ast.Tuple, ast.List)):     # a, b = f() - two unknowns
                    for el in tgt.elts:
                        if isinstance(el, ast.Name): self._retype(el.id, None)
            self.generic_visit(node)

        def visit_AnnAssign(self, node):
            """`c: Client = make()` - the annotation names the class the inference could not
            reach through the call."""
            if isinstance(node.target, ast.Name):
                cls = _annotated_class(node.annotation)
                if cls is None: cls = _called_class(node.value)
                if cls or node.value is not None: self._retype(node.target.id, cls)
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
            if (isinstance(node.ctx, ast.Load) and isinstance(node.value, ast.Name)
                    and id(node) not in self._call_funcs):
                recv = node.value.id
                edge = {"src": self.owner[-1], "mod": mod, "callee": node.attr, "recv": recv,
                        "method": True, "kind": "CALL", "attr_read": True,
                        "line": getattr(node, "lineno", 0)}
                if recv in ("self", "cls") and self.classes:
                    edge["encl_class"] = self.classes[-1]; edges.append(edge)
                elif self.vtypes[-1].get(recv):
                    edge["recv_type"] = self.vtypes[-1][recv]; edges.append(edge)
            self.generic_visit(node)

        def visit_Call(self, node):
            fn = node.func
            if isinstance(fn, ast.Attribute): self._call_funcs.add(id(fn))
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
                elif recv and self.vtypes[-1].get(recv): edge["recv_type"] = self.vtypes[-1][recv]      # x.method() where x = Foo() -> resolve to Foo.method (local type inference)
                if not method and self.vtypes[-1].get(callee):
                    # c(1) where c is a Client. Calling an INSTANCE runs its __call__, and the
                    # call site never writes that name - the same shape as __init__, and the
                    # same answer it used to give: "callers: (none)" for a method being called
                    # two lines away. Callable classes are ordinary Python: decorators written
                    # as classes, handlers, anything with state and one obvious verb.
                    edge["invoke_type"] = self.vtypes[-1][callee]
                edges.append(edge)
            self.generic_visit(node)                            # recurse into args/keywords (which may hold more calls)

    try:
        V().visit(tree)
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
    seen = {}
    for e in edges:
        k = (e["src"], e.get("recv"), e["callee"])
        if k in seen:
            seen[k]["lines"].append(e["line"])
        else:
            e["lines"] = [e["line"]]
            seen[k] = e
    for e in seen.values():
        e["lines"] = sorted(set(e["lines"]))
        e["line"] = e["lines"][0]                    # the first, for anything reading one line
    return (defs, list(seen.values()), imports, aliases, fromimp,
            {k: v for k, v in fromalt.items() if len(v) > 1}, fromorig, submodules)


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


def _annotated_class(ann):
    """The class an annotation names, when it names one plainly.

    `c: Client` and `c: "Client"` (a forward reference, and what every annotation becomes under
    `from __future__ import annotations`) both say exactly which class this is - the source
    stating the answer the tool was inferring around. A SUBSCRIPT does not: `Dict[str, Client]`
    is a dict, and reading Client out of it would resolve d.get() to a Client method. Only a
    bare name counts.
    """
    if isinstance(ann, ast.Name):
        return ann.id
    if isinstance(ann, ast.Attribute) and isinstance(ann.value, ast.Name):
        return f"{ann.value.id}.{ann.attr}"                # svc.Client - a module and a class
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        text = ann.value.strip()
        return text if text.isidentifier() else None
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
    cache = {}                                                  # INCREMENTAL: reuse a file's parse when its mtime is unchanged (single-tree only; multiroot ids differ by mode so it parses fresh)
    if write and not multiroot:
        try:
            with open(CACHE, encoding="utf-8") as _fh:
                _c = json.load(_fh)
            cache = _c.get("files", {}) if _c.get("_v") == _VERSION else {}   # discard the whole cache if codegraph's code changed (versioned namespace)
        except Exception: cache = {}
    nodes, calls, imports, unreadable = [], [], [], []
    stamps = {}                                                  # path -> [mtime, size], the graph's own record of what it read
    mod_alias, mod_from, mod_root, mod_sub, mod_alt = {}, {}, {}, {}, {}
    mod_orig = {}                                                # local alias -> the name the module actually defines
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
            _st = os.stat(path)
            stamp = [_st.st_mtime, _st.st_size]        # SIZE as well as time: a restored file can
                                                       # carry the timestamp it had before
        except OSError:
            # A DANGLING SYMLINK - a link left behind after a move, which every real repo
            # eventually has. os.walk lists it as a file and stat() then raises, which used to
            # kill the whole build with a traceback. One broken link is not a reason to refuse
            # to analyse a codebase; skip it and carry on.
            nodes.pop()                                          # drop the module node just added
            continue
        stamps[path] = stamp
        # POP, not get: this file's old parse is finished with the moment it is copied, and
        # the copy goes into newcache. Holding both tables whole is where a build peaks - the
        # entries are released one at a time now, so the old table shrinks as the new one grows
        # instead of the two standing at full size together.
        c = cache.pop(path, None)
        if c and c.get("stamp") == stamp:                       # unchanged file -> reuse cached parse (deep-copied so resolution can't leak back into the cache)
            # A shallow copy PER EDGE, not a deep one. Resolution writes dst/confidence/
            # candidates onto these dicts, and those writes must not reach the cache that is
            # about to be written back - but every value it touches is a scalar, and the only
            # nested value an edge carries is its list of line numbers, which nothing mutates
            # after parsing. copy.deepcopy walked six million objects to protect against a
            # write that does not happen; on a large tree that was a third of a warm build.
            d, e, im, al, fi = (c["defs"], [dict(x) for x in c["calls"]],
                                c["imports"], c["aliases"], c["fromimp"])
            sub = c.get("submodules", {}); alt = c.get("fromalt", {})
            orig = c.get("fromorig", {})
        else:
            try:
                d, e, im, al, fi, alt, orig, sub = _defs_and_calls(path, mid)
            except Unparseable as ex:
                unreadable.append(str(ex))
                nodes.pop()                                      # not a module we can describe
                continue
        if not multiroot:
            newcache[path] = {"stamp": stamp, "defs": d, "calls": [dict(x) for x in e], "imports": im,
                              "aliases": al, "fromimp": fi, "fromalt": alt,
                              "fromorig": orig, "submodules": sub}
        nodes += d; calls += e; imports += im
        mod_alias[mid] = al; mod_from[mid] = fi; mod_root[mid] = root; mod_sub[mid] = sub
        mod_alt[mid] = alt; mod_orig[mid] = orig
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
    def _real_module(name):
        # The PACKAGE first. Python's own finder looks for pkg/__init__.py before pkg.py in the
        # same directory, so when a repo holds both, `import thing` is the package - and this
        # used to answer with the file.
        pkg = f"{name}/__init__"
        return pkg if pkg in allmods else name

    for table in (mod_alias, mod_from):
        for mid in table:
            table[mid] = {k: _real_module(v) for k, v in table[mid].items()}
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
            if target in allmods:
                mod_alias.setdefault(mid, {}).setdefault(name, target)
    # RE-EXPORTS. `pkg/__init__` does `from .thing import load`, and another module does
    # `from pkg import load`. The name is not defined in pkg/__init__ at all, so the import
    # pointed at a module that does not have it. It happened to resolve anyway whenever the
    # name was unique in the tree - which is luck, and stops being luck the moment two modules
    # define it. Follow the chain to where the name actually lives, with a hop limit because a
    # circular re-export is expressible even if it would not import.
    defined_in = {(n["module"], n["name"]) for n in nodes if n["kind"] in ("func", "class")}
    for mid in mod_from:
        for name, target in list(mod_from[mid].items()):
            hops, seen_hops = 0, set()
            while (target, name) not in defined_in and target in mod_from and hops < 8:
                nxt = mod_from[target].get(name)
                if not nxt or nxt in seen_hops: break
                seen_hops.add(nxt); target = nxt; hops += 1
            mod_from[mid][name] = target
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
            cid = by_modname.get((where_, cname)) if where_ else None
            return [cid] if cid and kind_of.get(cid) == "class" else []
        real = mod_orig.get(srcmod, {}).get(rt, rt)              # from svc import Client as C
        scoped = (cls_ids.get(srcmod, {}).get(rt)
                  or by_modname.get((mod_from.get(srcmod, {}).get(rt), real))
                  or by_modname.get((mod_sub.get(srcmod, {}).get(rt), real)))
        if scoped:
            return [scoped]
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
                hits = [c for c in _classes_named(b, n["module"], n["id"].rsplit(".", 1)[0])
                        if c.split(".")[-1] == want]
                if len(hits) == 1: resolved.append(hits[0])
            bases_of[n["id"]] = resolved
    imported_in = {m: set(mod_alias.get(m, ())) | set(mod_from.get(m, ())) | set(mod_sub.get(m, ()))
                   for m in set(mod_alias) | set(mod_from) | set(mod_sub)}   # names each module actually imported
    mro_cache = {}                                                # linearisation is reused across edges
    for e in calls:                                               # resolve each call to a SPECIFIC definition, import-aware (highest confidence first)
        srcmod = e.get("mod") or e["src"].split(".")[0]; recv = e.get("recv"); callee = e["callee"]; method = e.get("method"); dst = None; conf = None
        ec = e.get("encl_class")
        if ec and (ec + "." + callee) in def_ids:                # self.method()/cls.method() -> the method in the ENCLOSING class (exact scope)
            dst = ec + "." + callee; conf = "SELF-METHOD"
        elif ec:
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
        it = e.get("invoke_type")
        if not dst and it:
            hits = []
            for c in _classes_named(it, srcmod, e["src"]):
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
            for c in _classes_named(rt, srcmod, e["src"]):
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
            dst = by_modname.get((mod_alias[srcmod][recv], callee)); conf = "QUALIFIED" if dst else conf
        if (not dst and method and not e.get("recv_local")
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
            stars = mod_alt.get(srcmod, {}).get("*") or [mod_from[srcmod]["*"]]
            hits = [by_modname[(m, callee)] for m in stars if (m, callee) in by_modname]
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
                conf = "UNTYPED"
        e["dst"] = dst; e["confidence"] = conf
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
            for c in _mro(e["dst"], bases_of, mro_cache):
                if (c + ".__init__") in def_ids:
                    ctor.append({**e, "dst": c + ".__init__", "confidence": "CONSTRUCTOR"})
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
    keep = ("src", "dst", "callee", "confidence", "recv", "method", "mod", "line", "lines",
            "candidates")
    calls = [{k: e[k] for k in keep if k in e} for e in calls]
    graph = {"version": _VERSION,
             "nodes": nodes, "calls": calls, "imports": imports, "dirs": sorted(dirs),
             "sources": {p: stamps[p] for p in sorted(stamps)}, "unreadable": sorted(unreadable)}
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


def _is_stale(g):
    """True if the graph no longer describes what is on disk.

    Newer files are only half of it. A file that was DELETED leaves every remaining mtime
    untouched, so the mtime test alone said "fresh" and the graph went on answering about code
    that no longer existed. The set of files is compared too, which catches additions and
    deletions in the same pass.
    """
    if g.get("version") != _VERSION:
        return True                            # a different codegraph built this; its answers
                                               # are that version's, not this one's
    if not os.path.exists(OUT): return True
    known = g.get("sources")
    if not isinstance(known, dict): return True   # a graph from before stamps: rebuild once
    seen = {}
    for d in g.get("dirs", [HOME]):
        for dp, dns, fns in os.walk(d):
            dns[:] = [x for x in dns if not _prune_dir(dp, x)]
            for fn in fns:
                if not fn.endswith(".py"): continue
                full = os.path.join(dp, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    # A DANGLING SYMLINK, which every long-lived repo has one of. This used to
                    # return True - "something changed" - so a single broken link meant every
                    # query rebuilt the whole graph, for ever, in silence. The build already
                    # skips these; the freshness check has to skip the same ones or the two
                    # disagree about what the tree even contains.
                    continue
                seen[full] = [st.st_mtime, st.st_size]
    # An exact comparison, not "is anything newer than the graph". A file restored from a
    # backup, a checkout, cp -p, rsync -t or a container layer keeps the timestamp it had, so
    # it lands OLDER than the graph while holding different code - and the old test called that
    # fresh. The graph then answered about functions that no longer exist and denied ones that
    # do, with a success code.
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
    d = os.path.abspath(start or os.getcwd())
    while True:
        cand = os.path.join(d, "codegraph.json")
        if os.path.exists(cand):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            return OUT                                # reached the filesystem root; no graph
        d = parent


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


def unused(g):
    """Every function definition nothing in this tree calls, as (id, file:line, called_by_python).

    `stats` has counted these from the start and there was no way to SEE them, which made the
    number useless: 332 of something you cannot list is not a finding. Checking a real
    codebase's 44 by hand turned up zero dead functions - they were a dispatch table, names
    reached from a web page, two handlers the standard library's HTTP server calls by name, and
    a `__str__`. So the last field is a warning, not a verdict: a dunder is called BY PYTHON,
    for you, and will always be here.
    """
    called = {e["dst"] for e in g["calls"] if e.get("dst")}
    files = _files(g)
    out = []
    for n in g["nodes"]:
        if n["kind"] == "func" and n["id"] not in called:
            where_ = f"{files.get(n['module'], n['module'] + '.py')}:{n['line']}"
            out.append((n["id"], where_, n["name"].startswith("__") and n["name"].endswith("__")))
    return sorted(out)


def stats(g):
    kinds = defaultdict(int)
    for n in g["nodes"]: kinds[n["kind"]] += 1
    conf = defaultdict(int)
    for e in g["calls"]: conf[e.get("confidence", "?")] += 1
    called = {e["dst"] for e in g["calls"] if e.get("dst")}
    defs = [n["id"] for n in g["nodes"] if n["kind"] == "func"]
    unreachable = [d for d in defs if d not in called]           # never called in-tree (entrypoints, dead code, or dynamic)
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
            "func_defs": len(defs), "never_called_in_tree": len(unreachable)}


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
        else: print("\n".join(found) or "(none)")
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
    elif a[0] == "unused":
        rows = [r for r in unused(load()) if wanted(r[0])]
        if as_json:
            _emit({"query": "unused",
                   "results": [{"id": i, "at": loc, "called_by_python": d} for i, loc, d in rows]})
        else:
            print("\n".join(f"{i}  {loc}" + ("   (python calls this one)" if d else "")
                             for i, loc, d in rows) or "(none)")
            if rows:
                sys.stdout.flush()   # or the caveat lands above the list it is about, since
                                     # stderr is unbuffered and stdout is not when piped
                print(f"\n{len(rows)} definition(s) nothing here calls. Not the same as dead: a "
                      f"dispatch table,\na plugin registry, a web route or a framework callback "
                      f"all look like this.", file=sys.stderr)
    elif a[0] == "stats": print(json.dumps(stats(load()), indent=2))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main())
