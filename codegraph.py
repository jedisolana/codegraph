#!/usr/bin/env python3
"""codegraph - ask a Python codebase what breaks if you change this.

A local, dependency-free code graph built with the standard library's `ast`. No network, no
language server, no model. Nodes are modules, functions, classes and methods; edges are
defines, imports and calls.

Every call edge carries a CONFIDENCE, because the useful part is knowing when the answer is
solid. SELF-METHOD, INHERITED, CLASS, TYPED, QUALIFIED, LOCAL, RESOLVED and CONSTRUCTOR each
pin a call to exactly one definition. AMBIGUOUS lists the candidates instead of choosing.
BUILTIN, EXTERNAL and UNTYPED say the target is not here, or cannot be determined. Nothing is
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
  codegraph stats              counts, resolution rate, never-called definitions
  codegraph --selftest         29 ground-truth checks, several of them red-first
  codegraph --help             this text

Exit codes: 0 answered, 1 the name is unknown here or a search matched nothing, 2 the name
matches several definitions or the command was malformed.
"""
import ast
import builtins
import contextlib
import copy
import glob
import hashlib
import json
import os
import sys
import threading
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
        _VERSION = hashlib.sha1(_fh.read()).hexdigest()[:12]
except Exception:
    _VERSION = "0"


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
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(n.name)                   # the NAME binds here; the body is its own scope
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            # An import binds the name TO THE MODULE, which is the one binding resolution wants
            # to follow rather than be blocked by. Counting it as a shadow meant the deliberate
            # cycle-break - `def f(): import memory; return memory.recall()` - lost every call
            # edge through it, in a tool that recognises that idiom well enough to keep it out
            # of the cycle report.
            pass
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            freed.update(n.names)                 # declared elsewhere; collected and removed last,
        stack.extend(ast.iter_child_nodes(n))     # because the walk order is not source order
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
    except (SyntaxError, ValueError, RecursionError) as ex:
        # py2, a template, a half-written file. Skipping is right; skipping in SILENCE is not -
        # an empty module in the graph looks exactly like a file with nothing in it.
        raise Unparseable(f"{os.path.relpath(path)}: could not parse: {ex}") from ex
    except OSError as ex:
        # A file the process cannot READ - restrictive permissions in a vendored directory, a
        # container running as a different user. One such file used to end the whole build on
        # a PermissionError traceback, which is the same mistake a dangling symlink once made:
        # a single awkward file is not a reason to refuse to analyse a codebase.
        raise Unparseable(f"{os.path.relpath(path)}: could not read: {ex.strerror or ex}") from ex
    defs, edges, imports = [], [], []
    aliases, fromimp = {}, {}                                   # module aliases (name->module) and from-imports (name->module) for import-aware call resolution
    # One name imported from two different modules - the try/except ImportError idiom. A plain
    # dict keeps the LAST binding, which for that idiom is the FALLBACK: the tool named
    # slow.parse as the definite target of parse() while fast.parse, the one that actually runs
    # when the import succeeds, showed no callers at all. Both are recorded, and a name with
    # two possible sources is answered the way every other ambiguity is.
    fromalt = defaultdict(list)
    submodules = {}                                             # name -> the module id it MIGHT be, confirmed in build()

    class V(ast.NodeVisitor):
        def __init__(self):
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
                        # the name may be a SUBMODULE, same as the absolute form - the two
                        # branches have to learn the same things or one of them lags behind
                        submodules[a.asname or a.name] = f"{target}/{a.name}"
                else:                                            # from . import thing - each NAME is itself a module
                    for a in node.names:
                        target = "/".join([*base, a.name])
                        imports.append({"src": mod, "callee": target, "kind": "IMPORT", "line": node.lineno, "module_level": ml})
                        aliases[a.asname or a.name] = target
                        _bind(fromimp, fromalt, a.asname or a.name, target)   # `from . import thing` also allows a bare thing() if it is a func
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
                    # ...but the name might be a SUBMODULE rather than a function, and
                    # `from pkg import mod` then mod.func() is ordinary package code. Recorded
                    # as a candidate; build() keeps it only if that module really exists.
                    submodules[a.asname or a.name] = f"{node.module.replace('.', '/')}/{a.name}"

        def visit_ClassDef(self, node):
            self._decorators(node)
            qid = self._qual(node.name)
            # Base classes by NAME. Resolved to ids in build(), where the whole tree is known, so
            # `self.method()` can be found on a parent instead of giving up - which is the single
            # most common shape this used to miss.
            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
            defs.append({"id": qid, "kind": "class", "name": node.name, "module": mod,
                         "line": node.lineno, "bases": bases})
            self.scope.append(node.name); self.classes.append(qid)   # methods walk under this class scope
            for c in node.body: self.visit(c)
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
            defs.append({"id": qid, "kind": "func", "name": node.name, "module": mod, "line": node.lineno})
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

        def visit_Call(self, node):
            fn = node.func
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
        raise Unparseable(f"{os.path.relpath(path)}: too deeply nested to analyse "
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
            {k: v for k, v in fromalt.items() if len(v) > 1}, submodules)


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
    mod_alias, mod_from, mod_root, mod_sub, mod_alt = {}, {}, {}, {}, {}; newcache = {}
    for path, d, root in pairs:
        rel = os.path.relpath(path, d)[:-3].replace(os.sep, "/")   # 'memory' for a top-level file, 'sub/mod' for a nested one (no .py) - path-relative id, no nested collision
        base = os.path.basename(rel)
        mid = f"{root}/{rel}" if multiroot else rel             # SINGLE flat tree: bare 'memory' (unchanged). Nested: 'sub/mod'. Multi-tree: 'root/...'
        nodes.append({"id": mid, "kind": "module", "name": base, "module": mid, "line": 0})
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
        c = cache.get(path)
        if c and c.get("stamp") == stamp:                       # unchanged file -> reuse cached parse (deep-copied so resolution can't leak back into the cache)
            d, e, im, al, fi = c["defs"], copy.deepcopy(c["calls"]), c["imports"], c["aliases"], c["fromimp"]
            sub = c.get("submodules", {}); alt = c.get("fromalt", {})
        else:
            try:
                d, e, im, al, fi, alt, sub = _defs_and_calls(path, mid)
            except Unparseable as ex:
                unreadable.append(str(ex))
                nodes.pop()                                      # not a module we can describe
                continue
        if not multiroot:
            newcache[path] = {"stamp": stamp, "defs": d, "calls": [dict(x) for x in e], "imports": im,
                              "aliases": al, "fromimp": fi, "fromalt": alt, "submodules": sub}
        nodes += d; calls += e; imports += im
        mod_alias[mid] = al; mod_from[mid] = fi; mod_root[mid] = root; mod_sub[mid] = sub
        mod_alt[mid] = alt
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
    bases_of = {}                                                # class id -> base class ids
    for n in nodes:
        if n["kind"] == "class" and n.get("bases"):
            here = cls_ids.get(n["module"], {})
            resolved = []
            for b in n["bases"]:
                if b in here: resolved.append(here[b])           # a base in the same module
                else:
                    hits = [i for i in public.get(b, []) if i.split(".")[-1] == b]
                    if len(hits) == 1: resolved.append(hits[0])  # exactly one class of that name in the tree
            bases_of[n["id"]] = resolved
    kind_of = {n["id"]: n["kind"] for n in nodes}                # for walking a call's scope chain
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
        rt = e.get("recv_type")
        if not dst and rt:                                       # x.method() where `x = Foo()` (local type inference) -> Foo.method
            # WHICH class called Foo? The one this module can see - defined here, or imported
            # here - before any tree-wide search. It used to take whichever id sorted first,
            # with no ambiguity test at all: a file writing `from b.svc import Client` had
            # c.get() resolved into a/svc.Client, one line after the Client() call itself was
            # labelled AMBIGUOUS. The tool contradicted itself inside a single function.
            if "." in rt:
                # `import svc` then `svc.Client()`: the module part says exactly where to look,
                # so there is nothing to search and nothing to be ambiguous about. This is how
                # package code constructs things, and it used to give no type at all.
                who, cname = rt.rsplit(".", 1)
                where_ = mod_alias.get(srcmod, {}).get(who) or mod_sub.get(srcmod, {}).get(who)
                cid = by_modname.get((where_, cname)) if where_ else None
                cands = [cid] if cid and kind_of.get(cid) == "class" else []
            else:
                scoped = (cls_ids.get(srcmod, {}).get(rt)
                          or by_modname.get((mod_from.get(srcmod, {}).get(rt), rt))
                          or by_modname.get((mod_sub.get(srcmod, {}).get(rt), rt)))
                cands = ([scoped] if scoped
                         else [i for i in public.get(rt, []) if kind_of.get(i) == "class"])
            if len(cands) == 1:
                if (cands[0] + "." + callee) in def_ids:
                    dst = cands[0] + "." + callee; conf = "TYPED"
            elif len(cands) > 1:
                # Several classes answer to that name and nothing here says which. Same rule
                # as a bare call: list them, pick none.
                hits = [c + "." + callee for c in cands if (c + "." + callee) in def_ids]
                if len(hits) == 1: dst = hits[0]; conf = "TYPED"
                elif len(hits) > 1: conf = "AMBIGUOUS"; e["candidates"] = sorted(hits)
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
        if not dst and not method and not conf and callee in mod_from.get(srcmod, {}):     # BARE recall() under `from memory import recall`
            alts = mod_alt.get(srcmod, {}).get(callee)
            hits = ([by_modname[(m, callee)] for m in alts if (m, callee) in by_modname]
                    if alts else [])
            if len(hits) > 1:
                # Imported from two places under one name. Which one runs depends on the
                # machine, so neither is the answer - the candidates are.
                conf = "AMBIGUOUS"; e["candidates"] = sorted(hits)
            else:
                dst = by_modname.get((mod_from[srcmod][callee], callee)); conf = "QUALIFIED" if dst else conf
        if not dst and not method and not conf and by_modname.get((srcmod, callee)):       # a BARE call to a function in the SAME module
            dst = by_modname[(srcmod, callee)]; conf = "LOCAL"
        if not dst and not method and not conf:                  # a BARE call
            if callee in _BUILTINS: conf = "BUILTIN"             # next/len/sorted/open... - certainly not yours
            else:
                tgts = public.get(callee, [])
                if len(tgts) == 1: dst = tgts[0]; conf = "RESOLVED"
                elif len(tgts) > 1: conf = "AMBIGUOUS"; e["candidates"] = tgts
                else: conf = "EXTERNAL"
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
    graph = {"version": _VERSION,
             "nodes": nodes, "calls": calls, "imports": imports, "dirs": sorted(dirs),
             "sources": {p: stamps[p] for p in sorted(stamps)}, "unreadable": sorted(unreadable)}
    if write:
        for target, what in ((CACHE, "cache"), (OUT, "graph")):
            try:
                _jwrite({"_v": _VERSION, "files": newcache} if what == "cache" else graph, target)
            except OSError as ex:
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
    ok = (ok1 and ok2 and ok3 and ok4 and ok5 and ok6 and ok7 and ok8 and ok9 and ok10 and ok11 and ok12
          and ok13 and ok14 and ok15 and ok16 and ok17 and ok18 and ok19 and ok20 and ok21 and ok22 and ok18a and ok18b and ok23 and ok24 and ok25 and ok26 and ok27)
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
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)                                   # POSIX atomic rename
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


def _by_name(g, name):
    return [n["id"] for n in g["nodes"] if n["name"] == name and n["kind"] in ("func", "class")]


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
    """function ids that call `target`. When `target` resolves to a definition, follow the RESOLVED edges -
    so a query for pulse.digest returns only its real callers, NOT callers of the same-named court.digest.
    Falls back to bare-name matching only for external/unknown targets."""
    ids = set(_one(g, target))
    if ids:
        return sorted({e["src"] for e in g["calls"] if e.get("dst") in ids})
    name = target.split(".")[-1]
    return sorted({e["src"] for e in g["calls"] if e["callee"] == name})


def calls_from(g, node_id):
    """RESOLVED in-tree calls made by node_id (the specific definitions it reaches)."""
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
    out = []
    for n in g["nodes"]:
        if n["id"] in want:
            where_ = f"{n['module']}.py:{n['line']}"
            if n.get("shadows"):     # the live definition, plus the lines it overrides
                where_ += " (shadows " + ", ".join(f"line {ln}" for ln in n["shadows"]) + ")"
            out.append((n["id"], where_))
    return sorted(out)


def find(g, substr):
    """fuzzy symbol search - every function/class whose name CONTAINS substr (replaces broad greps for a name)."""
    s = substr.lower()
    return sorted((n["id"], f"{n['module']}.py:{n['line']}") for n in g["nodes"]
                  if n["kind"] in ("func", "class") and s in n["name"].lower())


def sites(g, target):
    """every CALL SITE of target as (file:line, caller) - the exact places to edit when you change it (the
    REFACTOR helper). Uses resolved edges when target is a known def; else the bare callee name."""
    ids = set(_one(g, target)); name = target.split(".")[-1]
    mod_of = {n["id"]: n["module"] for n in g["nodes"]}
    out = set()
    for e in g["calls"]:
        if (e.get("dst") in ids) if ids else (e["callee"] == name):
            m = e.get("mod") or mod_of.get(e["src"]) or e["src"].split(".")[0]
            for ln in e.get("lines") or [e["line"]]:
                out.add((f"{m}.py:{ln}", e["src"]))
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
        if e.get("module_level", True) and e["src"] in intree and e["callee"] in intree and e["src"] != e["callee"]:
            imp[e["src"]].add(e["callee"])
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
                    if len(comp) > 1: out.append(tuple(sorted(comp)))
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


def _one_target(g, name):
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
        print(f"{name!r} is a module, not a function - try: codegraph deps {name}", file=sys.stderr)
        return None, 1
    if kind == "unknown":
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
        _explain(g, exc)
        return None, 2
    return ids[0], 0


def _main(argv=None):
    """The CLI, as a function so `pip install` can expose it as a console script -- and so the
    tests can drive it in-process instead of shelling out."""
    a = list(sys.argv[1:] if argv is None else argv)
    # Every query verb takes a name. Forgetting it used to be an IndexError traceback - the
    # first thing a new user sees when they type a command from memory.
    NEEDS = {"callers": 1, "calls": 1, "blast": 1, "where": 1, "find": 1, "sites": 1,
             "impact": 1, "deps": 1, "path": 2}
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
    elif a[0] == "callers":
        g = load()
        t, rc = _one_target(g, a[1])
        if t is None: return rc
        print("\n".join(callers_of(g, t)) or "(none)")
    elif a[0] == "calls":
        g = load()
        t, rc = _one_target(g, a[1])
        if t is None: return rc
        print("\n".join(calls_from(g, t)) or "(none)")
    elif a[0] == "blast":
        g = load()
        t, rc = _one_target(g, a[1])
        if t is None: return rc
        print("\n".join(blast_radius(g, t)) or "(none)")
    elif a[0] == "where":
        g = load()
        hits = where(g, a[1])
        if not hits and _describe(g, a[1])[0] == "module":
            # Every other verb explains this; `where` used to answer "(not found)" about a
            # module sitting right there in the graph.
            print(f"{a[1]!r} is a module, not a function - try: codegraph deps {a[1]}",
                  file=sys.stderr)
            return 1
        print("\n".join(f"{i}  {loc}" for i, loc in hits) or "(not found)")
        if not hits: return 1                    # a search that matched nothing, like grep
    elif a[0] == "find":
        hits = find(load(), a[1])
        print("\n".join(f"{i}  {loc}" for i, loc in hits) or "(none)")
        if not hits: return 1
    elif a[0] == "sites":
        g = load()
        t, rc = _one_target(g, a[1])
        if t is None: return rc
        print("\n".join(f"{loc}  {c}" for loc, c in sites(g, t)) or "(none)")
    elif a[0] == "path":
        g = load()
        for end in (a[1], a[2]):                     # both endpoints, same rules as every verb
            t, rc = _one_target(g, end)
            if t is None: return rc
        p = path(g, a[1], a[2])
        print(" -> ".join(p) if p else "(no path)")
    elif a[0] == "deps":
        g = load()
        if not any(n["kind"] == "module" and n["id"] == a[1] for n in g["nodes"]):
            # "(none)/(none)" for a module that does not exist reads exactly like a module
            # with no dependencies, which is a different and much more reassuring fact.
            print(f"no module {a[1]!r} in the graph", file=sys.stderr)
            return 1
        im, imp = module_deps(g, a[1])
        print("imports:   " + (", ".join(im) or "(none)"))
        print("importers: " + (", ".join(imp) or "(none)"))
    elif a[0] == "cycles":
        cy = cycles(load())
        print("\n".join(" <-> ".join(group) for group in cy) or "(none)")
    elif a[0] == "impact":
        g = load()
        t, rc = _one_target(g, a[1])
        if t is None: return rc
        im = impact(g, t)
        print("callers:", ", ".join(im["callers"]) or "(none)")
        print("sites:  ", ", ".join(f"{l}" for l, c in im["sites"]) or "(none)")
        n = len(im["blast"])
        print(f"blast:   {n} function{'' if n == 1 else 's'} could be affected")
        if im["unresolved"]:
            u = im["unresolved"]
            one = len(u) == 1
            print(f"unsure:  {len(u)} call site{'' if one else 's'} {'uses' if one else 'use'} "
                  f"this name and could not be resolved - {', '.join(loc for loc, _ in u[:4])}"
                  + (" ..." if len(u) > 4 else ""))
    elif a[0] == "stats": print(json.dumps(stats(load()), indent=2))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main())
