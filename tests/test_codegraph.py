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
        self.assertIn("not found", r.stdout + r.stderr)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
