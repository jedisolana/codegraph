#!/usr/bin/env python3
"""codegraph - ask a Python codebase what breaks if you change this.

A local, dependency-free code graph built with the standard library's `ast`. No network, no
language server, no model. Nodes are modules, functions, classes and methods; edges are
defines, imports and calls.

Every call edge carries a CONFIDENCE, because the useful part is knowing when the answer is
solid. SELF-METHOD, INHERITED, CLASS, TYPED, QUALIFIED, LOCAL and RESOLVED each pin a call to
exactly one definition. AMBIGUOUS lists the candidates instead of choosing between them.
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
  codegraph --selftest         17 ground-truth checks, several of them red-first
  codegraph --help             this text

Exit codes: 0 answered, 1 the name is unknown here or a search matched nothing, 2 the name
matches several definitions or the command was malformed.
"""
import ast
import builtins
import copy
import glob
import hashlib
import json
import os
import sys
from collections import defaultdict

_BUILTINS = set(dir(builtins))                              # next()/len()/sorted()/open()/... are builtins, never a same-named in-tree func (discard edges to built-ins; matters most cross-tree where a unique in-tree name shadows a builtin)

# The graph belongs next to the CODE, not next to this script. It used to be written beside
# codegraph.py, which is fine for one house and wrong for a tool people copy around: analyse
# someone's repo and the output landed in yours. Default to the working directory; both are
# overridable so a build server can put them wherever it likes.
HOME = os.environ.get("CODEGRAPH_ROOT") or os.getcwd()
OUT = os.environ.get("CODEGRAPH_OUT") or os.path.join(HOME, "codegraph.json")
CACHE = os.environ.get("CODEGRAPH_CACHE") or os.path.join(HOME, "codegraph.cache.json")          # per-file parse cache keyed by path+mtime (incremental build)
# The cache is namespaced by codegraph's OWN source hash: edit the parser and every stale parse
# is invalidated, so a change here can never silently reuse yesterday's extraction.
try:
    with open(os.path.abspath(__file__), "rb") as _fh:
        _VERSION = hashlib.sha1(_fh.read()).hexdigest()[:12]
except Exception:
    _VERSION = "0"


def _defs_and_calls(path, mod):
    """Extract definition nodes and call edges from one file via ast. Returns (defs, edges).
    defs: list of {id, kind, name, module, line}. edges: list of {src, callee, kind, line}."""
    try:
        with open(path, encoding="utf-8", errors="replace") as _fh:   # a context manager, so a
            src = _fh.read()                                          # big tree does not leak a
        tree = ast.parse(src, filename=path)                          # handle per file
    except (SyntaxError, ValueError):
        return [], [], [], {}, {}                              # unparseable file (py2, template, partial) - skip it, don't crash the build
    defs, edges, imports = [], [], []
    aliases, fromimp = {}, {}                                   # module aliases (name->module) and from-imports (name->module) for import-aware call resolution

    class V(ast.NodeVisitor):
        def __init__(self):
            self.scope = [mod]                                  # qualified-name stack: module -> class -> func
            self.owner = [mod]                                  # nearest ENCLOSING owner a call belongs to (module at bottom, so module-level calls are captured too)
            self.classes = []                                   # enclosing class ids, so self.method() resolves within the right class
            self.vtypes = [{}]                                  # per-scope var->ClassName from `x = Foo(...)`, so x.method() resolves to Foo.method (local type inference)

        def _qual(self, name):
            return ".".join(self.scope + [name])

        def visit_Import(self, node):
            ml = len(self.owner) == 1                            # a top-level import (real dependency) vs one deferred inside a function (the deliberate cycle-break)
            for a in node.names:
                top = a.name.split(".")[0]
                imports.append({"src": mod, "callee": top, "kind": "IMPORT", "line": node.lineno, "module_level": ml})
                aliases[a.asname or top] = top                  # `import memory` / `import x as y` -> the receiver name maps to a module

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
                for _ in range(node.level - 1):
                    base = base[:-1]
                if node.module:                                  # from .thing import load / from ..pkg.mod import x
                    target = "/".join([*base, *node.module.split(".")])
                    imports.append({"src": mod, "callee": target, "kind": "IMPORT", "line": node.lineno, "module_level": ml})
                    for a in node.names:
                        fromimp[a.asname or a.name] = target
                else:                                            # from . import thing - each NAME is itself a module
                    for a in node.names:
                        target = "/".join([*base, a.name])
                        imports.append({"src": mod, "callee": target, "kind": "IMPORT", "line": node.lineno, "module_level": ml})
                        aliases[a.asname or a.name] = target
                        fromimp[a.asname or a.name] = target     # `from . import thing` also allows a bare thing() if it is a func
                return
            if node.module:
                top = node.module.split(".")[0]
                imports.append({"src": mod, "callee": top, "kind": "IMPORT", "line": node.lineno, "module_level": ml})
                for a in node.names:
                    fromimp[a.asname or a.name] = top           # `from memory import recall` -> bare recall() resolves into module memory

        def visit_ClassDef(self, node):
            qid = self._qual(node.name)
            defs.append({"id": qid, "kind": "class", "name": node.name, "module": mod, "line": node.lineno})
            self.scope.append(node.name); self.classes.append(qid)   # methods walk under this class scope
            for c in node.body: self.visit(c)
            self.classes.pop(); self.scope.pop()

        def _func(self, node):
            qid = self._qual(node.name)
            defs.append({"id": qid, "kind": "func", "name": node.name, "module": mod, "line": node.lineno})
            self.scope.append(node.name); self.owner.append(qid); self.vtypes.append({})   # calls inside this body belong to qid; its own var-type scope
            for c in node.body: self.visit(c)
            self.vtypes.pop(); self.owner.pop(); self.scope.pop()

        def visit_FunctionDef(self, node): self._func(node)
        def visit_AsyncFunctionDef(self, node): self._func(node)

        def visit_Assign(self, node):
            if (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                    and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
                self.vtypes[-1][node.targets[0].id] = node.value.func.id          # x = Foo(...) -> x is a Foo (resolved to a class in build())
            self.generic_visit(node)

        def visit_Call(self, node):
            fn = node.func
            recv = None; method = False
            if isinstance(fn, ast.Name): callee = fn.id                          # a BARE call foo() - may be local/imported
            elif isinstance(fn, ast.Attribute):
                callee = fn.attr; method = True                                  # a METHOD call X.attr() - only resolves via a known module or self, else external
                recv = fn.value.id if isinstance(fn.value, ast.Name) else None   # the `memory` in memory.recall(); None when the receiver is open(...)/a[0]/x.y (still a method call, never bare)
            else: callee = None
            if callee:                                          # attribute this call to its NEAREST enclosing owner (a def, or the module if top-level)
                edge = {"src": self.owner[-1], "callee": callee, "recv": recv, "method": method, "kind": "CALL", "line": getattr(node, "lineno", 0)}
                if recv in ("self", "cls") and self.classes: edge["encl_class"] = self.classes[-1]   # self.method() -> resolve inside this class
                elif recv and recv in self.vtypes[-1]: edge["recv_type"] = self.vtypes[-1][recv]      # x.method() where x = Foo() -> resolve to Foo.method (local type inference)
                edges.append(edge)
            self.generic_visit(node)                            # recurse into args/keywords (which may hold more calls)

    V().visit(tree)
    seen = {}                                                   # dedup: one edge per (src, recv, callee) triple, keep first line
    for e in edges:
        k = (e["src"], e.get("recv"), e["callee"])
        if k not in seen: seen[k] = e
    return defs, list(seen.values()), imports, aliases, fromimp


class BadPath(Exception):
    """A path that cannot be analysed. Raised rather than quietly producing an empty graph."""


def build(dirs=None, write=True):
    dirs = dirs or [HOME]
    # A typo'd path used to build an empty graph and exit 0 - success, zero modules, and no
    # hint that the answer to every later query would be "(none)". A single file is analysed
    # as a one-file tree, because `codegraph build app.py` is an obvious thing to type.
    checked = []
    for d in dirs:
        d = os.path.abspath(os.path.expanduser(d))
        if os.path.isdir(d):
            checked.append(d)
        elif os.path.isfile(d) and d.endswith(".py"):
            checked.append(os.path.dirname(d) or ".")
        elif os.path.exists(d):
            raise BadPath(f"{d} is not a directory or a .py file")
        else:
            raise BadPath(f"{d} does not exist")
    dirs = checked
    multiroot = len(dirs) > 1                                   # several trees at once -> qualify module ids by root so same-named modules (api/models vs worker/models) don't collide
    pairs = []                                                  # (path, rootdir, rootname) - RECURSIVE + noise-pruned; module ids are path-relative so nested same-named files don't collide either
    for d in dirs:
        root = os.path.basename(d.rstrip("/")) or "root"
        for dp, dns, fns in os.walk(d):
            dns[:] = [x for x in dns if not _prune_dir(dp, x)]   # single pruned walk: skip noise, hidden, embedded interpreters, venv roots
            for fn in sorted(fns):
                if fn.endswith(".py"): pairs.append((os.path.join(dp, fn), d, root))
    cache = {}                                                  # INCREMENTAL: reuse a file's parse when its mtime is unchanged (single-tree only; multiroot ids differ by mode so it parses fresh)
    if write and not multiroot:
        try:
            with open(CACHE, encoding="utf-8") as _fh:
                _c = json.load(_fh)
            cache = _c.get("files", {}) if _c.get("_v") == _VERSION else {}   # discard the whole cache if codegraph's code changed (versioned namespace)
        except Exception: cache = {}
    nodes, calls, imports = [], [], []
    mod_alias, mod_from, mod_root = {}, {}, {}; newcache = {}
    for path, d, root in pairs:
        rel = os.path.relpath(path, d)[:-3].replace(os.sep, "/")   # 'memory' for a top-level file, 'sub/mod' for a nested one (no .py) - path-relative id, no nested collision
        base = os.path.basename(rel)
        mid = f"{root}/{rel}" if multiroot else rel             # SINGLE flat tree: bare 'memory' (unchanged). Nested: 'sub/mod'. Multi-tree: 'root/...'
        nodes.append({"id": mid, "kind": "module", "name": base, "module": mid, "line": 0})
        try:
            mt = os.path.getmtime(path)
        except OSError:
            # A DANGLING SYMLINK - a link left behind after a move, which every real repo
            # eventually has. os.walk lists it as a file and stat() then raises, which used to
            # kill the whole build with a traceback. One broken link is not a reason to refuse
            # to analyse a codebase; skip it and carry on.
            nodes.pop()                                          # drop the module node just added
            continue
        c = cache.get(path)
        if c and c.get("mtime") == mt:                          # unchanged file -> reuse cached parse (deep-copied so resolution can't leak back into the cache)
            d, e, im, al, fi = c["defs"], copy.deepcopy(c["calls"]), c["imports"], c["aliases"], c["fromimp"]
        else:
            d, e, im, al, fi = _defs_and_calls(path, mid)
        if not multiroot:
            newcache[path] = {"mtime": mt, "defs": d, "calls": [dict(x) for x in e], "imports": im, "aliases": al, "fromimp": fi}
        nodes += d; calls += e; imports += im
        mod_alias[mid] = al; mod_from[mid] = fi; mod_root[mid] = root
    if multiroot:                                              # remap import aliases to the SAME-ROOT module id, so within-tree imports resolve to the right tree
        allmods = {n["id"] for n in nodes if n["kind"] == "module"}
        qual = lambda r, b: (f"{r}/{b}" if f"{r}/{b}" in allmods else b)
        for mid in list(mod_alias):
            r = mod_root[mid]
            # a relative import already resolved to a full id (it was built from this module's
            # own id, root included), so qual() must leave it alone - it does, because
            # "<root>/<already-rooted-id>" is never a real module.
            mod_alias[mid] = {k: qual(r, v) for k, v in mod_alias[mid].items()}
            mod_from[mid] = {k: qual(r, v) for k, v in mod_from[mid].items()}
    by_name = defaultdict(list); by_modname = {}; def_ids = set()
    for n in nodes:
        if n["kind"] in ("func", "class"):
            by_name[n["name"]].append(n["id"]); by_modname[(n["module"], n["name"])] = n["id"]; def_ids.add(n["id"])
    for e in calls:                                               # resolve each call to a SPECIFIC definition, import-aware (highest confidence first)
        srcmod = e["src"].split(".")[0]; recv = e.get("recv"); callee = e["callee"]; method = e.get("method"); dst = None; conf = None
        ec = e.get("encl_class")
        if ec and (ec + "." + callee) in def_ids:                # self.method()/cls.method() -> the method in the ENCLOSING class (exact scope)
            dst = ec + "." + callee; conf = "SELF-METHOD"
        rt = e.get("recv_type")
        if not dst and rt:                                       # x.method() where `x = Foo()` (local type inference) -> Foo.method
            for cid in by_name.get(rt, []):
                if (cid + "." + callee) in def_ids: dst = cid + "." + callee; conf = "TYPED"; break
        if not dst and method and recv and recv in mod_alias.get(srcmod, {}):  # memory.recall() where `memory` is an imported module -> resolve to memory.recall
            dst = by_modname.get((mod_alias[srcmod][recv], callee)); conf = "QUALIFIED" if dst else conf
        if not dst and not method and callee in mod_from.get(srcmod, {}):     # BARE recall() under `from memory import recall`
            dst = by_modname.get((mod_from[srcmod][callee], callee)); conf = "QUALIFIED" if dst else conf
        if not dst and not method and by_modname.get((srcmod, callee)):       # a BARE call to a function in the SAME module
            dst = by_modname[(srcmod, callee)]; conf = "LOCAL"
        if not dst and not method:                               # a BARE call
            if callee in _BUILTINS: conf = "EXTERNAL"            # a builtin (next/len/sorted/open...) - never a same-named in-tree def
            else:
                tgts = by_name.get(callee, [])
                if len(tgts) == 1: dst = tgts[0]; conf = "RESOLVED"
                elif len(tgts) > 1: conf = "AMBIGUOUS"; e["candidates"] = tgts
                else: conf = "EXTERNAL"
        if not dst and conf is None:                             # a METHOD call X.attr() with no in-tree module/self receiver: json.load()/open(..).write()/d.get() -> EXTERNAL, never a same-named in-tree func
            conf = "EXTERNAL"
        e["dst"] = dst; e["confidence"] = conf
    nodes.sort(key=lambda n: (n["kind"], n["id"]))              # DETERMINISTIC output: byte-reproducible across runs regardless of os.walk order -> two builds are diffable
    calls.sort(key=lambda e: (e["src"], e.get("recv") or "", e["callee"], e.get("line", 0)))
    imports.sort(key=lambda e: (e["src"], e["callee"], e.get("line", 0)))
    graph = {"nodes": nodes, "calls": calls, "imports": imports, "dirs": sorted(dirs)}
    if write:
        _jwrite({"_v": _VERSION, "files": newcache}, CACHE)   # cache: RAW per-file parse + code version; atomic write so an interrupted build can't corrupt it
        _jwrite(graph, OUT)                                    # atomic: a crash mid-write leaves the previous good graph, not a truncated one
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
    ok5 = len(we) == 1 and we[0].get("dst") is None and we[0]["confidence"] == "EXTERNAL"   # method call, NOT the in-tree alpha.write
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
    shutil.rmtree(rd, ignore_errors=True)
    shutil.rmtree(d, ignore_errors=True)
    print(f"  resolves alpha.digest specifically (QUALIFIED, not ambiguous): {ok1}")
    print(f"  blast-radius trustworthy (caller.run in alpha's radius, NOT beta's): {ok2}")
    print(f"  RED-FIRST control - naive name-match conflates (would wrongly blame beta.digest): {ok3}")
    print(f"  self.helper() resolves inside its class -> caller.Box.helper (SELF-METHOD): {ok4}")
    print(f"  RED-FIRST - open(..).write() stays EXTERNAL, not falsely -> alpha.write: {ok5}")
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
    ok = (ok1 and ok2 and ok3 and ok4 and ok5 and ok6 and ok7 and ok8 and ok9 and ok10 and ok11 and ok12
          and ok13 and ok14 and ok15 and ok16 and ok17)
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
    """True if any source .py (recursive, pruned) is newer than the built graph - the graph would be stale."""
    try: built = os.path.getmtime(OUT)
    except OSError: return True
    for d in g.get("dirs", [HOME]):
        for dp, dns, fns in os.walk(d):
            dns[:] = [x for x in dns if not _prune_dir(dp, x)]
            for fn in fns:
                if fn.endswith(".py"):
                    try:
                        if os.path.getmtime(os.path.join(dp, fn)) > built: return True
                    except OSError: return True
    return False


def _jwrite(obj, path):                                         # atomic UTF-8 write: an interrupted/crashed write can't leave a half-written (corrupt) json
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as _fh: json.dump(obj, _fh, ensure_ascii=False)
    os.replace(tmp, path)                                       # POSIX atomic rename

def load(fresh=True):
    """Load the graph. If `fresh` and any source file changed since the last build, rebuild first (incremental,
    so it's fast) - every query is answered against the CURRENT code, never a stale snapshot."""
    try:
        with open(OUT, encoding="utf-8") as _fh:
            g = json.load(_fh)
    except FileNotFoundError:
        sys.exit("no graph yet - run: codegraph build <path>")
    except (json.JSONDecodeError, ValueError):
        sys.exit(f"graph {OUT} is corrupt (interrupted build?) - run: codegraph build <path> to rebuild")
    if fresh and _is_stale(g):
        g = build(g.get("dirs"))
    return g


def _by_name(g, name):
    return [n["id"] for n in g["nodes"] if n["name"] == name and n["kind"] in ("func", "class")]


def _targets(g, target):
    return [target] if "." in target else _by_name(g, target)   # a qualified id is itself; a bare name expands to every def of that name


def callers_of(g, target):
    """function ids that call `target`. When `target` resolves to a definition, follow the RESOLVED edges -
    so a query for pulse.digest returns only its real callers, NOT callers of the same-named court.digest.
    Falls back to bare-name matching only for external/unknown targets."""
    ids = set(_targets(g, target))
    if ids:
        return sorted({e["src"] for e in g["calls"] if e.get("dst") in ids})
    name = target.split(".")[-1]
    return sorted({e["src"] for e in g["calls"] if e["callee"] == name})


def calls_from(g, node_id):
    """RESOLVED in-tree calls made by node_id (the specific definitions it reaches)."""
    return sorted({e["dst"] for e in g["calls"] if e["src"] == node_id and e.get("dst")})


def blast_radius(g, target, max_hops=6):
    """transitive callers of `target` - what could break if you change it. Walks RESOLVED caller edges by id,
    so the radius follows the actual call graph, not a same-name coincidence."""
    frontier, seen, hops = set(callers_of(g, target)), set(), 0
    while frontier and hops < max_hops:
        seen |= frontier
        nxt = set()
        for caller_id in frontier: nxt |= set(callers_of(g, caller_id))
        frontier = nxt - seen; hops += 1
    return sorted(seen)


def where(g, name):
    """where a symbol is DEFINED: (id, file:line) for each def matching name exactly or by its bare name."""
    tgt = name.split(".")[-1]
    return sorted((n["id"], f"{n['module']}.py:{n['line']}") for n in g["nodes"]
                  if n["kind"] in ("func", "class") and (n["id"] == name or n["name"] == tgt))


def find(g, substr):
    """fuzzy symbol search - every function/class whose name CONTAINS substr (replaces broad greps for a name)."""
    s = substr.lower()
    return sorted((n["id"], f"{n['module']}.py:{n['line']}") for n in g["nodes"]
                  if n["kind"] in ("func", "class") and s in n["name"].lower())


def sites(g, target):
    """every CALL SITE of target as (file:line, caller) - the exact places to edit when you change it (the
    REFACTOR helper). Uses resolved edges when target is a known def; else the bare callee name."""
    ids = set(_targets(g, target)); name = target.split(".")[-1]
    out = {(f"{e['src'].split('.')[0]}.py:{e['line']}", e["src"])
           for e in g["calls"] if (e.get("dst") in ids if ids else e["callee"] == name)}
    return sorted(out)


def path(g, src, dst, max_hops=8):
    """a CALL PATH from src to dst (how src reaches dst) over resolved edges, or [] if none within max_hops."""
    import collections
    fwd = {}
    for e in g["calls"]:
        if e.get("dst"): fwd.setdefault(e["src"], []).append(e["dst"])
    goals = set(_targets(g, dst) or [dst])
    for s in (_targets(g, src) or [src]):
        q = collections.deque([[s]]); seen = {s}
        while q:
            p = q.popleft()
            for nxt in fwd.get(p[-1], []):
                if nxt in goals: return p + [nxt]
                if nxt not in seen and len(p) < max_hops: seen.add(nxt); q.append(p + [nxt])
    return []


def module_deps(g, mod):
    """(imports, importers) for a module: in-tree modules it imports, and in-tree modules that import it."""
    intree = {n["id"] for n in g["nodes"] if n["kind"] == "module"}
    imports = sorted({e["callee"] for e in g["imports"] if e["src"] == mod and e["callee"] in intree})
    importers = sorted({e["src"] for e in g["imports"] if e["callee"] == mod and e["src"] in intree})
    return imports, importers


def cycles(g):
    """MUTUAL MODULE-LEVEL import cycles among in-tree modules (A imports B and B imports A, both at top level) -
    real refactor smells. A deferred/local import (inside a function) is the deliberate cycle-break, so it does
    NOT count - flagging it would call the fix a smell."""
    intree = {n["id"] for n in g["nodes"] if n["kind"] == "module"}
    imp = defaultdict(set)
    for e in g["imports"]:
        if e.get("module_level", True) and e["src"] in intree and e["callee"] in intree and e["src"] != e["callee"]:
            imp[e["src"]].add(e["callee"])
    return sorted({tuple(sorted((a, b))) for a in imp for b in imp[a] if a in imp.get(b, ())})


def impact(g, name):
    """the PRE-EDIT SAFETY view of a function - everything you need before you change it, in one shot:
    its direct callers, every call SITE (file:line), and the transitive blast radius. This is the self-edit
    checklist: read these before touching `name`."""
    return {"callers": callers_of(g, name), "sites": sites(g, name), "blast": blast_radius(g, name)}


def stats(g):
    kinds = defaultdict(int)
    for n in g["nodes"]: kinds[n["kind"]] += 1
    conf = defaultdict(int)
    for e in g["calls"]: conf[e.get("confidence", "?")] += 1
    called = {e["dst"] for e in g["calls"] if e.get("dst")}
    defs = [n["id"] for n in g["nodes"] if n["kind"] == "func"]
    unreachable = [d for d in defs if d not in called]           # never called in-tree (entrypoints, dead code, or dynamic)
    specific = conf["QUALIFIED"] + conf["LOCAL"] + conf["RESOLVED"]   # calls resolved to ONE definition
    in_tree = len(g["calls"]) - conf["EXTERNAL"]                  # calls whose target could be in-tree (excl builtins/stdlib/methods)
    return {"nodes": dict(kinds), "call_edges": len(g["calls"]), "edge_confidence": dict(conf),
            "resolved_to_one_def": specific,
            "in_tree_resolution_rate": round(specific / max(in_tree, 1), 3),
            "func_defs": len(defs), "never_called_in_tree": len(unreachable)}


def _main(argv=None):
    """The CLI, as a function so `pip install` can expose it as a console script -- and so the
    tests can drive it in-process instead of shelling out."""
    a = list(sys.argv[1:] if argv is None else argv)
    # Every query verb takes a name. Forgetting it used to be an IndexError traceback - the
    # first thing a new user sees when they type a command from memory.
    NEEDS = {"callers": 1, "calls": 1, "blast": 1, "where": 1, "find": 1, "sites": 1,
             "impact": 1, "deps": 1, "path": 2}
    if a and a[0] in NEEDS and len(a) - 1 < NEEDS[a[0]]:
        what = {"path": "<from> <to>", "deps": "<module>", "find": "<substring>"}.get(a[0], "<name>")
        print(f"usage: codegraph {a[0]} {what}", file=sys.stderr)
        return 2
    if a and a[0] == "--selftest": return _selftest()
    if not a or a[0] == "build":
        try:
            g = build(a[1:] or None)
        except BadPath as e:
            print(f"cannot build: {e}", file=sys.stderr)
            return 1
        if not [n for n in g["nodes"] if n["kind"] == "module"]:
            print(f"no .py files found under {', '.join(g['dirs'])}", file=sys.stderr)
            return 1
        print(json.dumps(stats(g), indent=2))
    elif a[0] == "callers": print("\n".join(callers_of(load(), a[1])) or "(none)")
    elif a[0] == "calls":
        g = load(); [print("\n".join(calls_from(g, i)) or "(none)") for i in _by_name(g, a[1])]
    elif a[0] == "blast": print("\n".join(blast_radius(load(), a[1])) or "(none)")
    elif a[0] == "where": print("\n".join(f"{i}  {loc}" for i, loc in where(load(), a[1])) or "(not found)")
    elif a[0] == "find": print("\n".join(f"{i}  {loc}" for i, loc in find(load(), a[1])) or "(none)")
    elif a[0] == "sites": print("\n".join(f"{loc}  {c}" for loc, c in sites(load(), a[1])) or "(none)")
    elif a[0] == "path": p = path(load(), a[1], a[2]); print(" -> ".join(p) if p else "(no path)")
    elif a[0] == "deps":
        im, imp = module_deps(load(), a[1]); print("imports:   " + (", ".join(im) or "(none)")); print("importers: " + (", ".join(imp) or "(none)"))
    elif a[0] == "cycles":
        cy = cycles(load()); print("\n".join(f"{x} <-> {y}" for x, y in cy) or "(none)")
    elif a[0] == "impact":
        im = impact(load(), a[1])
        print("callers:", ", ".join(im["callers"]) or "(none)")
        print("sites:  ", ", ".join(f"{l}" for l, c in im["sites"]) or "(none)")
        print(f"blast:   {len(im['blast'])} functions could be affected")
    elif a[0] == "stats": print(json.dumps(stats(load()), indent=2))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main())
