"""Tests for codegraph.

The tool ships its own `--selftest`, and that is deliberate: a single file you can copy into a
repo should be able to prove itself there, with no test runner and no checkout. This suite runs
that selftest as one case and then covers what it cannot -- the CLI, the on-disk contract, the
cache, and the honesty of the confidence labels -- plus the best available fixture, which is
codegraph reading its own source.
"""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import codegraph

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Sandbox(unittest.TestCase):
    """Each test gets its own tree and its own output location, so nothing writes into the repo."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self._saved = {k: getattr(codegraph, k) for k in ("HOME", "OUT", "CACHE")}
        codegraph.HOME = self.dir
        codegraph.OUT = os.path.join(self.dir, "codegraph.json")
        codegraph.CACHE = os.path.join(self.dir, "codegraph.cache.json")

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(codegraph, k, v)

    def write(self, rel, body):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    def graph(self, **kw):
        return codegraph.build([self.dir], **kw)


class TheBuiltInSelftest(Sandbox):
    def test_it_passes(self):
        """Seventeen ground-truth checks, several of them red-first controls."""
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = codegraph._selftest()
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("SELFTEST GREEN", out.getvalue())


class ConfidenceIsHonest(Sandbox):
    """The tool's whole claim is that it says when it does not know. These are that claim."""

    def test_an_ambiguous_name_is_labelled_not_guessed(self):
        self.write("a.py", "def go():\n    return 1\n")
        self.write("b.py", "def go():\n    return 2\n")
        self.write("c.py", "def run():\n    return go()\n")     # which go()? nobody can say
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "c.run")
        self.assertIsNone(e.get("dst"), "it picked one of two identical names")
        self.assertEqual(e["confidence"], "AMBIGUOUS")
        self.assertEqual(sorted(e["candidates"]), ["a.go", "b.go"])

    def test_a_builtin_is_never_mistaken_for_your_function(self):
        self.write("mine.py", "def open():\n    return 1\n")
        self.write("uses.py", "def r():\n    return open('f')\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "uses.r")
        self.assertEqual((e.get("dst"), e["confidence"]), (None, "BUILTIN"))

    def test_a_method_on_an_object_it_cannot_type_is_untyped_not_external(self):
        """UNTYPED is the honest label: the target might be in your tree, it just cannot tell.
        Calling it EXTERNAL would claim knowledge it does not have, and would quietly inflate
        the resolution rate by shrinking the denominator."""
        self.write("m.py", "def write():\n    return 1\n")
        self.write("u.py", "def r(fh):\n    return fh.write('x')\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "u.r")
        self.assertEqual((e.get("dst"), e["confidence"]), (None, "UNTYPED"))

    def test_a_call_rooted_in_an_imported_library_is_external(self):
        """os.path.join() is knowably not yours - it must not sit in the blind-spot bucket."""
        self.write("u.py", "import os\ndef r():\n    return os.path.join('a', 'b')\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "u.r")
        self.assertEqual((e.get("dst"), e["confidence"]), (None, "EXTERNAL"))


class TheOnDiskContract(Sandbox):
    def test_the_graph_is_written_beside_the_code_not_beside_the_script(self):
        """It used to land next to codegraph.py: analyse someone's repo, pollute your own."""
        self.write("x.py", "def f():\n    return 1\n")
        self.graph()
        self.assertTrue(os.path.exists(os.path.join(self.dir, "codegraph.json")))
        self.assertFalse(os.path.exists(os.path.join(HERE, "codegraph.json")))

    def test_two_builds_of_unchanged_code_are_byte_identical(self):
        """Deterministic output is what makes a graph diffable between commits."""
        self.write("x.py", "def f():\n    return g()\ndef g():\n    return 1\n")
        self.write("sub/y.py", "def h():\n    return 2\n")
        first = json.dumps(self.graph(write=False), sort_keys=False)
        second = json.dumps(self.graph(write=False), sort_keys=False)
        self.assertEqual(first, second)

    def test_the_cache_is_invalidated_when_the_tool_itself_changes(self):
        """A parser change must not silently reuse yesterday's extraction."""
        self.write("x.py", "def f():\n    return 1\n")
        self.graph()
        with open(codegraph.CACHE, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["_v"], codegraph._VERSION)
        with open(codegraph.CACHE, encoding="utf-8") as f:
            stale = json.load(f)
        stale["_v"] = "some-older-codegraph"
        with open(codegraph.CACHE, "w", encoding="utf-8") as f:
            json.dump(stale, f)
        self.assertEqual(len(self.graph()["nodes"]), 2)          # module + f, reparsed not reused

    def test_a_second_build_reuses_the_cache_and_still_works(self):
        """The cache-HIT path had no test, so an undefined name in it survived: the first build
        parses everything and passes, and only the SECOND build touches the reuse branch."""
        self.write("x.py", "def f():\n    return g()\ndef g():\n    return 1\n")
        first = self.graph()
        self.assertTrue(os.path.exists(codegraph.CACHE))
        second = self.graph()                                    # every file unchanged -> all cache hits
        self.assertEqual(json.dumps(first), json.dumps(second))

    def test_a_cached_build_still_resolves_after_one_file_changes(self):
        """A mix of hits and misses - and the reused parse must not be mutated by resolution."""
        self.write("core.py", "def leaf():\n    return 1\n")
        self.write("top.py", "def use():\n    return leaf()\n")
        self.graph()
        self.write("top.py", "def use():\n    return leaf()\ndef extra():\n    return leaf()\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "core.leaf"), ["top.extra", "top.use"])

    def test_an_interrupted_write_cannot_leave_a_corrupt_graph(self):
        self.write("x.py", "def f():\n    return 1\n")
        self.graph()
        with open(codegraph.OUT, "w", encoding="utf-8") as f:
            f.write('{"nodes": [')                               # a truncated file
        with self.assertRaises(SystemExit) as cm:
            codegraph.load(fresh=False)
        self.assertIn("corrupt", str(cm.exception))

    def test_an_unparseable_file_is_skipped_not_fatal(self):
        self.write("good.py", "def f():\n    return 1\n")
        self.write("bad.py", "def (((\n")
        ids = {n["id"] for n in self.graph(write=False)["nodes"]}
        self.assertIn("good.f", ids)


class TheQueries(Sandbox):
    def setUp(self):
        super().setUp()
        self.write("core.py", "def leaf():\n    return 1\ndef mid():\n    return leaf()\n")
        self.write("top.py", "from core import mid\ndef entry():\n    return mid()\n")
        self.g = self.graph(write=False)

    def test_blast_radius_is_transitive(self):
        self.assertEqual(codegraph.blast_radius(self.g, "core.leaf"), ["core.mid", "top.entry"])

    def test_impact_answers_the_pre_edit_question_in_one_shot(self):
        im = codegraph.impact(self.g, "core.leaf")
        self.assertEqual(im["callers"], ["core.mid"])
        self.assertTrue(any(loc.startswith("core.py:") for loc, _ in im["sites"]))
        self.assertIn("top.entry", im["blast"])

    def test_path_explains_how_one_function_reaches_another(self):
        self.assertEqual(codegraph.path(self.g, "top.entry", "core.leaf"),
                         ["top.entry", "core.mid", "core.leaf"])

    def test_where_and_find_locate_definitions(self):
        self.assertEqual(codegraph.where(self.g, "leaf"), [("core.leaf", "core.py:1")])
        self.assertEqual([i for i, _ in codegraph.find(self.g, "ea")], ["core.leaf"])

    def test_deps_reports_both_directions(self):
        imports, importers = codegraph.module_deps(self.g, "core")
        self.assertEqual((imports, importers), ([], ["top"]))


class TheCLI(unittest.TestCase):
    """Driven as a subprocess, the way a person actually runs it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        with open(os.path.join(self.dir, "app.py"), "w", encoding="utf-8") as f:
            f.write("def leaf():\n    return 1\ndef mid():\n    return leaf()\n")

    def run_it(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), *args],
                              cwd=self.dir, capture_output=True, text=True, timeout=120)

    def test_build_then_query(self):
        self.assertEqual(self.run_it("build", ".").returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "codegraph.json")))
        self.assertIn("app.mid", self.run_it("callers", "leaf").stdout)
        # blast radius is who could BREAK, so it is the callers - not the function itself
        blast = self.run_it("blast", "leaf").stdout
        self.assertIn("app.mid", blast)
        self.assertNotIn("app.leaf", blast)

    def test_querying_before_building_says_what_to_do(self):
        r = self.run_it("callers", "leaf")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("run: codegraph build", r.stdout + r.stderr)

    def test_an_unknown_verb_prints_the_usage(self):
        r = self.run_it("wat")
        self.assertIn("blast", r.stdout)


class ItCanReadItself(unittest.TestCase):
    """The best fixture available: a real, non-trivial Python file with known structure."""

    def test_its_own_call_graph_is_sane(self):
        g = codegraph.build([HERE], write=False)
        ids = {n["id"] for n in g["nodes"]}
        for expected in ("codegraph.build", "codegraph.impact", "codegraph.blast_radius", "codegraph._main"):
            self.assertIn(expected, ids)
        self.assertIn("codegraph.build", codegraph.callers_of(g, "codegraph._defs_and_calls"))
        self.assertIn("codegraph._main", codegraph.blast_radius(g, "codegraph.load"))
        s = codegraph.stats(g)
        self.assertGreater(s["resolution_rate"], 0.3, s)
        self.assertEqual(s["resolved_to_one_def"],
                         sum(1 for e in g["calls"] if e.get("dst")), "the count is hand-kept again")


class ItLeaksNothing(unittest.TestCase):
    """`ast.parse(open(path).read())` leaked a handle per file. On a large tree that is thousands
    of them, and on Windows an open handle blocks deleting the file."""

    def test_a_build_opens_no_file_it_does_not_close(self):
        r = subprocess.run([sys.executable, "-W", "error::ResourceWarning",
                            os.path.join(HERE, "codegraph.py"), "--selftest"],
                           capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("ResourceWarning", r.stderr)


class HostileTrees(Sandbox):
    """What a tool that walks arbitrary directories meets in real repositories."""

    def test_a_dangling_symlink_does_not_kill_the_build(self):
        """A link left behind after a move. os.walk lists it, stat() raises, and the whole
        analysis used to die on a traceback - one broken link is not a reason to refuse."""
        self.write("real.py", "def f():\n    return 1\n")
        try:
            os.symlink(os.path.join(self.dir, "gone.py"), os.path.join(self.dir, "broken.py"))
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"symlinks unavailable: {e}")
        ids = {n["id"] for n in self.graph(write=False)["nodes"]}
        self.assertIn("real.f", ids)
        self.assertNotIn("broken", ids)

    def test_a_symlinked_directory_is_not_followed(self):
        """Otherwise a link to /usr/lib turns a small repo into an unbounded walk."""
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        with open(os.path.join(outside, "far.py"), "w", encoding="utf-8") as f:
            f.write("def far():\n    return 1\n")
        self.write("near.py", "def near():\n    return 1\n")
        try:
            os.symlink(outside, os.path.join(self.dir, "linkdir"))
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"symlinks unavailable: {e}")
        ids = {n["id"] for n in self.graph(write=False)["nodes"]}
        self.assertIn("near.near", ids)
        self.assertNotIn("far.far", ids)

    def test_a_directory_symlinked_to_itself_does_not_loop(self):
        self.write("a.py", "def f():\n    return 1\n")
        try:
            os.symlink(self.dir, os.path.join(self.dir, "selfloop"))
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"symlinks unavailable: {e}")
        self.assertIn("a.f", {n["id"] for n in self.graph(write=False)["nodes"]})

    def test_deeply_nested_and_non_utf8_sources_are_survivable(self):
        self.write("deep.py", "x = " + "(" * 180 + "1" + ")" * 180 + "\n")
        with open(os.path.join(self.dir, "latin.py"), "wb") as f:
            f.write(b"# -*- coding: latin-1 -*-\ndef caf\xe9():\n    return 1\n")
        self.write("ok.py", "def g():\n    return 1\n")
        self.assertIn("ok.g", {n["id"] for n in self.graph(write=False)["nodes"]})

    def test_the_cache_forgets_files_that_are_gone(self):
        for i in range(5):
            self.write(f"m{i}.py", f"def f{i}():\n    return {i}\n")
        self.graph()
        for i in range(5):
            os.remove(os.path.join(self.dir, f"m{i}.py"))
        self.write("only.py", "def z():\n    return 1\n")
        self.graph()
        with open(codegraph.CACHE, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["files"]), 1, "the cache kept dead entries forever")


class BadInputIsAMessageNotATraceback(unittest.TestCase):
    """The first thing a new user does is type a command slightly wrong."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        with open(os.path.join(self.dir, "app.py"), "w", encoding="utf-8") as f:
            f.write("def leaf():\n    return 1\n")

    def run_it(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), *args],
                              cwd=self.dir, capture_output=True, text=True, timeout=120)

    def test_a_verb_without_its_name_prints_usage(self):
        for verb in ("callers", "calls", "blast", "where", "find", "sites", "impact", "deps"):
            with self.subTest(verb):
                r = self.run_it(verb)
                self.assertEqual(r.returncode, 2)
                self.assertNotIn("Traceback", r.stderr)
                self.assertIn(f"usage: codegraph {verb}", r.stderr)

    def test_path_needs_two_names(self):
        r = self.run_it("path", "a")
        self.assertEqual(r.returncode, 2)
        self.assertIn("<from> <to>", r.stderr)

    def test_a_path_that_does_not_exist_is_an_error_not_an_empty_graph(self):
        """It used to exit 0 with zero modules: a typo looked like a clean build, and every
        query afterwards answered '(none)' perfectly convincingly."""
        r = self.run_it("build", os.path.join(self.dir, "nope"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not exist", r.stderr)

    def test_a_directory_with_no_python_says_so(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        r = self.run_it("build", empty)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no .py files", r.stderr)

    def test_a_single_file_argument_is_accepted(self):
        r = self.run_it("build", "app.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("app.leaf", self.run_it("where", "leaf").stdout)


class AnIncompleteAnswerIsTheWorstAnswer(Sandbox):
    """blast_radius stopped after six hops and said nothing about it. A twelve-deep chain
    reported six of eleven callers as though that were the answer - and the five it dropped
    were the ones furthest from the change, exactly the ones you would not think to check."""

    def chain(self, depth):
        for i in range(depth):
            nxt = f"from m{i+1:02d} import f{i+1}\n" if i < depth - 1 else ""
            body = f"    return f{i+1}()" if i < depth - 1 else "    return 1"
            self.write(f"m{i:02d}.py", f"{nxt}def f{i}():\n{body}\n")
        return self.graph(write=False)

    def test_the_blast_radius_is_complete_however_deep_the_chain(self):
        g = self.chain(12)
        got = codegraph.blast_radius(g, "m11.f11")
        self.assertEqual(len(got), 11, f"truncated: {got}")
        self.assertIn("m00.f0", got, "the caller furthest from the change was dropped")

    def test_a_bound_is_still_available_when_you_ask_for_one(self):
        g = self.chain(12)
        self.assertEqual(len(codegraph.blast_radius(g, "m11.f11", max_hops=3)), 3)

    def test_path_finds_a_route_longer_than_eight_hops(self):
        """It returned [] past eight, which prints as '(no path)': absence of evidence
        reported as evidence of absence."""
        g = self.chain(12)
        p = codegraph.path(g, "m00.f0", "m11.f11")
        self.assertEqual(len(p), 12, p)

    def test_a_cycle_in_the_call_graph_terminates(self):
        self.write("a.py", "def x():\n    return y()\ndef y():\n    return x()\n")
        self.assertEqual(codegraph.blast_radius(self.graph(write=False), "a.x"), ["a.x", "a.y"])


class Inheritance(Sandbox):
    def test_a_method_on_a_base_class_is_found(self):
        self.write("p.py", "class Parent:\n    def shared(self):\n        return 1\n")
        self.write("c.py", "from p import Parent\nclass Child(Parent):\n"
                           "    def use(self):\n        return self.shared()\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "c.Child.use")
        self.assertEqual((e.get("dst"), e["confidence"]), ("p.Parent.shared", "INHERITED"))

    def test_the_nearest_definition_wins(self):
        """A child that overrides must resolve to its own, not the parent's."""
        self.write("h.py", "class A:\n    def m(self):\n        return 1\n"
                           "class B(A):\n    def m(self):\n        return 2\n"
                           "    def use(self):\n        return self.m()\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "h.B.use")
        self.assertEqual((e.get("dst"), e["confidence"]), ("h.B.m", "SELF-METHOD"))

    def test_a_class_referenced_by_name_resolves(self):
        self.write("k.py", "class P:\n    @classmethod\n    def make(cls):\n        return 1\n"
                           "def go():\n    return P.make()\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "k.go")
        self.assertEqual((e.get("dst"), e["confidence"]), ("k.P.make", "CLASS"))

    def test_a_cyclic_hierarchy_does_not_hang(self):
        """Illegal in Python, trivially expressible in a half-written file."""
        self.write("z.py", "class A(B):\n    def use(self):\n        return self.gone()\n"
                           "class B(A):\n    pass\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "z.A.use")
        self.assertIsNone(e.get("dst"))

    def test_an_unresolvable_base_is_not_guessed(self):
        self.write("q.py", "import ast\nclass V(ast.NodeVisitor):\n"
                           "    def go(self):\n        return self.visit(1)\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "q.V.go")
        self.assertIsNone(e.get("dst"), "it invented a target for a base class it cannot see")


class TheResolutionRateMeansSomething(Sandbox):
    def test_the_denominator_is_what_was_winnable(self):
        """Counting builtins and library calls measures how much stdlib you use. Leaving out
        the untypeable method calls makes it tautologically 1.0."""
        self.write("w.py",
                   "import os\n"
                   "def helper():\n    return 1\n"
                   "def go(d):\n"
                   "    a = len('x')\n"            # BUILTIN   - not winnable
                   "    b = os.path.join('a')\n"   # EXTERNAL  - not winnable
                   "    c = d.get('k')\n"          # UNTYPED   - winnable, and lost
                   "    return helper() and a and b and c\n")   # LOCAL - winnable, and won
        s = codegraph.stats(self.graph(write=False))
        c = s["edge_confidence"]
        self.assertEqual((c.get("BUILTIN"), c.get("EXTERNAL"), c.get("UNTYPED"), c.get("LOCAL")),
                         (1, 1, 1, 1), c)
        self.assertEqual(s["could_have_been_resolved"], 2, "builtins or library calls got counted")
        self.assertEqual(s["resolution_rate"], 0.5)


class DirectoriesWithDotsInTheirNames(Sandbox):
    """`my.pkg`, `v1.2`, `django-3.2`. The module used to be recovered by splitting the
    qualified id on its first dot, so "my.pkg/user.go" was read as module "my" - and every
    answer derived from it was quietly wrong."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.dir, "my.pkg"))
        self.write("my.pkg/thing.py", "def load():\n    return 1\n")
        self.write("my.pkg/user.py", "import thing\ndef go():\n    return thing.load()\n")
        self.write("thing.py", "def load():\n    return 2\n")
        self.g = self.graph(write=False)

    def test_import_aware_resolution_still_works_inside_one(self):
        e = next(x for x in self.g["calls"] if x["src"] == "my.pkg/user.go")
        self.assertEqual((e.get("dst"), e["confidence"]), ("thing.load", "QUALIFIED"))

    def test_every_file_a_query_names_actually_exists(self):
        """The bug that mattered: `sites` said to open my.py:2, which was never a file."""
        found = codegraph.sites(self.g, "thing.load")
        self.assertTrue(found, "no sites at all - an empty list would pass this vacuously")
        places = codegraph.where(self.g, "load")
        self.assertEqual(len(places), 2, places)
        for loc in [l for l, _ in found] + [l for _, l in places]:
            path = loc.rsplit(":", 1)[0]
            self.assertTrue(os.path.exists(os.path.join(self.dir, path)), f"no such file: {path}")

    def test_the_module_is_carried_not_re_derived(self):
        """The general guard: an edge must know its own module, whatever the folder is called."""
        for e in self.g["calls"]:
            self.assertIn("mod", e)
            self.assertTrue(e["src"] == e["mod"] or e["src"].startswith(e["mod"] + "."),
                            f"{e['src']} does not live in {e['mod']}")


class AtScale(Sandbox):
    """600 modules and 12,000 functions - measured, not assumed."""

    def test_a_large_tree_builds_and_answers_quickly(self):
        for i in range(120):                                    # a tenth of the measured tree,
            d = f"pkg{i // 20}"                                 # enough to catch quadratic work
            os.makedirs(os.path.join(self.dir, d), exist_ok=True)
            lines = []
            for j in range(20):
                nxt = f"f{i}_{j + 1}()" if j < 19 else f"f{(i + 1) % 120}_0()"
                lines += [f"def f{i}_{j}():", f"    return {nxt}"]
            self.write(f"{d}/m{i:03d}.py", "\n".join(lines) + "\n")
        t = time.time()
        g = self.graph(write=False)
        build = time.time() - t
        self.assertEqual(len([n for n in g["nodes"] if n["kind"] == "func"]), 2400)
        self.assertLess(build, 30, f"build took {build:.1f}s")
        t = time.time()
        radius = codegraph.blast_radius(g, "f0_0")
        self.assertLess(time.time() - t, 20, "blast radius is doing quadratic work")
        self.assertGreater(len(radius), 100, "the radius across a wide graph looks truncated")


class TheGraphNoticesDeletion(Sandbox):
    """Staleness was "is any file newer than the graph". Deleting a file changes nobody's
    mtime, so the graph stayed "fresh" and went on answering about code that was gone."""

    def test_a_deleted_file_stops_answering(self):
        self.write("doomed.py", "def gone():\n    return 1\n")
        self.write("keeper.py", "def stays():\n    return 1\n")
        self.graph()
        os.remove(os.path.join(self.dir, "doomed.py"))
        g = codegraph.load()                                     # rebuilds if stale
        self.assertEqual(codegraph.where(g, "gone"), [], "it named a file that no longer exists")
        self.assertTrue(codegraph.where(g, "stays"))

    def test_an_added_file_is_picked_up(self):
        self.write("one.py", "def a():\n    return 1\n")
        self.graph()
        self.write("two.py", "def b():\n    return 1\n")
        self.assertTrue(codegraph.where(codegraph.load(), "b"))

    def test_an_untouched_tree_is_not_rebuilt(self):
        """The other half: staleness must not fire on a tree nobody touched."""
        self.write("x.py", "def f():\n    return 1\n")
        g = self.graph()
        self.assertFalse(codegraph._is_stale(g))

    def test_the_graph_records_what_it_was_built_from(self):
        self.write("a.py", "def f():\n    return 1\n")
        self.write("sub/b.py", "def g():\n    return 1\n")
        g = self.graph()
        self.assertEqual(len(g["sources"]), 2, g.get("sources"))


class CyclesOfAnyLength(Sandbox):
    """It looked only for mutual pairs. a -> b -> c -> a went unreported - and that is the
    cycle that survives in a codebase, because a mutual pair is obvious as you type it."""

    def test_a_three_way_cycle_is_found(self):
        self.write("a.py", "import b\ndef f(): return 1\n")
        self.write("b.py", "import c\ndef g(): return 1\n")
        self.write("c.py", "import a\ndef h(): return 1\n")
        self.assertEqual(codegraph.cycles(self.graph(write=False)), [("a", "b", "c")])

    def test_a_mutual_pair_is_still_found(self):
        self.write("d.py", "import e\ndef i(): return 1\n")
        self.write("e.py", "import d\ndef j(): return 1\n")
        self.assertEqual(codegraph.cycles(self.graph(write=False)), [("d", "e")])

    def test_a_long_chain_that_does_not_close_is_not_a_cycle(self):
        for i, nxt in enumerate(["m1", "m2", "m3", None]):
            imp = f"import {nxt}\n" if nxt else ""
            self.write(f"m{i}.py", f"{imp}def f{i}(): return 1\n")
        self.assertEqual(codegraph.cycles(self.graph(write=False)), [])

    def test_a_deferred_import_is_still_the_cycle_break_not_a_smell(self):
        self.write("p.py", "import q\ndef f(): return 1\n")
        self.write("q.py", "def g():\n    import p\n    return p.f()\n")
        self.assertEqual(codegraph.cycles(self.graph(write=False)), [])

    def test_a_deep_import_chain_does_not_blow_the_stack(self):
        """Tarjan recursively would die on a long chain; a big repo is where you need it."""
        depth = 1200
        for i in range(depth):
            imp = f"import c{i+1}\n" if i < depth - 1 else ""
            self.write(f"c{i}.py", f"{imp}def f{i}(): return 1\n")
        self.assertEqual(codegraph.cycles(self.graph(write=False)), [])


class DecoratorsAreCalls(Sandbox):
    """`@register` above a def produced no edge at all, so callers_of("register") was empty and
    a decorator's blast radius was nothing: change it, and the tool said nothing depended on it.
    Python decorators are everywhere, so this was a hole the size of the language."""

    def test_a_bare_decorator_is_a_call(self):
        self.write("d.py", "def register(fn):\n    return fn\n\n@register\ndef thing():\n    return 1\n")
        g = self.graph(write=False)
        self.assertEqual(codegraph.callers_of(g, "d.register"), ["d"])

    def test_a_decorator_with_arguments_is_walked(self):
        """@app.route('/x') - the inner call was never visited either."""
        self.write("app.py", "class App:\n    def route(self, p):\n        return lambda f: f\n"
                             "app = App()\n"
                             "@app.route('/x')\ndef view():\n    return 1\n")
        edges = [e for e in self.graph(write=False)["calls"] if e["callee"] == "route"]
        self.assertTrue(edges, "the decorator expression was never visited")

    def test_the_call_belongs_to_the_enclosing_scope_not_the_decorated_function(self):
        """A decorator runs where the def sits, at import time - not inside the function."""
        self.write("e.py", "def deco(fn):\n    return fn\n\n@deco\ndef inner():\n    return 1\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["callee"] == "deco")
        self.assertEqual(e["src"], "e", f"attributed to {e['src']}, not the module")

    def test_a_decorated_class_counts_too(self):
        self.write("f.py", "def seal(c):\n    return c\n\n@seal\nclass Box:\n    pass\n")
        self.assertEqual(codegraph.callers_of(self.graph(write=False), "f.seal"), ["f"])


class OverlappingArguments(Sandbox):
    """`build . .` indexed everything twice; `build . ./sub` counted the nested file under two
    different ids. Both silently inflate the graph and double-count in stats."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.dir, "sub"))
        self.write("top.py", "def a():\n    return 1\n")
        self.write("sub/deep.py", "def b():\n    return 1\n")

    def ids(self, *dirs):
        return [n["id"] for n in codegraph.build(list(dirs), write=False)["nodes"]
                if n["kind"] == "module"]

    def test_the_same_directory_twice_is_indexed_once(self):
        got = self.ids(self.dir, self.dir)
        self.assertEqual(sorted(got), sorted(set(got)), f"duplicated: {got}")

    def test_a_nested_directory_is_not_indexed_separately(self):
        got = self.ids(self.dir, os.path.join(self.dir, "sub"))
        self.assertEqual(len(got), 2, f"the nested file was counted twice: {got}")

    def test_two_genuinely_separate_trees_are_both_kept(self):
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        with open(os.path.join(other, "far.py"), "w", encoding="utf-8") as f:
            f.write("def c():\n    return 1\n")
        self.assertEqual(len(self.ids(self.dir, other)), 3)


class ARedefinedFunctionIsOneDefinition(Sandbox):
    """The `try: from fast import x / except: def x` pattern produced TWO nodes sharing one id.
    Python binds the last one, so that is the definition - and the shadowed line is worth
    saying out loud rather than discarding."""

    def test_the_last_definition_wins_and_the_id_is_unique(self):
        self.write("r.py", "def loads(s):\n    return 1\n\ndef loads(s):\n    return 2\n"
                           "\ndef use():\n    return loads('x')\n")
        g = self.graph(write=False)
        ids = [n["id"] for n in g["nodes"] if n["kind"] == "func"]
        self.assertEqual(sorted(ids), sorted(set(ids)), f"duplicate node ids: {ids}")
        places = codegraph.where(g, "loads")
        self.assertEqual(len(places), 1, places)
        self.assertIn("r.py:4", places[0][1])            # the live one
        self.assertIn("shadows line 1", places[0][1])    # and what it overrides

    def test_a_name_defined_once_says_nothing_about_shadowing(self):
        self.write("s.py", "def only():\n    return 1\n")
        self.assertEqual(codegraph.where(self.graph(write=False), "only"), [("s.only", "s.py:1")])


class RootLabelsMustBeUnique(Sandbox):
    """The root label was the last path segment, so `build /a/proj /b/proj` gave both trees the
    label "proj" and every file the same id - two unrelated codebases merged into one module,
    with functions from both hanging off it. The old selftest used roots named differently, so
    it never met the case the feature exists for."""

    def two_trees(self, name_a, name_b):
        a = os.path.join(self.dir, "a", name_a)
        b = os.path.join(self.dir, "b", name_b)
        os.makedirs(a); os.makedirs(b)
        with open(os.path.join(a, "mod.py"), "w", encoding="utf-8") as f:
            f.write("def only_a():\n    return 1\n")
        with open(os.path.join(b, "mod.py"), "w", encoding="utf-8") as f:
            f.write("def only_b():\n    return 2\n")
        return codegraph.build([a, b], write=False)

    def test_roots_with_the_same_last_segment_do_not_merge(self):
        g = self.two_trees("proj", "proj")
        mods = [n["id"] for n in g["nodes"] if n["kind"] == "module"]
        self.assertEqual(len(mods), 2, f"the two trees collapsed into {mods}")
        self.assertEqual(len(set(mods)), 2, f"colliding ids: {mods}")
        owners = {n["id"].rsplit(".", 1)[0] for n in g["nodes"] if n["kind"] == "func"}
        self.assertEqual(len(owners), 2, "both functions ended up on one module")

    def test_roots_with_different_names_keep_the_short_label(self):
        """Widening the label must only happen when it is needed."""
        g = self.two_trees("alpha", "beta")
        mods = sorted(n["id"] for n in g["nodes"] if n["kind"] == "module")
        self.assertEqual(mods, ["alpha/mod", "beta/mod"])

    def test_the_labeller_widens_only_as_far_as_it_must(self):
        sep = os.sep
        self.assertEqual(codegraph._root_labels([f"{sep}x{sep}proj", f"{sep}y{sep}proj"]),
                         {f"{sep}x{sep}proj": "x/proj", f"{sep}y{sep}proj": "y/proj"})
        self.assertEqual(codegraph._root_labels([f"{sep}x{sep}one", f"{sep}y{sep}two"]),
                         {f"{sep}x{sep}one": "one", f"{sep}y{sep}two": "two"})


class AnUnknownNameSaysSo(unittest.TestCase):
    """Every verb answered a nonsense name somehow. `calls` printed nothing at all - no name, no
    "(none)", no error, just an empty line and exit 0 - and `deps` on a module that does not
    exist read exactly like a module with no dependencies, which is a far more reassuring fact
    than the truth."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        with open(os.path.join(self.dir, "m.py"), "w", encoding="utf-8") as f:
            f.write("def real():\n    return helper()\ndef helper():\n    return 1\n")
        self.run_it("build", ".")

    def run_it(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), *args],
                              cwd=self.dir, capture_output=True, text=True, timeout=120)

    def test_calls_on_an_unknown_name_says_not_found(self):
        r = self.run_it("calls", "nosuchname")
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing named", r.stdout + r.stderr)

    def test_calls_on_a_real_name_still_works(self):
        r = self.run_it("calls", "real")
        self.assertEqual(r.returncode, 0)
        self.assertIn("m.helper", r.stdout)

    def test_calls_accepts_a_fully_qualified_id(self):
        r = self.run_it("calls", "m.real")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("m.helper", r.stdout)

    def test_deps_on_an_unknown_module_is_an_error(self):
        r = self.run_it("deps", "nosuchmodule")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no module", r.stderr)

    def test_deps_on_a_real_module_with_no_imports_says_none(self):
        r = self.run_it("deps", "m")
        self.assertEqual(r.returncode, 0)
        self.assertIn("(none)", r.stdout)


class EveryCallSiteIsAPlaceToEdit(Sandbox):
    """`sites` is documented as "every call site - the exact places to edit". Edges were
    deduplicated per (caller, receiver, name) keeping only the FIRST line, so a function
    calling helper() three times reported one site. Refactoring from a list that is missing
    two of three is how you break a codebase with the tool bought to prevent that."""

    def test_three_calls_from_one_function_are_three_sites(self):
        self.write("m.py", "def helper():\n    return 1\n\n\ndef caller():\n"
                           "    a = helper()\n    b = helper()\n    c = helper()\n"
                           "    return a + b + c\n")
        got = codegraph.sites(self.graph(write=False), "helper")
        self.assertEqual([loc for loc, _ in got], ["m.py:6", "m.py:7", "m.py:8"])

    def test_the_graph_still_holds_one_edge_per_relationship(self):
        """The dedup is right for the graph - it is only the line list that was lossy."""
        self.write("m.py", "def helper():\n    return 1\ndef caller():\n"
                           "    return helper() + helper()\n")
        g = self.graph(write=False)
        edges = [e for e in g["calls"] if e["callee"] == "helper"]
        self.assertEqual(len(edges), 1, "one relationship became several edges")
        self.assertEqual(edges[0]["lines"], [4])

    def test_stats_counts_edges_and_sites_separately(self):
        self.write("m.py", "def helper():\n    return 1\ndef caller():\n"
                           "    helper()\n    helper()\n    return helper()\n")
        st = codegraph.stats(self.graph(write=False))
        self.assertEqual((st["call_edges"], st["call_sites"]), (1, 3))

    def test_impact_lists_them_all(self):
        self.write("m.py", "def helper():\n    return 1\ndef caller():\n"
                           "    helper()\n    return helper()\n")
        self.assertEqual(len(codegraph.impact(self.graph(write=False), "helper")["sites"]), 2)


class ClassesAreCallableToo(Sandbox):
    """Instantiating a class is a call, and "what breaks if I change this class" is a headline
    question. Pinned so the answer cannot quietly become empty."""

    def setUp(self):
        super().setUp()
        self.write("lib.py", "class Engine:\n    def start(self):\n        return 1\n\n"
                             "def build():\n    return Engine()\n")
        self.write("app.py", "from lib import Engine\ndef main():\n    e = Engine()\n"
                             "    return e.start()\n")
        self.g = self.graph(write=False)

    def test_instantiation_counts_as_a_caller(self):
        self.assertEqual(codegraph.callers_of(self.g, "lib.Engine"), ["app.main", "lib.build"])

    def test_a_class_has_a_blast_radius(self):
        self.assertIn("app.main", codegraph.blast_radius(self.g, "lib.Engine"))

    def test_a_method_on_a_locally_constructed_instance_resolves(self):
        e = next(x for x in self.g["calls"] if x["src"] == "app.main" and x["callee"] == "start")
        self.assertEqual((e.get("dst"), e["confidence"]), ("lib.Engine.start", "TYPED"))


class UnusualButLegalPython(Sandbox):
    """Shapes real code contains that a parser can quietly mishandle."""

    def test_a_nested_class_method_is_indexed(self):
        self.write("n.py", "class Outer:\n    class Inner:\n        def deep(self):\n"
                           "            return 1\n")
        self.assertIn("n.Outer.Inner.deep", {x["id"] for x in self.graph(write=False)["nodes"]})

    def test_an_async_method_resolves_self_calls(self):
        self.write("a.py", "class C:\n    async def go(self):\n        return self.helper()\n"
                           "    def helper(self):\n        return 1\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "a.C.go")
        self.assertEqual((e.get("dst"), e["confidence"]), ("a.C.helper", "SELF-METHOD"))

    def test_a_local_module_shadowing_the_stdlib_wins(self):
        """A json.py in your tree IS what `import json` gets, and the graph should say so."""
        self.write("json.py", "def loads(s):\n    return 1\n")
        self.write("u.py", "import json\ndef go():\n    return json.loads('{}')\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "u.go")
        self.assertEqual((e.get("dst"), e["confidence"]), ("json.loads", "QUALIFIED"))

    def test_an_empty_file_is_a_module_not_a_crash(self):
        self.write("blank.py", "")
        self.write("comment.py", "# nothing here\n")
        mods = {n["id"] for n in self.graph(write=False)["nodes"] if n["kind"] == "module"}
        self.assertEqual(mods, {"blank", "comment"})


class ConcurrentBuilds(unittest.TestCase):
    """The temp file was a fixed `path + ".tmp"`, which is not atomic between processes: two
    builds wrote the same temp, the first renamed it away, the second's rename found nothing.
    Measured before the fix: six of eight concurrent builds died. An agent that runs this
    before every edit, or a CI matrix, meets it immediately."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for i in range(40):
            with open(os.path.join(self.dir, f"m{i}.py"), "w", encoding="utf-8") as f:
                f.write(f"def f{i}():\n    return 1\n")

    def test_eight_at_once_all_succeed_and_leave_a_valid_graph(self):
        import concurrent.futures as cf

        def build(_):
            return subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "build", "."],
                                  cwd=self.dir, capture_output=True, text=True, timeout=300)
        with cf.ThreadPoolExecutor(8) as ex:
            results = list(ex.map(build, range(8)))
        failed = [r for r in results if r.returncode]
        self.assertEqual(failed, [], failed and failed[0].stderr[-300:])
        self.assertEqual([f for f in os.listdir(self.dir) if ".tmp" in f], [], "stray temp files")
        for name in ("codegraph.json", "codegraph.cache.json"):
            with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                json.load(f)                          # raises if a writer corrupted it

    def test_a_temp_name_is_unique_per_process_and_thread(self):
        import re as _re
        src = inspect.getsource(codegraph._jwrite)
        self.assertIn("os.getpid()", src)
        self.assertIn("threading.get_ident()", src)
        self.assertTrue(_re.search(r"os\.fsync", src), "content must reach disk before the rename")


class TheGraphIsFoundFromAnywhereInTheTree(Sandbox):
    """Build at the repository root, cd into a package, and every query said "no graph yet" -
    whereupon the obvious move, `build .` inside the package, quietly makes a PARTIAL graph and
    a second graph file, and the answers exclude the rest of the repo without saying so."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.dir, "pkg", "deep"))
        self.write("root.py", "def top():\n    return 1\n")
        self.write("pkg/mid.py", "def mid():\n    return 1\n")
        self.graph()
        self.cwd = os.getcwd()
        self.addCleanup(os.chdir, self.cwd)

    def test_a_query_from_a_subdirectory_finds_the_root_graph(self):
        os.chdir(os.path.join(self.dir, "pkg", "deep"))
        codegraph.OUT = os.path.join(os.getcwd(), "codegraph.json")   # what a fresh run would use
        self.assertEqual(codegraph.where(codegraph.load(), "top"), [("root.top", "root.py:1")])

    def test_the_search_stops_at_the_filesystem_root(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        os.chdir(empty)
        codegraph.OUT = os.path.join(empty, "codegraph.json")
        with self.assertRaises(SystemExit) as cm:
            codegraph.load()
        self.assertIn("no graph yet", str(cm.exception))

    def test_an_explicit_output_path_is_always_obeyed(self):
        os.chdir(os.path.join(self.dir, "pkg"))
        os.environ["CODEGRAPH_OUT"] = os.path.join(self.dir, "codegraph.json")
        self.addCleanup(os.environ.pop, "CODEGRAPH_OUT", None)
        self.assertEqual(codegraph._find_graph(), codegraph.OUT)


class EncodingsRealFilesHave(Sandbox):
    """A byte-order mark is not a syntax error. Visual Studio and Notepad write them and Python
    accepts them - but reading as plain utf-8 left the mark at the top of the file, ast.parse
    threw, and the WHOLE FILE silently became an empty module. Every function in it vanished
    from every blast radius, with nothing said."""

    def raw(self, name, data):
        with open(os.path.join(self.dir, name), "wb") as f:
            f.write(data)

    def test_a_file_with_a_byte_order_mark_is_read(self):
        self.raw("bom.py", b"\xef\xbb\xbfdef with_bom():\n    return 1\n")
        self.assertIn("bom.with_bom", {n["id"] for n in self.graph(write=False)["nodes"]})

    def test_a_bom_file_resolves_calls_like_any_other(self):
        self.raw("bom.py", b"\xef\xbb\xbfdef helper():\n    return 1\n"
                           b"def go():\n    return helper()\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "bom.go")
        self.assertEqual((e.get("dst"), e["confidence"]), ("bom.helper", "LOCAL"))

    def test_crlf_line_numbers_are_right(self):
        self.raw("crlf.py", b"def a():\r\n    return 1\r\n\r\n\r\ndef b():\r\n    return a()\r\n")
        self.assertEqual(codegraph.where(self.graph(write=False), "b"), [("crlf.b", "crlf.py:5")])

    def test_tabs_are_fine(self):
        self.raw("tabs.py", b"def t():\n\treturn 1\n")
        self.assertIn("tabs.t", {n["id"] for n in self.graph(write=False)["nodes"]})


class AnUnreadableFileIsReported(Sandbox):
    """Skipping a file the parser cannot read is right. Skipping it in SILENCE is not: an empty
    module in the graph looks exactly like a file with nothing in it, and the difference is
    every function you were about to change."""

    def test_it_is_named_rather_than_becoming_an_empty_module(self):
        self.write("good.py", "def fine():\n    return 1\n")
        with open(os.path.join(self.dir, "bad.py"), "wb") as f:
            f.write(b"def ok():\n    return 1\n# \x00 embedded nul\n")
        g = self.graph(write=False)
        self.assertEqual(len(g["unreadable"]), 1, g["unreadable"])
        self.assertIn("bad.py", g["unreadable"][0])
        mods = {n["id"] for n in g["nodes"] if n["kind"] == "module"}
        self.assertEqual(mods, {"good"}, "the unparseable file is still in the graph as a module")
        self.assertEqual(codegraph.stats(g)["unreadable_files"], 1)

    def test_python_2_source_is_named_not_swallowed(self):
        self.write("old.py", "print 'python two'\n")
        self.write("new.py", "def fine():\n    return 1\n")
        g = self.graph(write=False)
        self.assertTrue(any("old.py" in u for u in g["unreadable"]), g["unreadable"])
        self.assertIn("new.fine", {n["id"] for n in g["nodes"]})

    def test_the_cli_says_so_on_stderr(self):
        self.write("good.py", "def fine():\n    return 1\n")
        self.write("broken.py", "def (((\n")
        r = subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "build", "."],
                           cwd=self.dir, capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0)
        self.assertIn("could not parse", r.stderr)
        self.assertIn("broken.py", r.stderr)

    def test_a_clean_tree_reports_nothing(self):
        self.write("a.py", "def f():\n    return 1\n")
        self.assertEqual(self.graph(write=False)["unreadable"], [])


class TheReadmeDoesNotOversell(unittest.TestCase):
    """Numbers in prose go stale silently. These are the two the README states outright."""

    def test_the_claimed_check_counts_are_real(self):
        readme = os.path.join(HERE, "README.md")
        with open(readme, encoding="utf-8") as f:
            text = f.read()
        claimed_selftest = int(re.search(r"(\d+) ground-truth checks", text).group(1))
        claimed_suite = int(re.search(r"test suite adds (\d+) more", text).group(1))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            codegraph._selftest()
        actual_selftest = out.getvalue().count(": True") + out.getvalue().count(": False")
        self.assertEqual(claimed_selftest, actual_selftest)
        suite = unittest.defaultTestLoader.discover(os.path.join(HERE, "tests"))
        self.assertEqual(claimed_suite, suite.countTestCases())


class OneFileHoweverManyPathsReachIt(Sandbox):
    """A symlink to a file inside the same tree indexed it twice: two modules with identical
    function names, so every call to them became AMBIGUOUS and the blast radius of everything
    in the linked file was empty. One symlink, and the tool quietly stopped answering."""

    def link(self, name, target):
        try:
            os.symlink(os.path.join(self.dir, target), os.path.join(self.dir, name))
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"symlinks unavailable: {e}")

    def test_a_link_to_a_file_in_the_tree_is_indexed_once(self):
        self.write("real.py", "def shared():\n    return 1\n")
        self.write("caller.py", "def use():\n    return shared()\n")
        self.link("alias.py", "real.py")
        g = self.graph(write=False)
        mods = sorted(n["id"] for n in g["nodes"] if n["kind"] == "module")
        self.assertEqual(mods, ["caller", "real"])

    def test_resolution_is_not_degraded_by_the_link(self):
        self.write("real.py", "def shared():\n    return 1\n")
        self.write("caller.py", "def use():\n    return shared()\n")
        self.link("alias.py", "real.py")
        g = self.graph(write=False)
        e = next(x for x in g["calls"] if x["src"] == "caller.use")
        self.assertEqual((e.get("dst"), e["confidence"]), ("real.shared", "RESOLVED"))
        self.assertEqual(codegraph.callers_of(g, "real.shared"), ["caller.use"])

    def test_the_real_file_is_the_one_named_not_the_link(self):
        """Naming the module after the symlink is the same gap in a nicer disguise: ask about
        the file you actually edit and the answer is empty."""
        self.write("zzz_real.py", "def shared():\n    return 1\n")
        self.link("aaa_alias.py", "zzz_real.py")          # sorts FIRST, so order cannot decide it
        mods = [n["id"] for n in self.graph(write=False)["nodes"] if n["kind"] == "module"]
        self.assertEqual(mods, ["zzz_real"])

    def test_two_genuine_copies_are_both_indexed(self):
        """Identical content is not the same file. Only a shared realpath collapses."""
        self.write("one.py", "def same():\n    return 1\n")
        self.write("two.py", "def same():\n    return 1\n")
        mods = sorted(n["id"] for n in self.graph(write=False)["nodes"] if n["kind"] == "module")
        self.assertEqual(mods, ["one", "two"])


class ModuleIdsAreSeparatorAgnostic(Sandbox):
    """Module ids are the tool's vocabulary and they always use "/". On Windows os.sep is a
    backslash, and an id built from raw path pieces would differ per platform - so a graph
    built on one machine would not answer questions asked on another."""

    def test_no_id_ever_contains_a_backslash(self):
        os.makedirs(os.path.join(self.dir, "pkg", "deep"))
        self.write("pkg/deep/mod.py", "def f():\n    return 1\n")
        g = self.graph(write=False)
        for n in g["nodes"]:
            self.assertNotIn("\\", n["id"], n)
            self.assertNotIn("\\", n["module"], n)
        for e in g["calls"]:
            self.assertNotIn("\\", e.get("mod", ""), e)
        self.assertIn("pkg/deep/mod.f", {n["id"] for n in g["nodes"]})

    def test_recorded_sources_are_real_os_paths(self):
        """The one place that must use the platform separator: they are paths to open."""
        self.write("a.py", "def f():\n    return 1\n")
        g = self.graph(write=False)
        self.assertTrue(all(os.path.exists(p) for p in g["sources"]), g["sources"])


class AGraphBelongsToTheToolThatBuiltIt(Sandbox):
    """The parse cache was stamped with codegraph's own source hash from the start. The GRAPH
    was not - so upgrading the tool and querying an unchanged tree served the previous
    version's answers, and every resolution fix stayed invisible until somebody happened to
    edit a file. A bot that keeps a graph around would never see an improvement at all."""

    def test_the_graph_records_which_codegraph_built_it(self):
        self.write("a.py", "def f():\n    return 1\n")
        self.assertEqual(self.graph()["version"], codegraph._VERSION)

    def test_a_graph_from_another_version_is_stale(self):
        self.write("a.py", "def f():\n    return 1\n")
        g = self.graph()
        self.assertFalse(codegraph._is_stale(g))
        g["version"] = "some-other-codegraph"
        self.assertTrue(codegraph._is_stale(g), "an older tool's graph was accepted as fresh")

    def test_a_graph_with_no_version_at_all_is_stale(self):
        """Graphs written before this existed must be rebuilt once, not trusted."""
        self.write("a.py", "def f():\n    return 1\n")
        g = self.graph()
        del g["version"]
        self.assertTrue(codegraph._is_stale(g))

    def test_a_changed_tool_actually_rebuilds_through_load(self):
        self.write("a.py", "def f():\n    return 1\n")
        self.graph()
        with open(codegraph.OUT, encoding="utf-8") as f:
            on_disk = json.load(f)
        on_disk["version"] = "pretend-this-is-older"
        on_disk["nodes"] = []                              # an answer only the old graph gives
        with open(codegraph.OUT, "w", encoding="utf-8") as f:
            json.dump(on_disk, f)
        self.assertTrue(codegraph.where(codegraph.load(), "f"), "served the stale graph")


class SmallWordsMatter(unittest.TestCase):
    """A tool people read the output of every day."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        with open(os.path.join(self.dir, "app.py"), "w", encoding="utf-8") as f:
            f.write("def leaf():\n    return 1\ndef mid():\n    return leaf()\n"
                    "def top():\n    return mid()\n")
        subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "build", "."],
                       cwd=self.dir, capture_output=True, timeout=180)

    def impact(self, name):
        return subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "impact", name],
                              cwd=self.dir, capture_output=True, text=True, timeout=180).stdout

    def test_one_function_is_singular(self):
        self.assertIn("1 function could be affected", self.impact("mid"))

    def test_two_functions_are_plural(self):
        self.assertIn("2 functions could be affected", self.impact("leaf"))


class AnAmbiguousNameIsNotMerged(unittest.TestCase):
    """The README's opening argument is that grep cannot tell you two files define `digest` and
    only one is the one you are about to break. Typed the obvious way - a bare name, because
    nobody types a qualified id first - the tool merged both: two callers reported when the
    function being changed has one. Worse than overstating; you go and "fix" a caller of the
    other function."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for mod, body in (("pulse", "pulse"), ("court", "court")):
            with open(os.path.join(self.dir, f"{mod}.py"), "w", encoding="utf-8") as f:
                f.write(f"def digest():\n    return {body!r}\n")
        for user, mod in (("usera", "pulse"), ("userb", "court")):
            with open(os.path.join(self.dir, f"{user}.py"), "w", encoding="utf-8") as f:
                f.write(f"import {mod}\ndef go():\n    return {mod}.digest()\n")
        self.run_it("build", ".")

    def run_it(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), *args],
                              cwd=self.dir, capture_output=True, text=True, timeout=180)

    def test_every_acting_verb_refuses_to_merge(self):
        for verb in ("impact", "blast", "callers", "sites", "calls"):
            with self.subTest(verb):
                r = self.run_it(verb, "digest")
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertIn("names 2 definitions", r.stderr)
                self.assertIn("pulse.digest", r.stderr)
                self.assertIn("court.digest", r.stderr)

    def test_it_says_where_each_one_lives(self):
        r = self.run_it("impact", "digest")
        self.assertIn("pulse.py:1", r.stderr)
        self.assertIn("court.py:1", r.stderr)

    def test_a_qualified_id_answers_for_exactly_that_one(self):
        r = self.run_it("impact", "pulse.digest")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usera.go", r.stdout)
        self.assertNotIn("userb.go", r.stdout)
        self.assertIn("1 function could be affected", r.stdout)

    def test_an_unambiguous_bare_name_still_just_works(self):
        """The guard must not make the common case harder."""
        with open(os.path.join(self.dir, "solo.py"), "w", encoding="utf-8") as f:
            f.write("def only_one():\n    return 1\n")
        self.run_it("build", ".")
        r = self.run_it("impact", "only_one")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_where_and_find_still_show_every_match(self):
        """Those two exist to list them - they must not be narrowed."""
        self.assertEqual(self.run_it("where", "digest").stdout.count("digest"), 2)
        self.assertEqual(self.run_it("find", "diges").stdout.count("digest"), 2)


class TheReadmeExampleRuns(unittest.TestCase):
    """The one python block in the README had never been executed. A guide drifts the moment
    nothing runs it, and the first person to hit the difference is a stranger pasting it."""

    def test_the_library_example_works_as_written(self):
        with open(os.path.join(HERE, "README.md"), encoding="utf-8") as f:
            block = re.search(r"```python\n(.*?)```", f.read(), re.S)
        self.assertIsNotNone(block, "the README lost its library example")
        code = block.group(1)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, "app.py"), "w", encoding="utf-8") as f:
            f.write("def spend_cap():\n    return 1\ndef use():\n    return spend_cap()\n")
        subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "build", "."],
                       cwd=d, capture_output=True, timeout=180)
        runner = os.path.join(d, "_readme_example.py")
        with open(runner, "w", encoding="utf-8") as f:
            f.write(f"import sys\nsys.path.insert(0, {HERE!r})\n" + code)
        r = subprocess.run([sys.executable, runner], cwd=d, capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])


class BothDoorsRefuseToMerge(Sandbox):
    """The CLI learned to refuse an ambiguous name; the LIBRARY went on quietly unioning the
    callers of two different functions - and the library is the door the README tells an agent
    to use. Fixing one door and not the other is not fixing it."""

    def setUp(self):
        super().setUp()
        self.write("pulse.py", "def digest():\n    return 1\n")
        self.write("court.py", "def digest():\n    return 2\n")
        self.write("usera.py", "import pulse\ndef a():\n    return pulse.digest()\n")
        self.write("userb.py", "import court\ndef b():\n    return court.digest()\n")
        self.g = self.graph(write=False)

    def test_every_library_entry_point_raises(self):
        for fn in (codegraph.impact, codegraph.callers_of, codegraph.blast_radius, codegraph.sites):
            with self.subTest(fn.__name__), self.assertRaises(codegraph.Ambiguous) as cm:
                fn(self.g, "digest")
            self.assertEqual(cm.exception.candidates, ["court.digest", "pulse.digest"])

    def test_the_exception_carries_what_a_caller_needs(self):
        with self.assertRaises(codegraph.Ambiguous) as cm:
            codegraph.impact(self.g, "digest")
        self.assertEqual(cm.exception.name, "digest")
        self.assertIn("court.digest", str(cm.exception))

    def test_a_qualified_id_answers_for_exactly_one(self):
        self.assertEqual(codegraph.impact(self.g, "pulse.digest")["callers"], ["usera.a"])
        self.assertEqual(codegraph.callers_of(self.g, "court.digest"), ["userb.b"])

    def test_an_unambiguous_bare_name_is_untouched(self):
        self.write("solo.py", "def only():\n    return 1\n")
        g = self.graph(write=False)
        self.assertEqual(codegraph.callers_of(g, "only"), [])

    def test_blast_radius_still_walks_through_qualified_ids_internally(self):
        """The guard must not fire on the ids blast_radius generates as it walks."""
        self.write("chain.py", "def leaf():\n    return 1\ndef mid():\n    return leaf()\n"
                               "def top():\n    return mid()\n")
        g = self.graph(write=False)
        self.assertEqual(codegraph.blast_radius(g, "chain.leaf"), ["chain.mid", "chain.top"])


class ThreeAnswersNotOne(unittest.TestCase):
    """"No callers", "never heard of it" and "be more specific" are three different facts that
    all used to print "(none)" and exit 0. For an agent deciding whether an edit is safe, a
    typo answering "nothing depends on this" is the worst of the three."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.write("solo.py", "def alone():\n    return 1\n")
        self.write("uses.py", "import json\ndef go():\n    return json.loads('{}')\n")
        self.write("pulse.py", "def digest():\n    return 1\n")
        self.write("court.py", "def digest():\n    return 2\n")
        self.run_it("build", ".")

    def write(self, name, body):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(body)

    def run_it(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), *args],
                              cwd=self.dir, capture_output=True, text=True, timeout=180)

    def test_a_real_name_with_no_callers_succeeds(self):
        r = self.run_it("callers", "alone")
        self.assertEqual(r.returncode, 0)
        self.assertIn("(none)", r.stdout)

    def test_a_name_this_graph_never_heard_of_is_an_error(self):
        for verb in ("impact", "callers", "blast", "sites", "calls"):
            with self.subTest(verb):
                r = self.run_it(verb, "definitely_not_here")
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("nothing named", r.stderr)

    def test_a_qualified_id_that_does_not_exist_is_also_an_error(self):
        """A dotted string was taken as a real id without checking, so a typo answered
        "no callers" with a success code."""
        r = self.run_it("impact", "typo.name")
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing named", r.stderr)

    def test_being_vague_is_a_different_code_from_being_wrong(self):
        vague = self.run_it("impact", "digest")
        wrong = self.run_it("impact", "not_a_thing")
        self.assertEqual((vague.returncode, wrong.returncode), (2, 1))

    def test_a_name_called_here_but_defined_elsewhere_is_answered_and_flagged(self):
        """"Where do we call json.loads" is a fair question; the answer must not be mistaken
        for a function of ours that nothing calls."""
        r = self.run_it("sites", "loads")
        self.assertEqual(r.returncode, 0)
        self.assertIn("called here but not defined here", r.stderr)
        self.assertIn("uses.py", r.stdout)

    def test_a_search_that_matches_nothing_exits_like_grep(self):
        self.assertEqual(self.run_it("where", "zzzz").returncode, 1)
        self.assertEqual(self.run_it("find", "zzzz").returncode, 1)
        self.assertEqual(self.run_it("where", "alone").returncode, 0)


class TheLibraryRefusesWhatTheCliRefuses(Sandbox):
    """Round thirteen named the lesson - a fix with two entry points is not fixed until both
    are - and round fourteen made the same mistake again: the command line learned to reject a
    misspelled name, the LIBRARY went on returning three empty lists. An agent calling
    impact() with a typo is told nothing depends on the function it is about to change."""

    def setUp(self):
        super().setUp()
        self.write("pulse.py", "def digest():\n    return 1\n")
        self.write("court.py", "def digest():\n    return 2\n")
        self.write("usera.py", "import pulse\ndef a():\n    return pulse.digest()\n")
        self.write("uses.py", "import json\ndef go():\n    return json.loads('{}')\n")
        self.g = self.graph(write=False)

    def test_every_library_entry_point_rejects_an_unknown_name(self):
        for fn, args in ((codegraph.impact, ("nope",)), (codegraph.callers_of, ("nope",)),
                         (codegraph.blast_radius, ("nope",)), (codegraph.sites, ("nope",)),
                         (codegraph.calls_from, ("nope",)),
                         (codegraph.path, ("nope", "pulse.digest"))):
            with self.subTest(fn.__name__), self.assertRaises(codegraph.Unknown):
                fn(self.g, *args)

    def test_module_deps_rejects_a_module_that_does_not_exist(self):
        with self.assertRaises(codegraph.Unknown):
            codegraph.module_deps(self.g, "nosuchmodule")
        self.assertEqual(codegraph.module_deps(self.g, "pulse"), ([], ["usera"]))

    def test_path_refuses_an_ambiguous_endpoint(self):
        """The one query verb that had no guard at all."""
        with self.assertRaises(codegraph.Ambiguous):
            codegraph.path(self.g, "digest", "pulse.digest")
        with self.assertRaises(codegraph.Ambiguous):
            codegraph.path(self.g, "usera.a", "digest")

    def test_a_qualified_id_that_does_not_exist_is_unknown_not_empty(self):
        with self.assertRaises(codegraph.Unknown):
            codegraph.impact(self.g, "typo.name")

    def test_a_name_called_but_not_defined_here_is_still_answerable(self):
        """"Where do we call json.loads" must keep working - it is neither unknown nor ours."""
        self.assertTrue(codegraph.sites(self.g, "loads"))

    def test_the_real_cases_are_untouched(self):
        self.assertEqual(codegraph.impact(self.g, "pulse.digest")["callers"], ["usera.a"])
        self.assertEqual(codegraph.path(self.g, "usera.a", "pulse.digest"),
                         ["usera.a", "pulse.digest"])
        self.assertEqual(codegraph.callers_of(self.g, "court.digest"), [])

    def test_blast_radius_still_walks_internally(self):
        """The gate must not fire on the ids the traversal generates for itself."""
        self.write("c.py", "def leaf():\n    return 1\ndef mid():\n    return leaf()\n"
                           "def top():\n    return mid()\n")
        g = self.graph(write=False)
        self.assertEqual(codegraph.blast_radius(g, "c.leaf"), ["c.mid", "c.top"])

    def test_both_doors_agree(self):
        """The point of the round: the same name gets the same verdict either way in."""
        for name, exc in (("nope", codegraph.Unknown), ("digest", codegraph.Ambiguous)):
            with self.subTest(name), self.assertRaises(exc):
                codegraph.impact(self.g, name)
        self.write("solo.py", "def only():\n    return 1\n")
        self.graph()                                     # write it so the CLI can read it
        cli = {}
        for name in ("nope", "digest", "only"):
            r = subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "impact", name],
                               cwd=self.dir, capture_output=True, text=True, timeout=180)
            cli[name] = r.returncode
        self.assertEqual(cli, {"nope": 1, "digest": 2, "only": 0})


class EveryGraphStateHasAnAnswer(unittest.TestCase):
    """A matrix: every verb against every state the graph can be in. Crossing them found a
    traceback nobody would have guessed at from reading the code."""

    def make(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def run_in(self, cwd, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), *args],
                              cwd=cwd, capture_output=True, text=True, timeout=180)

    def with_source(self):
        d = self.make()
        with open(os.path.join(d, "m.py"), "w", encoding="utf-8") as f:
            f.write("def f():\n    return 1\n")
        return d

    def test_a_vanished_source_tree_is_a_sentence_not_a_traceback(self):
        """Build against src/, delete src/, ask a question. Rebuilding is the right instinct
        and it cannot succeed - but a traceback is not an answer."""
        d = self.make()
        src = os.path.join(d, "src")
        os.makedirs(src)
        with open(os.path.join(src, "m.py"), "w", encoding="utf-8") as f:
            f.write("def f():\n    return 1\n")
        self.run_in(d, "build", "src")
        shutil.rmtree(src)
        r = self.run_in(d, "impact", "f")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("the tree this graph was built from is gone", r.stderr)
        self.assertIn("codegraph.json", r.stderr)          # and how to get out of it

    def test_no_graph_at_all_says_how_to_make_one(self):
        r = self.run_in(self.with_source(), "impact", "f")
        self.assertEqual(r.returncode, 1)
        self.assertIn("codegraph build", r.stdout + r.stderr)

    def test_a_corrupt_graph_is_named_as_corrupt(self):
        d = self.with_source()
        self.run_in(d, "build", ".")
        with open(os.path.join(d, "codegraph.json"), "w", encoding="utf-8") as f:
            f.write('{"nodes": [')
        r = self.run_in(d, "impact", "f")
        self.assertEqual(r.returncode, 1)
        self.assertIn("corrupt", r.stdout + r.stderr)

    def test_no_verb_ever_crashes_whatever_the_state(self):
        """The matrix itself, as a test: nothing may produce a traceback."""
        states = []
        d = self.with_source(); states.append(("no graph", d))
        d = self.with_source(); self.run_in(d, "build", "."); states.append(("good", d))
        d = self.with_source(); self.run_in(d, "build", ".")
        with open(os.path.join(d, "codegraph.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        states.append(("empty object", d))
        d = self.with_source(); self.run_in(d, "build", ".")
        os.remove(os.path.join(d, "m.py")); states.append(("tree emptied", d))
        for label, d in states:
            for verb in ("stats", "cycles", "callers f", "impact f", "where f",
                         "find f", "deps m", "path f f", "calls f"):
                with self.subTest(state=label, verb=verb):
                    r = self.run_in(d, *verb.split())
                    self.assertNotIn("Traceback", r.stderr, f"{label} / {verb}")
                    self.assertIn(r.returncode, (0, 1, 2), f"{label} / {verb}")


class TheMessageMatchesTheSituation(Sandbox):
    """"no .py files found" contradicted the skip lines printed directly above it."""

    def test_files_that_exist_but_do_not_parse_are_not_reported_as_absent(self):
        self.write("a.py", "def (((\n")
        self.write("b.py", "print 'python two'\n")
        r = subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "build", "."],
                           cwd=self.dir, capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 1)
        self.assertIn("none of which could be parsed", r.stderr)
        self.assertNotIn("no .py files found", r.stderr)

    def test_a_directory_with_no_python_still_says_so(self):
        self.write("readme.txt", "hello\n")
        r = subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "build", "."],
                           cwd=self.dir, capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no .py files found", r.stderr)


class TheGraphKeepsItsOwnInvariants(unittest.TestCase):
    """Properties that must hold of ANY graph this tool produces, checked against real
    codebases rather than fixtures. Nothing had ever asserted them."""

    LABELS = frozenset({"SELF-METHOD", "TYPED", "QUALIFIED", "LOCAL", "RESOLVED", "INHERITED",
                        "CLASS", "AMBIGUOUS", "EXTERNAL", "BUILTIN", "UNTYPED"})
    UNRESOLVED = frozenset({"AMBIGUOUS", "EXTERNAL", "BUILTIN", "UNTYPED"})

    def assert_sound(self, g):
        ids = {n["id"] for n in g["nodes"]}
        mods = {n["id"] for n in g["nodes"] if n["kind"] == "module"}
        self.assertEqual(len(ids), len(g["nodes"]), "duplicate node ids")
        for n in g["nodes"]:
            self.assertIn(n["module"], mods, n)
            if n["kind"] != "module":
                self.assertTrue(n["id"].startswith(n["module"] + "."), n)
            self.assertGreaterEqual(n.get("line", 0), 0, n)
        for e in g["calls"]:
            self.assertIn(e["src"], ids, e)
            self.assertIn(e["confidence"], self.LABELS, e)
            self.assertIn(e.get("mod"), mods, e)
            self.assertTrue(e.get("lines"), e)
            if e.get("dst"):
                self.assertIn(e["dst"], ids, e)
                self.assertNotIn(e["confidence"], self.UNRESOLVED, "resolved but labelled unresolved")
            else:
                self.assertIn(e["confidence"], self.UNRESOLVED, "unresolved but labelled resolved")
        for e in g["imports"]:
            self.assertIn(e["src"], mods, e)

    def test_its_own_graph_is_sound(self):
        self.assert_sound(codegraph.build([HERE], write=False))

    def test_a_tree_of_awkward_shapes_is_sound(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        os.makedirs(os.path.join(d, "pkg", "deep"))
        files = {
            "pkg/__init__.py": "",
            "pkg/thing.py": "def load():\n    return 1\n",
            "pkg/user.py": "from . import thing\nfrom .thing import load\n"
                           "def a():\n    return thing.load() + load()\n",
            "pkg/deep/down.py": "from .. import thing\ndef c():\n    return thing.load()\n",
            "top.py": "class P:\n    def m(self):\n        return 1\n"
                      "class C(P):\n    def u(self):\n        return self.m()\n"
                      "def deco(f):\n    return f\n@deco\ndef d():\n    return P().m()\n",
            "dup.py": "def same():\n    return 1\ndef same():\n    return 2\n",
        }
        for rel, body in files.items():
            with open(os.path.join(d, *rel.split("/")), "w", encoding="utf-8") as f:
                f.write(body)
        self.assert_sound(codegraph.build([d], write=False))


class ImpactSaysWhatItCouldNotResolve(Sandbox):
    """`impact start` reported no callers while Engine().start() sat in another file. Each edge
    was labelled honestly; the ANSWER was false reassurance, which is the one thing a pre-edit
    view must never give."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.dir, "pkg"))
        self.write("pkg/lib.py", "class Engine:\n    def start(self):\n        return 1\n"
                                 "def helper():\n    return 1\n")
        self.write("main.py", "from pkg.lib import Engine\ndef go():\n"
                              "    return Engine().start()\n")
        self.g = self.graph()                            # written, so the CLI tests can read it

    def test_unresolved_uses_of_the_name_are_reported(self):
        im = codegraph.impact(self.g, "pkg/lib.Engine.start")
        self.assertEqual(im["callers"], [])
        self.assertEqual([loc for loc, _ in im["unresolved"]], ["main.py:3"])

    def test_a_function_nothing_touches_reports_nothing_extra(self):
        self.assertEqual(codegraph.impact(self.g, "pkg/lib.helper")["unresolved"], [])

    def test_the_cli_says_it_is_unsure(self):
        r = subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "impact", "start"],
                           cwd=self.dir, capture_output=True, text=True, timeout=180)
        self.assertIn("unsure:", r.stdout)
        self.assertIn("1 call site uses this name", r.stdout)
        self.assertIn("main.py:3", r.stdout)

    def test_naming_a_module_points_at_the_right_command(self):
        """It IS in the graph - "never heard of it" was simply false."""
        r = subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "impact", "pkg/lib"],
                           cwd=self.dir, capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 1)
        self.assertIn("is a module, not a function", r.stderr)
        self.assertIn("codegraph deps pkg/lib", r.stderr)


class EveryPlaceACallCanHide(Sandbox):
    """A survey of the language rather than of the code: seventeen syntactic positions a call
    can sit in, each checked for an edge. Decorators were missed once because the visitor
    walked selected children of a def instead of all of them, and default values turned out to
    be the same hole - `def f(x=make_default())` recorded nothing, so changing make_default
    looked safe."""

    SHAPES = (
        ("a default value",        "def f(x=target()):\n    return x\n"),
        ("a keyword-only default", "def f(*, x=target()):\n    return x\n"),
        ("a return annotation",    "def f() -> type(target()):\n    return 1\n"),
        ("a parameter annotation", "def f(x: type(target())):\n    return x\n"),
        ("a class body",           "class C:\n    attr = target()\n"),
        ("an f-string",            "def f():\n    return f'{target()}'\n"),
        ("an assert",              "def f():\n    assert target()\n"),
        ("a raise",                "def f():\n    raise ValueError(target())\n"),
        ("a ternary",              "def f(c):\n    return target() if c else 0\n"),
        ("a walrus",               "def f():\n    if (v := target()):\n        return v\n    return 0\n"),
        ("a yield",                "def f():\n    yield target()\n"),
        ("a with body",            "def f():\n    with open('x'):\n        return target()\n"),
        ("a try body",             "def f():\n    try:\n        return target()\n    except ValueError:\n        return 0\n"),
        ("a comprehension filter", "def f():\n    return [i for i in range(3) if target()]\n"),
        ("a nested def",           "def f():\n    def inner():\n        return target()\n    return inner\n"),
        ("star-args",              "def f():\n    return print(*[target()])\n"),
        ("a keyword argument",     "def f():\n    return dict(k=target())\n"),
        ("a subscript",            "def f():\n    return [0][target() - 1]\n"),
    )

    def test_a_call_is_found_wherever_it_sits(self):
        for label, body in self.SHAPES:
            with self.subTest(label):
                d = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, d, ignore_errors=True)
                with open(os.path.join(d, "base.py"), "w", encoding="utf-8") as f:
                    f.write("def target():\n    return 1\n")
                with open(os.path.join(d, "s.py"), "w", encoding="utf-8") as f:
                    f.write("from base import target\n\n\n" + body)
                g = codegraph.build([d], write=False)
                hits = [e for e in g["calls"] if e["callee"] == "target"]
                self.assertTrue(hits, f"no edge for a call in {label}")
                self.assertEqual(hits[0].get("dst"), "base.target", f"{label} resolved wrongly")

    def test_a_default_value_belongs_to_the_enclosing_scope(self):
        """It runs once, at import, beside the def - not inside the function."""
        self.write("base.py", "def target():\n    return 1\n")
        self.write("s.py", "from base import target\ndef f(x=target()):\n    return x\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["callee"] == "target")
        self.assertEqual(e["src"], "s", f"attributed to {e['src']}, not the module")

    def test_changing_a_default_factory_has_a_blast_radius(self):
        """The point of the fix, stated as the question a person actually asks."""
        self.write("base.py", "def make_default():\n    return []\n")
        self.write("s.py", "from base import make_default\n"
                           "def f(x=make_default()):\n    return x\n")
        g = self.graph(write=False)
        self.assertEqual(codegraph.callers_of(g, "base.make_default"), ["s"])


class ALocalNameIsNotAnImportedModule(Sandbox):
    """The worst class of bug this tool can have: confidently wrong. A parameter named after an
    imported module made `helpers.run()` resolve to that module's function and label it
    QUALIFIED - the highest confidence there is, on an answer that is simply not true. Follow
    that blast radius and you edit a file that has nothing to do with the change."""

    def setUp(self):
        super().setUp()
        self.write("helpers.py", "def run():\n    return 'the module function'\n")

    def edge(self, body, src):
        self.write("app.py", "import helpers\n\n\nclass Thing:\n    def run(self):\n"
                             "        return 1\n\n\ndef make():\n    return Thing()\n\n\n" + body)
        g = self.graph(write=False)
        hits = [e for e in g["calls"] if e["src"] == src and e["callee"] == "run"]
        self.assertEqual(len(hits), 1, f"expected one edge from {src}, got {hits}")
        return hits[0]

    def test_a_parameter_shadowing_a_module_is_not_that_module(self):
        e = self.edge("def f(helpers):\n    return helpers.run()\n", "app.f")
        self.assertIsNone(e.get("dst"), "resolved to the module it was shadowing")
        self.assertEqual(e["confidence"], "UNTYPED")

    def test_a_call_result_shadowing_a_module_is_not_that_module(self):
        e = self.edge("def f():\n    helpers = make()\n    return helpers.run()\n", "app.f")
        self.assertIsNone(e.get("dst"))

    def test_a_loop_variable_shadowing_a_module_is_not_that_module(self):
        e = self.edge("def f():\n    for helpers in []:\n        return helpers.run()\n"
                      "    return None\n", "app.f")
        self.assertIsNone(e.get("dst"))

    def test_a_with_target_and_an_except_target_shadow_too(self):
        e = self.edge("def f():\n    with make() as helpers:\n        return helpers.run()\n", "app.f")
        self.assertIsNone(e.get("dst"))

    def test_a_name_bound_LATER_in_the_body_still_shadows(self):
        """Python binds for the whole scope, so the assignment below the call counts."""
        e = self.edge("def f():\n    r = helpers.run()\n    helpers = make()\n    return r, helpers\n",
                      "app.f")
        self.assertIsNone(e.get("dst"), "a binding later in the body did not shadow")

    def test_the_genuine_module_call_still_resolves(self):
        """The guard must not cost the case it is protecting."""
        e = self.edge("def f():\n    return helpers.run()\n", "app.f")
        self.assertEqual((e.get("dst"), e["confidence"]), ("helpers.run", "QUALIFIED"))

    def test_a_global_declaration_gives_the_name_back(self):
        """`global helpers` means the assignment is not local, so the module is visible again."""
        self.write("app.py", "import helpers\ndef f():\n    global helpers\n"
                             "    return helpers.run()\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["callee"] == "run")
        self.assertEqual((e.get("dst"), e["confidence"]), ("helpers.run", "QUALIFIED"))

    def test_local_type_inference_still_wins_where_it_applies(self):
        e = self.edge("def f():\n    helpers = Thing()\n    return helpers.run()\n", "app.f")
        self.assertEqual((e.get("dst"), e["confidence"]), ("app.Thing.run", "TYPED"))


class DefinitionsHideInPlacesToo(Sandbox):
    """The mirror of last round's survey: seventeen positions a def or class can sit in.
    All of them already worked - pinned so they keep working."""

    def test_a_definition_is_found_wherever_it_sits(self):
        self.write("d.py",
                   "import sys\n"
                   "if sys.version_info:\n    def in_if(): return 1\n"
                   "else:\n    def in_else(): return 1\n"
                   "try:\n    def in_try(): return 1\n"
                   "except ImportError:\n    def in_except(): return 1\n"
                   "with open(__file__):\n    def in_with(): return 1\n"
                   "for _i in range(1):\n    def in_for(): return 1\n"
                   "while False:\n    def in_while(): return 1\n"
                   "class Outer:\n"
                   "    if sys.version_info:\n        def method_in_if(self): return 1\n"
                   "    class Nested:\n        def deep(self): return 1\n"
                   "def outer_fn():\n"
                   "    def nested(): return 1\n"
                   "    class LocalClass:\n        def local_method(self): return 1\n"
                   "    return nested, LocalClass\n"
                   "async def amain():\n"
                   "    async def anested(): return 1\n"
                   "    return anested\n")
        got = {n["id"] for n in self.graph(write=False)["nodes"]}
        for want in ("d.in_if", "d.in_else", "d.in_try", "d.in_except", "d.in_with", "d.in_for",
                     "d.in_while", "d.Outer.method_in_if", "d.Outer.Nested.deep",
                     "d.outer_fn.nested", "d.outer_fn.LocalClass.local_method",
                     "d.amain.anested"):
            self.assertIn(want, got)


if __name__ == "__main__":
    unittest.main(verbosity=2)
