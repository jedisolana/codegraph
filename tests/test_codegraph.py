"""Tests for codegraph.

The tool ships its own `--selftest`, and that is deliberate: a single file you can copy into a
repo should be able to prove itself there, with no test runner and no checkout. This suite runs
that selftest as one case and then covers what it cannot -- the CLI, the on-disk contract, the
cache, and the honesty of the confidence labels -- plus the best available fixture, which is
codegraph reading its own source.
"""
from __future__ import annotations

import ast
import contextlib
import inspect
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

# Below the path setup on purpose, which is what E402 is about: neither module is importable
# until sys.path names the directory holding it, and this suite is run from a checkout rather
# than an install. Moving them up would not be tidier, it would be an ImportError.
import readme_stats as scrub_stats  # noqa: E402
import scrub  # noqa: E402

import codegraph  # noqa: E402


def samefile_key(path):
    """One spelling for a path, so a git-printed one and an OS-printed one compare equal.

    git prints forward slashes on every platform. Windows prints backslashes. Joining the
    first onto a Windows root gives `D:\a\repo\tests/test_codegraph.py`, which is a different
    STRING from the same file's os.path.abspath - and comparing those two strings has now
    broken this suite twice on Windows and never once here.
    """
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


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
        # Two modules starred in, both defining `go`. This is how a bare name reaches two
        # definitions in real Python - without the imports it is a NameError, and a fixture
        # that leaves them out is testing a guess rather than the language.
        self.write("a.py", "def go():\n    return 1\n")
        self.write("b.py", "def go():\n    return 2\n")
        self.write("c.py", "from a import *\nfrom b import *\n"
                           "def run():\n    return go()\n")     # which go()? nobody can say
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
        # Whether one is ALREADY here is the developer's business - running the tool in its own
        # repository is the most ordinary thing there is. What must be true is that this build
        # did not put one here.
        beside_the_script = os.path.join(HERE, "codegraph.json")
        before = os.path.exists(beside_the_script)
        stamp = os.path.getmtime(beside_the_script) if before else None
        self.graph()
        self.assertTrue(os.path.exists(os.path.join(self.dir, "codegraph.json")))
        self.assertEqual(os.path.exists(beside_the_script), before,
                         "the build created a graph next to the script")
        if before:
            self.assertEqual(os.path.getmtime(beside_the_script), stamp,
                             "the build overwrote the graph next to the script")

    def test_two_builds_of_unchanged_code_are_byte_identical(self):
        """Deterministic output is what makes a graph diffable between commits."""
        self.write("x.py", "def f():\n    return g()\ndef g():\n    return 1\n")
        self.write("sub/y.py", "def h():\n    return 2\n")
        first = json.dumps(self.graph(write=False), sort_keys=False)
        second = json.dumps(self.graph(write=False), sort_keys=False)
        self.assertEqual(first, second)

    def test_two_builds_in_separate_processes_agree_byte_for_byte(self):
        """The test above runs both builds in ONE process, where the hash seed is fixed - so
        the one kind of nondeterminism that actually happens is invisible to it.

        Output that depends on the iteration order of a set or a dict is identical every time
        within a process and differs between them, which is how it reaches a user as "the graph
        changed and nothing else did". Resolution builds several sets on the way through, so
        this runs two builds as subprocesses under deliberately different hash seeds.
        """
        self.write("x.py", "class A:\n"
                           "    def __new__(cls): return super().__new__(cls)\n"
                           "    def __enter__(self): return self\n"
                           "    def __exit__(self, *a): return False\n"
                           "    @property\n"
                           "    def tag(self): return 1\n")
        self.write("sub/y.py", "from ..x import A\n"
                               "def use():\n"
                               "    a = A()\n"
                               "    with a:\n"
                               "        return a.tag\n")
        out = []
        for seed in ("0", "12345"):
            env = {**os.environ, "PYTHONHASHSEED": seed,
                   "CODEGRAPH_OUT": os.path.join(self.dir, f"g{seed}.json"),
                   "CODEGRAPH_CACHE": os.path.join(self.dir, f"c{seed}.json")}
            r = subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "build", "."],
                               cwd=self.dir, capture_output=True, text=True, timeout=180, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(os.path.join(self.dir, f"g{seed}.json"), encoding="utf-8") as f:
                out.append(f.read())
        self.assertEqual(out[0], out[1],
                         "the graph depends on hash order: identical input, different bytes")

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
        self.write("top.py", "from core import leaf\ndef use():\n    return leaf()\n")
        self.graph()
        self.write("top.py", "from core import leaf\n"
                             "def use():\n    return leaf()\ndef extra():\n    return leaf()\n")
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
        the untypeable method calls makes it tautologically 1.0.

        The UNTYPED example has to be a name this tree DEFINES somewhere, because that is what
        the label now claims: the receiver could not be typed, and the target might be here.
        `d.get('k')` is not that - nothing here has a `get`, so it cannot be ours."""
        self.write("w.py",
                   "import os\n"
                   "def helper():\n    return 1\n"
                   "class Store:\n    def fetch(self, k):\n        return k\n"
                   "def go(d):\n"
                   "    a = len('x')\n"            # BUILTIN   - not winnable
                   "    b = os.path.join('a')\n"   # EXTERNAL  - not winnable
                   "    c = d.fetch('k')\n"        # UNTYPED   - winnable, and lost
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
            nxt_i = (i + 1) % 120
            lines = [f"from pkg{nxt_i // 20}.m{nxt_i:03d} import f{nxt_i}_0"]
            for j in range(20):
                nxt = f"f{i}_{j + 1}()" if j < 19 else f"f{nxt_i}_0()"
                lines += [f"def f{i}_{j}():", f"    return {nxt}"]
            self.write(f"{d}/{'__init__' if False else ''}m{i:03d}.py", "\n".join(lines) + "\n")
            self.write(f"{d}/__init__.py", "")
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
        self.write("caller.py", "from real import shared\ndef use():\n    return shared()\n")
        self.link("alias.py", "real.py")
        g = self.graph(write=False)
        e = next(x for x in g["calls"] if x["src"] == "caller.use" and x["callee"] == "shared")
        self.assertEqual((e.get("dst"), e["confidence"]), ("real.shared", "QUALIFIED"))
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
        self.assertIn("none of which could be read", r.stderr)   # the umbrella term: a syntax
                                                                 # error and a locked file both
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

    LABELS = frozenset({"SELF-METHOD", "TYPED", "QUALIFIED", "LOCAL", "INHERITED",
                        "CLASS", "CONSTRUCTOR", "AMBIGUOUS", "EXTERNAL", "BUILTIN", "UNTYPED"})
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
    """`impact start` reported no callers while a call to it sat in another file. Each edge was
    labelled honestly; the ANSWER was false reassurance, which is the one thing a pre-edit view
    must never give.

    The fixture used to be `Engine().start()`, which is now resolved outright - the class is
    written at the call site and reading it is no longer optional. What stands in its place is
    the shape still genuinely out of reach: a receiver that came back from a function whose
    return type nobody wrote down. The feature this class covers is what `impact` says when it
    cannot tell, so the fixture has to be something it actually cannot tell.
    """

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.dir, "pkg"))
        self.write("pkg/lib.py", "class Engine:\n    def start(self):\n        return 1\n"
                                 "def helper():\n    return 1\n")
        self.write("main.py", "from pkg.lib import Engine\n"
                              "def make():\n    return Engine()\n"
                              "def go():\n    return make().start()\n")
        self.g = self.graph()                            # written, so the CLI tests can read it

    def test_unresolved_uses_of_the_name_are_reported(self):
        im = codegraph.impact(self.g, "pkg/lib.Engine.start")
        self.assertEqual(im["callers"], [])
        self.assertEqual([loc for loc, _ in im["unresolved"]], ["main.py:5"])

    def test_a_method_called_straight_off_a_constructor_resolves(self):
        """`Leg("stock", 100).payoff(110)` names its class in the same expression, and that was
        read only when it went through a variable first - so the one-liner resolved to nothing
        and `unused` called the method dead while three lines called it.

        Found by running this tool over somebody else's codebase rather than its own; the
        standard library writes the shape 944 times.
        """
        self.write("m.py", "class Leg:\n"
                           "    def payoff(self, s):\n        return s\n"
                           "\n"
                           "def use():\n    return Leg().payoff(110)\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Leg.payoff"), ["m.use"])
        self.assertNotIn("m.Leg.payoff", [u[0] for u in codegraph.unused(g)])

    def test_a_function_nothing_touches_reports_nothing_extra(self):
        self.assertEqual(codegraph.impact(self.g, "pkg/lib.helper")["unresolved"], [])

    def test_the_cli_says_it_is_unsure(self):
        r = subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "impact", "start"],
                           cwd=self.dir, capture_output=True, text=True, timeout=180)
        self.assertIn("unsure:", r.stdout)
        self.assertIn("1 call site uses this name", r.stdout)
        self.assertIn("main.py:5", r.stdout)

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


class ThreeMoreWaysToBeCertainAndWrong(Sandbox):
    """Round nineteen fixed a local name shadowing an imported MODULE. The same fault sat in
    three sibling paths, unswept: a local name shadowing a CLASS, a local name shadowing a
    module-level FUNCTION, and a variable assigned two different classes in two branches -
    which was resolved by taking whichever branch happened to be walked last."""

    HEADER = ("class Parent:\n    @classmethod\n    def make(cls):\n        return 1\n\n\n"
              "class Alpha:\n    def go(self):\n        return 1\n\n\n"
              "class Beta:\n    def go(self):\n        return 1\n\n\n"
              "def helper():\n    return 1\n\n\n")

    def edge(self, body, src, callee):
        self.write("app.py", self.HEADER + body)
        hits = [e for e in self.graph(write=False)["calls"]
                if e["src"] == src and e["callee"] == callee]
        self.assertEqual(len(hits), 1, f"expected one edge, got {hits}")
        return hits[0]

    def test_a_parameter_shadowing_a_class_is_not_that_class(self):
        e = self.edge("def f(Parent):\n    return Parent.make()\n", "app.f", "make")
        self.assertIsNone(e.get("dst"), "a parameter resolved to the class it shadows")
        self.assertEqual(e["confidence"], "UNTYPED")

    def test_a_parameter_shadowing_a_module_function_is_not_that_function(self):
        e = self.edge("def f(helper):\n    return helper()\n", "app.f", "helper")
        self.assertIsNone(e.get("dst"))

    def test_two_branches_two_types_is_not_a_type(self):
        """It picked whichever branch was walked last and called it TYPED: right half the
        time, and certain both times."""
        e = self.edge("def f(c):\n    if c:\n        x = Alpha()\n    else:\n"
                      "        x = Beta()\n    return x.go()\n", "app.f", "go")
        self.assertIsNone(e.get("dst"))
        self.assertEqual(e["confidence"], "UNTYPED")

    def test_the_genuine_cases_all_still_resolve(self):
        """A guard that costs the cases it protects is not worth having."""
        e = self.edge("def f():\n    return Parent.make()\n", "app.f", "make")
        self.assertEqual((e.get("dst"), e["confidence"]), ("app.Parent.make", "CLASS"))
        e = self.edge("def f():\n    return helper()\n", "app.f", "helper")
        self.assertEqual((e.get("dst"), e["confidence"]), ("app.helper", "LOCAL"))
        e = self.edge("def f():\n    x = Alpha()\n    return x.go()\n", "app.f", "go")
        self.assertEqual((e.get("dst"), e["confidence"]), ("app.Alpha.go", "TYPED"))

    def test_a_nested_def_is_still_the_function_a_bare_call_means(self):
        """A nested def binds its name locally too - but `def g()` then `g()` really is that g,
        so the bare-name guard must look only at names bound to a VALUE."""
        self.write("n.py", "def outer():\n    def inner():\n        return 1\n"
                           "    return inner()\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["callee"] == "inner")
        self.assertEqual((e.get("dst"), e["confidence"]), ("n.outer.inner", "LOCAL"))

    def test_resolution_on_correct_code_is_unchanged(self):
        """Measured on this tool's own source: the same 364 edges resolve before and after.
        The guard bites only where a name genuinely shadows."""
        g = codegraph.build([HERE], write=False)
        self.assertGreater(codegraph.stats(g)["resolved_to_one_def"], 300)


class MethodLookupFollowsPython(Sandbox):
    """A depth-first walk of the bases is right for a chain and wrong for a diamond. With
    `class D(B, C)` where both derive from A, and both A and C define m, depth-first reaches A
    through B and stops - but Python's order is D, B, C, A, so C.m is what runs. The tool said
    A.m, labelled INHERITED. Ground truth here is the interpreter, not my reading of the rules."""

    HIERARCHY = (
        "class A:\n    def m(self):\n        return 'A'\n\n\n"
        "class B(A):\n    pass\n\n\n"
        "class C(A):\n    def m(self):\n        return 'C'\n\n\n"
        "class D(B, C):\n    def go(self):\n        return self.m()\n\n\n"
        "class Chain(A):\n    def go(self):\n        return self.m()\n"
    )

    def test_the_diamond_resolves_the_way_python_resolves_it(self):
        self.write("app.py", self.HIERARCHY)
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "app.D.go")
        self.assertEqual((e.get("dst"), e["confidence"]), ("app.C.m", "INHERITED"))

    def test_the_interpreter_agrees(self):
        """The strongest form of this test: import the fixture and ask Python."""
        self.write("app.py", self.HIERARCHY)
        sys.path.insert(0, self.dir)
        self.addCleanup(sys.path.remove, self.dir)
        self.addCleanup(sys.modules.pop, "app", None)
        import importlib
        app = importlib.import_module("app")
        self.assertEqual([c.__name__ for c in app.D.__mro__][:4], ["D", "B", "C", "A"])
        self.assertEqual(app.D().go(), "C")             # what actually runs
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "app.D.go")
        self.assertEqual(e["dst"], "app.C.m")           # what the tool says runs

    def test_a_plain_chain_is_unaffected(self):
        self.write("app.py", self.HIERARCHY)
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "app.Chain.go")
        self.assertEqual(e.get("dst"), "app.A.m")

    def test_an_override_still_beats_every_base(self):
        self.write("o.py", "class A:\n    def m(self):\n        return 1\n"
                           "class B(A):\n    def m(self):\n        return 2\n"
                           "    def go(self):\n        return self.m()\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "o.B.go")
        self.assertEqual((e.get("dst"), e["confidence"]), ("o.B.m", "SELF-METHOD"))

    def test_a_cyclic_hierarchy_terminates(self):
        """Illegal in Python, trivially expressible in a half-written file."""
        self.write("z.py", "class A(B):\n    def go(self):\n        return self.gone()\n"
                           "class B(A):\n    pass\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "z.A.go")
        self.assertIsNone(e.get("dst"))

    def test_an_unlinearisable_hierarchy_does_not_hang_or_lie(self):
        """C3 fails on this one - Python refuses to create the class at all."""
        self.write("bad.py", "class X:\n    def m(self):\n        return 1\n"
                             "class Y(X):\n    pass\n"
                             "class Z(X, Y):\n    def go(self):\n        return self.m()\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["src"] == "bad.Z.go")
        self.assertIn(e.get("dst"), (None, "bad.X.m"))   # partial or nothing, never invented


class ImportingASubmodule(Sandbox):
    """Three ordinary ways to reach into a package, and none of them resolved. An alias was
    mapped to the top-level package name, so `import pkg.mod as m` then m.func() looked for
    func in the package's __init__ and missed the submodule entirely - which is how most
    package code is written."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.dir, "pkg"))
        self.write("pkg/__init__.py", "def func():\n    return 'the package init'\n")
        self.write("pkg/mod.py", "def func():\n    return 'the submodule'\n")

    def edge(self, body, src):
        self.write("app.py", body)
        hits = [e for e in self.graph(write=False)["calls"]
                if e["src"] == src and e["callee"] == "func"]
        self.assertEqual(len(hits), 1, f"expected one edge, got {hits}")
        return hits[0]

    def test_import_submodule_as_alias(self):
        e = self.edge("import pkg.mod as m\ndef f():\n    return m.func()\n", "app.f")
        self.assertEqual((e.get("dst"), e["confidence"]), ("pkg/mod.func", "QUALIFIED"))

    def test_from_package_import_submodule(self):
        e = self.edge("from pkg import mod\ndef f():\n    return mod.func()\n", "app.f")
        self.assertEqual((e.get("dst"), e["confidence"]), ("pkg/mod.func", "QUALIFIED"))

    def test_a_whole_dotted_receiver(self):
        e = self.edge("import pkg.mod\ndef f():\n    return pkg.mod.func()\n", "app.f")
        self.assertEqual((e.get("dst"), e["confidence"]), ("pkg/mod.func", "QUALIFIED"))

    def test_the_package_init_is_still_reachable(self):
        """`from pkg import func` names a FUNCTION, not a module - it must not be confused
        with the submodule case."""
        e = self.edge("from pkg import func\ndef f():\n    return func()\n", "app.f")
        self.assertEqual(e.get("dst"), "pkg/__init__.func")

    def test_an_alias_to_a_library_is_still_external(self):
        """`import os.path as p` must not invent a module in this tree."""
        self.write("app.py", "import os.path as p\ndef f():\n    return p.join('a')\n")
        e = next(x for x in self.graph(write=False)["calls"] if x["callee"] == "join")
        self.assertIsNone(e.get("dst"))
        self.assertEqual(e["confidence"], "EXTERNAL")

    def test_a_shadowed_submodule_alias_is_still_refused(self):
        """The guard from the last two rounds has to hold on this path too."""
        e = self.edge("from pkg import mod\ndef f(mod):\n    return mod.func()\n", "app.f")
        self.assertIsNone(e.get("dst"), "a parameter resolved to the submodule it shadows")

    def test_it_lifts_resolution_on_a_real_codebase(self):
        """The reason this matters: `from package import module` is how packages are used.
        On this tool's own tests, that pattern is most of the imports."""
        g = codegraph.build([HERE], write=False)
        self.assertGreater(codegraph.stats(g)["resolution_rate"], 0.4)


class TheRemainingImportForms(Sandbox):
    """Sweeping the siblings of last round's fix rather than waiting for a later round to find
    them: relative submodule imports, and names re-exported through a package's __init__."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.dir, "pkg", "sub"))
        self.write("pkg/__init__.py", "from .thing import load\n")
        self.write("pkg/thing.py", "def load():\n    return 'the real load'\n")
        self.write("pkg/sub/__init__.py", "")
        self.write("pkg/sub/leaf.py", "def deep():\n    return 1\n")

    def edge(self, src, callee):
        hits = [e for e in self.graph(write=False)["calls"]
                if e["src"] == src and e["callee"] == callee]
        self.assertEqual(len(hits), 1, f"expected one edge from {src}, got {hits}")
        return hits[0]

    def test_a_relative_import_of_a_submodule_resolves(self):
        """The absolute form was fixed last round and this one was left behind - the two
        branches have to learn the same things or one of them lags."""
        self.write("pkg/rel.py", "from .sub import leaf\ndef f():\n    return leaf.deep()\n")
        e = self.edge("pkg/rel.f", "deep")
        self.assertEqual((e.get("dst"), e["confidence"]), ("pkg/sub/leaf.deep", "QUALIFIED"))

    def test_a_relative_alias_still_resolves(self):
        self.write("pkg/rel.py", "from . import thing as t\ndef f():\n    return t.load()\n")
        self.assertEqual(self.edge("pkg/rel.f", "load")["dst"], "pkg/thing.load")

    def test_a_name_re_exported_through_a_package_is_followed_home(self):
        """`pkg/__init__` does `from .thing import load`; another module does
        `from pkg import load`. The name is not defined in __init__ at all."""
        self.write("app.py", "from pkg import load\ndef f():\n    return load()\n")
        e = self.edge("app.f", "load")
        self.assertEqual((e.get("dst"), e["confidence"]), ("pkg/thing.load", "QUALIFIED"))

    def test_the_re_export_is_followed_rather_than_guessed(self):
        """It used to resolve only because the name was unique in the tree, which is luck.
        A second load() elsewhere turned that luck into AMBIGUOUS."""
        self.write("decoy.py", "def load():\n    return 'a different load'\n")
        self.write("app.py", "from pkg import load\ndef f():\n    return load()\n")
        e = self.edge("app.f", "load")
        self.assertEqual(e.get("dst"), "pkg/thing.load", "the decoy broke it")

    def test_a_circular_re_export_terminates(self):
        self.write("a.py", "from b import spin\n")
        self.write("b.py", "from a import spin\n")
        self.write("app.py", "from a import spin\ndef f():\n    return spin()\n")
        e = self.edge("app.f", "spin")
        self.assertIsNone(e.get("dst"))          # nothing defines it; never invented

    def test_an_aliased_from_import_of_a_submodule_resolves(self):
        self.write("app.py", "from pkg.sub import leaf as lf\ndef f():\n    return lf.deep()\n")
        self.assertEqual(self.edge("app.f", "deep")["dst"], "pkg/sub/leaf.deep")


class WhenTheFilesystemSaysNo(Sandbox):
    """Two ordinary situations - a container running as another user, a read-only mount - and
    both ended in a PermissionError traceback."""

    def test_one_unreadable_file_does_not_end_the_build(self):
        """The same mistake a dangling symlink once made: a single awkward file is not a
        reason to refuse to analyse a codebase."""
        self.write("ok.py", "def good():\n    return 1\n")
        locked = os.path.join(self.dir, "locked.py")
        with open(locked, "w", encoding="utf-8") as f:
            f.write("def secret():\n    return 1\n")
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o600)
        if os.access(locked, os.R_OK):
            self.skipTest("cannot make a file unreadable here (running as root?)")
        g = self.graph(write=False)
        self.assertIn("ok.good", {n["id"] for n in g["nodes"]})
        self.assertTrue(any("could not read" in u for u in g["unreadable"]), g["unreadable"])

    def test_the_message_names_the_actual_complaint(self):
        """A syntax error and a permission error are not the same problem, and "could not
        parse" for a file nobody could open sends you looking at the wrong thing."""
        self.write("bad.py", "def (((\n")
        g = self.graph(write=False)
        self.assertTrue(any("could not parse" in u for u in g["unreadable"]))


class WhenTheDirectoryIsReadOnly(unittest.TestCase):
    """The analysis works; only the writing fails, and there is somewhere else to put it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        with open(os.path.join(self.dir, "m.py"), "w", encoding="utf-8") as f:
            f.write("def f():\n    return 1\n")
        # 0o500, not 0o555: what this test needs is a directory IT cannot write to, and the
        # group and world bits do nothing for that while making a scanner right to complain.
        os.chmod(self.dir, 0o500)
        self.addCleanup(os.chmod, self.dir, 0o700)
        if os.access(self.dir, os.W_OK):
            self.skipTest("cannot make a directory read-only here (running as root?)")

    def run_it(self, env=None):
        return subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "build", "."],
                              cwd=self.dir, capture_output=True, text=True, timeout=180,
                              env={**os.environ, **(env or {})})

    def test_it_explains_instead_of_crashing(self):
        r = self.run_it()
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("cannot write", r.stderr)

    def test_the_message_names_the_file_that_actually_failed(self):
        """It used to report the graph's directory whichever write failed, which sends people
        to the wrong place - the cache is written first."""
        r = self.run_it()
        self.assertIn("codegraph.cache.json", r.stderr)

    def test_the_advice_it_prints_actually_works(self):
        """The escape hatch redirected the graph and left the cache writing into the directory
        that was unwritable in the first place. Advice that has not been run is not advice."""
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        target = os.path.join(out, "codegraph.json")
        r = self.run_it({"CODEGRAPH_OUT": target})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(target))
        self.assertTrue(os.path.exists(os.path.join(out, "codegraph.cache.json")),
                        "the cache did not follow the graph")


class ReadingAPropertyRunsIt(Sandbox):
    """`c.endpoint` on a property is a call, and it writes no parentheses.

    Nothing about it is an ast.Call, so the call visitor never saw one: every @property in a
    tree answered "callers: (none)", and `impact` on one said changing it would break nothing.
    That is the tool's worst shape of wrong answer, because it is the answer somebody deletes
    code on. In the standard library it covered 807 definitions, 331 of which really are read
    somewhere.
    """

    def test_a_property_read_through_an_annotation_has_a_caller(self):
        self.write("m.py", "class Cfg:\n"
                           "    @property\n"
                           "    def endpoint(self):\n"
                           "        return 'x'\n"
                           "\n"
                           "def reads(c: Cfg):\n"
                           "    return c.endpoint\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Cfg.endpoint"), ["m.reads"])

    def test_the_method_beside_it_already_worked(self):
        """The control that makes the test above mean something: same class, same annotation,
        same shape. If this ever fails the two are broken together and neither proves anything
        about properties."""
        self.write("m.py", "class Cfg:\n"
                           "    def method(self):\n"
                           "        return 'y'\n"
                           "\n"
                           "def calls(c: Cfg):\n"
                           "    return c.method()\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Cfg.method"), ["m.calls"])

    def test_a_property_read_on_self_resolves_in_its_own_class(self):
        self.write("m.py", "class Cfg:\n"
                           "    @property\n"
                           "    def endpoint(self):\n"
                           "        return 'x'\n"
                           "\n"
                           "    def show(self):\n"
                           "        return self.endpoint\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Cfg.endpoint"), ["m.Cfg.show"])

    def test_an_inherited_property_is_found_through_the_mro(self):
        """`self.raw` in a subclass, where the property is on the mixin - the shape the
        standard library's buffered IO is built out of, and where most of the real edges are."""
        self.write("m.py", "class Mixin:\n"
                           "    @property\n"
                           "    def raw(self):\n"
                           "        return self._raw\n"
                           "\n"
                           "class Reader(Mixin):\n"
                           "    def peek(self):\n"
                           "        return self.raw\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Mixin.raw"), ["m.Reader.peek"])

    def test_cached_property_counts_and_so_does_a_setter(self):
        self.write("m.py", "from functools import cached_property\n"
                           "\n"
                           "class Cfg:\n"
                           "    @cached_property\n"
                           "    def heavy(self):\n"
                           "        return 1\n"
                           "\n"
                           "    @property\n"
                           "    def name(self):\n"
                           "        return self._n\n"
                           "\n"
                           "    @name.setter\n"
                           "    def name(self, v):\n"
                           "        self._n = v\n"
                           "\n"
                           "def use(c: Cfg):\n"
                           "    return c.heavy, c.name\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Cfg.heavy"), ["m.use"])
        self.assertEqual(codegraph.callers_of(g, "m.Cfg.name"), ["m.use"])

    def test_an_ordinary_attribute_is_not_a_call(self):
        """The negative half, and the one that keeps the graph honest. Every typed attribute
        read is recorded while parsing, because whether a name is a property cannot be known
        one file at a time - and all but a fraction are plain fields that must be dropped."""
        self.write("m.py", "class Cfg:\n"
                           "    def __init__(self):\n"
                           "        self.count = 0\n"
                           "\n"
                           "    def show(self):\n"
                           "        return self.count\n"
                           "\n"
                           "def use(c: Cfg):\n"
                           "    return c.count\n")
        g = self.graph()
        reads = [e for e in g["calls"] if e["callee"] == "count"]
        self.assertEqual(reads, [], f"a plain attribute became a call edge: {reads}")

    def test_a_property_name_belonging_to_another_class_is_not_borrowed(self):
        """Two classes, one property name. Reading it off the class that does not define it
        must not point at the one that does."""
        self.write("m.py", "class HasIt:\n"
                           "    @property\n"
                           "    def tag(self):\n"
                           "        return 'a'\n"
                           "\n"
                           "class HasNot:\n"
                           "    def __init__(self):\n"
                           "        self.tag = 'b'\n"
                           "\n"
                           "def use(x: HasNot):\n"
                           "    return x.tag\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.HasIt.tag"), [])

    def test_a_property_that_nothing_reads_is_still_reported_unused(self):
        self.write("m.py", "class Cfg:\n"
                           "    @property\n"
                           "    def never(self):\n"
                           "        return 1\n")
        g = self.graph()
        self.assertIn("m.Cfg.never", [u[0] for u in codegraph.unused(g)])


class TheLanguageCallsThingsTheSourceNeverNames(Sandbox):
    """`with r:` runs `__enter__`. `for x in r` runs `__iter__`. `len(r)` runs `__len__`.

    None of these is an ast.Call on the method, so a graph built from call nodes recorded no
    edge for any of them: 2,780 definitions in the standard library are reached only this way,
    and 98% of them reported no caller. `unused` already annotated them "(python calls this
    one)" - the tool knew the category existed and still answered `impact __exit__` with
    "nothing depends on this".

    Same reach as a written method call: the receiver has to be `self`/`cls` in a class, or a
    local whose class is known.
    """

    HEAD = ("class R:\n"
            "    def __enter__(self): return self\n"
            "    def __exit__(self, *a): return False\n"
            "    def __iter__(self): return iter([])\n"
            "    def __len__(self): return 0\n"
            "    def __getitem__(self, k): return k\n"
            "    def __setitem__(self, k, v): pass\n"
            "    def __delitem__(self, k): pass\n"
            "    def __add__(self, o): return o\n"
            "    def __neg__(self): return self\n"
            "    def __eq__(self, o): return True\n"
            "    def __contains__(self, o): return True\n"
            "    def __str__(self): return ''\n")

    def build(self, body):
        self.write("m.py", self.HEAD + "\n" + body)
        return self.graph()

    def test_a_with_block_calls_enter_and_exit(self):
        g = self.build("def use():\n    r = R()\n    with r:\n        pass\n")
        self.assertEqual(codegraph.callers_of(g, "m.R.__enter__"), ["m.use"])
        self.assertEqual(codegraph.callers_of(g, "m.R.__exit__"), ["m.use"])

    def test_a_for_loop_calls_iter(self):
        g = self.build("def use():\n    r = R()\n    for _ in r:\n        pass\n")
        self.assertEqual(codegraph.callers_of(g, "m.R.__iter__"), ["m.use"])

    def test_the_three_subscript_forms_are_three_different_methods(self):
        g = self.build("def use():\n    r = R()\n    r[1]\n    r[2] = 3\n    del r[4]\n")
        self.assertEqual(codegraph.callers_of(g, "m.R.__getitem__"), ["m.use"])
        self.assertEqual(codegraph.callers_of(g, "m.R.__setitem__"), ["m.use"])
        self.assertEqual(codegraph.callers_of(g, "m.R.__delitem__"), ["m.use"])

    def test_a_builtin_that_is_a_method_in_disguise(self):
        g = self.build("def use():\n    r = R()\n    return len(r), str(r)\n")
        self.assertEqual(codegraph.callers_of(g, "m.R.__len__"), ["m.use"])
        self.assertEqual(codegraph.callers_of(g, "m.R.__str__"), ["m.use"])

    def test_operators_resolve_on_the_left_operand(self):
        g = self.build("def use():\n    r = R()\n    return (r + 1), (-r), (r == 2)\n")
        self.assertEqual(codegraph.callers_of(g, "m.R.__add__"), ["m.use"])
        self.assertEqual(codegraph.callers_of(g, "m.R.__neg__"), ["m.use"])
        self.assertEqual(codegraph.callers_of(g, "m.R.__eq__"), ["m.use"])

    def test_in_reverses_the_operands(self):
        """The one place in the table where the receiver is on the right: `a in b` runs
        b.__contains__. Written the other way round this would silently attribute every
        membership test to the wrong class."""
        g = self.build("def use():\n    r = R()\n    return 1 in r\n")
        self.assertEqual(codegraph.callers_of(g, "m.R.__contains__"), ["m.use"])

    def test_an_async_with_calls_the_async_pair(self):
        self.write("m.py", "class A:\n"
                           "    async def __aenter__(self): return self\n"
                           "    async def __aexit__(self, *a): return False\n"
                           "\n"
                           "async def use():\n"
                           "    a = A()\n"
                           "    async with a:\n"
                           "        pass\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.A.__aenter__"), ["m.use"])
        self.assertEqual(codegraph.callers_of(g, "m.A.__aexit__"), ["m.use"])

    def test_it_resolves_on_self_and_through_a_base_class(self):
        self.write("m.py", "class Base:\n"
                           "    def __iter__(self): return iter([])\n"
                           "\n"
                           "class Sub(Base):\n"
                           "    def walk(self):\n"
                           "        for _ in self:\n"
                           "            pass\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Base.__iter__"), ["m.Sub.walk"])

    def test_an_untyped_receiver_records_nothing(self):
        """The negative half. `with thing:` on a parameter nobody annotated is exactly the
        blind spot a written `thing.method()` has, and inventing an answer here would be worse
        than the gap - there is more than one class in a tree with an __enter__."""
        g = self.build("def use(thing):\n    with thing:\n        pass\n")
        self.assertEqual(codegraph.callers_of(g, "m.R.__enter__"), [])

    def test_a_method_the_class_does_not_have_is_external_not_unknown(self):
        """`for row in rows` where rows subclasses something outside the tree runs an
        __iter__ that is genuinely not here. Left as UNTYPED it would claim the target might
        be in the tree and drag the resolution rate down with a case nobody could win."""
        self.write("m.py", "class Rows(list):\n"
                           "    pass\n"
                           "\n"
                           "def use():\n"
                           "    r = Rows()\n"
                           "    for _ in r:\n"
                           "        pass\n")
        g = self.graph()
        got = [e["confidence"] for e in g["calls"] if e["callee"] == "__iter__"]
        self.assertEqual(got, ["EXTERNAL"], f"expected EXTERNAL, got {got}")


class IterationCountsHoweverItIsWritten(Sandbox):
    """`for x in r` was recorded and `[x for x in r]` was not - the same operation, two
    spellings, two different answers.

    The statement form is under half of it: the standard library writes 11,571 `for` statements
    against 15,322 comprehension clauses, unpackings, star-expansions and augmented
    assignments, none of which produced an edge.
    """

    HEAD = ("class R:\n"
            "    def __iter__(self): return iter([])\n"
            "    def __iadd__(self, o): return self\n")

    def build(self, body):
        self.write("m.py", self.HEAD + "\n" + body)
        return self.graph()

    def test_a_comprehension_iterates(self):
        g = self.build("def use():\n    r = R()\n    return [x for x in r]\n")
        self.assertEqual(codegraph.callers_of(g, "m.R.__iter__"), ["m.use"])

    def test_a_generator_expression_iterates(self):
        g = self.build("def use():\n    r = R()\n    return sum(x for x in r)\n")
        self.assertEqual(codegraph.callers_of(g, "m.R.__iter__"), ["m.use"])

    def test_a_dict_comprehension_iterates(self):
        g = self.build("def use():\n    r = R()\n    return {x: 1 for x in r}\n")
        self.assertEqual(codegraph.callers_of(g, "m.R.__iter__"), ["m.use"])

    def test_the_second_clause_of_a_comprehension_iterates_too(self):
        """Only the first iterable runs in the enclosing scope; the rest run inside the
        comprehension's own. Both are iterations, and a version that walked only the first
        would pass every test above."""
        self.write("m.py", self.HEAD + "\n"
                   "class S:\n"
                   "    def __iter__(self): return iter([])\n"
                   "\n"
                   "def use():\n"
                   "    r = R()\n"
                   "    s = S()\n"
                   "    return [(a, b) for a in r for b in s]\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.R.__iter__"), ["m.use"])
        self.assertEqual(codegraph.callers_of(g, "m.S.__iter__"), ["m.use"])

    def test_unpacking_iterates(self):
        g = self.build("def use():\n    r = R()\n    a, b = r\n    return a, b\n")
        self.assertEqual(codegraph.callers_of(g, "m.R.__iter__"), ["m.use"])

    def test_a_plain_assignment_does_not(self):
        """The negative half: `x = r` binds a name and iterates nothing."""
        g = self.build("def use():\n    r = R()\n    x = r\n    return x\n")
        self.assertEqual(codegraph.callers_of(g, "m.R.__iter__"), [])

    def test_a_star_expansion_iterates_but_a_star_target_does_not(self):
        """`[*r]` iterates. In `a, *b = r` the star is a TARGET being filled, and the single
        iteration belongs to the unpacking - counting both would report it twice."""
        g = self.build("def spread():\n    r = R()\n    return [*r]\n"
                       "\n"
                       "def target():\n    r = R()\n    a, *b = r\n    return a, b\n")
        self.assertEqual(codegraph.callers_of(g, "m.R.__iter__"), ["m.spread", "m.target"])
        sites = [e for e in g["calls"]
                 if e.get("dst") == "m.R.__iter__" and e["src"] == "m.target"]
        self.assertEqual(len(sites), 1, f"the unpacking was counted twice: {sites}")

    def test_yield_from_iterates(self):
        g = self.build("def use():\n    r = R()\n    yield from r\n")
        self.assertEqual(codegraph.callers_of(g, "m.R.__iter__"), ["m.use"])

    def test_an_augmented_assignment_runs_the_in_place_method(self):
        g = self.build("def use():\n    r = R()\n    r += 1\n    return r\n")
        self.assertEqual(codegraph.callers_of(g, "m.R.__iadd__"), ["m.use"])

    def test_it_falls_back_to_the_plain_operator_when_there_is_no_in_place_one(self):
        """`x += y` on a class with only `__add__` runs `__add__`. Which name the interpreter
        reaches is a fact about the class, so it cannot be decided while parsing the line -
        and `__add__` appears in nearly four times as many standard-library files as
        `__iadd__`, so this is the common case."""
        self.write("m.py", "class OnlyAdd:\n"
                           "    def __add__(self, o): return self\n"
                           "\n"
                           "def use():\n"
                           "    x = OnlyAdd()\n"
                           "    x += 1\n"
                           "    return x\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.OnlyAdd.__add__"), ["m.use"])

    def test_the_in_place_method_wins_when_the_class_has_both(self):
        """The other half of the fallback, and the one that keeps it honest: crediting both
        would say `__add__` has a caller when the interpreter never reaches it."""
        self.write("m.py", "class Both:\n"
                           "    def __add__(self, o): return self\n"
                           "    def __iadd__(self, o): return self\n"
                           "\n"
                           "def use():\n"
                           "    x = Both()\n"
                           "    x += 1\n"
                           "    return x\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Both.__iadd__"), ["m.use"])
        self.assertEqual(codegraph.callers_of(g, "m.Both.__add__"), [])


class TwoCallsAreOneEdgeOnlyIfTheyLandInTheSamePlace(Sandbox):
    """One method holding `super(A, self).run()` and `super(C, self).run()` produced ONE edge.

    Edges were deduplicated on (caller, receiver, name), and neither super writes a receiver,
    so the two collapsed. The survivor kept the other one's line number as one of its own call
    sites: the tool lost a caller and invented a site in the same breath, and reported both
    with confidence. Two edges are the same relationship only when they resolve to the same
    place, so everything that decides that belongs in the key.
    """

    def test_two_supers_in_one_method_are_two_edges(self):
        self.write("m.py", "class A:\n"
                           "    def run(self): return 'a'\n"
                           "\n"
                           "class B:\n"
                           "    def run(self): return 'b'\n"
                           "\n"
                           "class C(A, B):\n"
                           "    def run(self):\n"
                           "        x = super(A, self).run()\n"
                           "        y = super(C, self).run()\n"
                           "        return x, y\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.A.run"), ["m.C.run"])
        self.assertEqual(codegraph.callers_of(g, "m.B.run"), ["m.C.run"])

    def test_neither_edge_claims_the_other_ones_line(self):
        """The half a caller list cannot show. `sites` promises the exact places to edit, and
        the merged edge offered two lines for a call that happens on one of them."""
        self.write("m.py", "class A:\n"
                           "    def run(self): return 'a'\n"
                           "\n"
                           "class B:\n"
                           "    def run(self): return 'b'\n"
                           "\n"
                           "class C(A, B):\n"
                           "    def run(self):\n"
                           "        x = super(A, self).run()\n"
                           "        y = super(C, self).run()\n"
                           "        return x, y\n")
        g = self.graph()
        lines = {e["dst"]: e["lines"] for e in g["calls"] if e["callee"] == "run"}
        self.assertEqual(lines, {"m.B.run": [9], "m.A.run": [10]})

    def test_no_field_escapes_the_attribute_read_key_either(self):
        """Attribute reads are deduplicated on four fields rather than eighteen, because they
        reach exactly two resolution paths and are emitted forty thousand at a time to keep
        nine hundred. That is a second key, and a second key is a second thing that can fall
        behind the edges it identifies - so it gets the same check as the first."""
        seen = set()
        for rel in ("codegraph.py", "tests/test_codegraph.py", "tools/mutation.py"):
            _defs, edges, *_rest = codegraph._defs_and_calls(
                os.path.join(HERE, rel), os.path.basename(rel)[:-3])
            for e in edges:
                if e.get("attr_read"):
                    seen.update(e.keys())
        self.assertTrue(seen, "no attribute-read edges were produced to check")
        # `attr_read`, `method` and `kind` are constant on every one of them; `line` is where
        # it was written, which is what the dedup deliberately collapses.
        escaped = seen - set(codegraph.ATTR_IDENTITY) - {"attr_read", "method", "kind",
                                                         "mod", "line", "lines"}
        self.assertEqual(escaped, set(),
                         f"an attribute-read edge carries {escaped}, which its key ignores")

    def test_a_short_key_cannot_collide_with_a_long_one(self):
        """The two keys share one dictionary. Without something to tell them apart, a
        five-field tuple could in principle equal the first five of an eighteen-field one."""
        self.assertNotIn(codegraph.ATTR_IDENTITY_MARK, codegraph.EDGE_IDENTITY)
        self.assertTrue(codegraph.ATTR_IDENTITY_MARK.startswith("\0"))

    def test_the_same_call_written_twice_is_still_one_edge(self):
        """The control. Splitting on everything would be as wrong as merging on nothing:
        calling the same helper three times is one relationship with three sites."""
        self.write("m.py", "def helper(): return 1\n"
                           "\n"
                           "def use():\n"
                           "    helper()\n"
                           "    helper()\n"
                           "    helper()\n")
        g = self.graph()
        edges = [e for e in g["calls"] if e.get("dst") == "m.helper"]
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["lines"], [4, 5, 6])

    def test_no_field_escapes_the_identity_tuple(self):
        """The key is a written list because deriving it costs a fifth of the build. The
        failure mode of a written list is forgetting to add to it, and the symptom is an edge
        quietly swallowing another - so the list is checked rather than trusted."""
        seen = set()
        for rel in ("codegraph.py", "tests/test_codegraph.py", "tools/mutation.py"):
            _defs, edges, *_rest = codegraph._defs_and_calls(
                os.path.join(HERE, rel), os.path.basename(rel)[:-3])
            for e in edges:
                seen.update(e.keys())
        escaped = seen - set(codegraph.EDGE_IDENTITY) - {"line", "lines"}
        self.assertEqual(escaped, set(),
                         f"an edge carries {escaped}, which nothing distinguishes it by")
        self.assertGreater(len(seen), 8, "the scan produced almost no edges to check")


class ThingsCalledWhenAClassOrAStringIsBuilt(Sandbox):
    """More calls with no call syntax, found by asking what else the language runs on its own.

    `class Child(Base)` runs `Base.__init_subclass__`. Building a dataclass runs
    `__post_init__` from an `__init__` that is generated and therefore is not in the graph at
    all. An f-string placeholder runs `__format__`. In the standard library those three had 2
    of 45, 5 of 25 and 3 of 29 definitions with a caller.
    """

    def test_defining_a_subclass_calls_init_subclass(self):
        self.write("m.py", "class Base:\n"
                           "    def __init_subclass__(cls, **kw): pass\n"
                           "\n"
                           "class Child(Base):\n"
                           "    pass\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Base.__init_subclass__"), ["m"])

    def test_a_class_does_not_call_its_own_init_subclass(self):
        """It is looked up on the order AFTER the new class, exactly as `super()` is - Python
        does not run a class's own `__init_subclass__` when defining it."""
        self.write("m.py", "class Solo:\n"
                           "    def __init_subclass__(cls, **kw): pass\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Solo.__init_subclass__"), [])

    def test_building_a_dataclass_calls_post_init(self):
        self.write("m.py", "import dataclasses\n"
                           "\n"
                           "@dataclasses.dataclass\n"
                           "class Point:\n"
                           "    x: int\n"
                           "    def __post_init__(self): pass\n"
                           "\n"
                           "def make():\n"
                           "    return Point(1)\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Point.__post_init__"), ["m.make"])

    def test_a_class_that_writes_its_own_init_is_not_counted_twice(self):
        """Only a generated `__init__` is invisible. A class that writes one calls
        `__post_init__` in the open, and crediting the construction site as well would invent
        a caller that is not there."""
        self.write("m.py", "class Manual:\n"
                           "    def __init__(self):\n"
                           "        self.__post_init__()\n"
                           "    def __post_init__(self): pass\n"
                           "\n"
                           "def make():\n"
                           "    return Manual()\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Manual.__post_init__"), ["m.Manual.__init__"])

    def test_an_f_string_formats_its_value(self):
        self.write("m.py", "class F:\n"
                           "    def __format__(self, spec): return 'f'\n"
                           "\n"
                           "def use():\n"
                           "    f = F()\n"
                           "    return f'{f}'\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.F.__format__"), ["m.use"])

    def test_the_r_conversion_runs_repr_instead(self):
        self.write("m.py", "class F:\n"
                           "    def __format__(self, spec): return 'f'\n"
                           "    def __repr__(self): return 'r'\n"
                           "\n"
                           "def use():\n"
                           "    f = F()\n"
                           "    return f'{f!r}'\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.F.__repr__"), ["m.use"])
        self.assertEqual(codegraph.callers_of(g, "m.F.__format__"), [])

    def test_a_class_with_no_format_falls_back_to_str(self):
        """object.__format__ with an empty spec calls str(), so a class that defines only
        __str__ is what an f-string actually reaches - the same fallback shape as `x += y`."""
        self.write("m.py", "class S:\n"
                           "    def __str__(self): return 's'\n"
                           "\n"
                           "def use():\n"
                           "    s = S()\n"
                           "    return f'{s}'\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.S.__str__"), ["m.use"])


class ARenamedImportIsStillTheSameFunction(Sandbox):
    """`from pkg import load` resolved and `from pkg import load as l` did not.

    A package re-exports a name, and the chain that follows it to where the name really lives
    was walked under the name written HERE. Alias it and no module along the chain has ever
    heard of that name, so the first hop fails and the call comes back EXTERNAL - the label
    that means "not in your tree" about a function two directories away. Two spellings of one
    import, two different answers, and the wrong one silent.
    """

    def tree(self, consumer):
        self.write("a/__init__.py", "")
        self.write("a/inner.py", "def shared():\n    return 'a'\n")
        self.write("a/sub/__init__.py", "from ..inner import shared\n")
        self.write("uses.py", consumer)
        return self.graph()

    def test_a_reexported_name_resolves_under_an_alias(self):
        g = self.tree("from a.sub import shared as s\n\n\ndef use():\n    return s()\n")
        self.assertEqual(codegraph.callers_of(g, "a/inner.shared"), ["uses.use"])

    def test_the_unaliased_form_still_resolves(self):
        """The control that makes the test above a comparison rather than a claim: this one
        always worked, and if it ever stops the two are broken together."""
        g = self.tree("from a.sub import shared\n\n\ndef use():\n    return shared()\n")
        self.assertEqual(codegraph.callers_of(g, "a/inner.shared"), ["uses.use"])

    def test_a_direct_aliased_import_still_resolves(self):
        """The other control: aliasing was never broken on its own, only in combination with
        a re-export, which is what made it look like the alias worked."""
        g = self.tree("from a.inner import shared as s\n\n\ndef use():\n    return s()\n")
        self.assertEqual(codegraph.callers_of(g, "a/inner.shared"), ["uses.use"])

    def test_a_name_renamed_again_midway_is_still_followed(self):
        """Every hop is free to rename it. Following the chain to the right module and then
        asking that module for the wrong name resolves to nothing, which is indistinguishable
        from a call to something outside the tree."""
        self.write("pk/__init__.py", "")
        self.write("pk/base.py", "def original():\n    return 1\n")
        self.write("pk/mid/__init__.py", "from ..base import original as renamed\n")
        self.write("top.py", "from pk.mid import renamed as again\n\n\ndef use():\n    return again()\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "pk/base.original"), ["top.use"])

    def test_a_circular_reexport_terminates(self):
        """Two packages re-exporting each other is expressible even though it would not
        import. The walk has a hop limit and a seen-set; carrying a renamed name through it
        must not give either of them the slip."""
        self.write("x/__init__.py", "from y import thing as thing\n")
        self.write("y/__init__.py", "from x import thing as thing\n")
        self.write("go.py", "from x import thing\n\n\ndef use():\n    return thing()\n")
        g = self.graph()                      # the assertion is that this returns at all
        self.assertTrue(any(n["id"] == "go.use" for n in g["nodes"]))


class ConstructingRunsNewAsWellAsInit(Sandbox):
    """`X()` runs `__new__` and then `__init__`, and only the second half was recorded.

    The constructor edge was added because `impact __init__` on a class built in twenty places
    answered "callers: (none)". The same argument covers `__new__`, and it was left out: 237
    definitions in the standard library, 8 of them with a caller. A class that defines only
    `__new__` - a singleton, an immutable type, anything that interns its instances - reported
    that nothing depends on the method that builds it.

    Not an either/or. A class defining both runs both, so both get the edge.
    """

    def test_a_class_with_only_new_has_a_constructor_caller(self):
        self.write("m.py", "class Singleton:\n"
                           "    def __new__(cls, *a):\n"
                           "        return super().__new__(cls)\n"
                           "\n"
                           "def build():\n"
                           "    return Singleton()\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Singleton.__new__"), ["m.build"])

    def test_a_class_with_both_credits_both(self):
        """Python calls `__new__` then `__init__`; picking one would be inventing an answer
        about which half of construction a change can reach."""
        self.write("m.py", "class Both:\n"
                           "    def __new__(cls, *a):\n"
                           "        return super().__new__(cls)\n"
                           "    def __init__(self, x):\n"
                           "        self.x = x\n"
                           "\n"
                           "def build():\n"
                           "    return Both(1)\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Both.__new__"), ["m.build"])
        self.assertEqual(codegraph.callers_of(g, "m.Both.__init__"), ["m.build"])

    def test_an_inherited_new_is_found_through_the_mro(self):
        """Same order the interpreter uses, the way an inherited `__init__` already was."""
        self.write("m.py", "class Base:\n"
                           "    def __new__(cls, *a):\n"
                           "        return super().__new__(cls)\n"
                           "\n"
                           "class Sub(Base):\n"
                           "    pass\n"
                           "\n"
                           "def build():\n"
                           "    return Sub()\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Base.__new__"), ["m.build"])

    def test_a_class_with_neither_produces_no_such_edge(self):
        """The negative half: `object.__new__` is not in the tree, and inventing an edge to a
        method the class does not have would be worse than the gap it fills."""
        self.write("m.py", "class Plain:\n"
                           "    pass\n"
                           "\n"
                           "def build():\n"
                           "    return Plain()\n")
        g = self.graph()
        made_up = [e for e in g["calls"] if e["callee"] in ("__new__", "__init__")]
        self.assertEqual(made_up, [])


class TheCommitMessageHookRefusesWhatCannotBeTakenBack(unittest.TestCase):
    """A stray CJK character landed mid-word in a commit message here and went out on a push.

    It is invisible in a terminal at a glance and survives any review that reads for meaning
    rather than for bytes. A commit message cannot be corrected after a push without rewriting
    history, and that removes nothing - the original stays fetchable by SHA - so the only place
    to catch it is before the commit exists.

    The hook is tested because the first version of it used `grep -P`, which BSD grep does not
    have, and the failure was swallowed: it reported success on every message. A guard that
    cannot run and says nothing is worse than no guard, because it is also believed.
    """

    HOOK = os.path.join(HERE, ".githooks", "commit-msg")
    # Run it THROUGH bash rather than as an executable. Windows has no shebang: handing a
    # shell script to CreateProcess raises "WinError 193, %1 is not a valid Win32 application",
    # which is what the first version of this class did on three of the nine CI jobs. Git for
    # Windows runs hooks under its own bundled bash, so bash is what actually runs this in
    # anger on every platform, and it is what the test should use.
    BASH = shutil.which("bash")

    def check(self, text, env=None):
        path = os.path.join(tempfile.mkdtemp(), "COMMIT_EDITMSG")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), ignore_errors=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return subprocess.run([self.BASH, self.HOOK, path], capture_output=True, text=True,
                              timeout=60, env={**os.environ, **(env or {})})

    def setUp(self):
        if not self.BASH:
            self.skipTest("no bash to run a git hook with")

    def test_the_hook_is_in_the_repository_and_marked_executable(self):
        self.assertTrue(os.path.exists(self.HOOK), "the hook is not in the repository")
        mode = subprocess.run([shutil.which("git") or "git", "ls-files", "-s",
                               ".githooks/commit-msg"], cwd=HERE, capture_output=True, text=True)
        # git records the mode itself, which is the one that survives a clone on any platform.
        self.assertTrue(mode.stdout.startswith("100755"),
                        f"git has it as {mode.stdout.split()[0] if mode.stdout else 'absent'}, not 100755")

    def test_it_refuses_a_message_with_a_character_outside_ascii(self):
        r = self.check("Subject line\n\nand a test\u5df2 checked\n")
        self.assertEqual(r.returncode, 1, f"the hook accepted it: {r.stdout} {r.stderr}")
        self.assertIn("U+5DF2", r.stderr, "it did not name the character it objected to")

    def test_it_accepts_an_ordinary_message(self):
        r = self.check("Subject line\n\nand a test already checked\n")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_git_scaffolding_is_not_the_message(self):
        """Everything after a `#` is stripped before the message is stored, and the template
        git writes there is not always ASCII."""
        r = self.check("Subject line\n# a comment with \u00e9 in it\n")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_there_is_a_way_past_it(self):
        """A guard with no override gets uninstalled the first time it is wrong."""
        r = self.check("Subject\n\n\u5df2\n", env={"CODEGRAPH_ALLOW_UTF8": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)


class AClassNestedInsideAClassIsStillAType(Sandbox):
    """`i = Outer.Inner()` then `i.method()` - the construction resolved and the method did not.

    Resolving a dotted type name only ever read the first part as a MODULE, so `Outer.Inner`
    found nothing, the variable was left untyped, and every call on it afterwards came back
    unresolved. The constructor edge landed perfectly the whole time, which is what made this
    hard to see: the class was clearly known, and the line after it was a blind spot.
    """

    def test_a_variable_built_from_a_nested_class_is_typed(self):
        self.write("m.py", "class Outer:\n"
                           "    class Inner:\n"
                           "        def method(self):\n"
                           "            return 1\n"
                           "\n"
                           "def use():\n"
                           "    i = Outer.Inner()\n"
                           "    return i.method()\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Outer.Inner.method"), ["m.use"])

    def test_the_construction_itself_always_resolved(self):
        """The control that shows which half was broken."""
        self.write("m.py", "class Outer:\n"
                           "    class Inner:\n"
                           "        def __init__(self):\n"
                           "            pass\n"
                           "\n"
                           "def use():\n"
                           "    return Outer.Inner()\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Outer.Inner.__init__"), ["m.use"])

    def test_a_module_qualified_class_still_wins(self):
        """The reading that already worked, and must keep working: `svc.Client()` where `svc`
        is an imported module. A class of the same name must not steal it."""
        self.write("svc.py", "class Client:\n"
                             "    def get(self):\n"
                             "        return 1\n")
        self.write("m.py", "import svc\n"
                           "\n"
                           "def use():\n"
                           "    c = svc.Client()\n"
                           "    return c.get()\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "svc.Client.get"), ["m.use"])

    def test_a_dotted_name_that_is_neither_types_nothing(self):
        """The negative half. `thing.Widget()` where `thing` is a parameter names no class the
        file can see, and guessing at one is how a blast radius points at the wrong code."""
        self.write("m.py", "class Widget:\n"
                           "    def go(self):\n"
                           "        return 1\n"
                           "\n"
                           "def use(thing):\n"
                           "    w = thing.Widget()\n"
                           "    return w.go()\n")
        g = self.graph()
        self.assertEqual(codegraph.callers_of(g, "m.Widget.go"), [])


class OptionalIsNotAContainer(Sandbox):
    """`c: Client | None` says outright what c is, and it was read as nothing.

    A subscripted annotation was skipped wholesale, on the sound reasoning that
    `Dict[str, Client]` is a dict and reading Client out of it would resolve `d.get()` to a
    Client method. `Optional[Client]`, `Union[Client, None]` and `Client | None` are not
    containers: each says "a Client, or nothing at all", and a method called on one is a
    Client's method with no second candidate.

    It was the costliest annotation gap left. In an installed-packages corpus `X | None` is 414
    of the annotated parameters against 550 plain ones - two in five stated their type and were
    not listened to.
    """

    HEAD = ("from typing import Dict, List, Optional, Union\n"
            "\n"
            "class Client:\n"
            "    def go(self): return 1\n"
            "\n"
            "class Server:\n"
            "    def go(self): return 2\n")

    def build(self, body):
        self.write("m.py", self.HEAD + "\n" + body)
        return self.graph()

    def test_optional_of_one_class_is_that_class(self):
        g = self.build("def use(c: Optional[Client]):\n    return c.go()\n")
        self.assertEqual(codegraph.callers_of(g, "m.Client.go"), ["m.use"])

    def test_the_new_union_spelling_too(self):
        g = self.build("def use(c: Client | None):\n    return c.go()\n")
        self.assertEqual(codegraph.callers_of(g, "m.Client.go"), ["m.use"])

    def test_and_the_old_one(self):
        g = self.build("def use(c: Union[Client, None]):\n    return c.go()\n")
        self.assertEqual(codegraph.callers_of(g, "m.Client.go"), ["m.use"])

    def test_a_forward_reference_spelling_it_out(self):
        """A string annotation used to be read only when the whole of it was one identifier."""
        g = self.build('def use(c: "Optional[Client]"):\n    return c.go()\n')
        self.assertEqual(codegraph.callers_of(g, "m.Client.go"), ["m.use"])

    def test_a_union_of_two_real_classes_is_not_guessed(self):
        """`Client | Server` does name a class the call could reach. Picking one is the guess
        this tool exists not to make, and picking wrongly points a blast radius at code that
        cannot be affected."""
        g = self.build("def use(x: Client | Server):\n    return x.go()\n")
        self.assertEqual(codegraph.callers_of(g, "m.Client.go"), [])
        self.assertEqual(codegraph.callers_of(g, "m.Server.go"), [])

    def test_three_options_with_a_none_is_still_two_options(self):
        g = self.build("def use(x: Union[Client, Server, None]):\n    return x.go()\n")
        self.assertEqual(codegraph.callers_of(g, "m.Client.go"), [])
        self.assertEqual(codegraph.callers_of(g, "m.Server.go"), [])

    def test_a_container_of_clients_is_not_a_client(self):
        """The reasoning the old rule was built on, which still holds and must keep holding:
        a dict of Clients is a dict."""
        g = self.build("def use(d: Dict[str, Client]):\n    return d.go()\n"
                       "\n"
                       "def use2(items: List[Client]):\n    return items.go()\n")
        self.assertEqual(codegraph.callers_of(g, "m.Client.go"), [])


class AnAttributeOnTheInstanceHasAClassToo(Sandbox):
    """`self.db = Database()` in the constructor, `self.db.query()` in the method below it.

    That is how most object-oriented Python is written, and `self.a.b()` was named in this
    tool's own description of what it could not resolve: 14,034 such call sites in the standard
    library, none of them answerable. The type is written down in three places and none was
    read - the constructor call, an annotation on the assignment, and a bare annotation in the
    class body.
    """

    HEAD = ("class Database:\n"
            "    def query(self): return 1\n"
            "\n"
            "class Cache:\n"
            "    def read(self): return 2\n")

    def build(self, body):
        self.write("m.py", self.HEAD + "\n" + body)
        return self.graph()

    def test_an_attribute_built_in_init_is_typed(self):
        g = self.build("class Service:\n"
                       "    def __init__(self):\n"
                       "        self.db = Database()\n"
                       "\n"
                       "    def run(self):\n"
                       "        return self.db.query()\n")
        self.assertEqual(codegraph.callers_of(g, "m.Database.query"), ["m.Service.run"])

    def test_a_method_written_above_the_constructor_still_sees_it(self):
        """The reason the class body is scanned before any of it is visited. Reading the
        attribute types as the methods go past would answer this one and no other, and would
        do it depending on the order somebody happened to write the file in."""
        g = self.build("class Service:\n"
                       "    def run(self):\n"
                       "        return self.db.query()\n"
                       "\n"
                       "    def __init__(self):\n"
                       "        self.db = Database()\n")
        self.assertEqual(codegraph.callers_of(g, "m.Database.query"), ["m.Service.run"])

    def test_an_annotated_assignment_counts(self):
        g = self.build("class Service:\n"
                       "    def __init__(self):\n"
                       "        self.cache: Cache = build()\n"
                       "\n"
                       "    def run(self):\n"
                       "        return self.cache.read()\n"
                       "\n"
                       "def build(): return Cache()\n")
        self.assertEqual(codegraph.callers_of(g, "m.Cache.read"), ["m.Service.run"])

    def test_a_bare_class_body_annotation_counts(self):
        """`db: Database` with no value at all - the class states the type and assigns it
        somewhere the tool cannot see, which is the whole point of writing it."""
        g = self.build("class Service:\n"
                       "    db: Database\n"
                       "\n"
                       "    def __init__(self, db):\n"
                       "        self.db = db\n"
                       "\n"
                       "    def run(self):\n"
                       "        return self.db.query()\n")
        self.assertEqual(codegraph.callers_of(g, "m.Database.query"), ["m.Service.run"])

    def test_an_attribute_holding_two_classes_is_typed_as_neither(self):
        """Which one a call reaches depends on which branch ran. Answering with the first one
        seen would be a coin flip wearing a confidence label."""
        g = self.build("class Service:\n"
                       "    def __init__(self, flag):\n"
                       "        if flag:\n"
                       "            self.thing = Database()\n"
                       "        else:\n"
                       "            self.thing = Cache()\n"
                       "\n"
                       "    def run(self):\n"
                       "        return self.thing.query()\n")
        self.assertEqual(codegraph.callers_of(g, "m.Database.query"), [])

    def test_a_nested_class_keeps_its_own_self(self):
        """`self` inside a class defined within a class is the inner one's."""
        g = self.build("class Outer:\n"
                       "    class Inner:\n"
                       "        def __init__(self):\n"
                       "            self.db = Database()\n"
                       "\n"
                       "    def run(self):\n"
                       "        return self.db.query()\n")
        self.assertEqual(codegraph.callers_of(g, "m.Database.query"), [])

    def test_an_attribute_assigned_inside_a_branch_is_still_found(self):
        """Assignments are statements and hide inside `if`, `try` and `with`. The scan walks
        statement bodies rather than every node, which is a sixth of the build on a large
        tree - and it has to keep reaching them."""
        g = self.build("class Service:\n"
                       "    def __init__(self):\n"
                       "        try:\n"
                       "            self.db = Database()\n"
                       "        except OSError:\n"
                       "            raise\n"
                       "\n"
                       "    def run(self):\n"
                       "        return self.db.query()\n")
        self.assertEqual(codegraph.callers_of(g, "m.Database.query"), ["m.Service.run"])


class OneAnswerForWhatAReceiverIs(Sandbox):
    """`self.conn.__len__()` resolved and `len(self.conn)` did not. Same object, same line of
    reasoning, two answers.

    Instance-attribute types were taught to written method calls and to nothing else, so the
    calls the language makes from syntax - `with`, `for`, a subscript, a builtin, a property
    read - all went back to being blind on exactly the receivers that had just been solved.
    Every one of them now asks the same question in the same place.
    """

    HEAD = ("class Conn:\n"
            "    def __enter__(self): return self\n"
            "    def __exit__(self, *a): return False\n"
            "    def __iter__(self): return iter([])\n"
            "    def __len__(self): return 0\n"
            "    def __getitem__(self, k): return k\n"
            "\n"
            "class Cfg:\n"
            "    @property\n"
            "    def endpoint(self): return 'x'\n"
            "\n"
            "class Service:\n"
            "    def __init__(self):\n"
            "        self.conn = Conn()\n"
            "        self.cfg = Cfg()\n")

    def build(self, body):
        self.write("m.py", self.HEAD + body)
        return self.graph()

    def test_a_with_block_on_an_attribute(self):
        g = self.build("\n    def go(self):\n        with self.conn:\n            pass\n")
        self.assertEqual(codegraph.callers_of(g, "m.Conn.__enter__"), ["m.Service.go"])
        self.assertEqual(codegraph.callers_of(g, "m.Conn.__exit__"), ["m.Service.go"])

    def test_iterating_an_attribute(self):
        g = self.build("\n    def go(self):\n        for _ in self.conn:\n            pass\n")
        self.assertEqual(codegraph.callers_of(g, "m.Conn.__iter__"), ["m.Service.go"])

    def test_a_builtin_on_an_attribute(self):
        g = self.build("\n    def go(self):\n        return len(self.conn)\n")
        self.assertEqual(codegraph.callers_of(g, "m.Conn.__len__"), ["m.Service.go"])

    def test_subscripting_an_attribute(self):
        g = self.build("\n    def go(self):\n        return self.conn[0]\n")
        self.assertEqual(codegraph.callers_of(g, "m.Conn.__getitem__"), ["m.Service.go"])

    def test_a_property_on_an_attribute(self):
        g = self.build("\n    def go(self):\n        return self.cfg.endpoint\n")
        self.assertEqual(codegraph.callers_of(g, "m.Cfg.endpoint"), ["m.Service.go"])

    def test_an_attribute_with_no_known_class_still_says_nothing(self):
        """The negative half: an attribute the class never assigns has no type, and `with`
        on it must not reach for whichever class happens to define __enter__."""
        g = self.build("\n    def go(self):\n        with self.mystery:\n            pass\n")
        self.assertEqual(codegraph.callers_of(g, "m.Conn.__enter__"), [])


class TheStatsBlockInTheReadmeIsRealOutput(unittest.TestCase):
    """The README published a `stats` block as this tool's output, and eight of its eleven
    numbers were wrong.

    Nothing had ever run it. The block was true when it was written and drifted every time
    resolution improved, which is the same failure as the test count that sat at 159 while the
    suite grew to 249 - a number in prose is a claim, and a claim nobody checks is a claim that
    goes stale silently. Worse here, because the block is the evidence for the paragraph
    underneath it.

    The example was moved onto this repository so it can be regenerated rather than believed.
    It moves when the tool's resolution moves, and that is exactly when it should be re-read.
    """

    def test_every_number_in_the_block_is_what_the_tool_says(self):
        import re
        with open(os.path.join(HERE, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        block = re.search(r"```json\n(\{.*?\n\})\n```", readme, re.S)
        self.assertIsNotNone(block, "the README no longer publishes a stats block")
        claimed = json.loads(block.group(1))

        out = os.path.join(tempfile.mkdtemp(), "g.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(out), ignore_errors=True)
        env = {**os.environ, "CODEGRAPH_OUT": out,
               "CODEGRAPH_CACHE": os.path.join(os.path.dirname(out), "c.json")}
        r = subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "build", HERE],
                           capture_output=True, text=True, timeout=300, env=env,
                           cwd=os.path.dirname(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        actual = json.loads(r.stdout)

        # `call_sites` is the one figure that depends on the interpreter: a placeholder inside
        # a multi-line f-string reported the line the STRING starts on before Python 3.12 and
        # its own line from 3.12, so the same code is one call site on old Python and two on
        # new. Nothing else moves. Enforcing it on every version made this suite permanently
        # red on three of nine CI jobs for a fact about CPython's parser rather than a fact
        # about this tool - and the note beside the block in the README says so out loud.
        here = f"{sys.version_info.major}.{sys.version_info.minor}"
        for key, want in claimed.items():
            self.assertIn(key, actual, f"the README publishes {key!r}, which stats no longer reports")
            if key == "call_sites" and here != scrub_stats.GENERATED_ON:
                self.assertAlmostEqual(
                    actual[key], want, delta=max(2, want // 500),
                    msg=f"call_sites moved further than f-string line attribution explains: "
                        f"README {want}, this interpreter {actual[key]}")
                continue
            self.assertEqual(
                actual[key], want,
                f"README says {key} = {want}, the tool says {actual[key]}.\n"
                f"    The block measures this whole repository, so any .py change moves it.\n"
                f"    Refresh it with:  python3 tools/readme_stats.py")

    def test_the_block_adds_up(self):
        """Independent of the tool: a hand-edited block can be stale AND self-consistent, so
        this is not sufficient - but a block that contradicts itself is beyond stale."""
        import re
        with open(os.path.join(HERE, "README.md"), encoding="utf-8") as f:
            block = re.search(r"```json\n(\{.*?\n\})\n```", f.read(), re.S)
        d = json.loads(block.group(1))
        self.assertEqual(sum(d["edge_confidence"].values()), d["call_edges"],
                         "the confidence counts do not sum to the edge count")
        self.assertAlmostEqual(d["resolved_to_one_def"] / d["could_have_been_resolved"],
                               d["resolution_rate"], places=3)


class TheMutationCountInTheDocsIsReal(Sandbox):
    """Both documents publish how many mutations the tool admits, and they disagreed with each
    other and with the file: the CHANGELOG said 208, the README said 710, and there were 839.

    Nothing checked either. The selftest count beside them is checked and was right, which is
    the whole argument - the number with a test on it stayed true and the two without it drifted
    in different directions.

    Only the COUNT is verified here. Whether every one of them is caught takes hours, so that
    claim is made by running `tools/mutation.py` and is not something a unit test can stand
    behind - but a count that is wrong makes the sentence around it wrong too, and the count is
    free.
    """

    def harness(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "mutharness", os.path.join(HERE, "tools", "mutation.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)      # main() sits behind if __name__ == "__main__"
        return mod

    def actual(self):
        with open(os.path.join(HERE, "codegraph.py"), encoding="utf-8") as f:
            return len(list(self.harness().candidates(ast.parse(f.read()))))

    def test_both_documents_state_the_real_number(self):
        import re
        n = self.actual()
        self.assertGreater(n, 100, "the harness found almost no mutations to make")
        for name in ("README.md", "CHANGELOG.md"):
            with open(os.path.join(HERE, name), encoding="utf-8") as f:
                text = f.read()
            claims = [int(x) for x in re.findall(r"(\d[\d,]*) mutations", text.replace(",", ""))]
            self.assertTrue(claims, f"{name} no longer states a mutation count")
            for c in claims:
                self.assertEqual(c, n, f"{name} says {c} mutations; the file admits {n}")

    def test_the_two_documents_agree_with_each_other(self):
        """They did not, and each was wrong in its own direction - which is what happens to a
        number written twice and checked nowhere."""
        import re
        counts = {}
        for name in ("README.md", "CHANGELOG.md"):
            with open(os.path.join(HERE, name), encoding="utf-8") as f:
                counts[name] = {int(x) for x in
                                re.findall(r"(\d[\d,]*) mutations", f.read().replace(",", ""))}
        self.assertEqual(counts["README.md"], counts["CHANGELOG.md"])


class ThePostCommitHookMovesTheZoneAndNothingElse(unittest.TestCase):
    """It runs `git commit --amend` on every commit, and nothing had ever tested it.

    The claim in its own header is the part that matters: only the offset changes, the epoch is
    preserved, so nothing is backdated and history stays in order. A hook that amends commits
    and gets that wrong rewrites time on every commit you make, quietly, and the damage is
    already in the history by the time anyone looks.
    """

    HOOK = os.path.join(HERE, ".githooks", "post-commit")
    BASH = shutil.which("bash")
    GIT = shutil.which("git")

    def setUp(self):
        if not (self.BASH and self.GIT):
            self.skipTest("needs bash and git")
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        def git(*a, **kw):
            return subprocess.run([self.GIT, *a], cwd=self.dir, capture_output=True,
                                  text=True, timeout=60, **kw)
        self.git = git
        git("init", "-q")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "t")
        with open(os.path.join(self.dir, "a.txt"), "w") as f:
            f.write("one\n")
        git("add", "-A")
        # a deliberately wrong zone to move away from
        env = {**os.environ, "GIT_AUTHOR_DATE": "2021-06-07T12:00:00-0500",
               "GIT_COMMITTER_DATE": "2021-06-07T12:00:00-0500"}
        git("commit", "-q", "-m", "first", env=env)

    def run_hook(self):
        return subprocess.run([self.BASH, self.HOOK], cwd=self.dir, capture_output=True,
                              text=True, timeout=120, env={**os.environ, "HOME": self.dir})

    def test_the_offset_moves_and_the_instant_does_not(self):
        before_epoch = self.git("log", "-1", "--format=%at").stdout.strip()
        before_zone = self.git("log", "-1", "--format=%ai").stdout.strip().split()[-1]
        self.run_hook()
        after_epoch = self.git("log", "-1", "--format=%at").stdout.strip()
        after_zone = self.git("log", "-1", "--format=%ai").stdout.strip().split()[-1]
        self.assertEqual(before_epoch, after_epoch,
                         "the hook moved the instant, not just the zone - this backdates history")
        self.assertEqual(after_zone, "+0900", f"zone is {after_zone}, not the one it promises")
        self.assertNotEqual(before_zone, after_zone, "the fixture did not start in another zone")

    def test_the_commit_message_and_tree_survive_the_amend(self):
        tree = self.git("log", "-1", "--format=%T").stdout.strip()
        self.run_hook()
        self.assertEqual(self.git("log", "-1", "--format=%s").stdout.strip(), "first")
        self.assertEqual(self.git("log", "-1", "--format=%T").stdout.strip(), tree,
                         "an amend that changes the tree is not a timestamp fix")

    def test_it_terminates_when_git_is_the_one_running_it(self):
        """The hook amends, and an amend fires the hook. Without something to stop the second
        pass it calls itself for ever.

        The first version of this test ran the hook by hand, twice, and asserted the result did
        not move. It passed with the re-entry guard deleted, because invoking a hook directly is
        not the situation the guard exists for - git has to be the one running it. A check that
        cannot fail is not a check, so this one installs the hooks directory and lets git fire
        it, which is the only way the recursion is reachable at all.

        It guards the PAIR. There are two stops in that hook - an env marker set across the
        amend, and a check that the zone is already right - and removing either one on its own
        still terminates, because the other catches it. Remove both and git hangs, which is what
        this test then reports. That is worth knowing: the redundancy is real, and neither half
        can be tested by itself through observed behaviour.
        """
        self.git("config", "core.hooksPath", os.path.join(HERE, ".githooks"))
        with open(os.path.join(self.dir, "b.txt"), "w") as f:
            f.write("two\n")
        self.git("add", "-A")
        # 20s, not the default 60: when this fails it fails by hanging, and a minute of
        # nothing is a minute somebody waits to be told the hook eats itself.
        r = subprocess.run([self.GIT, "commit", "-q", "-m", "second"], cwd=self.dir,
                           capture_output=True, text=True, timeout=20,
                     env={**os.environ, "HOME": self.dir,
                          "GIT_AUTHOR_DATE": "2021-06-08T12:00:00-0500",
                          "GIT_COMMITTER_DATE": "2021-06-08T12:00:00-0500"})
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        zone = self.git("log", "-1", "--format=%ai").stdout.strip().split()[-1]
        self.assertEqual(zone, "+0900", "git-driven commit did not get the zone")
        self.assertEqual(self.git("log", "--format=%s").stdout.split(), ["second", "first"],
                         "the amend loop left extra commits behind")

    def test_the_escape_hatch_works(self):
        before = self.git("log", "-1", "--format=%ai").stdout.strip()
        subprocess.run([self.BASH, self.HOOK], cwd=self.dir, capture_output=True, text=True,
                       timeout=120, env={**os.environ, "HOME": self.dir, "CODEGRAPH_NO_TZ": "1"})
        self.assertEqual(self.git("log", "-1", "--format=%ai").stdout.strip(), before)


class TheStatsRefresherOnlyTouchesTheBlock(unittest.TestCase):
    """`tools/readme_stats.py` rewrites README.md in place, and nothing tested it.

    It exists because the stats block is compared against a fresh build, so any change to any
    .py file moves it and refreshing has to be one command rather than a paragraph. That makes
    it a script people will run without reading - which is exactly the kind that has to be sure
    it changes only what it says it changes.
    """

    SCRIPT = os.path.join(HERE, "tools", "readme_stats.py")
    README = os.path.join(HERE, "README.md")

    def read(self, path=None):
        with open(path or self.README, encoding="utf-8") as f:
            return f.read()

    def write(self, text, path=None):
        with open(path or self.README, "w", encoding="utf-8") as f:
            f.write(text)

    def setUp(self):
        """A COPY, every time. These tests used to run the script against the repository's own
        README, which had two consequences worth naming. One of them repaired the block before
        asserting it was current, so that assertion could never fail - a check with no power to
        say no. And the suite quietly edited a tracked file as a side effect of running, which
        is how a genuinely stale README would have been papered over instead of reported."""
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.copy = os.path.join(self.dir, "README.md")
        shutil.copyfile(self.README, self.copy)

    def keep_readme(self):
        original = self.read(self.copy)
        return original

    def run_it(self, *args):
        return subprocess.run([sys.executable, self.SCRIPT, *args, self.copy], cwd=HERE,
                              capture_output=True, text=True, timeout=300)

    def test_check_reports_current_and_changes_nothing(self):
        self.run_it()                    # on the COPY - the repository is not touched
        before = self.read(self.copy)
        r = self.run_it("--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("current", r.stdout)
        self.assertEqual(self.read(self.copy), before,
                         "--check is supposed to report, not edit")

    def test_check_notices_a_stale_block_and_still_changes_nothing(self):
        """The half that matters: it has to be able to say no."""
        path = self.copy
        original = self.keep_readme()
        broken = re.sub(r'"call_edges": \d+', '"call_edges": 999999', original, count=1)
        self.assertNotEqual(broken, original, "the fixture did not actually change the block")
        self.write(broken, path)
        r = self.run_it("--check")
        self.assertEqual(r.returncode, 1, "a stale block must be a non-zero exit")
        self.assertIn("STALE", r.stdout)
        self.assertEqual(self.read(path), broken,
                         "--check edited the file it was only asked to inspect")

    def test_the_committed_readme_is_actually_current(self):
        """The gate the old arrangement could not have: nothing repairs anything first, so this
        one is free to go red and say the block needs refreshing."""
        r = subprocess.run([sys.executable, self.SCRIPT, "--check"], cwd=HERE,
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0,
                         "the README block is stale - run: python3 tools/readme_stats.py")

    def test_it_repairs_a_stale_block_and_leaves_the_rest_alone(self):
        path = self.copy
        original = self.keep_readme()
        self.write(re.sub(r'"call_edges": \d+', '"call_edges": 999999', original, count=1), path)
        self.assertEqual(self.run_it().returncode, 0)
        fixed = self.read(path)
        self.assertNotIn("999999", fixed, "it did not repair the number it was run for")
        # Everything outside its remit has to be byte-identical. Its remit is the block AND the
        # two sentences that quote the rate back - which is deliberate, because a block that
        # moves while the prose beside it still says the old number is the exact failure this
        # script exists to prevent. That surprised the first version of this test.
        def strip(s):
            s = re.sub(r"```json\n\{.*?\n\}\n```", "<BLOCK>", s, count=1, flags=re.S)
            s = re.sub(r"That 0\.\d+ says", "That <RATE> says", s, count=1)
            return re.sub(r"it placed \d+%", "it placed <PCT>%", s, count=1)
        a, b = strip(original), strip(fixed)
        if a != b:
            import difflib
            diff = [l for l in difflib.unified_diff(a.splitlines(), b.splitlines(), n=0)
                    if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
            self.fail(f"it changed prose outside the block: {diff[:6]}")

    def test_it_says_so_rather_than_guessing_when_the_block_is_gone(self):
        path = self.copy
        original = self.keep_readme()
        self.write(re.sub(r"```json\n\{.*?\n\}\n```", "", original, count=1, flags=re.S), path)
        r = self.run_it("--check")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no longer contains", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TheFrontDoor(unittest.TestCase):
    """The first command anyone types is the tool's name, and the second is `--help`. Both used
    to be wrong: `--help` exited 2, so `codegraph --help && echo ok` printed nothing, and a bare
    `codegraph` silently BUILT - walking whatever directory you were standing in and writing two
    files into it. Typed in a home directory that is a traversal of everything you own."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        with open(os.path.join(self.dir, "app.py"), "w", encoding="utf-8") as f:
            f.write("def leaf():\n    return 1\n")

    def run_it(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), *args],
                              cwd=self.dir, capture_output=True, text=True, timeout=120)

    def leftovers(self):
        return sorted(f for f in os.listdir(self.dir) if f.startswith("codegraph"))

    def test_asking_for_help_is_not_a_failure(self):
        for flag in ("-h", "--help", "help"):
            r = self.run_it(flag)
            self.assertEqual(r.returncode, 0, f"{flag} exited {r.returncode}: {r.stderr}")
            self.assertIn("codegraph impact", r.stdout, flag)
            self.assertEqual(r.stderr, "", flag)

    def test_a_bare_invocation_explains_itself_instead_of_writing_files(self):
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("codegraph build", r.stdout)
        self.assertEqual(self.leftovers(), [], "a bare invocation wrote into the directory")

    def test_the_library_door_agrees_with_the_command_line(self):
        """`_main([])` is the same entry point `pip install` exposes as a console script."""
        cwd = os.getcwd()
        saved = {k: getattr(codegraph, k) for k in ("HOME", "OUT", "CACHE")}
        # HOME is captured at import, so chdir alone would have let the old behaviour build the
        # REPOSITORY and write into it - a leftover check pointed at the sandbox saw nothing.
        codegraph.HOME = self.dir
        codegraph.OUT = os.path.join(self.dir, "codegraph.json")
        codegraph.CACHE = os.path.join(self.dir, "codegraph.cache.json")
        os.chdir(self.dir)
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = codegraph._main([])
        finally:
            os.chdir(cwd)
            for k, v in saved.items():
                setattr(codegraph, k, v)
        self.assertEqual(rc, 0)
        self.assertIn("codegraph build", out.getvalue())
        self.assertEqual(self.leftovers(), [])

    def test_a_wrong_command_is_still_an_error(self):
        """The fix for `--help` must not turn every typo into a success."""
        self.assertEqual(self.run_it("wat").returncode, 2)
        self.assertEqual(self.run_it("callers").returncode, 2)   # verb, no name

    def test_help_names_every_verb_the_tool_accepts(self):
        """A verb added without a line in the help is a feature nobody can find."""
        with open(os.path.join(HERE, "codegraph.py"), encoding="utf-8") as f:
            src = f.read()
        body = src[src.index("def _main("):]
        verbs = set(re.findall(r'a\[0\] == "([a-z]+)"', body))
        verbs |= set(re.findall(r'"([a-z]+)": [12],', body))     # the arity table
        self.assertGreaterEqual(len(verbs), 12, verbs)
        for verb in sorted(verbs):
            self.assertIn(f"codegraph {verb}", codegraph.__doc__, f"{verb} is undocumented")


class NothingShippedNamesItsAuthor(unittest.TestCase):
    """Everything in this repository is published: the source, the help text a user prints, the
    comments, the README, the CI file. A private origin story in a code comment - the machine it
    grew up on, the private modules beside it, the assistant that helped write it - ships with
    it. Git metadata is the part people remember to scrub; the help text is the part they read."""

    # Assembled from fragments so this guard does not trip over its own evidence.
    FORBIDDEN = ("cla" + "ude", "anthro" + "pic", "co-auth" + "ored-by", "gpt-", "openai",
                 "super" + "agent", "wiring." + "py", "graph" + "ify", "the brain",
                 "/users/", "c:\\users\\", "@gmail", "@icloud", "127.0.0.1:8420")
    SKIP_DIRS = frozenset({".git", "__pycache__", ".ruff_cache", "build", "dist", ".venv"})
    TEXT = frozenset({".py", ".md", ".toml", ".yml", ".yaml", ".cfg", ".txt", ".json", ".sh", ""})

    def shipped_files(self):
        """What git tracks - which is what "shipped" means, and is not the same as what happens
        to be lying in the directory. Running codegraph inside its own repository leaves a
        codegraph.json full of absolute paths, and this scan used to read it and fail: the tool
        being used normally broke its own test suite."""
        # normpath on BOTH sides: git prints forward slashes, and joining one onto a Windows
        # root gives `D:\a\repo\tests/test_codegraph.py`, which is not the string
        # os.path.abspath(__file__) produces. The self-exclusion below then missed, this file
        # scanned itself, and found its own list of forbidden markers. Green on two machines,
        # red on the third.
        me = samefile_key(__file__)
        tracked = subprocess.run([shutil.which("git") or "git", "ls-files", "-z"],
                                 cwd=HERE, capture_output=True, text=True)
        if tracked.returncode == 0 and tracked.stdout:
            paths = [os.path.normpath(os.path.join(HERE, p))
                     for p in tracked.stdout.split("\0") if p]
        else:                                    # not a checkout: fall back to walking
            paths = []
            for root, dirs, names in os.walk(HERE):
                dirs[:] = [d for d in dirs
                           if d not in self.SKIP_DIRS and not d.endswith(".egg-info")]
                paths += [os.path.join(root, n) for n in names]
        for full in paths:
            if samefile_key(full) == me or os.path.splitext(full)[1].lower() not in self.TEXT:
                continue
            if os.path.isfile(full):
                yield full

    def test_no_shipped_file_names_a_private_origin(self):
        checked = 0
        for full in self.shipped_files():
            try:
                with open(full, encoding="utf-8") as f:
                    low = f.read().lower()
            except (OSError, UnicodeDecodeError):
                continue
            checked += 1
            for bad in self.FORBIDDEN:
                self.assertNotIn(bad.lower(), low,
                                 f"{os.path.relpath(full, HERE)} contains {bad!r}")
        self.assertGreater(checked, 4, "the scan found almost nothing to read")

    def test_nothing_shipped_carries_a_date(self):
        """A year in a licence, a changelog heading or a comment dates the work and says when
        somebody was sitting at a keyboard. The one identity this repository carries is a
        handle."""
        year = re.compile(r"\b(?:19|20)[0-9]{2}\b")
        for full in self.shipped_files():
            try:
                with open(full, encoding="utf-8") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            fenced = False
            for n, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("```"):
                    fenced = not fenced
                    continue
                # A fenced block is OUTPUT, not prose. The same distinction the source scan
                # makes between a comment and a string literal: what dates the work is somebody
                # writing a date down, not a number that happens to be shaped like one. This
                # used to be two hardcoded values - "1,849" and "236,000" - which skipped the
                # whole line they appeared on and needed a new entry every time the tool
                # reported a count between 1900 and 2099. `"EXTERNAL": 1967` was the next one.
                if fenced:
                    continue
                if "MCP_PROTOCOL" in line:
                    # The one exception, and deliberately the narrowest one that works: the MCP
                    # spec identifies its versions with strings shaped like dates. That is a
                    # fact about the protocol, not about when this was written, and it is
                    # allowed only on the line that defines the constant - so smuggling a real
                    # date past this rule means naming it MCP_PROTOCOL, in public, on purpose.
                    continue
                self.assertIsNone(year.search(line),
                                  f"{os.path.relpath(full, HERE)}:{n} carries a year: {line.strip()[:70]}")

    def test_the_licence_names_the_handle_and_nobody_else(self):
        """A sibling repository shipped a licence copyrighting `freeboard contributors` - a
        project name from before it was renamed. It survived every scrub, because a scan that
        hunts forbidden words and a year does not notice a plausible-looking name that is
        simply the wrong one. The only way to check a licence is to say what it must say."""
        with open(os.path.join(HERE, "LICENSE"), encoding="utf-8") as f:
            claims = [ln.strip() for ln in f if ln.strip().lower().startswith("copyright")]
        self.assertEqual(claims, ["Copyright (c) jedisolana"])

    def test_the_metadata_names_an_author(self):
        """An empty author field is not neutral: the package page then describes the tool and
        credits nobody, which is how a first upload quietly loses its attribution."""
        with open(os.path.join(HERE, "pyproject.toml"), encoding="utf-8") as f:
            toml = f.read()
        self.assertIn('authors = [{ name = "jedisolana" }]', toml)
        self.assertIn("https://x.com/jedisolana", toml)

    def test_the_help_text_a_user_prints_is_clean(self):
        """The one string the tool puts on a stranger's screen, checked on its own."""
        low = codegraph.__doc__.lower()
        for bad in self.FORBIDDEN:
            self.assertNotIn(bad.lower(), low, f"the help text contains {bad!r}")
        self.assertIn("codegraph - ask a Python codebase", codegraph.__doc__)


class ChangingAConstructorBreaksEveryoneWhoBuildsIt(Sandbox):
    """`impact __init__` answered "callers: (none), blast: 0 functions" for a class constructed
    all over the tree, and exited 0. Constructing an object runs its __init__; the edge simply
    was not there. Of every method in Python this is the one most often edited and the one whose
    callers are hardest to grep for, because none of them mention it by name."""

    TREE = ("class Client:\n"
            "    def __init__(self, url):\n"
            "        self.url = url\n"
            "    def get(self):\n"
            "        return self.url\n"
            "\n"
            "def make():\n"
            "    return Client('a')\n"
            "\n"
            "def make_two():\n"
            "    return Client('b')\n")

    def test_impact_names_the_places_that_construct_the_class(self):
        self.write("svc.py", self.TREE)
        g = self.graph(write=False)
        im = codegraph.impact(g, "svc.Client.__init__")
        self.assertEqual(sorted(im["callers"]), ["svc.make", "svc.make_two"])
        self.assertEqual({loc for loc, _ in im["sites"]}, {"svc.py:8", "svc.py:11"})
        self.assertEqual(len(im["blast"]), 2)

    def test_the_edge_to_the_class_itself_survives(self):
        """The constructor edge is added BESIDE the class edge, not instead of it - `Client()`
        both constructs a Client and runs its __init__, and both questions have askers."""
        self.write("svc.py", self.TREE)
        g = self.graph(write=False)
        self.assertEqual(sorted(codegraph.callers_of(g, "svc.Client")),
                         ["svc.make", "svc.make_two"])

    def test_a_subclass_points_at_the_init_it_inherits(self):
        """Mid() runs Base.__init__ - resolved through the same C3 order the interpreter uses."""
        self.write("h.py", "class Base:\n"
                           "    def __init__(self, a):\n"
                           "        self.a = a\n"
                           "\n"
                           "class Mid(Base):\n"
                           "    pass\n"
                           "\n"
                           "def build():\n"
                           "    return Mid(1)\n")
        g = self.graph(write=False)
        self.assertEqual(codegraph.callers_of(g, "h.Base.__init__"), ["h.build"])

    def test_a_class_with_no_init_gains_no_edge(self):
        """A guard: the fix must invent nothing where there is no __init__ to run."""
        self.write("p.py", "class Plain:\n    pass\n\ndef make():\n    return Plain()\n")
        g = self.graph(write=False)
        self.assertEqual([e for e in g["calls"] if e["confidence"] == "CONSTRUCTOR"], [])
        self.assertEqual(codegraph.callers_of(g, "p.Plain"), ["p.make"])


class AQualifiedNameMeansTheOneYouNamed(Sandbox):
    """`Base.__init__` is how a person says which __init__ they mean. It matched no id, fell
    through to "called here but not defined here", and the answer was then assembled from every
    edge whose callee was `__init__` - so the question about Base returned a caller of Own, in a
    graph that defines Base.__init__, with exit code 0."""

    def setUp(self):
        super().setUp()
        self.write("h.py", "class Base:\n"
                           "    def __init__(self, a):\n"
                           "        self.a = a\n"
                           "\n"
                           "class Own(Base):\n"
                           "    def __init__(self, a, b):\n"
                           "        super().__init__(a)\n"
                           "        self.b = b\n"
                           "\n"
                           "def build_base():\n"
                           "    return Base(1)\n"
                           "\n"
                           "def build_own():\n"
                           "    return Own(1, 2)\n")
        self.g = self.graph(write=False)

    def test_class_dot_method_is_the_definition_it_names(self):
        # Own.__init__ is a real caller of Base.__init__ - it writes super().__init__(a).
        self.assertEqual(codegraph.callers_of(self.g, "Base.__init__"),
                         ["h.Own.__init__", "h.build_base"])
        self.assertEqual(codegraph.callers_of(self.g, "Own.__init__"), ["h.build_own"])

    def test_where_agrees_with_the_other_verbs(self):
        """where used to strip the qualification and list every __init__ in the tree."""
        self.assertEqual([i for i, _ in codegraph.where(self.g, "Base.__init__")],
                         ["h.Base.__init__"])
        self.assertEqual([i for i, _ in codegraph.where(self.g, "__init__")],
                         ["h.Base.__init__", "h.Own.__init__"])

    def test_the_bare_name_is_still_ambiguous(self):
        """Two definitions answer to `__init__`, and neither may be picked for you."""
        with self.assertRaises(codegraph.Ambiguous):
            codegraph.callers_of(self.g, "__init__")

    def test_a_qualified_name_that_matches_nothing_is_still_unknown(self):
        """The protection the old exact-id rule was there for: a typo must not answer 0."""
        with self.assertRaises(codegraph.Unknown):
            codegraph.callers_of(self.g, "typo.name")

    def test_an_exact_id_beats_a_longer_one_that_ends_with_it(self):
        self.write(os.path.join("pkg", "h.py"), "def only():\n    return 1\n")
        g = self.graph(write=False)
        self.assertIn("pkg/h.only", {n["id"] for n in g["nodes"]})
        self.assertEqual(codegraph._ids_matching(g, "pkg/h.only"), ["pkg/h.only"])
        self.assertEqual(codegraph._ids_matching(g, "h.only"), ["pkg/h.only"])

    def test_a_directory_boundary_counts_as_a_boundary(self):
        """Ids nest directories with "/" and symbols with "." - `mod.f` has to reach
        `pkg/deep/mod.f`, and `init` must never reach `__init__`."""
        self.write(os.path.join("pkg", "deep", "mod.py"), "def f():\n    return 1\n")
        g = self.graph(write=False)
        self.assertEqual(codegraph._ids_matching(g, "mod.f"), ["pkg/deep/mod.f"])
        self.assertEqual(codegraph._ids_matching(g, "deep/mod.f"), ["pkg/deep/mod.f"])
        self.assertEqual(codegraph._ids_matching(g, "init"), [])


class ABareNameObeysPythonsScopeRules(Sandbox):
    """Bare calls were resolved through a (module, name) dict. Two functions in one module can
    share a name - two nested helpers both called `inner`, or `wrapper` inside any two
    decorators - and a dict holds one of them: whichever parsed last, silently. The other's
    callers were attributed to it, labelled LOCAL, at full confidence."""

    def edge(self, g, src, callee):
        m = [e for e in g["calls"] if e["src"] == src and e["callee"] == callee]
        self.assertEqual(len(m), 1, m)
        return m[0].get("dst"), m[0]["confidence"]

    def test_two_nested_helpers_with_one_name_stay_apart(self):
        self.write("n.py", "def outer():\n"
                           "    def inner():\n"
                           "        return 1\n"
                           "    return inner()\n"
                           "\n"
                           "def other():\n"
                           "    def inner():\n"
                           "        return 2\n"
                           "    return inner()\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "n.outer", "inner"), ("n.outer.inner", "LOCAL"))
        self.assertEqual(self.edge(g, "n.other", "inner"), ("n.other.inner", "LOCAL"))

    def test_a_blast_radius_does_not_stop_at_the_collision(self):
        """The consequence, and the reason it matters: the edge went to the other function, so
        everything above the real caller dropped out of the answer."""
        self.write("n.py", "def helper():\n"
                           "    return 0\n"
                           "\n"
                           "def outer():\n"
                           "    def inner():\n"
                           "        return helper()\n"
                           "    return inner()\n"
                           "\n"
                           "def other():\n"
                           "    def inner():\n"
                           "        return 2\n"
                           "    return inner()\n"
                           "\n"
                           "def main():\n"
                           "    return outer()\n")
        g = self.graph(write=False)
        self.assertEqual(sorted(codegraph.blast_radius(g, "n.helper")),
                         ["n.main", "n.outer", "n.outer.inner"])

    def test_a_bare_call_never_reaches_a_method(self):
        """`helper()` written at module level cannot be Box.helper - a bare name does not see
        a class's contents. The dict happily returned the method when it parsed last."""
        self.write("m.py", "def helper():\n"
                           "    return 2\n"
                           "\n"
                           "def top():\n"
                           "    return helper()\n"
                           "\n"
                           "class Box:\n"
                           "    def helper(self):\n"
                           "        return 1\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "m.top", "helper"), ("m.helper", "LOCAL"))
        self.assertEqual(codegraph.callers_of(g, "m.Box.helper"), [])

    def test_a_method_still_sees_its_own_class(self):
        """A guard: self.helper() must keep resolving, and must not be broken by the scope
        walk skipping class bodies."""
        self.write("b.py", "class Box:\n"
                           "    def helper(self):\n"
                           "        return 1\n"
                           "    def use(self):\n"
                           "        return self.helper()\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "b.Box.use", "helper"), ("b.Box.helper", "SELF-METHOD"))

    def test_an_inner_function_can_still_call_out_to_the_module(self):
        """Enclosing scopes are searched in order, and the module is the last of them."""
        self.write("o.py", "def target():\n"
                           "    return 1\n"
                           "\n"
                           "def outer():\n"
                           "    def inner():\n"
                           "        return target()\n"
                           "    return inner()\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "o.outer.inner", "target"), ("o.target", "LOCAL"))

    def test_a_nested_def_shadows_an_imported_name(self):
        """Python resolves the nearest binding, and a local def is nearer than an import."""
        self.write("lib.py", "def run():\n    return 'library'\n")
        self.write("app.py", "from lib import run\n"
                             "\n"
                             "def go():\n"
                             "    def run():\n"
                             "        return 'the local one'\n"
                             "    return run()\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "app.go", "run"), ("app.go.run", "LOCAL"))
        self.assertEqual(codegraph.callers_of(g, "lib.run"), [])


class GeneratedPythonIsStillPython(Sandbox):
    """A machine-written file - a 30,000-term constant, a generated table - nests deeper than
    the interpreter's own stack. ast.parse survives it; walking the tree does not. One such file
    ended the WHOLE build on a traceback thousands of frames long, so a tree of perfectly good
    code got no graph at all because of one file nobody wrote by hand."""

    def test_one_unwalkable_file_does_not_end_the_build(self):
        self.write("good.py", "def ok():\n    return 1\n")
        self.write("gen.py", "TABLE = " + "1+" * 30000 + "1\n")
        g = self.graph(write=False)
        self.assertIn("good.ok", {n["id"] for n in g["nodes"]})
        self.assertEqual(len(g["unreadable"]), 1, g["unreadable"])
        self.assertIn("gen.py", g["unreadable"][0])
        self.assertNotIn("gen", {n["id"] for n in g["nodes"]})

    def test_the_skip_says_which_file_and_why(self):
        """Silence would be worse: an absent module looks exactly like an empty one."""
        self.write("gen.py", "TABLE = " + "1+" * 30000 + "1\n")
        g = self.graph(write=False)
        self.assertIn("nested", g["unreadable"][0].lower())


class SuperNamesNoTargetAndHasExactlyOne(Sandbox):
    """`super().run()` is the one call shape where the caller cannot mention the callee by
    name - and the target is not a guess: it is the next class in the interpreter's own order.
    It landed as UNTYPED, so `impact Base.run` reported no callers for a method that every
    subclass overrides and then calls."""

    def edge(self, g, src, callee):
        m = [e for e in g["calls"] if e["src"] == src and e["callee"] == callee]
        self.assertEqual(len(m), 1, m)
        return m[0].get("dst"), m[0]["confidence"]

    def test_super_reaches_the_base_method(self):
        self.write("s.py", "class Base:\n"
                           "    def run(self):\n"
                           "        return 1\n"
                           "\n"
                           "class Kid(Base):\n"
                           "    def run(self):\n"
                           "        return super().run() + 1\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "s.Kid.run", "run"), ("s.Base.run", "INHERITED"))
        self.assertEqual(codegraph.callers_of(g, "s.Base.run"), ["s.Kid.run"])

    def test_super_init_is_a_caller_of_the_base_constructor(self):
        self.write("s.py", "class Base:\n"
                           "    def __init__(self, a):\n"
                           "        self.a = a\n"
                           "\n"
                           "class Kid(Base):\n"
                           "    def __init__(self, a):\n"
                           "        super().__init__(a)\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "s.Kid.__init__", "__init__"),
                         ("s.Base.__init__", "INHERITED"))

    def test_a_diamond_follows_the_interpreters_order(self):
        """D's super() is B, not A - C3, checked against what Python itself reports."""
        self.write("d.py", "class A:\n    def m(self):\n        return 1\n"
                           "class B(A):\n    def m(self):\n        return super().m()\n"
                           "class C(A):\n    def m(self):\n        return super().m()\n"
                           "class D(B, C):\n    def m(self):\n        return super().m()\n")
        g = self.graph(write=False)
        ns = {}
        exec("class A:\n def m(self): pass\n"
             "class B(A):\n def m(self): pass\n"
             "class C(A):\n def m(self): pass\n"
             "class D(B, C):\n def m(self): pass\n", ns)
        self.assertEqual([c.__name__ for c in ns["D"].__mro__][:4], ["D", "B", "C", "A"])
        self.assertEqual(self.edge(g, "d.D.m", "m"), ("d.B.m", "INHERITED"))

    def test_an_explicit_super_starts_after_the_class_it_names(self):
        self.write("e.py", "class A:\n    def m(self):\n        return 1\n"
                           "class B(A):\n    def m(self):\n        return 2\n"
                           "class C(B):\n    def m(self):\n        return super(B, self).m()\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "e.C.m", "m"), ("e.A.m", "INHERITED"))

    def test_super_to_a_base_outside_the_tree_stays_unresolved(self):
        """A guard: nothing may be invented when the base is not something this graph can see."""
        self.write("x.py", "import json\n"
                           "class Enc(json.JSONEncoder):\n"
                           "    def default(self, o):\n"
                           "        return super().default(o)\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "x.Enc.default", "default"), (None, "UNTYPED"))


class AGraphKnowsExactlyWhatItRead(Sandbox):
    """Freshness was "is any file newer than the graph". A file restored from a backup, a
    checkout, cp -p, rsync -t or a container layer keeps the timestamp it had, so it lands OLDER
    than the graph while holding different code - and that test called it fresh."""

    def test_a_file_restored_with_its_old_timestamp_is_not_fresh(self):
        self.write("m.py", "def alpha():\n    return 1\n")
        codegraph.build([self.dir])                       # written, so a query can find it
        st = os.stat(os.path.join(self.dir, "m.py"))
        self.write("m.py", "def beta():\n    return 2\n")
        os.utime(os.path.join(self.dir, "m.py"), (st.st_atime, st.st_mtime))
        g = codegraph.load()
        self.assertEqual([i for i, _ in codegraph.where(g, "beta")], ["m.beta"])
        with self.assertRaises(codegraph.Unknown):
            codegraph.callers_of(g, "alpha")

    def raw(self):
        """The graph exactly as it sits on disk. `load()` rebuilds a stale graph before it
        returns, so asking _is_stale about ITS result can only ever say False - a check that
        passes for the wrong reason."""
        with open(codegraph.OUT, encoding="utf-8") as f:
            return json.load(f)

    def test_the_graph_records_a_stamp_for_every_file_it_read(self):
        path = self.write("a.py", "def f():\n    return 1\n")
        g = self.graph(write=False)
        self.assertIsInstance(g["sources"], dict)
        (recorded, stamp), = g["sources"].items()
        self.assertEqual(recorded, path)
        self.assertEqual(stamp, codegraph._stamp(path))
        size, digest = stamp
        self.assertEqual(size, os.path.getsize(path))
        self.assertRegex(digest, r"\A[0-9a-f]{32}\Z")

    def test_the_stamp_moves_when_only_the_content_does(self):
        """The guard on the stamp itself: it must be a fact about the bytes and nothing else.
        A stamp that reads the clock would hold still here."""
        path = self.write("a.py", "def alpha():\n    return 1\n")
        before = codegraph._stamp(path)
        st = os.stat(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("def gamma():\n    return 1\n")
        with contextlib.suppress(OSError, NotImplementedError):   # see the skip above; the
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))   # assertion here needs no clock
        after = codegraph._stamp(path)
        self.assertEqual(after[0], before[0], "the fixture must not change the size")
        self.assertNotEqual(after, before)

    def test_the_stamp_holds_still_when_only_the_clock_does(self):
        """And the other direction, which is what makes the incremental build worth having:
        touching a file must not cost a reparse."""
        path = self.write("a.py", "def alpha():\n    return 1\n")
        before = codegraph._stamp(path)
        os.utime(path, None)
        self.assertEqual(codegraph._stamp(path), before)

    def test_the_cache_is_keyed_on_size_as_well_as_time(self):
        """Same timestamp, different content: the incremental cache must not serve the old
        parse back."""
        self.write("m.py", "def alpha():\n    return 1\n")
        codegraph.build([self.dir])
        st = os.stat(os.path.join(self.dir, "m.py"))
        self.write("m.py", "def beta():\n    return 2\n")
        os.utime(os.path.join(self.dir, "m.py"), (st.st_atime, st.st_mtime))
        g = codegraph.build([self.dir])
        self.assertIn("m.beta", {n["id"] for n in g["nodes"]})
        self.assertNotIn("m.alpha", {n["id"] for n in g["nodes"]})

    def test_a_dangling_symlink_does_not_make_every_query_rebuild(self):
        """The build has skipped these since round three. The freshness check called one
        "something changed" - so every query rebuilt the whole graph, for ever, in silence."""
        self.write("a.py", "def f():\n    return 1\n")
        try:
            os.symlink(os.path.join(self.dir, "gone.py"), os.path.join(self.dir, "broken.py"))
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"symlinks unavailable: {e}")
        codegraph.build([self.dir])
        self.assertFalse(codegraph._is_stale(self.raw()))

    def test_a_real_edit_is_still_seen(self):
        """The guard on the other side: exact stamps must not make it blind."""
        self.write("m.py", "def alpha():\n    return 1\n")
        codegraph.build([self.dir])
        self.assertFalse(codegraph._is_stale(self.raw()))
        self.write("m.py", "def alpha():\n    return 1\ndef added():\n    return 2\n")
        self.assertTrue(codegraph._is_stale(self.raw()))


class APathToYourselfIsAPath(Sandbox):
    def test_the_path_from_a_function_to_itself_is_that_function(self):
        """It answered "(no path)", which reads as "these two are unconnected"."""
        self.write("a.py", "def f():\n    return 1\n")
        g = self.graph(write=False)
        self.assertEqual(codegraph.path(g, "a.f", "a.f"), ["a.f"])


class ADottedCallNeedsAnImport(Sandbox):
    """`thing.load()` resolved into an in-tree module named `thing` whenever the receiver
    happened to spell a module id - imported or not. A file holding `config = C()` and calling
    config.load() was recorded as calling load() in a config.py it had never heard of, labelled
    QUALIFIED, and the real method showed no callers."""

    def edge(self, g, src, callee):
        m = [e for e in g["calls"] if e["src"] == src and e["callee"] == callee]
        self.assertEqual(len(m), 1, m)
        return m[0].get("dst"), m[0]["confidence"]

    def test_a_global_object_is_not_the_module_of_the_same_name(self):
        self.write("config.py", "def load():\n    return 'the module function'\n")
        self.write("app.py", "class C:\n"
                             "    def load(self):\n"
                             "        return 'the object method'\n"
                             "\n"
                             "config = C()\n"
                             "\n"
                             "def go():\n"
                             "    return config.load()\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "app.go", "load"), (None, "UNTYPED"))
        self.assertEqual(codegraph.callers_of(g, "config.load"), [])

    def test_a_package_path_still_resolves_when_it_was_imported(self):
        """The guard: this must not cost the case the branch exists for."""
        self.write(os.path.join("pkg", "__init__.py"), "")
        self.write(os.path.join("pkg", "mod.py"), "def work():\n    return 1\n")
        self.write("user.py", "import pkg.mod\n"
                              "\n"
                              "def go():\n"
                              "    return pkg.mod.work()\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "user.go", "work"), ("pkg/mod.work", "QUALIFIED"))


class RelativeImportsCannotClimbOutOfTheTree(Sandbox):
    """`from ... import thing` is an ImportError in Python - "attempted relative import beyond
    top-level package". The extra dots were discarded silently, so the import landed on a
    TOP-LEVEL module of the same name: the exact wrong answer the relative-import work removed,
    still reachable by writing one dot too many."""

    def test_too_many_dots_resolve_to_nothing_rather_than_the_top_level(self):
        self.write("thing.py", "def load():\n    return 'TOP LEVEL - the wrong one'\n")
        self.write(os.path.join("pkg", "__init__.py"), "")
        self.write(os.path.join("pkg", "up.py"), "from ... import thing\n"
                                                 "def f():\n"
                                                 "    return thing.load()\n")
        g = self.graph(write=False)
        self.assertEqual([(i["src"], i["callee"]) for i in g["imports"]], [])
        call, = [e for e in g["calls"] if e["callee"] == "load"]
        self.assertIsNone(call.get("dst"))
        self.assertEqual(codegraph.callers_of(g, "thing.load"), [])

    def test_the_dots_that_do_fit_still_work(self):
        """The guard: `from .. import thing` one level up is ordinary package code."""
        self.write("thing.py", "def load():\n    return 'TOP LEVEL - the wrong one'\n")
        self.write(os.path.join("pkg", "__init__.py"), "")
        self.write(os.path.join("pkg", "thing.py"), "def load():\n    return 'the package one'\n")
        self.write(os.path.join("pkg", "deep", "__init__.py"), "")
        self.write(os.path.join("pkg", "deep", "down.py"), "from .. import thing\n"
                                                           "def c():\n"
                                                           "    return thing.load()\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "pkg/deep/down.c"]
        self.assertEqual((call.get("dst"), call["confidence"]), ("pkg/thing.load", "QUALIFIED"))


class TheCommandLineSaysWhatItIsDoing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for name, body in (("a.py", "def one():\n    return 1\n"),
                           ("b.py", "def two():\n    return 2\n")):
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write(body)

    def run_it(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), *args],
                              cwd=self.dir, capture_output=True, text=True, timeout=180)

    def test_a_file_argument_admits_it_is_building_the_directory(self):
        """`codegraph build a.py` analyses the whole folder - on a monorepo, a much bigger
        scope than was asked for - and it used to do that in silence."""
        r = self.run_it("build", "a.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("is a file", r.stderr)
        self.assertIn("directory", r.stderr)
        with open(os.path.join(self.dir, "codegraph.json"), encoding="utf-8") as f:
            ids = {n["id"] for n in json.load(f)["nodes"]}
        self.assertEqual({"a.one", "b.two"} & ids, {"a.one", "b.two"})

    def test_a_blank_argument_is_a_missing_argument(self):
        """`codegraph find "$NAME"` with NAME unset printed every symbol in the codebase and
        exited 0 - the empty pattern that matches everything, reported as a search that found
        something."""
        self.assertEqual(self.run_it("build", ".").returncode, 0)
        for verb in ("find", "callers", "where", "impact"):
            r = self.run_it(verb, "")
            self.assertEqual(r.returncode, 2, f"{verb}: {r.stdout}{r.stderr}")
            self.assertIn("usage:", r.stderr, verb)
        self.assertEqual(self.run_it("path", "one", "  ").returncode, 2)

    def test_where_explains_a_module_like_every_other_verb(self):
        """It answered "(not found)" about a module sitting in the graph."""
        self.assertEqual(self.run_it("build", ".").returncode, 0)
        r = self.run_it("where", "a")
        self.assertEqual(r.returncode, 1)
        self.assertIn("is a module", r.stderr)
        self.assertIn("deps a", r.stderr)


class AnImportNamesTheModuleNotJustItsPackage(Sandbox):
    """`from b.svc import helper` recorded the module as "b" - the first segment. The lookup
    for helper then missed b/svc entirely, the call fell through to a tree-wide name match, and
    a second helper somewhere else made it AMBIGUOUS: "say which", in a file that had said
    which. The relative-import branch has always recorded the whole path."""

    def setUp(self):
        super().setUp()
        self.write(os.path.join("b", "__init__.py"), "")
        self.write(os.path.join("b", "svc.py"), "def helper():\n    return 'the right one'\n")
        self.write("other.py", "def helper():\n    return 'a different module'\n")

    def test_a_dotted_import_resolves_the_name_it_imported(self):
        self.write("app.py", "from b.svc import helper\n\ndef go():\n    return helper()\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.go"]
        self.assertEqual((call.get("dst"), call["confidence"]), ("b/svc.helper", "QUALIFIED"))
        self.assertEqual(codegraph.callers_of(g, "other.helper"), [])

    def test_both_the_package_and_the_module_are_recorded_as_imports(self):
        self.write("app.py", "from b.svc import helper\n\ndef go():\n    return helper()\n")
        g = self.graph(write=False)
        edges = {(i["src"], i["callee"]) for i in g["imports"]}
        self.assertIn(("app", "b/svc"), edges)
        # The package, named the way the GRAPH names it: a package's module id is its
        # __init__, and an import edge that said "b" pointed at no node at all.
        self.assertIn(("app", "b/__init__"), edges)
        self.assertIn("b/svc", codegraph.module_deps(g, "app")[0])

    def test_an_out_of_tree_import_invents_no_module(self):
        """A guard: `from os.path import join` must not make os/path look like ours."""
        self.write("app.py", "from os.path import join\n\ndef go():\n    return join('a', 'b')\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.go"]
        self.assertIsNone(call.get("dst"))
        self.assertEqual(codegraph.module_deps(g, "app")[0], [])


class ATypeNameIsResolvedWhereItWasWritten(Sandbox):
    """`x = Foo()` then `x.method()` took whichever Foo sorted first in the whole tree, with no
    ambiguity test at all. A file writing `from b.svc import Client` had c.get() resolved into
    a/svc.Client - one line after the Client() call itself was labelled AMBIGUOUS. The tool
    contradicted itself inside a single function."""

    def setUp(self):
        super().setUp()
        for pkg, tag in (("a", "A"), ("b", "B")):
            self.write(os.path.join(pkg, "__init__.py"), "")
            self.write(os.path.join(pkg, "svc.py"),
                       f"class Client:\n    def get(self):\n        return '{tag}'\n")

    def edge(self, g, callee):
        m = [e for e in g["calls"] if e["src"] == "app.go" and e["callee"] == callee]
        self.assertEqual(len(m), 1, m)
        return m[0]

    def test_the_import_says_which_class_it_is(self):
        self.write("app.py", "from b.svc import Client\n"
                             "\n"
                             "def go():\n"
                             "    c = Client()\n"
                             "    return c.get()\n")
        g = self.graph(write=False)
        got = self.edge(g, "get")
        self.assertEqual((got.get("dst"), got["confidence"]), ("b/svc.Client.get", "TYPED"))
        self.assertEqual(codegraph.callers_of(g, "a/svc.Client.get"), [])

    def test_a_class_in_this_module_wins(self):
        self.write("app.py", "class Client:\n"
                             "    def get(self):\n"
                             "        return 'local'\n"
                             "\n"
                             "def go():\n"
                             "    c = Client()\n"
                             "    return c.get()\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "get").get("dst"), "app.Client.get")

    def test_with_nothing_to_say_which_it_picks_none(self):
        """Two classes of that name and no import: the same rule a bare call follows."""
        self.write("app.py", "def go():\n    c = Client()\n    return c.get()\n")
        g = self.graph(write=False)
        got = self.edge(g, "get")
        self.assertIsNone(got.get("dst"))
        self.assertEqual(got["confidence"], "AMBIGUOUS")
        self.assertEqual(got["candidates"], ["a/svc.Client.get", "b/svc.Client.get"])
        self.assertEqual(codegraph.callers_of(g, "a/svc.Client.get"), [])
        self.assertEqual(codegraph.callers_of(g, "b/svc.Client.get"), [])


class OneNameImportedFromTwoPlaces(Sandbox):
    """The try/except ImportError idiom binds one name from two modules. A plain dict keeps the
    LAST one - which for that idiom is the FALLBACK - so the tool named slow.parse as the
    definite target while fast.parse, the one that actually runs when the import succeeds,
    showed no callers at all."""

    TRY = ("try:\n"
           "    from fast import parse\n"
           "except ImportError:\n"
           "    from slow import parse\n"
           "\n"
           "def go():\n"
           "    return parse()\n")

    def setUp(self):
        super().setUp()
        self.write("fast.py", "def parse():\n    return 'FAST'\n")
        self.write("slow.py", "def parse():\n    return 'slow'\n")

    def test_neither_source_is_picked_and_both_are_named(self):
        self.write("app.py", self.TRY)
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.go"]
        self.assertIsNone(call.get("dst"))
        self.assertEqual(call["confidence"], "AMBIGUOUS")
        self.assertEqual(call["candidates"], ["fast.parse", "slow.parse"])
        self.assertEqual(codegraph.callers_of(g, "fast.parse"), [])
        self.assertEqual(codegraph.callers_of(g, "slow.parse"), [])

    def test_impact_says_it_could_not_place_the_call(self):
        """Silence would be the whole problem again: an empty answer must carry the reason."""
        self.write("app.py", self.TRY)
        g = self.graph(write=False)
        self.assertTrue(codegraph.impact(g, "fast.parse")["unresolved"])

    def test_the_cached_parse_remembers_both(self):
        """A second build reuses the stored parse, and the cache had no room for the second
        binding until it was given one."""
        self.write("app.py", self.TRY)
        codegraph.build([self.dir])
        g = codegraph.build([self.dir])
        call, = [e for e in g["calls"] if e["src"] == "app.go"]
        self.assertEqual(call["confidence"], "AMBIGUOUS")

    def test_one_import_still_resolves(self):
        """The guard: an ordinary from-import is not ambiguous just because it exists."""
        self.write("app.py", "from fast import parse\n\ndef go():\n    return parse()\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.go"]
        self.assertEqual((call.get("dst"), call["confidence"]), ("fast.parse", "QUALIFIED"))

    def test_the_same_name_from_the_same_module_twice_is_not_ambiguous(self):
        """Re-importing the same thing is a duplicate line, not a second source."""
        self.write("app.py", "from fast import parse\nfrom fast import parse\n"
                             "\ndef go():\n    return parse()\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.go"]
        self.assertEqual(call.get("dst"), "fast.parse")


class APackageShadowsAModuleOfTheSameName(Sandbox):
    """A repo holding both thing.py and thing/__init__.py. Python's finder looks for the
    package first, so `import thing` is the directory - and the tool answered with the file."""

    def test_import_thing_means_the_package(self):
        self.write("thing.py", "def f():\n    return 'the file'\n")
        self.write(os.path.join("thing", "__init__.py"), "def f():\n    return 'the package'\n")
        self.write("app.py", "import thing\n\ndef go():\n    return thing.f()\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.go"]
        self.assertEqual(call.get("dst"), "thing/__init__.f")
        self.assertEqual(codegraph.callers_of(g, "thing.f"), [])

    def test_a_plain_module_is_still_itself(self):
        """The guard: with no package of that name, nothing changes."""
        self.write("thing.py", "def f():\n    return 1\n")
        self.write("app.py", "import thing\n\ndef go():\n    return thing.f()\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.go"]
        self.assertEqual(call.get("dst"), "thing.f")


class TheSourceSaysWhatTypeItIs(Sandbox):
    """`def send(c: Client)` then c.get() was UNTYPED - the tool inferring around an answer the
    source had written down. Annotations are the modern way to say which class a name holds,
    and the one form that says it about a PARAMETER, where no assignment exists to read."""

    def setUp(self):
        super().setUp()
        self.write("svc.py", "class Client:\n"
                             "    def get(self):\n        return 1\n"
                             "    def close(self):\n        return 2\n")

    def edge(self, g, src, callee="get"):
        m = [e for e in g["calls"] if e["src"] == src and e["callee"] == callee]
        self.assertEqual(len(m), 1, m)
        return m[0].get("dst"), m[0]["confidence"]

    def test_an_annotated_parameter_is_a_type(self):
        self.write("app.py", "from svc import Client\n"
                             "\n"
                             "def send(c: Client):\n"
                             "    return c.get()\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "app.send"), ("svc.Client.get", "TYPED"))
        self.assertEqual(codegraph.callers_of(g, "svc.Client.get"), ["app.send"])

    def test_a_forward_reference_in_quotes_counts(self):
        """What every annotation becomes under `from __future__ import annotations`."""
        self.write("app.py", "from svc import Client\n"
                             "\n"
                             "def send(c: 'Client'):\n"
                             "    return c.get()\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "app.send"), ("svc.Client.get", "TYPED"))

    def test_a_keyword_only_parameter_counts_too(self):
        self.write("app.py", "from svc import Client\n"
                             "\n"
                             "def send(*, c: Client):\n"
                             "    return c.get()\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "app.send"), ("svc.Client.get", "TYPED"))

    def test_an_annotated_local_is_a_type(self):
        self.write("app.py", "from svc import Client\n"
                             "\n"
                             "def make():\n"
                             "    return Client()\n"
                             "\n"
                             "def send():\n"
                             "    c: Client = make()\n"
                             "    return c.get()\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "app.send"), ("svc.Client.get", "TYPED"))

    def test_a_container_annotation_is_not_its_contents(self):
        """The guard, and the reason only a bare name counts: `Dict[str, Client]` is a dict.
        Reading Client out of it would resolve d.get() - a dict method - to Client.get."""
        self.write("app.py", "from typing import Dict\n"
                             "from svc import Client\n"
                             "\n"
                             "def send(d: Dict[str, Client]):\n"
                             "    return d.get('k')\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "app.send"), (None, "UNTYPED"))

    def test_a_chained_assignment_binds_every_name(self):
        """`a = b = Client()` bound neither: the check wanted exactly one target."""
        self.write("app.py", "from svc import Client\n"
                             "\n"
                             "def send():\n"
                             "    a = b = Client()\n"
                             "    return a.get() + b.close()\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "app.send", "get"), ("svc.Client.get", "TYPED"))
        self.assertEqual(self.edge(g, "app.send", "close"), ("svc.Client.close", "TYPED"))


class ARebindingIsInformationToo(Sandbox):
    """A name reassigned to anything that was not another class call kept its old type. So
    `x = Foo()` followed by `x = load_config()` left x a Foo, and x.go() was answered with
    Foo.go at full confidence - the wrong class, from a line the tool had walked straight past."""

    def setUp(self):
        super().setUp()
        self.write("m.py", "class Foo:\n    def go(self):\n        return 1\n"
                           "class Bar:\n    def go(self):\n        return 2\n"
                           "def load_config():\n    return Bar()\n")

    def go_edge(self, g, src):
        m = [e for e in g["calls"] if e["src"] == src and e["callee"] == "go"]
        self.assertEqual(len(m), 1, m)
        return m[0].get("dst")

    def test_a_reassignment_takes_the_type_away(self):
        """A bare `Name()` on the right was the only shape that counted. `m.load_config()` is
        an attribute call, so the line was walked straight past and x stayed a Foo."""
        self.write("app.py", "import m\n"
                             "from m import Foo\n"
                             "\n"
                             "def use():\n"
                             "    x = Foo()\n"
                             "    x = m.load_config()\n"
                             "    return x.go()\n")
        g = self.graph(write=False)
        self.assertIsNone(self.go_edge(g, "app.use"))
        self.assertEqual(codegraph.callers_of(g, "m.Foo.go"), [])

    def test_a_plain_rebinding_takes_it_away_as_well(self):
        self.write("app.py", "from m import Foo\n"
                             "\n"
                             "def use(other):\n"
                             "    x = Foo()\n"
                             "    x = other\n"
                             "    return x.go()\n")
        g = self.graph(write=False)
        self.assertIsNone(self.go_edge(g, "app.use"))

    def test_the_optional_idiom_still_resolves(self):
        """`x = None` then `x = Foo()` is how half of Python initialises an optional. None
        holds no methods, so it cannot be what the call goes through."""
        self.write("app.py", "from m import Foo\n"
                             "\n"
                             "def use(flag):\n"
                             "    x = None\n"
                             "    if flag:\n"
                             "        x = Foo()\n"
                             "    return x.go()\n")
        g = self.graph(write=False)
        self.assertEqual(self.go_edge(g, "app.use"), "m.Foo.go")

    def test_unpacking_over_a_known_name_takes_it_away(self):
        """A tuple target was skipped entirely, so `a` kept the type it had two lines up."""
        self.write("app.py", "from m import Foo, load_config\n"
                             "\n"
                             "def use():\n"
                             "    a = Foo()\n"
                             "    a, b = load_config(), 1\n"
                             "    return a.go()\n")
        g = self.graph(write=False)
        self.assertIsNone(self.go_edge(g, "app.use"))


class ANestedDefinitionIsNotAvailableToTheTree(Sandbox):
    """A class or function defined inside another function is not visible outside it - `Inner()`
    in a second function is a NameError. The tree-wide fallback offered it anyway, resolved the
    call, and then typed the variable from it: two confident answers about a name Python would
    refuse to look up at all."""

    TREE = ("def holder():\n"
            "    class Inner:\n"
            "        def run(self):\n"
            "            return 1\n"
            "    return Inner()\n"
            "\n"
            "def uses_inner():\n"
            "    i = Inner()\n"
            "    return i.run()\n")

    def test_a_class_nested_in_a_function_is_not_reachable_from_another(self):
        self.write("app.py", self.TREE)
        g = self.graph(write=False)
        made = [e for e in g["calls"] if e["src"] == "app.uses_inner" and e["callee"] == "Inner"]
        self.assertEqual([(e.get("dst"), e["confidence"]) for e in made], [(None, "EXTERNAL")])
        self.assertEqual(codegraph.callers_of(g, "app.holder.Inner"), ["app.holder"])
        self.assertEqual(codegraph.callers_of(g, "app.holder.Inner.run"), [])

    def test_the_function_that_owns_it_still_reaches_it(self):
        """The guard: the scope chain is what makes the nested name legal where it is legal."""
        self.write("app.py", self.TREE)
        g = self.graph(write=False)
        own, = [e for e in g["calls"] if e["src"] == "app.holder" and e["callee"] == "Inner"]
        self.assertEqual((own.get("dst"), own["confidence"]), ("app.holder.Inner", "LOCAL"))

    def test_a_nested_function_is_not_offered_either(self):
        self.write("app.py", "def holder():\n"
                             "    def work():\n"
                             "        return 1\n"
                             "    return work()\n"
                             "\n"
                             "def elsewhere():\n"
                             "    return work()\n")
        g = self.graph(write=False)
        far, = [e for e in g["calls"] if e["src"] == "app.elsewhere"]
        self.assertIsNone(far.get("dst"))
        self.assertEqual(codegraph.callers_of(g, "app.holder.work"), ["app.holder"])


class TheQualifiedWayToBuildAThing(Sandbox):
    """`import svc` then `svc.Client()` is how package code constructs things, and it gave no
    type at all - so the very next line's c.get() was a blind spot in one of the most ordinary
    shapes Python has. The module part says exactly where to look: there is nothing to search
    and nothing to be ambiguous about."""

    def setUp(self):
        super().setUp()
        self.write("svc.py", "class Client:\n"
                             "    def get(self):\n        return 1\n"
                             "def make():\n    return 2\n")

    def get_edge(self, g, src):
        m = [e for e in g["calls"] if e["src"] == src and e["callee"] == "get"]
        self.assertEqual(len(m), 1, m)
        return m[0].get("dst"), m[0]["confidence"]

    def test_a_module_qualified_constructor_gives_a_type(self):
        self.write("app.py", "import svc\n"
                             "\n"
                             "def go():\n"
                             "    c = svc.Client()\n"
                             "    return c.get()\n")
        g = self.graph(write=False)
        self.assertEqual(self.get_edge(g, "app.go"), ("svc.Client.get", "TYPED"))

    def test_a_module_qualified_annotation_gives_a_type(self):
        self.write("app.py", "import svc\n"
                             "\n"
                             "def go(c: svc.Client):\n"
                             "    return c.get()\n")
        g = self.graph(write=False)
        self.assertEqual(self.get_edge(g, "app.go"), ("svc.Client.get", "TYPED"))

    def test_a_receiver_that_is_not_an_imported_module_invents_nothing(self):
        """The guard: `x = factory.Client()` where factory is a local object must stay unknown
        - the same rule round twenty-nine put on every other dotted receiver."""
        self.write("app.py", "def go(factory):\n"
                             "    c = factory.Client()\n"
                             "    return c.get()\n")
        g = self.graph(write=False)
        self.assertEqual(self.get_edge(g, "app.go"), (None, "UNTYPED"))

    def test_a_module_function_is_not_a_class(self):
        """`x = svc.make()` names a function. It is not a constructor and must not be typed."""
        self.write("app.py", "import svc\n"
                             "\n"
                             "def go():\n"
                             "    c = svc.make()\n"
                             "    return c.get()\n")
        g = self.graph(write=False)
        self.assertEqual(self.get_edge(g, "app.go"), (None, "UNTYPED"))


class ALambdaParameterIsAScope(Sandbox):
    """A lambda's parameters were not a scope at all, so `lambda config: config.dumps(x)` read
    config as the imported module and resolved the call into it - QUALIFIED, the tool's highest
    confidence, on a receiver that is whatever the caller passes in."""

    def setUp(self):
        super().setUp()
        self.write("config.py", "def dumps(x):\n    return 1\n")

    def edge(self, g, src):
        m = [e for e in g["calls"] if e["src"] == src and e["callee"] == "dumps"]
        self.assertEqual(len(m), 1, m)
        return m[0].get("dst"), m[0]["confidence"]

    def test_a_parameter_shadows_at_module_level(self):
        self.write("app.py", "import config\n\nhandler = lambda config: config.dumps(1)\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "app"), (None, "UNTYPED"))

    def test_a_parameter_shadows_inside_a_function(self):
        self.write("app.py", "import config\n"
                             "\n"
                             "def go():\n"
                             "    return (lambda config: config.dumps(2))(None)\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "app.go"), (None, "UNTYPED"))

    def test_a_lambda_that_shadows_nothing_still_resolves(self):
        """The guard: the fix is a scope, not a blindfold."""
        self.write("app.py", "import config\n\nhandler = lambda x: config.dumps(x)\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "app"), ("config.dumps", "QUALIFIED"))

    def test_a_default_value_is_evaluated_outside_the_lambda(self):
        """`lambda config=config.dumps(0): ...` - the default runs in the enclosing scope,
        where config is still the module."""
        self.write("app.py", "import config\n\nhandler = lambda config=config.dumps(0): 1\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "app"), ("config.dumps", "QUALIFIED"))


class TheModuleIsAScopeToo(Sandbox):
    """Every function got a pre-scan of the names it binds; the file's own top level got an
    empty set. So a top-level `for config in rows:` or `with open(p) as config:` left config
    looking like the imported module, and config.dumps() resolved into it - on a receiver that
    is a number, or a file."""

    def setUp(self):
        super().setUp()
        self.write("config.py", "def dumps(x):\n    return 1\n")

    def edge(self, g, src):
        m = [e for e in g["calls"] if e["src"] == src and e["callee"] == "dumps"]
        self.assertEqual(len(m), 1, m)
        return m[0].get("dst"), m[0]["confidence"]

    def test_a_top_level_loop_target_shadows(self):
        self.write("app.py", "import config\n\nfor config in [2]:\n    config.dumps(5)\n")
        self.assertEqual(self.edge(self.graph(write=False), "app"), (None, "UNTYPED"))

    def test_a_top_level_with_target_shadows(self):
        self.write("app.py", "import config\n"
                             "\n"
                             "with open(__file__) as config:\n"
                             "    config.dumps(6)\n")
        self.assertEqual(self.edge(self.graph(write=False), "app"), (None, "UNTYPED"))

    def test_an_import_alone_is_not_a_shadow(self):
        """The guard: the import is the binding resolution is trying to follow."""
        self.write("app.py", "import config\n\ndef go():\n    return config.dumps(7)\n")
        self.assertEqual(self.edge(self.graph(write=False), "app.go"),
                         ("config.dumps", "QUALIFIED"))

    def test_a_top_level_class_is_not_a_shadow_of_itself(self):
        """The guard that caught a regression while this was being written: a module's own
        `class Parent` is the definition Parent.make() is looking for."""
        self.write("app.py", "class Parent:\n"
                             "    @classmethod\n"
                             "    def make(cls):\n"
                             "        return 1\n"
                             "\n"
                             "def go():\n"
                             "    return Parent.make()\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.go" and e["callee"] == "make"]
        self.assertEqual((call.get("dst"), call["confidence"]), ("app.Parent.make", "CLASS"))


class ADeferredImportIsStillAnImport(Sandbox):
    """`def f(): import memory; return memory.recall()` is the deliberate cycle-break, and the
    tool knows the idiom well enough to keep it out of the cycle report - then lost every call
    edge through it, because the local import counted as a name shadowing the module."""

    def setUp(self):
        super().setUp()
        self.write("memory.py", "def recall(x):\n    return 1\n")

    def test_a_local_import_resolves_the_call(self):
        self.write("app.py", "def deferred():\n"
                             "    import memory\n"
                             "    return memory.recall(1)\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.deferred"]
        self.assertEqual((call.get("dst"), call["confidence"]), ("memory.recall", "QUALIFIED"))

    def test_a_local_from_import_resolves_too(self):
        self.write("app.py", "def deferred():\n"
                             "    from memory import recall\n"
                             "    return recall(2)\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.deferred"]
        self.assertEqual((call.get("dst"), call["confidence"]), ("memory.recall", "QUALIFIED"))

    def test_it_is_still_not_a_module_level_dependency(self):
        """The guard: resolving the call must not turn the cycle-break back into a cycle."""
        self.write("memory.py", "import app\ndef recall(x):\n    return app.go()\n")
        self.write("app.py", "def go():\n"
                             "    import memory\n"
                             "    return memory.recall(1)\n")
        g = self.graph(write=False)
        self.assertEqual(codegraph.cycles(g), [])
        self.assertEqual(codegraph.callers_of(g, "memory.recall"), ["app.go"])


class AComprehensionKeepsItsVariable(Sandbox):
    """In Python 3 a comprehension's loop variable is its own scope: it shadows inside the
    comprehension and does not exist outside it. The pre-scan collected it as a name bound in
    the ENCLOSING scope, so `[r for config in rows]` silenced every config.x() call around it -
    and once the module itself gained a pre-scan, one such line at the top of a file silenced
    the whole file."""

    def setUp(self):
        super().setUp()
        self.write("config.py", "def dumps(x):\n    return 1\n")

    def edge(self, g, src):
        m = [e for e in g["calls"] if e["src"] == src and e["callee"] == "dumps"]
        self.assertEqual(len(m), 1, m)
        return m[0].get("dst"), m[0]["confidence"]

    def test_it_does_not_leak_out_of_a_function(self):
        self.write("app.py", "import config\n"
                             "\n"
                             "def go(rows):\n"
                             "    xs = [r for config in rows]\n"
                             "    return config.dumps(len(xs))\n")
        self.assertEqual(self.edge(self.graph(write=False), "app.go"),
                         ("config.dumps", "QUALIFIED"))

    def test_it_does_not_leak_out_of_a_module(self):
        self.write("app.py", "import config\n"
                             "\n"
                             "ROWS = [r for config in [[1]]]\n"
                             "\n"
                             "def go():\n"
                             "    return config.dumps(1)\n")
        self.assertEqual(self.edge(self.graph(write=False), "app.go"),
                         ("config.dumps", "QUALIFIED"))

    def test_it_does_shadow_inside_itself(self):
        """The other half, and the reason this cannot simply be deleted from the pre-scan."""
        self.write("app.py", "import config\n"
                             "\n"
                             "ROWS = [config.dumps(r) for config in [[1]]]\n")
        self.assertEqual(self.edge(self.graph(write=False), "app"), (None, "UNTYPED"))

    def test_the_first_iterable_is_evaluated_outside(self):
        """`[x for config in config.dumps(0)]` - the outermost iterable runs in the enclosing
        scope, before the loop variable exists."""
        self.write("app.py", "import config\n\nROWS = [x for config in config.dumps(0)]\n")
        self.assertEqual(self.edge(self.graph(write=False), "app"),
                         ("config.dumps", "QUALIFIED"))


class AModuleThatImportsItself(Sandbox):
    """`deps` reported the module as its own importer while `cycles` said "(none)". Two verbs
    cannot disagree about whether an edge is there."""

    def test_a_self_import_is_a_cycle(self):
        self.write("selfmod.py", "import selfmod\n"
                                 "def a():\n"
                                 "    return selfmod.b()\n"
                                 "def b():\n"
                                 "    return 1\n")
        g = self.graph(write=False)
        self.assertEqual(codegraph.cycles(g), [("selfmod",)])
        imports, importers = codegraph.module_deps(g, "selfmod")
        self.assertEqual((imports, importers), (["selfmod"], ["selfmod"]))

    def test_the_command_line_does_not_print_a_cycle_of_one_name(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, "selfmod.py"), "w", encoding="utf-8") as f:
            f.write("import selfmod\ndef a():\n    return selfmod.a()\n")
        cg = os.path.join(HERE, "codegraph.py")
        subprocess.run([sys.executable, cg, "build", "."], cwd=d, capture_output=True, timeout=180)
        r = subprocess.run([sys.executable, cg, "cycles"], cwd=d,
                           capture_output=True, text=True, timeout=180)
        self.assertIn("selfmod <-> itself", r.stdout)

    def test_two_modules_are_still_reported_as_a_pair(self):
        self.write("a.py", "import b\ndef f():\n    return 1\n")
        self.write("b.py", "import a\ndef g():\n    return 1\n")
        self.assertEqual(codegraph.cycles(self.graph(write=False)), [("a", "b")])


class AGraphFileHasToBeAGraph(unittest.TestCase):
    """Valid JSON is not the same as a graph. A file left by another tool, a bad merge, or
    someone's codegraph.json from a different project parses perfectly and then every query
    dies on a traceback - `[]` has no .get, and the first thing asked of it is g.get("version").
    The parse was guarded from the beginning; the shape never was."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        with open(os.path.join(self.dir, "m.py"), "w", encoding="utf-8") as f:
            f.write("def f():\n    return 1\n")

    def run_it(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), *args],
                              cwd=self.dir, capture_output=True, text=True, timeout=180)

    def write_graph(self, text):
        with open(os.path.join(self.dir, "codegraph.json"), "w", encoding="utf-8") as f:
            f.write(text)

    def test_json_of_the_wrong_shape_is_a_message_not_a_traceback(self):
        for text in ("[]", "null", '"a string"', '{"nodes": {}}', "42"):
            self.write_graph(text)
            r = self.run_it("callers", "f")
            self.assertNotIn("Traceback", r.stderr, text)
            self.assertIn("not a codegraph graph", r.stdout + r.stderr, text)
            self.assertEqual(r.returncode, 1, text)

    def test_a_real_graph_is_still_accepted(self):
        self.assertEqual(self.run_it("build", ".").returncode, 0)
        self.assertEqual(self.run_it("callers", "f").returncode, 0)


class AClassBodyIsAScope(Sandbox):
    """`class A: config = 1` and then config.dumps() in that body is the class attribute, not
    the imported module - and it resolved into the module. A METHOD, on the other hand, does not
    see class attributes at all, which is the half a naive fix gets wrong."""

    def setUp(self):
        super().setUp()
        self.write("config.py", "def dumps(x):\n    return 1\n")

    def edge(self, g, src):
        m = [e for e in g["calls"] if e["src"] == src and e["callee"] == "dumps"]
        self.assertEqual(len(m), 1, m)
        return m[0].get("dst"), m[0]["confidence"]

    def test_a_class_attribute_shadows_in_the_class_body(self):
        self.write("app.py", "import config\n"
                             "\n"
                             "class A:\n"
                             "    config = 'an attribute'\n"
                             "    y = config.dumps(1)\n")
        self.assertEqual(self.edge(self.graph(write=False), "app"), (None, "UNTYPED"))

    def test_a_method_does_not_see_the_class_attribute(self):
        """The guard. Python looks straight past class scope from inside a method, so config
        there is still the module - and a fix that left the frame on the stack would silence
        every method of every class that has an attribute named like an import."""
        self.write("app.py", "import config\n"
                             "\n"
                             "class A:\n"
                             "    config = 'an attribute'\n"
                             "\n"
                             "    def method(self):\n"
                             "        return config.dumps(1)\n")
        self.assertEqual(self.edge(self.graph(write=False), "app.A.method"),
                         ("config.dumps", "QUALIFIED"))

    def test_a_class_nested_in_a_class_does_not_see_the_outer_one(self):
        self.write("app.py", "import config\n"
                             "\n"
                             "class A:\n"
                             "    config = 'an attribute'\n"
                             "\n"
                             "    class Inner:\n"
                             "        y = config.dumps(1)\n")
        self.assertEqual(self.edge(self.graph(write=False), "app"),
                         ("config.dumps", "QUALIFIED"))


class MatchBindsNamesToo(Sandbox):
    """`case [config]` and `case str() as config` bind a name, and they carry it as a plain
    string on the pattern rather than as a Name node - so the pre-scan walked straight past
    them and config.dumps() resolved into the imported module."""

    def setUp(self):
        super().setUp()
        if sys.version_info < (3, 10):
            self.skipTest("match statements arrive in 3.10")
        self.write("config.py", "def dumps(x):\n    return 1\n")

    def dst(self, g, src):
        m = [e for e in g["calls"] if e["src"] == src and e["callee"] == "dumps"]
        self.assertEqual(len(m), 1, m)
        return m[0].get("dst")

    def test_a_capture_pattern_binds(self):
        self.write("app.py", "import config\n"
                             "\n"
                             "def go(v):\n"
                             "    match v:\n"
                             "        case [config]:\n"
                             "            return config.dumps(1)\n")
        self.assertIsNone(self.dst(self.graph(write=False), "app.go"))

    def test_an_as_pattern_binds(self):
        self.write("app.py", "import config\n"
                             "\n"
                             "def go(v):\n"
                             "    match v:\n"
                             "        case str() as config:\n"
                             "            return config.dumps(1)\n")
        self.assertIsNone(self.dst(self.graph(write=False), "app.go"))

    def test_a_match_that_binds_nothing_leaves_the_module_alone(self):
        self.write("app.py", "import config\n"
                             "\n"
                             "def go(v):\n"
                             "    match v:\n"
                             "        case []:\n"
                             "            return config.dumps(1)\n")
        self.assertEqual(self.dst(self.graph(write=False), "app.go"), "config.dumps")


class EveryLocationIsAPlaceYouCanOpen(unittest.TestCase):
    """`sites` promises the exact places to edit, and built them out of module ids. An id is a
    path only while there is one tree: with two roots the ids carry a synthetic label, so the
    answer named a file that does not exist - or, worse, one that does and is the wrong file."""

    def setUp(self):
        # Two roots under DIFFERENT parents, so a module id is visibly not a path: the ids come
        # out "api/util" and "worker/job" while the files live at "a/api/util.py" and
        # "b/worker/job.py" relative to the common root.
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "a", "api"))
        os.makedirs(os.path.join(self.root, "b", "worker"))
        self.write("a/api/util.py", "def shared():\n    return 1\n")
        self.write("b/worker/job.py", "from api.util import shared\n"
                                      "def run():\n"
                                      "    return shared()\n")
        self.out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        self._saved = {k: getattr(codegraph, k) for k in ("HOME", "OUT", "CACHE")}
        codegraph.OUT = os.path.join(self.out, "codegraph.json")
        codegraph.CACHE = os.path.join(self.out, "codegraph.cache.json")

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(codegraph, k, v)

    def write(self, rel, body):
        with open(os.path.join(self.root, *rel.split("/")), "w", encoding="utf-8") as f:
            f.write(body)

    def test_two_roots_give_paths_that_resolve(self):
        g = codegraph.build([os.path.join(self.root, "a", "api"),
                             os.path.join(self.root, "b", "worker")], write=False)
        (_, loc), = codegraph.where(g, "shared")
        site, = codegraph.sites(g, "api/util.shared")
        for path in (loc.rsplit(":", 1)[0], site[0].rsplit(":", 1)[0]):
            self.assertTrue(os.path.exists(os.path.join(self.root, path)),
                            f"{path} does not exist under {self.root}")

    def test_one_root_still_reads_as_a_plain_relative_path(self):
        g = codegraph.build([self.root], write=False)
        (_, loc), = codegraph.where(g, "shared")
        self.assertEqual(loc, "a/api/util.py:1")


class CallingAnInstanceRunsItsCall(Sandbox):
    """`c = Client()` then `c(1)`. Calling an instance runs __call__, and the call site never
    writes that name - so `impact Client.__call__` answered "callers: (none), blast: 0" about a
    method being called two lines away. The same shape as __init__, and the same wrong answer.
    Callable classes are ordinary Python: decorators written as classes, handlers, anything with
    state and one obvious verb."""

    def edge(self, g, callee):
        m = [e for e in g["calls"] if e["src"] == "m.uses" and e["callee"] == callee]
        self.assertEqual(len(m), 1, m)
        return m[0].get("dst"), m[0]["confidence"]

    def test_calling_a_typed_local_reaches_call(self):
        self.write("m.py", "class Client:\n"
                           "    def __call__(self, x):\n"
                           "        return x\n"
                           "\n"
                           "def uses():\n"
                           "    c = Client()\n"
                           "    return c(1)\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g, "c"), ("m.Client.__call__", "TYPED"))
        im = codegraph.impact(g, "m.Client.__call__")
        self.assertEqual(im["callers"], ["m.uses"])
        self.assertEqual([loc for loc, _ in im["sites"]], ["m.py:7"])

    def test_an_inherited_call_counts(self):
        """The same rule CONSTRUCTOR follows: the method that actually runs, through the MRO."""
        self.write("m.py", "class Callable:\n"
                           "    def __call__(self, x):\n"
                           "        return x\n"
                           "\n"
                           "class Sub(Callable):\n"
                           "    pass\n"
                           "\n"
                           "def uses():\n"
                           "    b = Sub()\n"
                           "    return b(2)\n")
        self.assertEqual(self.edge(self.graph(write=False), "b"),
                         ("m.Callable.__call__", "TYPED"))

    def test_a_class_that_is_not_callable_invents_nothing(self):
        """The guard: most objects are not callable, and calling one is a TypeError, not an
        edge to be found."""
        self.write("m.py", "class Plain:\n"
                           "    def work(self):\n"
                           "        return 1\n"
                           "\n"
                           "def uses():\n"
                           "    p = Plain()\n"
                           "    return p()\n")
        self.assertEqual(self.edge(self.graph(write=False), "p"), (None, "UNTYPED"))

    def test_an_untyped_local_is_still_untyped(self):
        """The guard on the other side: without a type there is nothing to look __call__ up on."""
        self.write("m.py", "def uses(c):\n    return c(1)\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "m.uses"]
        self.assertIsNone(call.get("dst"))


class AnImportEdgeNamesTheModuleTheGraphKnows(Sandbox):
    """`import sysconfig` was recorded as "sysconfig"; the module's id is "sysconfig/__init__".
    The resolver normalised that and the import table did not, so the edge pointed at a name no
    node has - and `deps` answered "importers: (none)" for a package half the tree imports."""

    def test_a_package_can_name_its_importers(self):
        self.write(os.path.join("pkg", "__init__.py"), "def helper():\n    return 1\n")
        self.write("app.py", "import pkg\n\ndef go():\n    return pkg.helper()\n")
        g = self.graph(write=False)
        self.assertEqual(codegraph.module_deps(g, "pkg/__init__")[1], ["app"])
        self.assertIn("pkg/__init__", codegraph.module_deps(g, "app")[0])

    def test_a_cycle_through_a_package_init_is_visible(self):
        self.write(os.path.join("pkg", "__init__.py"), "import app\ndef helper():\n    return 1\n")
        self.write("app.py", "import pkg\ndef go():\n    return pkg.helper()\n")
        self.assertEqual(codegraph.cycles(self.graph(write=False)), [("app", "pkg/__init__")])

    def test_from_package_import_submodule_is_recorded(self):
        """`from pkg import sub` really does import pkg.sub - and `deps pkg/sub` could not name
        the files that import it."""
        self.write(os.path.join("pkg", "__init__.py"), "")
        self.write(os.path.join("pkg", "sub.py"), "def work():\n    return 1\n")
        self.write("app.py", "from pkg import sub\n\ndef go():\n    return sub.work()\n")
        g = self.graph(write=False)
        self.assertEqual(codegraph.module_deps(g, "pkg/sub")[1], ["app"])

    def test_a_name_that_is_not_a_module_records_no_import(self):
        """The guard: `from pkg import helper` imports a FUNCTION, and there is no pkg/helper."""
        self.write(os.path.join("pkg", "__init__.py"), "def helper():\n    return 1\n")
        self.write("app.py", "from pkg import helper\n\ndef go():\n    return helper()\n")
        g = self.graph(write=False)
        self.assertEqual({i["callee"] for i in g["imports"]}, {"pkg/__init__"})


class ANameImportedFromOutsideIsOutside(Sandbox):
    """`from time import sleep` says exactly where the name came from, and it is not here. The
    tree-wide "exactly one definition of that name" rule fired anyway: across the standard
    library that answered thousands of calls with functions in unrelated packages."""

    def test_a_bare_call_to_an_out_of_tree_name_stays_external(self):
        self.write("sched.py", "def sleep(n):\n    return 'the in-tree one - the wrong answer'\n")
        self.write("app.py", "from time import sleep\n"
                             "\n"
                             "def go():\n"
                             "    return sleep(1)\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.go"]
        self.assertIsNone(call.get("dst"))
        self.assertEqual(call["confidence"], "EXTERNAL")
        self.assertEqual(codegraph.callers_of(g, "sched.sleep"), [])

    def test_an_in_tree_import_still_resolves(self):
        """The guard: the rule is about where the name came FROM, not about giving up."""
        self.write("sched.py", "def sleep(n):\n    return 1\n")
        self.write("app.py", "from sched import sleep\n\ndef go():\n    return sleep(1)\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.go"]
        self.assertEqual((call.get("dst"), call["confidence"]), ("sched.sleep", "QUALIFIED"))


class AnAliasedImportKeepsItsRealName(Sandbox):
    """`from operator import index as _index` - the module holds `index`, and the lookup was
    made with the LOCAL name. It found nothing, fell through to the tree-wide rule, and answered
    with an unrelated function that happened to be the only other `_index` in the tree."""

    def test_the_module_is_asked_for_the_name_it_defines(self):
        self.write("ops.py", "def index(x):\n    return 1\n")
        self.write("far.py", "def _index(x):\n    return 'the wrong answer'\n")
        self.write("app.py", "from ops import index as _index\n"
                             "\n"
                             "def go():\n"
                             "    return _index(1)\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.go"]
        self.assertEqual((call.get("dst"), call["confidence"]), ("ops.index", "QUALIFIED"))
        self.assertEqual(codegraph.callers_of(g, "far._index"), [])

    def test_an_aliased_class_types_its_variable(self):
        self.write("svc.py", "class Client:\n    def get(self):\n        return 1\n")
        self.write("app.py", "from svc import Client as C\n"
                             "\n"
                             "def go():\n"
                             "    c = C()\n"
                             "    return c.get()\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.go" and e["callee"] == "get"]
        self.assertEqual((call.get("dst"), call["confidence"]), ("svc.Client.get", "TYPED"))


class AStarImportIsARealBinding(Sandbox):
    """`from turtle import *` was bound as a name spelled "*", which nothing ever calls. So a
    bare home() fell through to the tree-wide "one definition of that name" rule: "say which",
    to a file that had said which - or, where the tree held exactly one other candidate, that
    other candidate, with confidence. Across the standard library it accounted for 289 edges."""

    def setUp(self):
        super().setUp()
        self.write("turtle.py", "def home():\n    return 'the right one'\n")
        self.write("commands.py", "def home():\n    return 'a different module'\n")

    def edge(self, g, src="dance.main"):
        m = [e for e in g["calls"] if e["src"] == src and e["callee"] == "home"]
        self.assertEqual(len(m), 1, m)
        return m[0].get("dst"), m[0]["confidence"]

    def test_a_bare_name_comes_from_the_module_that_was_starred(self):
        self.write("dance.py", "from turtle import *\n\ndef main():\n    return home()\n")
        g = self.graph(write=False)
        self.assertEqual(self.edge(g), ("turtle.home", "QUALIFIED"))
        self.assertEqual(codegraph.callers_of(g, "commands.home"), [])

    def test_two_stars_that_both_define_it_are_ambiguous(self):
        """Which one wins depends on the order of the imports, which is not something to
        answer with confidence."""
        self.write("dance.py", "from turtle import *\n"
                               "from commands import *\n"
                               "\n"
                               "def main():\n"
                               "    return home()\n")
        g = self.graph(write=False)
        got = self.edge(g)
        self.assertEqual(got, (None, "AMBIGUOUS"))
        call, = [e for e in g["calls"] if e["src"] == "dance.main"]
        self.assertEqual(call["candidates"], ["commands.home", "turtle.home"])

    def test_an_explicit_import_still_wins(self):
        """The guard: a name imported by hand is not a guess about a star."""
        self.write("dance.py", "from turtle import *\n"
                               "from commands import home\n"
                               "\n"
                               "def main():\n"
                               "    return home()\n")
        self.assertEqual(self.edge(self.graph(write=False)), ("commands.home", "QUALIFIED"))

    def test_a_star_from_outside_the_tree_invents_nothing(self):
        self.write("dance.py", "from tkinter import *\n\ndef main():\n    return home()\n")
        g = self.graph(write=False)
        dst, _ = self.edge(g)
        self.assertIsNone(dst)


class ADottedImportImportsBothOfThem(Sandbox):
    """`import logging.handlers` imports the package and the module inside it. Only the package
    was recorded, so `deps logging/handlers` could not name the files that import it - the same
    hole `from pkg import sub` had, in the other import statement."""

    def setUp(self):
        super().setUp()
        self.write(os.path.join("logging", "__init__.py"), "")
        self.write(os.path.join("logging", "handlers.py"), "class MemoryHandler:\n    pass\n")

    def test_both_the_package_and_the_module_are_importers(self):
        self.write("user.py", "import logging.handlers\n"
                              "\n"
                              "def go():\n"
                              "    return logging.handlers.MemoryHandler()\n")
        g = self.graph(write=False)
        self.assertEqual(codegraph.module_deps(g, "logging/handlers")[1], ["user"])
        self.assertEqual(codegraph.module_deps(g, "logging/__init__")[1], ["user"])

    def test_the_call_through_it_still_resolves(self):
        self.write("user.py", "import logging.handlers\n"
                              "\n"
                              "def go():\n"
                              "    return logging.handlers.MemoryHandler()\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "user.go"]
        self.assertEqual(call.get("dst"), "logging/handlers.MemoryHandler")

    def test_a_plain_import_records_one_edge(self):
        """The guard: `import logging` has no submodule to add."""
        self.write("user.py", "import logging\n\ndef go():\n    return 1\n")
        g = self.graph(write=False)
        self.assertEqual([i["callee"] for i in g["imports"]], ["logging/__init__"])


class WhatSecurityMdPromises(unittest.TestCase):
    """A tool people are told to copy into their own repository makes three promises about what
    it will do there: no network, no execution, nothing but the standard library. Prose goes
    stale the moment nothing runs it - the last count in SECURITY.md was already wrong."""

    NETWORK = frozenset({"socket", "ssl", "urllib", "http", "ftplib", "smtplib", "poplib",
                         "imaplib", "telnetlib", "xmlrpc", "webbrowser", "requests",
                         "urllib3", "httpx", "asyncio"})
    EXECUTION = frozenset({"subprocess", "ctypes", "importlib", "runpy", "pty", "multiprocessing"})

    def setUp(self):
        with open(os.path.join(HERE, "codegraph.py"), encoding="utf-8") as f:
            self.src = f.read()
        self.tree = ast.parse(self.src)
        self.imports = set()
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Import):
                self.imports.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                self.imports.add(n.module.split(".")[0])

    def test_it_imports_nothing_but_the_standard_library(self):
        if not hasattr(sys, "stdlib_module_names"):
            self.skipTest("sys.stdlib_module_names arrives in 3.10")   # 3.9 is the floor
        outside = sorted(m for m in self.imports if m not in sys.stdlib_module_names)
        self.assertEqual(outside, [], "a dependency crept in")

    def test_it_cannot_reach_the_network(self):
        self.assertEqual(sorted(self.imports & self.NETWORK), [])

    def test_it_cannot_run_anything(self):
        """It parses your code. A tool that imported the files it analyses would run whatever
        is at the top of them."""
        self.assertEqual(sorted(self.imports & self.EXECUTION), [])
        called = {n.func.id for n in ast.walk(self.tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertEqual(sorted(called & {"exec", "eval", "compile", "__import__"}), [])

    def test_the_module_count_in_security_md_is_the_real_one(self):
        words = {"nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
                 "fourteen": 14, "fifteen": 15, "sixteen": 16}
        with open(os.path.join(HERE, "SECURITY.md"), encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"It imports (\w+) standard-library modules", text)
        self.assertIsNotNone(m, "SECURITY.md lost the sentence this checks")
        self.assertEqual(words.get(m.group(1)), len(self.imports),
                         f"SECURITY.md says {m.group(1)}; the file imports {len(self.imports)}")


class ASkipMessageMustNotBeAbleToCrash(Sandbox):
    """Every "skipped" line went through `os.path.relpath`, which RAISES on Windows when the
    file and the current directory are on different drives. The checkout is on D: and the temp
    directory is on C:, so one unparseable file ended the whole build with a ValueError - the
    exact thing the skip path exists to prevent, on an entire platform. Found by CI, on the
    first run, because this machine has one drive."""

    def test_a_build_survives_a_relpath_that_raises(self):
        self.write("good.py", "def ok():\n    return 1\n")
        self.write("bad.py", "def (((\n")
        real = os.path.relpath

        def two_drives(path, start=None):
            # Only the one-argument form crosses drives. `relpath(file, root)` inside build()
            # is computing a module id from a root the file was found under, which cannot.
            if start is None:
                raise ValueError("path is on mount 'C:', start on mount 'D:'")
            return real(path, start)

        os.path.relpath = two_drives
        try:
            g = codegraph.build([self.dir], write=False)
        finally:
            os.path.relpath = real
        self.assertIn("good.ok", {n["id"] for n in g["nodes"]})
        self.assertEqual(len(g["unreadable"]), 1, g["unreadable"])
        self.assertIn("bad.py", g["unreadable"][0])

    def test_a_far_away_file_keeps_its_real_path(self):
        """`../../../../../opt/homebrew/...` is not a shorter way to say an absolute path, and
        it is what the message used to contain when the tree was nowhere near the cwd."""
        self.assertEqual(codegraph._say("/opt/x/y.py"),
                         os.path.relpath("/opt/x/y.py")
                         if len(os.path.relpath("/opt/x/y.py")) < len("/opt/x/y.py")
                         else "/opt/x/y.py")
        deep = os.path.join(self.dir, "m.py")
        self.assertLessEqual(len(codegraph._say(deep)), len(deep))

    def test_both_recursion_limits_give_the_same_sentence(self):
        """A generated file blows either the parser's stack or the walker's, depending on the
        platform and the version. The person reading the message does not care, and a test
        that asserted one wording passed on a Mac and failed on Linux."""
        self.write("gen.py", "TABLE = " + "1+" * 30000 + "1\n")
        g = self.graph(write=False)
        self.assertEqual(len(g["unreadable"]), 1, g["unreadable"])
        self.assertIn("too deeply nested", g["unreadable"][0])


class TheCacheIsAnOptimisationNotARequirement(Sandbox):
    """Eight builds at once on Windows, and the one that lost the race to the cache file exited
    1 with no graph at all - reporting "Access is denied" about a directory the user could
    plainly write to. Two bugs in one line: a rename that Windows refuses while another process
    holds the file open, and a cache failure treated as a reason to have no answer."""

    def test_a_build_survives_a_cache_it_cannot_write(self):
        self.write("m.py", "def f():\n    return 1\n")
        codegraph.CACHE = os.path.join(self.dir, "not-a-directory", "codegraph.cache.json")
        g = codegraph.build([self.dir])
        self.assertIn("m.f", {n["id"] for n in g["nodes"]})
        self.assertTrue(os.path.exists(codegraph.OUT), "the graph itself must still be written")

    def test_the_graph_failing_to_write_is_still_an_error(self):
        """The guard: the graph is the product. Only the cache is expendable."""
        self.write("m.py", "def f():\n    return 1\n")
        codegraph.OUT = os.path.join(self.dir, "not-a-directory", "codegraph.json")
        with self.assertRaises(codegraph.BadPath):
            codegraph.build([self.dir])

    def test_a_rename_windows_refuses_for_a_moment_is_waited_out(self):
        """POSIX renames over an open file. Windows returns "Access is denied" until the other
        process lets go, which is a moment, not a refusal."""
        real = os.replace
        state = {"left": 3}

        def busy(src, dst, *a, **kw):
            if state["left"] > 0:
                state["left"] -= 1
                raise PermissionError(5, "Access is denied")
            return real(src, dst, *a, **kw)

        self.write("m.py", "def f():\n    return 1\n")
        os.replace = busy
        try:
            g = codegraph.build([self.dir])
        finally:
            os.replace = real
        self.assertEqual(state["left"], 0, "the retry never happened")
        self.assertIn("m.f", {n["id"] for n in g["nodes"]})
        self.assertTrue(os.path.exists(codegraph.OUT))

    def test_a_rename_that_never_succeeds_still_reports(self):
        """The guard on the other side: a retry loop that swallows a real failure is worse
        than no retry loop."""
        real = os.replace

        def always_busy(src, dst, *a, **kw):
            raise PermissionError(5, "Access is denied")

        self.write("m.py", "def f():\n    return 1\n")
        os.replace = always_busy
        try:
            with self.assertRaises(codegraph.BadPath):
                codegraph.build([self.dir])
        finally:
            os.replace = real


class ASecondBuildIsTheSameBuild(Sandbox):
    """The cached parse is handed to resolution, which writes dst and confidence onto every
    edge it touches. Those writes must not reach the cache that is about to be written back, or
    the second build answers from the first build's conclusions instead of re-deriving them."""

    TREE = (
        ("svc.py", "class Client:\n"
                  "    def __init__(self):\n        pass\n"
                   "    def get(self):\n        return 1\n"),
        ("app.py", "from svc import Client\n"
                  "\n"
                  "def go():\n"
                  "    c = Client()\n"
                  "    return c.get()\n"
                  "\n"
                   "def again():\n"
                   "    return go()\n"),
    )

    def build_tree(self):
        for name, body in self.TREE:
            self.write(name, body)

    def test_the_warm_graph_is_identical_to_the_cold_one(self):
        self.build_tree()
        cold = codegraph.build([self.dir])
        warm = codegraph.build([self.dir])
        self.assertEqual(json.dumps(cold, sort_keys=True), json.dumps(warm, sort_keys=True))
        self.assertEqual(codegraph.callers_of(warm, "svc.Client.get"), ["app.go"])

    def test_the_cache_holds_no_conclusions(self):
        """It is a record of what was PARSED. A dst or a confidence in there is a conclusion,
        and a conclusion cached is a conclusion never re-checked when the rest of the tree
        changes around it."""
        self.build_tree()
        codegraph.build([self.dir])
        with open(codegraph.CACHE, encoding="utf-8") as f:
            cache = json.load(f)
        edges = [e for entry in cache["files"].values() for e in entry["calls"]]
        self.assertTrue(edges, "no edges were cached at all")
        for e in edges:
            self.assertNotIn("dst", e, e)
            self.assertNotIn("confidence", e, e)
            self.assertNotIn("candidates", e, e)

    def test_resolving_does_not_reach_back_into_the_cache_in_memory(self):
        """The shallow copy is per EDGE, so the dicts resolution writes to are not the dicts
        the cache holds. A single shared list of dicts would make this fail."""
        self.build_tree()
        codegraph.build([self.dir])
        with open(codegraph.CACHE, encoding="utf-8") as f:
            before = json.load(f)
        codegraph.build([self.dir])
        with open(codegraph.CACHE, encoding="utf-8") as f:
            after = json.load(f)
        self.assertEqual(json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True))

    def test_the_line_numbers_survive_the_copy(self):
        """The one nested value an edge carries. A copy that flattened it would take `sites`
        down to one line per relationship, which is a promise this makes on the front page."""
        self.write("m.py", "def helper():\n    return 1\n"
                           "def uses():\n"
                           "    a = helper()\n"
                           "    b = helper()\n"
                           "    c = helper()\n"
                           "    return a + b + c\n")
        cold = codegraph.build([self.dir])
        warm = codegraph.build([self.dir])
        expected = ["m.py:4", "m.py:5", "m.py:6"]
        self.assertEqual([loc for loc, _ in codegraph.sites(cold, "m.helper")], expected)
        self.assertEqual([loc for loc, _ in codegraph.sites(warm, "m.helper")], expected)


class TheHandWrittenWalkMatchesTheStandardOne(unittest.TestCase):
    """`ast.iter_child_nodes` is a generator wrapping `iter_fields`, which is another generator
    with a try/except per field, and five million nodes pay for both. Both walks here read the
    fields directly instead - which is only safe while they yield exactly what the standard one
    yields, in exactly the same order. Order matters: two definitions of one name resolve to
    whichever the walk reaches LAST."""

    def children_the_fast_way(self, node):
        out = []
        for f in node._fields:
            v = getattr(node, f, None)
            if type(v) is list:
                out += [x for x in v if isinstance(x, ast.AST)]
            elif isinstance(v, ast.AST):
                out.append(v)
        return out

    def assert_same_walk(self, source, label):
        tree = ast.parse(source)
        seen = 0
        for node in ast.walk(tree):
            seen += 1
            self.assertEqual([id(c) for c in self.children_the_fast_way(node)],
                             [id(c) for c in ast.iter_child_nodes(node)],
                             f"{label}: {type(node).__name__} yields different children")
        return seen

    def test_over_this_tools_own_source(self):
        with open(os.path.join(HERE, "codegraph.py"), encoding="utf-8") as f:
            n = self.assert_same_walk(f.read(), "codegraph.py")
        self.assertGreater(n, 5000, "that fixture was too small to mean anything")

    def test_over_its_own_test_suite(self):
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            n = self.assert_same_walk(f.read(), "the tests")
        # The count, not just the absence of a failure. A comparison that walks nothing
        # reports the same silence as a comparison that agrees, and only one of those is
        # evidence - the sibling above has checked it from the start and these two did not.
        self.assertGreater(n, 5000, "that walk covered almost nothing")

    def test_over_syntax_this_file_does_not_happen_to_contain(self):
        exotic = ("async def a(x: int = 1, *args, k: str = 'v', **kw) -> bool:\n"
                  "    async with open('x') as fh:\n"
                  "        async for line in fh:\n"
                  "            yield [i async for i in fh if i]\n"
                  "    return await b(*args, **kw)\n"
                  "\n"
                  "class C(dict, metaclass=type):\n"
                  "    x: int = 0\n"
                  "    def m(self):\n"
                  "        try:\n"
                  "            del self.x\n"
                  "        except (KeyError, AttributeError) as e:\n"
                  "            raise RuntimeError from e\n"
                  "        finally:\n"
                  "            pass\n"
                  "        return {k: v for k, v in ()}, {j for j in ()}, (g for g in ())\n"
                  "\n"
                  "f = lambda *a, **k: (n := 1) and a[1:2, ...]\n"
                  "assert f, 'x'\n"
                  "global_var: dict = {**{}, 'a': f'{1!r:>{2}}'}\n")
        if sys.version_info >= (3, 10):
            exotic += ("def g(v):\n"
                       "    match v:\n"
                       "        case [1, *rest] | {'k': _} if rest:\n"
                       "            return rest\n"
                       "        case C(x=0) as got:\n"
                       "            return got\n")
        n = self.assert_same_walk(exotic, "exotic syntax")
        self.assertGreater(n, 40, "that fixture was too small to mean anything")

    def test_a_redefined_name_still_resolves_to_the_last_one(self):
        """The observable consequence of walk order, checked end to end."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, "m.py"), "w", encoding="utf-8") as f:
            f.write("def pick():\n    return 'first'\n"
                    "def pick():\n    return 'second'\n"
                    "def use():\n    return pick()\n")
        g = codegraph.build([d], write=False)
        node, = [n for n in g["nodes"] if n["id"] == "m.pick"]
        self.assertEqual(node["line"], 3, "the live definition is the last one")
        self.assertEqual(node.get("shadows"), [1])


class EveryWayPythonBindsAName(unittest.TestCase):
    """The pre-scan decides what shadows an imported module, and it now dispatches on the exact
    node type through a table. A table can lose an entry silently - and the failure would be a
    name that stops shadowing, which does not crash, it just answers wrongly. So: every binding
    form Python has, in one function, checked one by one."""

    FORMS = (
        ("parameter",            "def f(bound):\n    pass\n"),
        ("keyword-only",         "def f(*, bound):\n    pass\n"),
        ("positional-only",      "def f(bound, /):\n    pass\n"),
        ("star-args",            "def f(*bound):\n    pass\n"),
        ("star-kwargs",          "def f(**bound):\n    pass\n"),
        ("assignment",           "def f():\n    bound = 1\n"),
        ("annotated assignment", "def f():\n    bound: int = 1\n"),
        ("augmented assignment", "def f():\n    bound += 1\n"),
        ("tuple unpacking",      "def f():\n    (bound, x) = (1, 2)\n"),
        ("starred unpacking",    "def f():\n    a, *bound = [1, 2]\n"),
        ("for target",           "def f():\n    for bound in []:\n        pass\n"),
        ("async for target",     "async def f():\n    async for bound in []:\n        pass\n"),
        ("with target",          "def f():\n    with open('x') as bound:\n        pass\n"),
        ("except target",        "def f():\n    try:\n        pass\n"
                                 "    except ValueError as bound:\n        pass\n"),
        ("walrus",               "def f():\n    if (bound := 1):\n        pass\n"),
        ("del",                  "def f():\n    del bound\n"),
        # `import` is the deliberate exception - see the test below it.
        ("nested def",           "def f():\n    def bound():\n        pass\n"),
        ("nested class",         "def f():\n    class bound:\n        pass\n"),
    )
    MATCH_FORMS = (
        ("match capture",        "def f(v):\n    match v:\n        case [bound]:\n            pass\n"),
        ("match as",             "def f(v):\n    match v:\n        case str() as bound:\n            pass\n"),
        ("match rest",           "def f(v):\n    match v:\n        case {**bound}:\n            pass\n"),
    )

    def bindings(self, source):
        fn = ast.parse(source).body[0]
        values, defs = codegraph._bound_names(fn)
        return values | defs

    def test_every_form_binds(self):
        forms = list(self.FORMS)
        if sys.version_info >= (3, 10):
            forms += list(self.MATCH_FORMS)
        for label, source in forms:
            with self.subTest(label):
                self.assertIn("bound", self.bindings(source), f"{label} stopped binding")
        self.assertGreaterEqual(len(forms), 18)

    def test_an_import_binds_the_name_but_does_not_shadow_the_module(self):
        """The one deliberate exception: `import bound` inside a function binds `bound` to the
        MODULE, which is the binding resolution follows rather than one it must refuse."""
        values, _defs = codegraph._bound_names(ast.parse("def f():\n    import bound\n").body[0])
        self.assertNotIn("bound", values)

    def test_a_name_only_read_binds_nothing(self):
        self.assertNotIn("bound", self.bindings("def f():\n    return bound + bound.attr\n"))

    def test_a_comprehension_variable_does_not_leak(self):
        self.assertNotIn("bound", self.bindings("def f(rows):\n    return [r for bound in rows]\n"))

    def test_a_walrus_inside_a_comprehension_does_leak(self):
        """PEP 572 says it binds in the CONTAINING scope, which is why a comprehension cannot
        simply be skipped whole."""
        self.assertIn("bound", self.bindings("def f(rows):\n    return [(bound := r) for r in rows]\n"))

    def test_global_takes_a_name_back_out(self):
        self.assertNotIn("bound", self.bindings("def f():\n    global bound\n    bound = 1\n"))


class ResolutionDoesNotDependOnEdgeOrder(Sandbox):
    """The per-file lookups are refreshed only when the module changes, which is fast because
    edges arrive grouped by the file they came from. If they ever stop arriving that way - a
    parallel build, a different append order - the cheap version must still be the correct one,
    not merely the fast one."""

    def tree(self):
        self.write("config.py", "def dumps(x):\n    return 1\n")
        self.write("other.py", "def dumps(x):\n    return 2\n")
        self.write("a.py", "import config\n\ndef ga():\n    return config.dumps(1)\n")
        self.write("b.py", "import other\n\ndef gb():\n    return other.dumps(2)\n")
        self.write("c.py", "import config\n\ndef gc():\n    return config.dumps(3)\n")

    def test_interleaved_edges_resolve_the_same_way(self):
        self.tree()
        grouped = codegraph.build([self.dir], write=False)
        want = {(e["src"], e.get("dst")) for e in grouped["calls"] if e["callee"] == "dumps"}
        self.assertEqual(want, {("a.ga", "config.dumps"), ("b.gb", "other.dumps"),
                                ("c.gc", "config.dumps")})

        real_defs = codegraph._defs_and_calls

        def shuffled(path, mid):
            out = list(real_defs(path, mid))
            out[1] = list(reversed(out[1]))          # edges out of one file, back to front
            return tuple(out)

        codegraph._defs_and_calls = shuffled
        try:
            jumbled = codegraph.build([self.dir], write=False)
        finally:
            codegraph._defs_and_calls = real_defs
        got = {(e["src"], e.get("dst")) for e in jumbled["calls"] if e["callee"] == "dumps"}
        self.assertEqual(got, want, "the answer changed with the order the edges arrived in")


class CheckedAgainstTheInterpretersOwnSymbolTable(unittest.TestCase):
    """The pre-scan decides what shadows what, and everything downstream trusts it. Rather than
    check it against more of my own reasoning, check it against CPython's: `symtable` is the
    compiler's real scope analysis, written in C, and it answers the same question.

    Skipped from 3.12 on, where PEP 709 inlines comprehensions and the symbol table starts
    reporting a function's locals differently from the semantics this models. The oracle has to
    be checked before it can be believed - on 3.14 it reports a scope's locals as `['.format']`.
    """

    def scope_pairs(self, node, st):
        kids = list(st.get_children())
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                want = "class" if isinstance(child, ast.ClassDef) else "function"
                match = [k for k in kids
                         if k.get_name() == child.name and k.get_type() == want]
                if len(match) == 1:                  # a name defined twice is not worth guessing
                    yield child, match[0]
                    yield from self.scope_pairs(child, match[0])

    def compare(self, source, label):
        """Returns the names CPython binds in a scope that the pre-scan does not."""
        import symtable
        tree = ast.parse(source)
        st = symtable.symtable(source, label, "exec")
        holes, scopes = [], 0
        for node, scope in self.scope_pairs(tree, st):
            if scope.get_type() == "class":
                continue                              # class scope follows its own rules
            scopes += 1
            values, defs = codegraph._bound_names(node)
            mine = values | defs
            for sym in scope.get_symbols():
                if not (sym.is_local() or sym.is_parameter()):
                    continue
                if sym.is_imported() or sym.is_declared_global():
                    continue                          # bound to a module, or to another scope
                if sym.get_name() not in mine:
                    holes.append((label, node.name, sym.get_name()))
        return holes, scopes

    def sources(self):
        """This repository, and then as much of the running interpreter's own library as fits
        in a second - real code nobody wrote for this tool."""
        import sysconfig
        yield os.path.join(HERE, "codegraph.py")
        yield os.path.abspath(__file__)
        lib = sysconfig.get_paths()["stdlib"]
        got = 0
        for name in sorted(os.listdir(lib)):
            if got >= 120:
                break
            full = os.path.join(lib, name)
            if name.endswith(".py") and os.path.isfile(full):
                got += 1
                yield full

    def test_it_misses_nothing_cpython_binds(self):
        if sys.version_info >= (3, 12):
            self.skipTest("PEP 709 changed what symtable reports as a function's locals")
        holes, scopes, files = [], 0, 0
        for path in self.sources():
            try:
                with open(path, encoding="utf-8-sig", errors="replace") as f:
                    src = f.read()
                found, n = self.compare(src, os.path.basename(path))
            except (SyntaxError, ValueError, RecursionError, OSError):
                continue
            files += 1
            holes += found
            scopes += n
        self.assertGreater(scopes, 500, f"only {scopes} scopes over {files} files - too few to mean anything")
        self.assertEqual(holes[:8], [], f"{len(holes)} names CPython binds and the pre-scan does not")

    def test_the_oracle_is_the_one_i_think_it_is(self):
        """A control. If symtable ever stops reporting an ordinary local, the test above would
        pass by finding nothing to disagree with."""
        if sys.version_info >= (3, 12):
            self.skipTest("PEP 709 changed what symtable reports as a function's locals")
        import symtable
        src = "def f(p):\n    loc = 1\n    for tgt in []:\n        pass\n    return p, loc, tgt\n"
        fn = symtable.symtable(src, "m", "exec").get_children()[0]
        locals_ = {s.get_name() for s in fn.get_symbols() if s.is_local() or s.is_parameter()}
        self.assertEqual(locals_, {"p", "loc", "tgt"})
        holes, scopes = self.compare(src, "m")
        self.assertEqual((holes, scopes), ([], 1))

    def test_a_name_both_imported_and_reassigned_is_treated_as_local(self):
        """The only shape the two ever disagreed on, across fifteen thousand scopes. CPython
        marks it imported; it is also assigned, so within that function the name is a variable
        and the pre-scan is right to say so."""
        values, _defs = codegraph._bound_names(ast.parse(
            "def f(msg):\n"
            "    import linecache\n"
            "    line = linecache.getline(msg)\n"
            "    linecache = None\n"
            "    return line, linecache\n").body[0])
        self.assertIn("linecache", values)


class ABaseClassIsANameLikeAnyOther(Sandbox):
    """Every other class name goes through one rule: the class this file defines or imported,
    before any tree-wide search. A BASE was matched by name across the whole tree with no test
    of whether the file had heard of it - so pandas' `class _BytesTarFile(io.BytesIO)` was given
    pip's vendored msgpack BytesIO as a parent, and `class CMakeExtension(Extension)` inherited
    from cryptography's x509 Extension. Wrong, confident, and then feeding the method lookup,
    super() and the constructor edges."""

    def dst(self, g, src, callee):
        m = [e for e in g["calls"] if e["src"] == src and e["callee"] == callee]
        self.assertEqual(len(m), 1, m)
        return m[0].get("dst")

    def test_a_base_from_outside_the_tree_is_not_a_class_inside_it(self):
        self.write("vendored.py", "class BytesIO:\n"
                                  "    def getvalue(self):\n        return 'the wrong parent'\n")
        self.write("app.py", "from io import BytesIO\n"
                             "\n"
                             "class Buffer(BytesIO):\n"
                             "    def use(self):\n"
                             "        return self.getvalue()\n")
        g = self.graph(write=False)
        self.assertIsNone(self.dst(g, "app.Buffer.use", "getvalue"))
        self.assertEqual(codegraph.callers_of(g, "vendored.BytesIO.getvalue"), [])

    def test_an_imported_base_wins_even_when_the_name_is_ambiguous(self):
        """Two classes called Backend, and a relative import that says which. The tree-wide
        rule gave up here, because it only ever fired on a unique name."""
        self.write(os.path.join("pkg", "__init__.py"), "")
        self.write(os.path.join("pkg", "_backend.py"),
                   "class Backend:\n    def run(self):\n        return 'the right one'\n")
        self.write("other.py", "class Backend:\n    def run(self):\n        return 'a decoy'\n")
        self.write(os.path.join("pkg", "impl.py"),
                   "from ._backend import Backend\n"
                   "\n"
                   "class Meson(Backend):\n"
                   "    def go(self):\n"
                   "        return self.run()\n")
        g = self.graph(write=False)
        self.assertEqual(self.dst(g, "pkg/impl.Meson.go", "run"), "pkg/_backend.Backend.run")
        self.assertEqual(codegraph.callers_of(g, "other.Backend.run"), [])

    def test_a_base_in_the_same_module_still_resolves(self):
        self.write("m.py", "class Parent:\n    def run(self):\n        return 1\n"
                           "class Child(Parent):\n    def go(self):\n        return self.run()\n")
        self.assertEqual(self.dst(self.graph(write=False), "m.Child.go", "run"), "m.Parent.run")

    def test_a_unique_name_nobody_imported_still_resolves(self):
        """The guard: the tree-wide fallback is still there for a file that names a base it
        never imported - which is what a package's own __init__ re-export looks like from
        here. It only stops being used when the file said where the name came from."""
        self.write("base.py", "class Solo:\n    def run(self):\n        return 1\n")
        self.write("app.py", "class Child(Solo):\n    def go(self):\n        return self.run()\n")
        self.assertEqual(self.dst(self.graph(write=False), "app.Child.go", "run"), "base.Solo.run")

    def test_super_and_the_constructor_edge_follow_the_corrected_parent(self):
        """The reason a wrong base matters: three other answers are built on top of it."""
        self.write("vendored.py", "class Handler:\n"
                                  "    def __init__(self):\n        pass\n")
        self.write("app.py", "from logging import Handler\n"
                             "\n"
                             "class Mine(Handler):\n"
                             "    def __init__(self):\n"
                             "        super().__init__()\n"
                             "\n"
                             "def make():\n"
                             "    return Mine()\n")
        g = self.graph(write=False)
        self.assertEqual(codegraph.callers_of(g, "vendored.Handler.__init__"), [])
        self.assertEqual(sorted(codegraph.callers_of(g, "app.Mine.__init__")), ["app.make"])


class AQualifiedBaseIsStillABase(Sandbox):
    """`class Mine(logging.Handler)` is as ordinary as the bare form, and only bare Names were
    collected - so a qualified base produced no parent at all, and every method inherited
    through it went unresolved."""

    def test_a_dotted_base_gives_the_class_its_parent(self):
        self.write(os.path.join("pkg", "__init__.py"), "")
        self.write(os.path.join("pkg", "core.py"),
                   "class Handler:\n    def emit(self):\n        return 1\n")
        self.write("app.py", "from pkg import core\n"
                             "\n"
                             "class Mine(core.Handler):\n"
                             "    def go(self):\n"
                             "        return self.emit()\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.Mine.go" and e["callee"] == "emit"]
        self.assertEqual((call.get("dst"), call["confidence"]), ("pkg/core.Handler.emit", "INHERITED"))

    def test_a_dotted_base_from_outside_the_tree_invents_nothing(self):
        self.write("decoy.py", "class Handler:\n    def emit(self):\n        return 'wrong'\n")
        self.write("app.py", "import logging\n"
                             "\n"
                             "class Mine(logging.Handler):\n"
                             "    def go(self):\n"
                             "        return self.emit()\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.Mine.go"]
        self.assertIsNone(call.get("dst"))
        self.assertEqual(codegraph.callers_of(g, "decoy.Handler.emit"), [])

    def test_a_subscripted_base_is_not_mistaken_for_a_class(self):
        """`class Mine(Generic[T])` names Generic, not T, and a Subscript is not a name at
        all - the same reason `Dict[str, Client]` is not a Client."""
        self.write("app.py", "from typing import Generic, TypeVar\n"
                             "T = TypeVar('T')\n"
                             "\n"
                             "class Mine(Generic[T]):\n"
                             "    def go(self):\n"
                             "        return 1\n")
        g = self.graph(write=False)
        node, = [n for n in g["nodes"] if n["id"] == "app.Mine"]
        self.assertEqual(node.get("bases"), [])


class AVariableKnowsTheClassBesideIt(Sandbox):
    """Two holes in local type inference, both found on a corpus of installed packages.

    pdfminer defines `class Parser` inside main() and builds one two lines later; the variable
    was typed as PILLOW's Parser, because an earlier round stopped OTHER functions reaching a
    nested class and never taught the owning one that it could. And `x = Sub(); x.method()`
    found nothing whenever the method lived on the parent - which in ordinary hierarchies is
    most of them - although the constructor edge had walked the MRO for six rounds."""

    def dst(self, g, src, callee):
        m = [e for e in g["calls"] if e["src"] == src and e["callee"] == callee]
        self.assertEqual(len(m), 1, m)
        return m[0].get("dst")

    def test_a_class_defined_in_this_function_is_the_one_meant(self):
        self.write("far.py", "class Parser:\n    def close(self):\n        return 'the wrong one'\n")
        self.write("app.py", "def main():\n"
                             "    class Parser:\n"
                             "        def close(self):\n"
                             "            return 'the right one'\n"
                             "    p = Parser()\n"
                             "    return p.close()\n")
        g = self.graph(write=False)
        self.assertEqual(self.dst(g, "app.main", "close"), "app.main.Parser.close")
        self.assertEqual(codegraph.callers_of(g, "far.Parser.close"), [])

    def test_another_function_still_cannot_reach_it(self):
        """The guard, and the rule this sits beside: nested means nested."""
        self.write("app.py", "def main():\n"
                             "    class Parser:\n"
                             "        def close(self):\n"
                             "            return 1\n"
                             "    return Parser()\n"
                             "\n"
                             "def elsewhere():\n"
                             "    p = Parser()\n"
                             "    return p.close()\n")
        g = self.graph(write=False)
        self.assertIsNone(self.dst(g, "app.elsewhere", "close"))
        self.assertEqual(codegraph.callers_of(g, "app.main.Parser.close"), [])

    def test_a_method_does_not_have_to_be_on_the_class_itself(self):
        self.write("m.py", "class Base:\n"
                           "    def feed(self, x):\n        return x\n"
                           "\n"
                           "class Sub(Base):\n"
                           "    pass\n"
                           "\n"
                           "def use():\n"
                           "    s = Sub()\n"
                           "    return s.feed(1)\n")
        g = self.graph(write=False)
        self.assertEqual(self.dst(g, "m.use", "feed"), "m.Base.feed")
        self.assertEqual(codegraph.callers_of(g, "m.Base.feed"), ["m.use"])

    def test_an_override_wins_over_what_it_overrides(self):
        """The MRO, not merely "somewhere up the chain"."""
        self.write("m.py", "class Base:\n"
                           "    def feed(self, x):\n        return 'base'\n"
                           "\n"
                           "class Sub(Base):\n"
                           "    def feed(self, x):\n        return 'sub'\n"
                           "\n"
                           "def use():\n"
                           "    s = Sub()\n"
                           "    return s.feed(1)\n")
        g = self.graph(write=False)
        self.assertEqual(self.dst(g, "m.use", "feed"), "m.Sub.feed")
        self.assertEqual(codegraph.callers_of(g, "m.Base.feed"), [])

    def test_a_method_on_no_parent_at_all_stays_unresolved(self):
        """The guard: walking the parents must not turn into inventing one."""
        self.write("m.py", "class Lonely:\n"
                           "    pass\n"
                           "\n"
                           "def use():\n"
                           "    s = Lonely()\n"
                           "    return s.feed(1)\n")
        g = self.graph(write=False)
        self.assertIsNone(self.dst(g, "m.use", "feed"))

    def test_two_classes_of_one_name_are_still_refused(self):
        self.write(os.path.join("a", "__init__.py"), "")
        self.write(os.path.join("b", "__init__.py"), "")
        self.write(os.path.join("a", "svc.py"), "class Base:\n    def feed(self):\n        return 1\n"
                                                "class Client(Base):\n    pass\n")
        self.write(os.path.join("b", "svc.py"), "class Base:\n    def feed(self):\n        return 2\n"
                                                "class Client(Base):\n    pass\n")
        self.write("app.py", "def use():\n    c = Client()\n    return c.feed()\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.use" and e["callee"] == "feed"]
        self.assertEqual(call["confidence"], "AMBIGUOUS")
        self.assertEqual(call["candidates"], ["a/svc.Base.feed", "b/svc.Base.feed"])


class ANameThisFileNeverMentionsIsNotYours(Sandbox):
    """There used to be one more label: a bare call matched against every definition in the
    tree, resolving whenever exactly one of them had that name. That is a coincidence, not a
    resolution - `turtle` builds up(), down(), left() and right() at import time rather than
    defining them, and those calls were answered with functions in `_pyrepl.commands`."""

    def test_a_bare_call_to_a_name_from_nowhere_is_external(self):
        self.write("far.py", "def orphan():\n    return 'the only one of its name'\n")
        self.write("app.py", "def go():\n    return orphan()\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.go"]
        self.assertIsNone(call.get("dst"))
        self.assertEqual(call["confidence"], "EXTERNAL")
        self.assertEqual(codegraph.callers_of(g, "far.orphan"), [])

    def test_the_four_ways_a_bare_name_does_reach_something(self):
        """The guard, and the reason the fallback could go: every real route has its own rule."""
        self.write("lib.py", "def imported():\n    return 1\n")
        self.write("starred.py", "def starry():\n    return 1\n")
        self.write("app.py", "from lib import imported\n"
                             "from starred import *\n"
                             "\n"
                             "def same_module():\n    return 1\n"
                             "\n"
                             "def go():\n"
                             "    def nested():\n        return 1\n"
                             "    return imported() + starry() + same_module() + nested()\n")
        g = self.graph(write=False)
        got = {e["callee"]: (e.get("dst"), e["confidence"])
               for e in g["calls"] if e["src"] == "app.go"}
        self.assertEqual(got["imported"], ("lib.imported", "QUALIFIED"))
        self.assertEqual(got["starry"], ("starred.starry", "QUALIFIED"))
        self.assertEqual(got["same_module"], ("app.same_module", "LOCAL"))
        self.assertEqual(got["nested"], ("app.go.nested", "LOCAL"))

    def test_a_builtin_is_still_a_builtin(self):
        self.write("app.py", "def go():\n    return len([1]) + sorted([2])[0]\n")
        g = self.graph(write=False)
        self.assertEqual({e["confidence"] for e in g["calls"] if e["src"] == "app.go"},
                         {"BUILTIN"})


class ABaseThatIsAVariableIsNotABase(Sandbox):
    """`V = Visitor[List[int]]` and then `class IntListVisitor(V)` is ordinary generic code, and
    V is a value. It was matched against every class in the tree and given a parent in an
    unrelated module - which then supplied a constructor edge and a method lookup. Calls have
    refused shadowed names from the beginning; bases never asked."""

    def test_a_base_bound_as_a_local_resolves_to_nothing(self):
        self.write("far.py", "class Alias:\n"
                             "    def __init__(self):\n        pass\n"
                             "    def run(self):\n        return 'the wrong parent'\n")
        self.write("app.py", "def make():\n"
                             "    Alias = dict\n"
                             "    class Mine(Alias):\n"
                             "        def go(self):\n"
                             "            return self.run()\n"
                             "    return Mine()\n")
        g = self.graph(write=False)
        node, = [n for n in g["nodes"] if n["id"] == "app.make.Mine"]
        self.assertEqual(node.get("bases"), [])
        self.assertEqual(codegraph.callers_of(g, "far.Alias.run"), [])
        self.assertEqual(codegraph.callers_of(g, "far.Alias.__init__"), [])

    def test_a_base_bound_at_module_level_is_refused_too(self):
        self.write("far.py", "class Built:\n    def run(self):\n        return 1\n")
        self.write("app.py", "Built = make_base()\n"
                             "\n"
                             "class Mine(Built):\n"
                             "    def go(self):\n"
                             "        return self.run()\n")
        g = self.graph(write=False)
        mine, = [n for n in g["nodes"] if n["id"] == "app.Mine"]
        self.assertEqual(mine.get("bases"), [])
        self.assertEqual(codegraph.callers_of(g, "far.Built.run"), [])

    def test_an_ordinary_base_is_untouched(self):
        self.write("base.py", "class Real:\n    def run(self):\n        return 1\n")
        self.write("app.py", "from base import Real\n"
                             "\n"
                             "class Mine(Real):\n"
                             "    def go(self):\n"
                             "        return self.run()\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.Mine.go" and e["callee"] == "run"]
        self.assertEqual((call.get("dst"), call["confidence"]), ("base.Real.run", "INHERITED"))


class AnImportWrittenAsACallIsStillAnImport(Sandbox):
    """`import_module("curses.textpad")` is an import with a different spelling. A plugin
    loader, a lazy import, or a test reaching for an optional module does exactly what an
    import statement does, and `deps` and `cycles` could not see any of it."""

    def test_a_literal_dynamic_import_is_recorded(self):
        self.write(os.path.join("pkg", "__init__.py"), "")
        self.write(os.path.join("pkg", "widget.py"), "def draw():\n    return 1\n")
        self.write("app.py", "from importlib import import_module\n"
                             "\n"
                             "def load():\n"
                             "    return import_module('pkg.widget')\n")
        g = self.graph(write=False)
        self.assertIn("pkg/widget", codegraph.module_deps(g, "app")[0])
        self.assertEqual(codegraph.module_deps(g, "pkg/widget")[1], ["app"])

    def test_the_old_two_argument_form_counts(self):
        self.write("plain.py", "def work():\n    return 1\n")
        self.write("app.py", "def load():\n    return __import__('plain')\n")
        g = self.graph(write=False)
        self.assertIn("plain", codegraph.module_deps(g, "app")[0])

    def test_a_name_that_is_not_a_literal_is_not_guessed(self):
        """The guard: a variable is a runtime decision and nobody can read it from here."""
        self.write("plain.py", "def work():\n    return 1\n")
        self.write("app.py", "from importlib import import_module\n"
                             "\n"
                             "def load(which):\n"
                             "    return import_module(which)\n")
        g = self.graph(write=False)
        self.assertEqual(codegraph.module_deps(g, "app")[0], [])

    def test_a_module_that_is_not_in_the_tree_adds_no_edge(self):
        self.write("app.py", "from importlib import import_module\n"
                             "\n"
                             "def load():\n"
                             "    return import_module('json.decoder')\n")
        g = self.graph(write=False)
        self.assertEqual(codegraph.module_deps(g, "app")[0], [])

    def test_a_dynamic_import_can_complete_a_cycle(self):
        """The point of recording it: a loader that reaches back into its own package is a
        real cycle, and it was invisible."""
        self.write("a.py", "from importlib import import_module\n"
                           "b = import_module('b')\n"
                           "def ga():\n    return 1\n")
        self.write("b.py", "import a\ndef gb():\n    return 1\n")
        self.assertEqual(codegraph.cycles(self.graph(write=False)), [("a", "b")])


class TheGraphCarriesWhatAQuestionNeeds(Sandbox):
    """An edge accumulates a dozen working fields on the way to a target - whether the receiver
    was a local, the root of a dotted chain, the enclosing class, the inferred type - and none
    of them is read once the target is known. They were written to disk, loaded back on every
    query, and held in memory through two serialisations."""

    # `method` is NOT here: whether a call was written `f()` or `x.f()` is a fact about the
    # call site, and nothing downstream can tell the two apart without it - `recv` cannot, being
    # None for a bare call and for `get_thing().f()` alike.
    WORKING = ("recv_root", "recv_path", "recv_local", "callee_local", "kind",
               "encl_class", "recv_type", "invoke_type", "super_of", "super_from")

    def tree(self):
        self.write("svc.py", "class Client:\n"
                             "    def __init__(self):\n        pass\n"
                             "    def get(self):\n        return 1\n")
        self.write("app.py", "import svc\n"
                             "from svc import Client\n"
                             "\n"
                             "class Mine(Client):\n"
                             "    def go(self):\n"
                             "        return super().get()\n"
                             "\n"
                             "def use(c: Client):\n"
                             "    x = svc.Client()\n"
                             "    return x.get() + c.get()\n")

    def test_the_working_fields_do_not_reach_the_graph(self):
        self.tree()
        g = self.graph(write=False)
        self.assertTrue(g["calls"], "no edges to check")
        for e in g["calls"]:
            for field in self.WORKING:
                self.assertNotIn(field, e, f"{field} survived into the graph: {e}")

    def test_everything_a_query_reads_is_still_there(self):
        """The other half. Dropping a field a verb needs would be a silent empty answer."""
        self.tree()
        g = self.graph(write=False)
        self.assertEqual(codegraph.callers_of(g, "svc.Client.get"),
                         ["app.Mine.go", "app.use"])
        self.assertEqual(codegraph.callers_of(g, "svc.Client.__init__"), ["app.use"])
        self.assertTrue(codegraph.sites(g, "svc.Client.get"))
        self.assertTrue(codegraph.impact(g, "svc.Client.get")["blast"])
        self.assertEqual(codegraph.stats(g)["nodes"]["class"], 2)
        labels = {e["confidence"] for e in g["calls"]}
        self.assertIn("TYPED", labels)
        self.assertIn("INHERITED", labels)
        self.assertIn("CONSTRUCTOR", labels)

    def test_the_cache_keeps_them_because_it_replays_resolution(self):
        """The cache is the input to a later resolution, so it needs the working fields the
        graph does not - a second build reads them back and must reach the same answers."""
        self.tree()
        codegraph.build([self.dir])
        with open(codegraph.CACHE, encoding="utf-8") as f:
            cached = json.load(f)
        edges = [e for entry in cached["files"].values() for e in entry["calls"]]
        self.assertTrue(any("recv_local" in e for e in edges), "the cache lost the working fields")
        warm = codegraph.build([self.dir])
        self.assertEqual(codegraph.callers_of(warm, "svc.Client.get"),
                         ["app.Mine.go", "app.use"])

    def test_an_ambiguous_edge_still_names_its_candidates(self):
        """`candidates` is the one working field that IS an answer."""
        self.write("a.py", "def go():\n    return 1\n")
        self.write("b.py", "def go():\n    return 2\n")
        self.write("c.py", "from a import *\nfrom b import *\ndef run():\n    return go()\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "c.run"]
        self.assertEqual(call["candidates"], ["a.go", "b.go"])


class UntypedMeansItMightBeYours(Sandbox):
    """UNTYPED is a claim, not a shrug: the receiver could not be typed, so the target MIGHT be
    in this tree. For `rows.append(x)` or `text.strip()` that claim is false and the tool can
    prove it - no definition anywhere carries that name. On one real codebase nine in ten
    "cannot tell" edges were `.get()`, `.items()`, `.join()` and `.assertEqual()`."""

    def label(self, g, callee):
        m = [e for e in g["calls"] if e["callee"] == callee and e["src"] == "app.go"]
        self.assertEqual(len(m), 1, m)
        return m[0]["confidence"]

    def test_a_method_no_definition_here_has_is_external(self):
        self.write("app.py", "def go(rows, text):\n"
                             "    rows.append(1)\n"
                             "    return text.strip()\n")
        g = self.graph(write=False)
        self.assertEqual(self.label(g, "append"), "EXTERNAL")
        self.assertEqual(self.label(g, "strip"), "EXTERNAL")

    def test_a_method_something_here_does_define_stays_untyped(self):
        """The other half, and the whole point of keeping the two labels apart: this one could
        genuinely be the tree's own, and saying EXTERNAL would claim knowledge."""
        self.write("store.py", "class Store:\n    def append(self, x):\n        return x\n")
        self.write("app.py", "def go(rows, text):\n"
                             "    rows.append(1)\n"
                             "    return text.strip()\n")
        g = self.graph(write=False)
        self.assertEqual(self.label(g, "append"), "UNTYPED")
        self.assertEqual(self.label(g, "strip"), "EXTERNAL")

    def test_a_bare_call_on_a_local_is_not_reclassified(self):
        """`handler = pick(); handler()` - the name written is the VARIABLE's, and says nothing
        about what it holds, which may very well be in this tree."""
        self.write("app.py", "def pick():\n    return None\n"
                             "\n"
                             "def go():\n"
                             "    handler = pick()\n"
                             "    return handler()\n")
        g = self.graph(write=False)
        call, = [e for e in g["calls"] if e["src"] == "app.go" and e["callee"] == "handler"]
        self.assertEqual(call["confidence"], "UNTYPED")

    def test_the_rate_counts_only_what_could_have_been_won(self):
        """A tree of nothing but list and string calls has no winnable calls to lose."""
        self.write("app.py", "def helper():\n    return 1\n"
                             "\n"
                             "def go(rows):\n"
                             "    rows.append(1)\n"
                             "    rows.sort()\n"
                             "    return helper()\n")
        s = codegraph.stats(self.graph(write=False))
        self.assertEqual(s["could_have_been_resolved"], 1)
        self.assertEqual(s["resolution_rate"], 1.0)


class TheReadmeListsEveryLabel(unittest.TestCase):
    """The label table is the front page's whole claim, and it drifts: it once listed `TYPED`
    twice, described `LOCAL` as same-module only after it learned to see enclosing functions,
    still called `EXTERNAL` "a method on an object it cannot type" after that became `UNTYPED`,
    and omitted `BUILTIN` and `UNTYPED` altogether. Nothing was checking it."""

    def readme_labels(self):
        with open(os.path.join(HERE, "README.md"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("| label |")
        end = text.index("\n\n", start)
        rows = [r for r in text[start:end].splitlines() if r.startswith("| `")]
        return {re.match(r"\| `([A-Z-]+)`", r).group(1) for r in rows}

    def test_the_table_and_the_tool_agree(self):
        known = TheGraphKeepsItsOwnInvariants.LABELS
        listed = self.readme_labels()
        self.assertEqual(listed - known, set(), "the README lists a label the tool cannot emit")
        self.assertEqual(known - listed, set(), "the tool emits a label the README omits")

    def test_each_label_is_listed_once(self):
        with open(os.path.join(HERE, "README.md"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("| label |")
        rows = [r for r in text[start:text.index("\n\n", start)].splitlines() if r.startswith("| `")]
        names = [re.match(r"\| `([A-Z-]+)`", r).group(1) for r in rows]
        self.assertEqual(sorted(names), sorted(set(names)), "a label is in the table twice")

    def test_the_table_says_which_ones_resolve(self):
        """A reader scanning it should not have to infer that from the prose."""
        with open(os.path.join(HERE, "README.md"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("| label |")
        rows = [r for r in text[start:text.index("\n\n", start)].splitlines() if r.startswith("| `")]
        answers = {re.match(r"\| `([A-Z-]+)`", r).group(1): r.rstrip("| ").rsplit("|", 1)[1].strip()
                   for r in rows}
        for label in TheGraphKeepsItsOwnInvariants.UNRESOLVED:
            self.assertEqual(answers[label], "no", label)
        for label in TheGraphKeepsItsOwnInvariants.LABELS - TheGraphKeepsItsOwnInvariants.UNRESOLVED:
            self.assertEqual(answers[label], "yes", label)


class TheCommandLineCanAnswerInData(unittest.TestCase):
    """Written after noticing that in a whole day of using this tool to investigate itself, the
    command line was never once used to do it - every question went through a Python one-liner
    reading codegraph.json directly. That is the tool saying something. It prints prose, and
    the thing asking was a program.

    The sharpest symptom: `impact` prints how MANY functions the blast radius holds and never
    which, although the library has returned the list all along."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.write("app.py", "def leaf():\n    return 1\n"
                             "def mid():\n    return leaf()\n"
                             "def top():\n    return mid()\n")
        self.write("other.py", "def leaf():\n    return 2\n")
        self.assertEqual(self.run_it("build", ".").returncode, 0)

    def write(self, name, body):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(body)

    def run_it(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), *args],
                              cwd=self.dir, capture_output=True, text=True, timeout=180)

    def as_json(self, *args):
        r = self.run_it(*args)
        try:
            return json.loads(r.stdout), r.returncode
        except json.JSONDecodeError:
            self.fail(f"{' '.join(args)} did not produce JSON:\n{r.stdout}\n{r.stderr}")

    def test_impact_names_the_blast_radius_it_only_counted(self):
        got, rc = self.as_json("impact", "app.leaf", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(got["target"], "app.leaf")
        self.assertEqual(got["callers"], ["app.mid"])
        self.assertEqual(got["blast"], ["app.mid", "app.top"])
        self.assertEqual(got["sites"], [{"at": "app.py:4", "caller": "app.mid"}])
        prose = self.run_it("impact", "app.leaf").stdout
        self.assertIn("2 functions", prose)
        self.assertNotIn("app.top", prose, "the prose names the count, not the members")

    def test_every_query_verb_answers_in_data(self):
        for args, want in (
            (("callers", "app.leaf"), {"query", "target", "results"}),
            (("calls", "app.mid"), {"query", "target", "results"}),
            (("blast", "app.leaf"), {"query", "target", "results"}),
            (("sites", "app.leaf"), {"query", "target", "results"}),
            (("where", "app.leaf"), {"query", "results"}),
            (("find", "lea"), {"query", "results"}),
            (("path", "app.top", "app.leaf"), {"query", "from", "to", "results"}),
            (("deps", "app"), {"query", "module", "imports", "importers"}),
            (("cycles",), {"query", "results"}),
        ):
            with self.subTest(args[0]):
                got, rc = self.as_json(*args, "--json")
                self.assertEqual(rc, 0)
                self.assertEqual(set(got), want)

    def test_a_refusal_is_data_too(self):
        """A misspelling and a function with no callers must never look alike, and telling
        them apart from prose means matching on an English sentence."""
        amb, rc = self.as_json("callers", "leaf", "--json")
        self.assertEqual(rc, 2)
        self.assertEqual(amb["error"], "ambiguous")
        self.assertEqual([c["id"] for c in amb["candidates"]], ["app.leaf", "other.leaf"])

        unknown, rc = self.as_json("callers", "nosuchname", "--json")
        self.assertEqual(rc, 1)
        self.assertEqual(unknown["error"], "unknown name")

        mod, rc = self.as_json("callers", "app", "--json")
        self.assertEqual(rc, 1)
        self.assertEqual(mod["error"], "not a function")
        self.assertIn("deps app", mod["try"])

    def test_an_empty_answer_is_an_empty_list_not_a_word(self):
        """`(none)` is fine for a person and is a parsing problem for anything else."""
        got, rc = self.as_json("callers", "app.top", "--json")
        self.assertEqual((rc, got["results"]), (0, []))

    def test_the_flag_goes_where_you_type_it(self):
        before, _ = self.as_json("--json", "callers", "app.leaf")
        after, _ = self.as_json("callers", "app.leaf", "--json")
        self.assertEqual(before, after)

    def test_without_the_flag_nothing_changed(self):
        """The guard: prose is still prose, for the person who is reading it."""
        r = self.run_it("callers", "app.leaf")
        self.assertEqual(r.stdout.strip(), "app.mid")
        self.assertEqual(self.run_it("callers", "app.top").stdout.strip(), "(none)")
        self.assertEqual(self.run_it("callers", "leaf").returncode, 2)


class TheNeverCalledListWasOnlyEverACount(Sandbox):
    """`stats` has reported `never_called_in_tree` from the start and there was no way to SEE
    the list, which made the number useless - 332 of something you cannot enumerate is not a
    finding. Checking one real codebase's 44 by hand turned up zero dead functions: a dispatch
    table, names reached from a web page, two handlers the standard library's HTTP server calls
    by name, and a `__str__`. So the verb warns rather than accuses."""

    def test_it_lists_what_stats_only_counted(self):
        self.write("m.py", "def used():\n    return 1\n"
                           "def unused_one():\n    return 2\n"
                           "def caller():\n    return used()\n")
        g = self.graph(write=False)
        rows = codegraph.unused(g)
        self.assertEqual([i for i, _at, _d in rows], ["m.caller", "m.unused_one"])
        st = codegraph.stats(g)
        self.assertEqual(st["not_called_in_scanned_roots"]
                         + st["uncalled_but_reachable_by_name"], len(rows),
                         "the two counts have to add up to the list, or one of them is quiet")

    def test_it_says_where(self):
        self.write("m.py", "def alone():\n    return 1\n")
        (_id, at, _d), = codegraph.unused(self.graph(write=False))
        self.assertEqual(at, "m.py:1")

    def test_a_constructor_that_is_built_is_not_in_it(self):
        """The list is only worth reading because the calls that never write a name - an
        __init__, a super(), a __call__ - are edges now."""
        self.write("m.py", "class Client:\n"
                           "    def __init__(self):\n        pass\n"
                           "\n"
                           "def make():\n    return Client()\n")
        ids = [i for i, _a, _d in codegraph.unused(self.graph(write=False))]
        self.assertNotIn("m.Client.__init__", ids)
        self.assertIn("m.make", ids)

    def test_a_dunder_is_flagged_as_pythons_business(self):
        self.write("m.py", "class Thing:\n"
                           "    def __str__(self):\n        return 'x'\n"
                           "    def helper(self):\n        return 1\n")
        rows = {i: d for i, _a, d in codegraph.unused(self.graph(write=False))}
        self.assertTrue(rows["m.Thing.__str__"], "Python calls this one for you")
        self.assertFalse(rows["m.Thing.helper"])

    def test_the_caveat_reaches_the_person_reading_it(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, "m.py"), "w", encoding="utf-8") as f:
            f.write("def alone():\n    return 1\n")
        cg = os.path.join(HERE, "codegraph.py")
        subprocess.run([sys.executable, cg, "build", "."], cwd=d, capture_output=True, timeout=180)
        r = subprocess.run([sys.executable, cg, "unused"], cwd=d,
                           capture_output=True, text=True, timeout=180)
        self.assertIn("m.alone", r.stdout)
        self.assertIn("Still not\nproof of dead", r.stderr)
        self.assertIn("only saw the roots it was pointed at", r.stderr)
        j = subprocess.run([sys.executable, cg, "unused", "--json"], cwd=d,
                           capture_output=True, text=True, timeout=180)
        self.assertEqual(json.loads(j.stdout)["results"],
                         [{"id": "m.alone", "at": "m.py:1", "reached_by": None}])


class AnAnswerYouCanScope(Sandbox):
    """Written after using this on a real shipped repository for the first time rather than on
    a fixture. `unused` returned 337 lines of which 293 were test methods - unittest calls
    those by reflection, so every one looks dead - and `blast` was 17 tests out of 25. A list
    that is seven-eighths noise is one nobody reads twice."""

    def setUp(self):
        super().setUp()
        self.write(os.path.join("app", "core.py"),
                   "def shared():\n    return 1\n"
                   "def orphan():\n    return 2\n"
                   "def user():\n    return shared()\n")
        self.write(os.path.join("tests", "test_core.py"),
                   "from app.core import shared\n"
                   "class T:\n"
                   "    def test_one(self):\n        return shared()\n"
                   "    def test_two(self):\n        return shared()\n")
        self.write(os.path.join("app", "__init__.py"), "")
        self.write(os.path.join("tests", "__init__.py"), "")
        codegraph.build([self.dir])

    def run_it(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), *args],
                              cwd=self.dir, capture_output=True, text=True, timeout=180)

    def lines(self, *args):
        r = self.run_it(*args)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return [ln for ln in r.stdout.splitlines() if ln.strip()]

    def test_exclude_drops_a_whole_corner_of_the_tree(self):
        every = self.lines("callers", "app/core.shared")
        self.assertEqual(len(every), 3)                      # one real caller, two test methods
        scoped = self.lines("callers", "app/core.shared", "--exclude", "tests/*")
        self.assertEqual(scoped, ["app/core.user"])

    def test_only_keeps_one_corner(self):
        self.assertEqual(self.lines("callers", "app/core.shared", "--only", "tests/*"),
                         ["tests/test_core.T.test_one", "tests/test_core.T.test_two"])

    def test_unused_no_longer_needs_scoping_to_be_readable(self):
        """This is why the class exists: 293 of 337 rows were unittest methods. They are still
        listed under --all, with the reason - but they are no longer the answer."""
        every = self.lines("unused")
        self.assertEqual([ln.split()[0] for ln in every], ["app/core.orphan", "app/core.user"])
        self.assertFalse(any("test_one" in ln for ln in every),
                         "a unittest method is not a finding")
        shown = self.lines("unused", "--all")
        self.assertTrue(any("test_one" in ln for ln in shown),
                        "--all must still show everything; hiding it would be the other lie")

    def test_unused_can_still_be_scoped(self):
        scoped = self.lines("unused", "--all", "--exclude", "tests/*")
        self.assertEqual([ln.split()[0] for ln in scoped], ["app/core.orphan", "app/core.user"])

    def test_it_works_on_the_other_verbs_too(self):
        for verb in ("blast", "sites", "impact", "where", "find"):
            with self.subTest(verb):
                arg = "shared" if verb in ("where", "find") else "app/core.shared"
                out = self.run_it(verb, arg, "--exclude", "tests/*")
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertNotIn("tests/", out.stdout, verb)

    def test_the_json_is_scoped_the_same_way(self):
        r = self.run_it("impact", "app/core.shared", "--exclude", "tests/*", "--json")
        got = json.loads(r.stdout)
        self.assertEqual(got["callers"], ["app/core.user"])
        self.assertEqual([s["caller"] for s in got["sites"]], ["app/core.user"])

    def test_both_at_once(self):
        # Filtered down to nothing still SAYS so - blank output is a worse answer than "(none)".
        self.assertEqual(self.lines("unused", "--only", "app/*", "--exclude", "*core*"), ["(none)"])

    def test_a_pattern_that_is_missing_is_a_usage_error(self):
        r = self.run_it("unused", "--exclude")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)

    def test_impact_puts_one_thing_on_one_line(self):
        """Comma-joining seven callers and fourteen sites produced a wrapped wall of text.
        Each site is paired with the caller it sits in, which is the pair you act on."""
        out = self.run_it("impact", "app/core.shared").stdout
        self.assertIn("callers of app/core.shared:", out)
        self.assertIn("\n  app/core.user\n", out)
        self.assertRegex(out, r"\n  app/core\.py:\d+  app/core\.user\n")
        self.assertIn("codegraph blast app/core.shared to list them", out)


class AskingWhatIsInHere(Sandbox):
    """There was no way to list what a codebase contains. `find` needs a substring and refuses
    a blank one - correctly, since an unset shell variable must not match everything - so
    surveying a tree meant reading codegraph.json by hand, which is exactly what I ended up
    doing while trying to rank a real project's functions by how far a change would reach."""

    def setUp(self):
        super().setUp()
        self.write(os.path.join("app", "__init__.py"), "")
        self.write(os.path.join("app", "core.py"),
                   "class Store:\n    def put(self):\n        return 1\n"
                   "def helper():\n    return 2\n")
        self.write(os.path.join("tests", "__init__.py"), "")
        self.write(os.path.join("tests", "test_core.py"), "def test_it():\n    return 3\n")
        codegraph.build([self.dir])

    def run_it(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), *args],
                              cwd=self.dir, capture_output=True, text=True, timeout=180)

    def test_it_lists_every_definition_with_its_kind(self):
        rows = codegraph.symbols(codegraph.load())
        self.assertEqual([(i, k) for i, _at, k in rows],
                         [("app/core.Store", "class"), ("app/core.Store.put", "func"),
                          ("app/core.helper", "func"), ("tests/test_core.test_it", "func")])

    def test_it_says_where_each_one_is(self):
        at = {i: loc for i, loc, _k in codegraph.symbols(codegraph.load())}
        self.assertEqual(at["app/core.helper"], "app/core.py:4")

    def test_it_scopes_like_the_others(self):
        out = self.run_it("symbols", "--exclude", "tests/*")
        self.assertEqual(out.returncode, 0)
        self.assertNotIn("tests/", out.stdout)
        self.assertIn("app/core.Store", out.stdout)

    def test_the_json_carries_the_kind(self):
        got = json.loads(self.run_it("symbols", "--only", "app/*", "--json").stdout)
        self.assertEqual({r["kind"] for r in got["results"]}, {"class", "func"})
        self.assertEqual(len(got["results"]), 3)

    def test_a_blank_find_is_still_refused(self):
        """The guard: this verb exists so that `find ""` did not have to become a wildcard."""
        self.assertEqual(self.run_it("find", "").returncode, 2)


class ItSurvivesBeingUsedOnItself(unittest.TestCase):
    """Running codegraph inside its own repository is the most ordinary thing a person does
    with it, and it broke the suite three ways: the private-origin scan read the generated
    codegraph.json and found absolute paths in it, the date scan read the same file, and one
    test asserted that no graph exists beside the script - true only until somebody runs it."""

    def test_the_generated_graph_is_not_a_shipped_file(self):
        scan = NothingShippedNamesItsAuthor()
        listed = set(scan.shipped_files())
        listed = {samefile_key(p) for p in listed}
        for name in ("codegraph.json", "codegraph.cache.json"):
            self.assertNotIn(samefile_key(os.path.join(HERE, name)), listed,
                             f"{name} is generated output, not something this repository ships")

    def test_what_it_scans_is_what_git_tracks(self):
        tracked = subprocess.run([shutil.which("git") or "git", "ls-files", "-z"],
                                 cwd=HERE, capture_output=True, text=True)
        if tracked.returncode != 0:
            self.skipTest("not a git checkout")
        known = {samefile_key(os.path.join(HERE, p)) for p in tracked.stdout.split("\0") if p}
        for path in NothingShippedNamesItsAuthor().shipped_files():
            self.assertIn(samefile_key(path), known,
                          f"{path} is not tracked and should not be scanned")

    def test_a_build_here_leaves_the_repository_answerable(self):
        """End to end: build a graph in this very directory, then ask it something."""
        cg = os.path.join(HERE, "codegraph.py")
        pre = os.path.exists(os.path.join(HERE, "codegraph.json"))
        try:
            build = subprocess.run([sys.executable, cg, "build", "."], cwd=HERE,
                                   capture_output=True, text=True, timeout=300)
            self.assertEqual(build.returncode, 0, build.stderr[-400:])
            r = subprocess.run([sys.executable, cg, "impact", "codegraph.build", "--json"],
                               cwd=HERE, capture_output=True, text=True, timeout=300)
            got = json.loads(r.stdout)
            self.assertEqual(got["target"], "codegraph.build")
            self.assertIn("codegraph.load", got["callers"])
            dead = subprocess.run([sys.executable, cg, "unused", "--only", "codegraph*",
                                   "--exclude", "*.V.*", "--json"],
                                  cwd=HERE, capture_output=True, text=True, timeout=300)
            self.assertEqual(json.loads(dead.stdout)["results"], [],
                             "codegraph has a function nothing calls")
        finally:
            if not pre:
                for n in ("codegraph.json", "codegraph.cache.json"):
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(HERE, n))


class TheScanExcludesItselfOnEveryPlatform(unittest.TestCase):
    """This file lists the markers it hunts for, so if the scan ever fails to skip it, it finds
    its own list and reports the guard as the leak. That happened: git prints forward slashes,
    joining one onto a Windows root gives `D:\\a\\repo\\tests/test_codegraph.py`, and that string
    is not what os.path.abspath produces. Green on macOS and Linux, red on Windows."""

    def test_this_file_is_never_scanned(self):
        listed = {samefile_key(p) for p in NothingShippedNamesItsAuthor().shipped_files()}
        self.assertNotIn(samefile_key(__file__), listed)

    def test_a_mixed_separator_path_still_matches_itself(self):
        """The comparison, isolated from the filesystem: the two spellings of one path."""
        native = os.path.join("tests", "test_codegraph.py")
        from_git = "tests/test_codegraph.py"
        self.assertEqual(samefile_key(os.path.join(HERE, native)),
                         samefile_key(os.path.join(HERE, from_git)))

    def test_the_scan_reads_something(self):
        """The guard on the other side: excluding too much would make it pass by scanning air."""
        listed = list(NothingShippedNamesItsAuthor().shipped_files())
        self.assertGreater(len(listed), 4, listed)
        self.assertTrue(any(p.endswith("codegraph.py") for p in listed))
        self.assertTrue(any(p.endswith("README.md") for p in listed))


class AFileCanChangeWithoutItsTimestampMoving(Sandbox):
    """The cache asked "same mtime, same size?" and called that the same file.

    An automated edit lands inside one second; `git checkout`, `cp -p` and `rsync -t` all put
    the old timestamp back on purpose. Rename a function to another of the same length and
    both halves of that key hold still. The graph then answered about a function that no
    longer exists and denied the one that does - with a success code, which is the worst way
    to be wrong, and precisely at the moment somebody is about to edit.
    """

    BEFORE = "def alpha():\n    return 1\n\ndef caller():\n    return alpha()\n"
    AFTER = "def gamma():\n    return 1\n\ndef caller():\n    return gamma()\n"

    def rewrite_holding_the_clock(self, rel, body):
        """Same bytes count, same mtime to the nanosecond. Only the content moves.

        A SKIP rather than a failure when the filesystem will not hold a timestamp exactly -
        FAT rounds to two seconds, and a network mount can round further. The premise of every
        test below is "the clock did not move"; where the platform cannot arrange that, there
        is nothing here to check, and asserting it would turn a filesystem's granularity into
        a red build on a machine nobody can look at.
        """
        path = os.path.join(self.dir, rel)
        st = os.stat(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        try:
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        except (OSError, NotImplementedError) as ex:      # pragma: no cover - platform
            self.skipTest(f"this filesystem will not set a timestamp: {ex}")
        after = os.stat(path)
        if after.st_mtime_ns != st.st_mtime_ns:           # pragma: no cover - platform
            self.skipTest("this filesystem will not hold a timestamp to the nanosecond")
        self.assertEqual(after.st_size, st.st_size, "the fixture must not change the size")
        return path

    def test_a_rebuild_sees_the_new_name(self):
        self.write("m.py", self.BEFORE)
        self.graph()
        self.rewrite_holding_the_clock("m.py", self.AFTER)
        ids = {n["id"] for n in self.graph()["nodes"]}
        self.assertIn("m.gamma", ids, "the rebuild reused a stale parse of a file that changed")
        self.assertNotIn("m.alpha", ids, "it is still reporting a function that was renamed away")

    def test_the_freshness_check_calls_it_stale(self):
        self.write("m.py", self.BEFORE)
        g = self.graph()
        self.assertFalse(codegraph._is_stale(g), "nothing changed yet")
        self.rewrite_holding_the_clock("m.py", self.AFTER)
        self.assertTrue(codegraph._is_stale(g),
                        "the graph describes code that is no longer on disk and says it is fresh")

    def test_the_answer_a_query_gives_is_the_new_one(self):
        """The whole point: `callers` is asked at the moment of an edit."""
        self.write("m.py", self.BEFORE)
        self.graph()
        self.rewrite_holding_the_clock("m.py", self.AFTER)
        g = codegraph.load()
        by_name = {n["id"] for n in g["nodes"]}
        self.assertIn("m.gamma", by_name)
        self.assertNotIn("m.alpha", by_name)


class NothingCallsThisIsAClaimThatHasToEarnItself(Sandbox):
    """`unused` reported 577 of this repo's own 734 functions, and every single one was wrong.

    505 were unittest test methods, 46 were setUp/tearDown, 22 were the `visit_*` methods of
    its own AST walker, and the four that were left were a callback handed to `signal.signal`,
    one handed to `subprocess` as `preexec_fn`, an alias assigned to four other names, and a
    `generic_visit` override. Zero real. A number that is wrong 100% of the time is not a
    conservative number, it is a broken one - and it was the headline of `stats`, printed
    bare, where somebody would either delete live code or stop believing the tool.

    The fix is the honesty already applied to edges: say WHY a name is on the list, and count
    the ones that are only there because nothing here calls things by name.
    """

    TREE = (
        ("app.py",
         "import signal\n"
         "\n"
         "def dead_and_really_dead():\n"          # the only real one
         "    return 1\n"
         "\n"
         "def on_signal(signum, frame):\n"        # handed to signal.signal by name
         "    return 2\n"
         "\n"
         "def card_one():\n"                      # dispatch table
         "    return 3\n"
         "\n"
         "CARDS = [card_one]\n"
         "signal.signal(signal.SIGINT, on_signal)\n"
         "\n"
         "class Walker:\n"
         "    def visit_Call(self, node):\n"       # dispatched by name, never written down
         "        return node\n"
         "\n"
         "    def __repr__(self):\n"              # python calls this one
         "        return 'w'\n"),
        ("test_app.py",
         "import unittest\n"
         "\n"
         "class T(unittest.TestCase):\n"
         "    def setUp(self):\n"
         "        pass\n"
         "\n"
         "    def test_something(self):\n"
         "        pass\n"),
    )

    def build(self):
        for rel, body in self.TREE:
            self.write(rel, body)
        return self.graph()

    def rows(self):
        return {i: reason for i, _where, reason in codegraph.unused(self.build())}

    def test_the_only_name_reported_unreached_is_the_only_dead_one(self):
        unreached = {i for i, r in self.rows().items() if not r}
        self.assertEqual(unreached, {"app.dead_and_really_dead"})

    def test_a_function_handed_to_something_by_name_is_not_unreached(self):
        self.assertTrue(self.rows()["app.on_signal"])

    def test_a_function_only_a_dispatch_table_holds_is_not_unreached(self):
        self.assertTrue(self.rows()["app.card_one"])

    def test_a_visitor_method_dispatched_by_name_is_not_unreached(self):
        self.assertTrue(self.rows()["app.Walker.visit_Call"])

    def test_a_test_method_and_its_fixture_are_not_unreached(self):
        r = self.rows()
        self.assertTrue(r["test_app.T.test_something"])
        self.assertTrue(r["test_app.T.setUp"])

    def test_a_dunder_is_not_unreached(self):
        self.assertTrue(self.rows()["app.Walker.__repr__"])

    def test_every_name_is_still_listed_so_nothing_is_hidden(self):
        """The brand is honest labelling, not a shorter list. Everything uncalled is still
        here; what changed is that each one says why."""
        self.assertEqual(set(self.rows()), {
            "app.dead_and_really_dead", "app.on_signal", "app.card_one",
            "app.Walker.visit_Call", "app.Walker.__repr__",
            "test_app.T.test_something", "test_app.T.setUp"})

    def test_stats_counts_the_two_apart(self):
        s = codegraph.stats(self.build())
        self.assertEqual(s["not_called_in_scanned_roots"], 1)
        self.assertEqual(s["uncalled_but_reachable_by_name"], 6)
        self.assertNotIn("never_called_in_tree", s,
                         "the old name claimed more than it could know")


class TheCountsAreInTheFileNotOnlyOnTheScreen(Sandbox):
    """`stats` printed the counts and the graph stored none of them, so anything reading
    codegraph.json had to recompute them from nodes and calls. The first integration written
    against it read `func_defs` off the file, found nothing, and printed zero - a gauge that
    reads zero rather than the truth, which is worse than no gauge at all."""

    def setUp(self):
        super().setUp()
        self.write("m.py", "def used():\n    return 1\n"
                           "def caller():\n    return used()\n"
                           "def orphan():\n    return 2\n")

    def test_the_written_graph_carries_its_own_stats(self):
        codegraph.build([self.dir])
        with open(codegraph.OUT, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertIn("stats", on_disk, "a reader of the file cannot get the numbers")
        self.assertEqual(on_disk["stats"]["func_defs"], 3)
        self.assertEqual(on_disk["stats"]["not_called_in_scanned_roots"], 2)

    def test_the_stored_block_is_what_stats_would_say(self):
        """Two numbers for one fact drift. This is the check that they cannot."""
        g = codegraph.build([self.dir])
        stored = dict(g["stats"])
        recomputed = codegraph.stats({k: v for k, v in g.items() if k != "stats"})
        self.assertEqual(stored, recomputed)

    def test_a_graph_loaded_back_still_has_them(self):
        codegraph.build([self.dir])
        self.assertEqual(codegraph.load()["stats"]["func_defs"], 3)


class NothingPrivateGetsPublished(unittest.TestCase):
    """A scrub done by reading passes until the once it does not.

    This repository has been caught by that twice: a dead project's name survived every review
    of the LICENCE because the reviews looked for forbidden words and years, and `--help` still
    named where the tool came from after a full pass had been declared clean. Both were plain
    text in files nobody thought to question.

    So the scrub is a test. Every check below plants the thing it is looking for and proves the
    scan goes red - a scrub that has never failed is not evidence that a tree is clean, it is
    evidence that nothing has been checked.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for cmd in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                    ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"]):
            subprocess.run(["git", "-C", self.dir] + cmd, check=True, capture_output=True)

    def commit(self, rel, body, message="a change"):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path) or self.dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        subprocess.run(["git", "-C", self.dir, "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.dir, "commit", "-q", "-m", message],
                       check=True, capture_output=True)

    def hits(self):
        return scrub.scan(self.dir)

    def labels(self):
        return {label for _w, _l, label, _f in self.hits()}

    # --- the negative control: a clean tree has to come back empty, or every check below
    # --- would pass for the wrong reason.

    def test_a_clean_tree_is_clean(self):
        self.commit("a.py", "def f():\n    return 1\n")
        self.assertEqual(self.hits(), [])

    # --- the positive controls, one per category.

    def test_it_catches_an_api_key(self):
        self.commit("conf.py", 'KEY = "sk-' + "a" * 32 + '"\n')  # scrub: fixture
        self.assertIn("secret", self.labels())

    def test_it_catches_a_github_token(self):
        self.commit("conf.py", 'T = "ghp_' + "b" * 36 + '"\n')  # scrub: fixture
        self.assertIn("secret", self.labels())

    def test_it_catches_a_private_key_header(self):
        self.commit("id.pem", "-----BEGIN RSA PRIVATE KEY-----\n")  # scrub: fixture
        self.assertIn("secret", self.labels())

    def test_it_catches_a_home_directory(self):
        self.commit("notes.md", "I ran it from /home/someone/project last night\n")  # scrub: fixture
        self.assertIn("home path", self.labels())

    def test_it_catches_a_windows_home_directory(self):
        self.commit("notes.md", r"the path was C:\Users\someone\Desktop" + "\n")  # scrub: fixture
        self.assertIn("home path", self.labels())

    def test_it_catches_a_real_email(self):
        self.commit("notes.md", "write to someone@somewhere.co.uk about it\n")  # scrub: fixture
        self.assertIn("email", self.labels())

    def test_it_catches_a_machine_address(self):
        self.commit("notes.md", "the box is at 10.1.2.3 over the tunnel\n")  # scrub: fixture
        self.assertIn("ip address", self.labels())

    def test_it_catches_a_word_from_a_deny_list_in_a_message(self):
        """Via the hashed deny list rather than a written-out pattern: an older test forbids any
        shipped file from naming an assistant, and the first version of this scanner failed it
        by spelling the names into its own regex.

        It brings its OWN list. The previous version relied on whatever sat in the developer's
        ~/.config, so it passed here and failed on every CI runner - a test that asks about the
        machine it is running on rather than about the code.
        """
        deny = os.path.join(self.dir, "deny.txt")
        word = "notarealassistantname"
        with open(deny, "w", encoding="utf-8") as fh:
            fh.write(scrub._hash(word) + "\n")
        self.commit("a.py", "x = 1\n",
                    message=f"a change\n\nCo-Authored-By: {word} <x@y.invalid>")
        real = scrub.DENY
        scrub.DENY = deny
        try:
            labels = {lab for _w, _l, lab, _f in scrub.scan(self.dir)}
        finally:
            scrub.DENY = real
        self.assertIn("private word", labels)

    def test_it_catches_a_date(self):
        self.commit("CHANGELOG.md", "## Released 2026-09-07\n")  # scrub: fixture
        self.assertIn("date", self.labels())

    def test_it_reads_commit_messages_not_only_files(self):
        """The half of a publication people forget: a message cannot be edited after a push
        without rewriting history."""
        self.commit("a.py", "def f():\n    return 1\n",
                    message="fixed it on the box at /home/someone")  # scrub: fixture
        where = {w for w, _l, _lab, _f in self.hits()}
        self.assertTrue(any(w.startswith("commit ") for w in where), self.hits())

    # --- the hashed deny list

    def test_a_private_word_is_caught_without_being_written_down(self):
        deny = os.path.join(self.dir, "deny.txt")
        word = "someinternalname"
        with open(deny, "w", encoding="utf-8") as fh:
            fh.write(scrub._hash(word) + "\n")
        self.commit("notes.md", f"the {word} host was rebooted\n")
        real = scrub.DENY
        scrub.DENY = deny
        try:
            hits = scrub.scan(self.dir)
        finally:
            scrub.DENY = real
        self.assertIn("private word", {lab for _w, _l, lab, _f in hits})
        self.assertFalse(any(word in str(f) for _w, _l, _lab, f in hits),
                         "the finding must not reprint the private word - a CI log is public")

    def test_the_deny_list_never_stores_the_word(self):
        deny = os.path.join(self.dir, "deny.txt")
        real = scrub.DENY
        scrub.DENY = deny
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                scrub.main(["--add", "someinternalname"])
            with open(deny, encoding="utf-8") as fh:
                written = fh.read()
        finally:
            scrub.DENY = real
        self.assertNotIn("someinternalname", written)
        self.assertIn(scrub._hash("someinternalname"), written)

    # --- and the thing it is all for

    def test_this_repositorys_tracked_tree_is_clean(self):
        """The gate that is checked on every commit. A file is fixed by editing it, so there is
        never a reason for this one to be red."""
        hits = scrub.scan(HERE, history=False)
        self.assertEqual(hits, [], "\n".join(f"{w}:{l}  {lab}: {f}" for w, l, lab, f in hits))

    def test_the_history_gate_exists_and_is_wired_to_the_publish_step(self):
        """The other half cannot be fixed by editing - a commit message changes only by
        rewriting history - so it is the gate run before this goes public rather than a test
        that sits red. This asserts the gate is actually wired up, because a gate nobody runs
        is not a gate."""
        with open(os.path.join(HERE, ".github", "workflows", "publish.yml"), encoding="utf-8") as fh:
            wf = fh.read()
        self.assertIn("scrub.py --history", wf,
                      "the publish workflow must run the history scrub")

    def test_the_deny_list_is_not_in_the_repository(self):
        """It was, with each word stored as a sha256, on the reasoning that a hash is not a
        word. It is not much else either: short dictionary words, no salt, so they come back by
        guessing and checking rather than by reversing - twelve of them fell to a sixteen-word
        list in under a second, in a file whose own comment said it existed to stop exactly
        that. Hashing is worth keeping and it is obfuscation; the protection is that the file
        is not published."""
        tracked = subprocess.run(["git", "-C", HERE, "ls-files"],
                                 capture_output=True, text=True, check=True).stdout.split()
        self.assertNotIn("tools/deny.txt", tracked,
                         "the deny list is back in the repository")
        self.assertFalse([p for p in tracked if p.endswith("deny.txt")],
                         "a deny list is tracked under some other name")

    def test_the_default_location_is_outside_any_checkout(self):
        self.assertNotIn(os.path.realpath(HERE), os.path.realpath(scrub.DENY),
                         "the default deny list sits inside the repository")

    def test_a_missing_deny_list_is_not_an_error(self):
        """A fresh clone has no list, and the pattern rules still have to run."""
        real = scrub.DENY
        scrub.DENY = os.path.join(self.dir, "nope", "deny.txt")
        try:
            self.assertEqual(scrub._denied_hashes(), set())
            self.commit("notes.md", "it lives in /home/someone\n")  # scrub: fixture
            self.assertIn("home path", {lab for _w, _l, lab, _f in scrub.scan(self.dir)})
        finally:
            scrub.DENY = real

    def test_it_reads_deleted_files_out_of_the_history(self):
        """The hole this nearly shipped with. Deleting a file from the tree does not remove it
        from the history - `git log -p` still prints it and anyone can check out the commit
        that had it. A scan of the tracked tree alone called this repository clean while two
        files of working notes sat in earlier commits."""
        self.commit("secret_notes.md", "the box lives at /home/someone\n")  # scrub: fixture
        subprocess.run(["git", "-C", self.dir, "rm", "-q", "secret_notes.md"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", self.dir, "commit", "-q", "-m", "remove the notes"],
                       check=True, capture_output=True)
        self.assertEqual(scrub.scan(self.dir, history=False), [],
                         "the tree really is clean once the file is deleted")
        deep = scrub.scan(self.dir, history=True)
        self.assertIn("home path", {lab for _w, _l, lab, _f in deep},
                      "the deleted file is still in the history and still readable")
        self.assertTrue(any(w.startswith("history ") for w, _l, _lab, _f in deep), deep)


class TheHooksRefuseWhatCannotBeTakenBack(unittest.TestCase):
    """The cleanup has been done twice. The second cost a history rewrite, a force-push, and
    then a delete-and-recreate of the repository - because a rewrite does not make the host
    forget: the orphaned objects stayed fetchable by their id, and a repository going public
    would have served them to anyone who asked.

    The only cheap moment is before the commit exists. These prove the hooks actually refuse,
    which is the half that matters: a hook that cannot run and says nothing is worse than no
    hook, because it is also believed.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.invalid"],
                    ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"],
                    ["config", "core.hooksPath", ".githooks"]):
            subprocess.run(["git", "-C", self.dir] + cmd, check=True, capture_output=True)
        # The hooks reach for tools/scrub.py relative to the repository root, so the fixture
        # repo carries a real copy of both rather than a stub.
        for rel in (".githooks/pre-commit", ".githooks/commit-msg", "tools/scrub.py"):
            dst = os.path.join(self.dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(os.path.join(HERE, rel), dst)
            if rel.startswith(".githooks"):
                os.chmod(dst, os.stat(dst).st_mode | stat.S_IXUSR)   # the owner's bit, no more
        # A deny list of its OWN, holding a made-up word. Copying the real one would have meant
        # writing a real private word into this file to test against it - which is the leak,
        # in the test for the leak. It is not a fixture problem that a marker can solve: a
        # marked line still ships the word.
        self.private_word = "notarealprojectname"
        # The list lives OUTSIDE the repository now, so the fixture points the hooks at its own
        # via the environment. Writing one into the fixture's tools/ was silently ignored, and
        # the test then proved only that an ordinary message passes.
        self.deny = os.path.join(self.dir, "deny.txt")
        with open(self.deny, "w", encoding="utf-8") as fh:
            fh.write(scrub._hash(self.private_word) + "\n")

    def commit(self, message="a change", **env):
        subprocess.run(["git", "-C", self.dir, "add", "-A"], check=True, capture_output=True)
        return subprocess.run(["git", "-C", self.dir, "commit", "-m", message],
                              capture_output=True, text=True,
                              env={**os.environ, "CODEGRAPH_DENY": self.deny, **env})

    def write(self, rel, body):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path) or self.dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)

    def committed(self):
        r = subprocess.run(["git", "-C", self.dir, "rev-list", "--all", "--count"],
                           capture_output=True, text=True)
        return int(r.stdout.strip() or 0)

    # --- the control: an ordinary commit must still work, or the hook is just breakage.

    def test_an_ordinary_commit_still_goes_through(self):
        self.write("app.py", "def f():\n    return 1\n")
        r = self.commit()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.committed(), 1)

    # --- and the half that matters.

    def test_a_file_with_a_home_path_is_refused(self):
        self.write("notes.md", "it lives in /home/someone/thing\n")  # scrub: fixture
        r = self.commit()
        self.assertEqual(r.returncode, 1, "the commit was allowed")
        self.assertIn("must not be committed", r.stderr)
        self.assertEqual(self.committed(), 0, "nothing may be recorded when it refuses")

    def test_a_file_with_a_key_is_refused(self):
        self.write("conf.py", 'K = "ghp_' + "c" * 36 + '"\n')  # scrub: fixture
        self.assertEqual(self.commit().returncode, 1)
        self.assertEqual(self.committed(), 0)

    def test_a_private_word_in_the_message_is_refused(self):
        """The half that forced the second cleanup: one word, in one message."""
        self.write("app.py", "def f():\n    return 1\n")
        r = self.commit(message=f"ran it against the {self.private_word} box")
        self.assertEqual(r.returncode, 1, "the message was allowed through")
        self.assertEqual(self.committed(), 0)

    def test_a_home_path_in_the_message_is_refused(self):
        self.write("app.py", "def f():\n    return 1\n")
        r = self.commit(message="fixed under /home/someone")  # scrub: fixture
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.committed(), 0)

    def test_a_marked_fixture_line_is_allowed_through(self):
        """Otherwise this suite could not be committed at all."""
        self.write("t.py", 'BAD = "/home/someone"  # scrub: fixture\n')
        r = self.commit()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_escape_hatch_works_and_is_deliberate(self):
        """A guard with no way past it gets deleted the first time it is wrong. This one takes
        a named environment variable, which nobody types by accident."""
        self.write("notes.md", "it lives in /home/someone/thing\n")  # scrub: fixture
        self.assertEqual(self.commit().returncode, 1)
        r = self.commit(CODEGRAPH_ALLOW_PRIVATE="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_hooks_are_present_and_executable(self):
        """True of any checkout, including a fresh clone and a CI runner."""
        for name in ("pre-commit", "commit-msg"):
            path = os.path.join(HERE, ".githooks", name)
            self.assertTrue(os.path.isfile(path), f"{name} is missing")
            self.assertTrue(os.access(path, os.X_OK), f"{name} is not executable")

    def test_a_working_checkout_has_them_turned_on(self):
        """The hooks are in the tree; git runs them only if core.hooksPath says so, and that is
        local config a clone does not carry. Present-but-not-installed is the shape every guard
        here has failed in, so this is checked rather than assumed.

        Skipped where nothing is committed from - a CI runner clones, tests and throws the
        checkout away, and failing there would only teach people to ignore this."""
        if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
            self.skipTest("nothing is committed from a CI checkout")
        r = subprocess.run(["git", "-C", HERE, "config", "core.hooksPath"],
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), ".githooks",
                         "the guards are not switched on here - "
                         "run: python3 tools/scrub.py --install-hooks")

    def test_an_unreachable_object_is_named_as_local_only(self):
        """`git add` writes the blob even when the commit is then refused, so a hook doing its
        job leaves one behind. A push never sends it and `git gc` removes it - but in a list it
        looks exactly like something welded into the history, and one of those is an afternoon
        of rewriting. So the report says which."""
        path = os.path.join(self.dir, "leak.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('X = "/home/someone"\n')                      # scrub: fixture
        subprocess.run(["git", "-C", self.dir, "add", "leak.py"], check=True, capture_output=True)
        os.remove(path)
        subprocess.run(["git", "-C", self.dir, "reset", "-q"], check=True, capture_output=True)
        hits = scrub.scan(self.dir, history=True)
        self.assertTrue(hits, "the staged-then-abandoned blob is still in the object store")
        self.assertTrue(any("UNREACHABLE" in w and "git gc" in w for w, _l, _lab, _f in hits),
                        [w for w, _l, _lab, _f in hits])

    def test_installing_grants_the_owner_bit_and_nothing_else(self):
        """CodeQL and ruff both called 0o755 here overly permissive, and they were right: git
        runs a hook as whoever runs git, so the owner's execute bit is the whole requirement.
        The justification for the old mode - "a hook that is not 0o755 does not run" - was
        simply false, and two scanners disagreeing with a justification usually settles it."""
        import stat as _stat
        if os.name != "posix":
            # Windows has no execute bit. os.chmod there honours exactly one thing, the
            # read-only flag, and whether a file can be run is decided by its extension and
            # the ACL - so there is no permission here to grant or withhold, and the installer's
            # chmod is a harmless no-op. Asserting a POSIX mode on Windows is asserting
            # something the platform does not have. CI found this; a mac cannot.
            self.skipTest("permission bits are a POSIX idea")
        hooks = os.path.join(self.dir, ".githooks")
        target = os.path.join(hooks, "pre-commit")
        os.chmod(target, 0o600)                       # owner read/write, nobody can run it
        real = scrub.HERE
        scrub.HERE = self.dir
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                scrub.main(["--install-hooks"])
        finally:
            scrub.HERE = real
        mode = os.stat(target).st_mode
        self.assertTrue(mode & _stat.S_IXUSR, "the owner cannot run the hook, so it will not run")
        self.assertFalse(mode & _stat.S_IXGRP, "the group does not need to run it")
        self.assertFalse(mode & _stat.S_IXOTH, "everybody does not need to run it")


class TheMutationHarnessCanBeAimed(unittest.TestCase):
    """It could only ever break codegraph.py, which left every tool in tools/ untested by the
    one check here that can prove a suite has teeth - including the scrub, which is the thing
    standing between a private word and a public repository.

    Aiming it somewhere else turned two hardcoded filenames into lies: the clean-tree guard
    asked git about codegraph.py while the run rewrote a different file, and the recovery
    message - the one that only ever prints after something has already gone wrong - referred
    to a name that was no longer in scope. A broken error path is worse than none, because it
    replaces a bad situation with a traceback.
    """

    def setUp(self):
        sys.path.insert(0, os.path.join(HERE, "tools"))
        import mutation
        self.mutation = mutation
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.target = os.path.join(self.dir, "thing.py")
        with open(self.target, "w", encoding="utf-8") as fh:
            fh.write("def f(a):\n    return a > 1\n")

    def run_main(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                code = self.mutation.main(argv)
            except SystemExit as e:
                return None, str(e.code), out.getvalue()
        return code, "", out.getvalue()

    def test_a_missing_target_is_refused_by_name(self):
        _c, err, _o = self.run_main(["10", "0", "--target", os.path.join(self.dir, "gone.py")])
        self.assertIn("no such file", err)

    def test_the_flag_needs_a_path(self):
        _c, err, _o = self.run_main(["10", "0", "--target"])
        self.assertIn("needs a path", err)

    def test_the_recovery_message_names_the_file_it_actually_broke(self):
        """It said codegraph.py whatever was aimed at, and the line telling you how to restore
        it referred to a variable that did not exist in that scope at all."""
        with open(os.path.join(self.dir, ".mutation-running"), "w", encoding="utf-8") as fh:
            fh.write("/somewhere/backup.py\n")
        _c, err, _o = self.run_main(["10", "0", "--target", self.target])
        self.assertIn("thing.py", err, "it named the wrong file")
        self.assertNotIn("codegraph.py", err)
        self.assertIn("git checkout --", err)

    def test_the_clean_tree_guard_asks_about_the_chosen_file(self):
        """It asked git about codegraph.py by name, so a dirty target would have been rewritten
        while a clean codegraph.py said everything was fine."""
        import inspect
        src = inspect.getsource(self.mutation.clean_tree)
        self.assertNotIn('"codegraph.py"', src,
                         "the guard still names one file rather than the one being rewritten")
        self.assertEqual(self.mutation.clean_tree(self.target), "untracked",
                         "a file git has never heard of is not a file with uncommitted changes")
        # A TRACKED file is clean or dirty depending on whether it has been edited, which is
        # not this test's business - asserting "clean" here made the answer depend on the state
        # of the working tree, so it passed or failed according to whether somebody was
        # midway through something. The invariant is that a tracked file is never "untracked".
        self.assertIn(self.mutation.clean_tree(os.path.join(HERE, "codegraph.py")),
                      ("clean", "dirty"))

    def test_a_file_outside_the_repository_is_refused_for_the_real_reason(self):
        """It said "has uncommitted changes", which was false and sent you to commit a file git
        does not track. The actual problem is that there is no way to restore it."""
        _c, err, _o = self.run_main(["10", "0", "--target", self.target])
        self.assertIn("not in this repository", err)
        self.assertNotIn("uncommitted changes", err)

    def test_a_path_on_another_drive_does_not_raise(self):
        """os.path.relpath RAISES across Windows drive letters rather than falling back, and a
        repository on D: with a temp file on C: is the ordinary case on a CI runner. This class
        of bug has now bitten twice, so it is pinned here rather than left to the next runner
        to discover - simulated, because a mac has no drive letters to cross."""
        real = os.path.relpath

        def across_drives(path, start=None):
            raise ValueError("path is on mount 'C:', start on mount 'D:'")

        os.path.relpath = across_drives
        try:
            named = self.mutation._rel(self.target)
            self.assertEqual(named, os.path.abspath(self.target),
                             "a path that cannot be made relative is not in the repository")
            self.assertEqual(self.mutation.clean_tree(self.target), "untracked")
            _c, err, _o = self.run_main(["10", "0", "--target", self.target])
            self.assertIn("not in this repository", err)
        finally:
            os.path.relpath = real


class WhatAmIAboutToBreak(Sandbox):
    """`impact` answers for a name you already know. The question before that one is which
    names you have touched at all - and the answer is sitting in the diff you have not
    committed yet.

    It reads the diff on STDIN rather than running git. This file imports nothing but the
    standard library and starts no processes, which is a property people rely on when they
    copy it into their own repository, and it is not worth spending to save a pipe. Reading a
    diff from stdin also works with hg, jj, a patch file, and a pull request fetched by
    something else.
    """

    TREE = (
        ("app.py",
         "def leaf():\n"                       # 1
         "    return 1\n"                      # 2
         "\n"                                  # 3
         "def mid():\n"                        # 4
         "    return leaf()\n"                 # 5
         "\n"                                  # 6
         "def top():\n"                        # 7
         "    return mid()\n"                  # 8
         "\n"                                  # 9
         "CONSTANT = 3\n"),                    # 10
    )

    def build(self):
        for rel, body in self.TREE:
            self.write(rel, body)
        return self.graph()

    def diff(self, path, *lines):
        hunks = "".join(f"@@ -{n},0 +{n},1 @@\n+    pass\n" for n in lines)
        return f"--- a/{path}\n+++ b/{path}\n{hunks}"

    def test_it_names_the_function_the_changed_line_is_inside(self):
        g = self.build()
        got = codegraph.changed(g, self.diff("app.py", 2), root=self.dir)
        self.assertEqual([t["id"] for t in got["touched"]], ["app.leaf"])

    def test_it_reports_who_that_function_would_break(self):
        g = self.build()
        got = codegraph.changed(g, self.diff("app.py", 2), root=self.dir)
        one, = got["touched"]
        self.assertEqual(one["callers"], ["app.mid"])
        self.assertEqual(sorted(one["blast"]), ["app.mid", "app.top"])

    def test_two_functions_touched_are_two_answers(self):
        g = self.build()
        got = codegraph.changed(g, self.diff("app.py", 2, 8), root=self.dir)
        self.assertEqual(sorted(t["id"] for t in got["touched"]), ["app.leaf", "app.top"])

    def test_a_line_outside_every_function_is_reported_apart(self):
        """Module level is not nothing - it runs on import, and changing it can reach anything
        in the file. But it is not a function either, so it must not be silently dropped."""
        g = self.build()
        got = codegraph.changed(g, self.diff("app.py", 10), root=self.dir)
        self.assertEqual(got["touched"], [])
        self.assertEqual(got["module_level"], ["app.py:10"])

    def test_a_file_the_graph_never_saw_is_named_not_ignored(self):
        """A brand new file, or one outside the scanned roots. Answering "nothing depends on
        this" for a file it never read is the one reply that must never be silent."""
        g = self.build()
        got = codegraph.changed(g, self.diff("brand_new.py", 1), root=self.dir)
        self.assertEqual(got["touched"], [])
        self.assertEqual(got["unknown_files"], ["brand_new.py"])

    def test_an_empty_diff_is_an_empty_answer_not_an_error(self):
        g = self.build()
        got = codegraph.changed(g, "", root=self.dir)
        self.assertEqual(got["touched"], [])
        self.assertEqual(got["unknown_files"], [])

    def test_a_deleted_file_does_not_crash_it(self):
        g = self.build()
        d = "--- a/app.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def leaf():\n-    return 1\n"
        got = codegraph.changed(g, d, root=self.dir)
        self.assertEqual(got["touched"], [])

    def test_only_python_files_are_looked_at(self):
        g = self.build()
        got = codegraph.changed(g, self.diff("notes.md", 1), root=self.dir)
        self.assertEqual(got["touched"], [])
        self.assertEqual(got["unknown_files"], [])

    def test_context_lines_are_not_reported_as_changed(self):
        """`git diff` prints three lines of context either side by default, and the hunk header
        counts them. Trusting that header said a function had changed because a NEIGHBOUR had -
        run against this repository it named `impact` off the back of an edit three lines away.
        A tool for deciding what to re-read must not pad the list."""
        # The file on disk is the NEW side of the diff - which is what it always is for
        # uncommitted work, and is worth being explicit about in the fixture.
        self.write("app.py",
                   "def leaf():\n    return 1\n\n"
                   "def mid():\n    return leaf()\n    x = 1\n\n"
                   "def top():\n    return mid()\n\nCONSTANT = 3\n")
        g = self.graph()
        # A real-shaped hunk: two context lines, one addition, one context line.
        d = ("--- a/app.py\n+++ b/app.py\n"
             "@@ -4,3 +4,4 @@\n"
             " def mid():\n"
             "     return leaf()\n"
             "+    x = 1\n"
             " \n")
        got = codegraph.changed(g, d, root=self.dir)
        self.assertEqual([t["id"] for t in got["touched"]], ["app.mid"])
        self.assertEqual(got["touched"][0]["lines"], [6],
                         "the context lines were counted as changes")

    def test_a_removed_line_does_not_advance_the_new_file_counter(self):
        """A deletion is not in the new file at all, so counting it shifts every line after it
        and blames the wrong function."""
        g = self.build()
        d = ("--- a/app.py\n+++ b/app.py\n"
             "@@ -1,3 +1,2 @@\n"
             " def leaf():\n"
             "-    x = 0\n"
             "     return 1\n")
        got = codegraph.changed(g, d, root=self.dir)
        self.assertEqual(got["touched"], [], "a pure deletion changes no line in the new file")

    def test_the_cli_reads_a_diff_from_stdin(self):
        self.build()
        r = subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "changed"],
                           cwd=self.dir, input=self.diff("app.py", 2),
                           capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("app.leaf", r.stdout)
        self.assertIn("app.mid", r.stdout)

    def test_the_json_form_carries_the_same_answer(self):
        self.build()
        r = subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "changed",
                            "--json"], cwd=self.dir, input=self.diff("app.py", 2),
                           capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        self.assertEqual(got["query"], "changed")
        self.assertEqual([t["id"] for t in got["touched"]], ["app.leaf"])

    def test_a_comment_only_change_breaks_nothing_and_says_so(self):
        """It used to land in "module level, runs on import, can reach anything in the file",
        which is alarming and untrue of a comment. A trailing comment is not in the AST either,
        so there was no function to attribute it to and nowhere sensible for it to go."""
        self.write("app.py", "def leaf():\n    return 1\n    # a note\n")
        g = self.graph()
        got = codegraph.changed(g, self.diff("app.py", 3), root=self.dir)
        self.assertEqual(got["touched"], [])
        self.assertEqual(got["module_level"], [],
                         "a comment was reported as a module-level risk")

    def test_a_blank_line_is_not_a_change_either(self):
        self.write("app.py", "def leaf():\n    return 1\n\n\nCONSTANT = 1\n")
        g = self.graph()
        got = codegraph.changed(g, self.diff("app.py", 3), root=self.dir)
        self.assertEqual((got["touched"], got["module_level"]), ([], []))

    def test_a_real_module_level_change_is_still_reported(self):
        """The control: dropping comments must not drop the thing the category exists for."""
        self.write("app.py", "def leaf():\n    return 1\n\nCONSTANT = 1\n")
        g = self.graph()
        got = codegraph.changed(g, self.diff("app.py", 4), root=self.dir)
        self.assertEqual(got["module_level"], ["app.py:4"])

    def test_consecutive_lines_are_shown_as_a_range(self):
        """One edit produced a row of thirty-five line numbers, which nobody reads."""
        self.assertEqual(codegraph._ranges([1, 2, 3, 7, 9, 10]), "1-3, 7, 9-10")
        self.assertEqual(codegraph._ranges([5]), "5")
        self.assertEqual(codegraph._ranges([]), "")

    def test_a_line_of_content_that_looks_like_a_header_is_not_one(self):
        """An added line whose content begins with `++` renders as `+++ ...`, which the parser
        read as a new file header - so it threw away everything and answered nothing. Diff a
        patch file, a stored diff, or documentation that contains a diff example, and this is
        the ordinary case rather than a strange one.

        The fix is what the @@ counts are actually for: they say how long the body is, so a
        line inside it is content whatever it starts with."""
        # No graph needed: this is about the parser, and building one would be setup that
        # proves nothing.
        d = ("--- a/app.py\n+++ b/app.py\n@@ -0,0 +1,2 @@\n"
             "+++ this is content, not a header\n"
             "+x = 1\n")
        self.assertEqual(codegraph._diff_lines(d), {"app.py": {1, 2}})

    def test_a_deletion_line_that_looks_like_a_header_is_not_one_either(self):
        d = ("--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,1 @@\n"
             "--- this was content\n"
             " x = 1\n")
        self.assertEqual(codegraph._diff_lines(d), {})

    def test_two_files_in_one_diff_stay_apart(self):
        d = ("--- a/one.py\n+++ b/one.py\n@@ -0,0 +1,1 @@\n+a = 1\n"
             "--- a/two.py\n+++ b/two.py\n@@ -0,0 +1,1 @@\n+b = 2\n")
        self.assertEqual(codegraph._diff_lines(d), {"one.py": {1}, "two.py": {1}})

    def test_a_new_file_is_read(self):
        d = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+def f():\n+    return 1\n"
        self.assertEqual(codegraph._diff_lines(d), {"new.py": {1, 2}})

    def test_a_changed_decorator_belongs_to_the_function_it_decorates(self):
        """`@app.route("/pay")` is the most consequential line in a web handler, and editing it
        was reported as a module-level risk - alarming, and less useful than naming the
        function. A decorator sits ABOVE the def, so a range starting at the def missed it."""
        self.write("app.py",
                   "def register(f):\n    return f\n\n"
                   "@register\n"                       # 4
                   "def handler():\n"                  # 5
                   "    return 1\n"                    # 6
                   "\n"
                   "def caller():\n    return handler()\n")
        g = self.graph()
        got = codegraph.changed(g, self.diff("app.py", 4), root=self.dir)
        self.assertEqual([t["id"] for t in got["touched"]], ["app.handler"])
        self.assertEqual(got["module_level"], [])
        self.assertEqual(got["touched"][0]["callers"], ["app.caller"])

    def test_the_line_above_a_decorator_is_still_module_level(self):
        """The control: widening the range must not swallow the code above it."""
        self.write("app.py",
                   "CONSTANT = 1\n"                    # 1
                   "\n"
                   "def register(f):\n    return f\n"
                   "\n"
                   "@register\n"
                   "def handler():\n    return 1\n")
        g = self.graph()
        got = codegraph.changed(g, self.diff("app.py", 1), root=self.dir)
        self.assertEqual(got["touched"], [])
        self.assertEqual(got["module_level"], ["app.py:1"])


class ABaseWrittenWithDotsIsStillABase(Sandbox):
    """`class T(unittest.TestCase)` is the most common class statement in Python, and every
    `self.assertEqual(...)` inside one was unresolved.

    Measured on the standard library, the unresolved-but-winnable calls are led by
    assertEqual at 15,872, then assertRaises at 5,983, assertTrue, assertFalse, assertIn,
    subTest, addCleanup - the whole unittest surface, all of them reached through `self` in a
    class whose base is written with a dot in it.

    Two separate faults, both here:

    1. A base with MORE THAN ONE dot - `pkg.base.Case` - was not captured at all. The name
       extractor read one level of attribute and gave up, so the class had no bases at all and
       the method lookup had nothing to walk.
    2. A base re-exported by a package - `pkg.Case`, where `pkg/__init__` does
       `from .case import Case` - was captured and then resolved to nothing, because the name
       is not defined in the package's `__init__` at all. That is exactly the shape of
       `unittest.TestCase`, which lives in `unittest/case.py`.

    The chain that answers the second already existed for `from pkg import Case`; it just was
    not consulted for `import pkg` followed by `pkg.Case`.
    """

    def test_a_base_two_packages_deep_is_captured(self):
        self.write(os.path.join("pkg", "__init__.py"), "")
        self.write(os.path.join("pkg", "base.py"),
                   "class Case:\n    def check(self):\n        return 1\n")
        self.write("m.py", "import pkg.base\n"
                           "class B(pkg.base.Case):\n"
                           "    def run(self):\n        return self.check()\n")
        g = self.graph()
        edge, = [e for e in g["calls"] if e["callee"] == "check"]
        self.assertEqual(edge.get("dst"), "pkg/base.Case.check")
        self.assertEqual(edge.get("confidence"), "INHERITED")

    def test_a_base_re_exported_by_its_package_resolves(self):
        """The `unittest.TestCase` shape, in miniature."""
        self.write(os.path.join("pkg", "case.py"),
                   "class Case:\n    def check(self):\n        return 1\n")
        self.write(os.path.join("pkg", "__init__.py"), "from .case import Case\n")
        self.write("m.py", "import pkg\n"
                           "class B(pkg.Case):\n"
                           "    def run(self):\n        return self.check()\n")
        g = self.graph()
        edge, = [e for e in g["calls"] if e["callee"] == "check"]
        self.assertEqual(edge.get("dst"), "pkg/case.Case.check")
        self.assertEqual(edge.get("confidence"), "INHERITED")

    def test_the_undotted_spelling_still_works(self):
        """The control. Two spellings of one base must give one answer, and this is the
        spelling that already did."""
        self.write(os.path.join("pkg", "__init__.py"), "")
        self.write(os.path.join("pkg", "base.py"),
                   "class Case:\n    def check(self):\n        return 1\n")
        self.write("m.py", "from pkg.base import Case\n"
                           "class B(Case):\n"
                           "    def run(self):\n        return self.check()\n")
        g = self.graph()
        edge, = [e for e in g["calls"] if e["callee"] == "check"]
        self.assertEqual(edge.get("dst"), "pkg/base.Case.check")

    def test_a_dotted_name_that_is_not_a_class_here_is_still_refused(self):
        """The guard. Widening what counts as a base must not start inventing them: a module
        that has no such class gives no base, rather than a tree-wide name match."""
        self.write(os.path.join("pkg", "__init__.py"), "")
        self.write(os.path.join("pkg", "base.py"), "OTHER = 1\n")
        self.write("elsewhere.py", "class Case:\n    def check(self):\n        return 1\n")
        self.write("m.py", "import pkg.base\n"
                           "class B(pkg.base.Case):\n"
                           "    def run(self):\n        return self.check()\n")
        g = self.graph()
        edge, = [e for e in g["calls"] if e["callee"] == "check"]
        self.assertIsNone(edge.get("dst"),
                          "it reached across the tree for a class the named module lacks")


class UnittestRunsTestFooNotJustTestUnderscoreFoo(Sandbox):
    """A third of the "nothing calls this" list on the standard library was test methods.

    `unittest.TestLoader().testMethodPrefix` is **"test"**, not "test_". Every `testFoo` in a
    TestCase is collected and run exactly like every `test_foo`, and the older camelCase
    spelling is what most of the standard library and a great deal of older code uses. Matching
    only the underscored form meant 1,316 methods that unittest runs on every CI job were
    offered up as safe to delete - the exact false positive this list was rewritten to stop,
    surviving in the half of the convention nobody checked against the library itself.
    """

    # A PLAIN class, deliberately. On a real `unittest.TestCase` subclass every method is
    # already excused as "inherited interface" - the base is outside the tree and may call
    # anything - so a fixture built on one cannot isolate the prefix rule at all, and a control
    # written there would be green whatever the prefix said. This is the mixin shape, which is
    # also where most of the standard library's camelCase tests actually live.
    TREE = ("class T:\n"
            "    def testCamelCase(self):\n        pass\n"
            "    def test_underscored(self):\n        pass\n"
            "    def testLog10(self):\n        pass\n"
            "    def helper_nothing_calls(self):\n        pass\n")

    def rows(self):
        self.write("t.py", self.TREE)
        return {i.rsplit(".", 1)[-1]: why for i, _w, why in codegraph.unused(self.graph())}

    def test_the_camelcase_spelling_is_recognised(self):
        r = self.rows()
        self.assertTrue(r["testCamelCase"], "unittest runs this one")
        self.assertTrue(r["testLog10"], "and this one")

    def test_the_underscored_spelling_still_is(self):
        self.assertTrue(self.rows()["test_underscored"])

    def test_a_method_that_is_not_a_test_is_still_reported(self):
        """The control. Widening the prefix must not excuse everything in the file."""
        self.assertFalse(self.rows()["helper_nothing_calls"],
                         "a helper nothing calls is the finding, and it was hidden")

    def test_the_prefix_is_the_one_unittest_actually_uses(self):
        """Pinned against the library rather than against a memory of it - which is how it came
        to be wrong in the first place."""
        import unittest as _u
        self.assertTrue(
            any(p == _u.TestLoader().testMethodPrefix for p in codegraph._DISPATCHED_PREFIX),
            f"unittest collects {_u.TestLoader().testMethodPrefix!r}; "
            f"this checks {codegraph._DISPATCHED_PREFIX}")


class AnInheritedInterfaceCanBeTwoLevelsUp(Sandbox):
    """The reason "inherited interface" asked only about a class's OWN bases.

    `class Fake(server.Handler)` where `server.Handler(http.server.SimpleHTTPRequestHandler)`:
    the declared base IS in the tree, so the class did not look foreign, and every override of
    a method the external grandparent calls - send_response, end_headers, log_message - was
    offered as reached by nothing. Found on a real repository, on overrides written an hour
    earlier in this session.

    Inheritance is transitive and the question is too: does ANY ancestor come from outside the
    scanned roots.
    """

    def rows(self):
        return {i: why for i, _w, why in codegraph.unused(self.graph())}

    def test_a_grandparent_outside_the_tree_still_counts(self):
        self.write("app.py", "import http.server\n"
                             "class Mine(http.server.BaseHTTPRequestHandler):\n"
                             "    def do_GET(self):\n        pass\n")
        self.write("t.py", "import app\n"
                           "class Fake(app.Mine):\n"
                           "    def end_headers(self):\n        pass\n")
        r = self.rows()
        self.assertTrue(r["app.Mine.do_GET"], "the direct case, which already worked")
        self.assertTrue(r["t.Fake.end_headers"],
                        "its base is in the tree, but ITS base is not - the interface is still "
                        "inherited from outside")

    def test_a_tree_that_is_foreign_nowhere_still_reports(self):
        """The control: if every ancestor is in the tree, nothing is excused by this rule and a
        method nothing calls is still the finding."""
        self.write("app.py", "class Root:\n    def r(self):\n        pass\n")
        self.write("t.py", "import app\n"
                           "class Mid(app.Root):\n    pass\n"
                           "class Leaf(Mid):\n"
                           "    def nobody_calls_me(self):\n        pass\n")
        r = self.rows()
        self.assertFalse(r["t.Leaf.nobody_calls_me"],
                         "every ancestor is in the tree, so nothing here is an outside interface")

    def test_a_cycle_in_the_bases_does_not_hang_it(self):
        """Expressible, even though it would not import."""
        self.write("app.py", "class A(B):\n    def m(self):\n        pass\n"
                             "class B(A):\n    pass\n")
        self.rows()          # the assertion is that this returns at all


class ADeclaredReturnTypeIsTheSourceSayingSo(Sandbox):
    """`def session(...) -> Estimate:` states the answer the inference was reaching around.

    Found on a real repository: `Estimate.human` was on the "nothing calls this" list while
    `est.human()` ran three times in server.py, because `est = cost.session(...)` left `est`
    untyped. That is the dangerous direction - a live method offered up as safe to delete.

    An earlier measurement said this was not worth building: the standard library has 592
    return annotations in total, mostly naming builtins. That was the wrong corpus. The
    standard library is decades-old code that predates annotations; a modern package annotates
    a third of its functions, and the value is not the resolution rate anyway - it is that
    ignoring a type the source states puts working code on a deletion list.
    """

    TREE = ("svc.py",
            "class Client:\n"
            "    def go(self):\n        return 1\n"
            "\n"
            "def make() -> Client:\n"
            "    return Client()\n")

    def build(self, caller):
        self.write(*self.TREE)
        self.write("app.py", caller)
        return self.graph()

    def edge(self, g, name="go"):
        hits = [e for e in g["calls"] if e["callee"] == name and e["src"].startswith("app.")]
        return hits[0] if hits else None

    def test_a_call_to_an_annotated_function_types_the_variable(self):
        g = self.build("import svc\n"
                       "def use():\n"
                       "    c = svc.make()\n"
                       "    return c.go()\n")
        e = self.edge(g)
        self.assertEqual(e.get("dst"), "svc.Client.go")

    def test_the_bare_import_spelling_works_too(self):
        g = self.build("from svc import make\n"
                       "def use():\n"
                       "    c = make()\n"
                       "    return c.go()\n")
        self.assertEqual(self.edge(g).get("dst"), "svc.Client.go")

    def test_it_reaches_a_method_the_class_inherits(self):
        self.write("svc.py",
                   "class Base:\n    def go(self):\n        return 1\n"
                   "class Client(Base):\n    pass\n"
                   "def make() -> Client:\n    return Client()\n")
        self.write("app.py", "import svc\n"
                             "def use():\n    c = svc.make()\n    return c.go()\n")
        self.assertEqual(self.edge(self.graph()).get("dst"), "svc.Base.go")

    # --- the guards. Each is a way this could start inventing answers.

    def test_no_annotation_means_no_type(self):
        self.write("svc.py", "class Client:\n    def go(self):\n        return 1\n"
                             "def make():\n    return Client()\n")
        self.write("app.py", "import svc\n"
                             "def use():\n    c = svc.make()\n    return c.go()\n")
        self.assertIsNone(self.edge(self.graph()).get("dst"),
                          "it invented a type the source never stated")

    def test_an_annotation_naming_nothing_here_means_no_type(self):
        self.write("svc.py", "def make() -> SomethingElse:\n    return 1\n")
        self.write("app.py", "import svc\n"
                             "def use():\n    c = svc.make()\n    return c.go()\n")
        self.assertIsNone(self.edge(self.graph()).get("dst"))

    def test_a_container_return_is_not_its_contents(self):
        """`-> list[Client]` is a list. Reading Client out of it is how a blast radius ends up
        pointing at the wrong code - the same rule annotations already follow elsewhere."""
        self.write("svc.py", "class Client:\n    def go(self):\n        return 1\n"
                             "def make() -> list[Client]:\n    return []\n")
        self.write("app.py", "import svc\n"
                             "def use():\n    c = svc.make()\n    return c.go()\n")
        self.assertIsNone(self.edge(self.graph()).get("dst"))

    def test_two_different_calls_are_not_a_type(self):
        """The existing rule, which this must not walk around: two answers is not a type."""
        self.write("svc.py",
                   "class A:\n    def go(self):\n        return 1\n"
                   "class B:\n    def go(self):\n        return 2\n"
                   "def one() -> A:\n    return A()\n"
                   "def two() -> B:\n    return B()\n")
        self.write("app.py", "import svc\n"
                             "def use(flag):\n"
                             "    c = svc.one()\n"
                             "    if flag:\n        c = svc.two()\n"
                             "    return c.go()\n")
        self.assertIsNone(self.edge(self.graph()).get("dst"))

    def test_a_later_plain_rebinding_still_clears_it(self):
        self.write("svc.py", "class Client:\n    def go(self):\n        return 1\n"
                             "def make() -> Client:\n    return Client()\n")
        self.write("app.py", "import svc\n"
                             "def use(other):\n"
                             "    c = svc.make()\n"
                             "    c = other\n"
                             "    return c.go()\n")
        self.assertIsNone(self.edge(self.graph()).get("dst"))

    def test_the_history_scrub_runs_on_every_push(self):
        """It was wired only to the publish workflow, which runs on a tag. So a private word
        written into a file, caught by the tree scan and removed from the file, sat in every
        earlier version of it for as long as nobody cut a release - and that is exactly what
        happened: two of them went unnoticed until somebody went looking, weeks of commits
        later. Fixing a file is not fixing the history, and a check that runs rarely finds
        things late."""
        with open(os.path.join(HERE, ".github", "workflows", "tests.yml"), encoding="utf-8") as fh:
            wf = fh.read()
        self.assertIn("scrub.py --history", wf,
                      "the history scrub must run on every push, not only on a release")
        self.assertIn("fetch-depth: 0", wf,
                      "it reads every commit, so a shallow checkout would make it pass blind")


class ADoorAnAgentCanKnockOn(Sandbox):
    """Everything this tool knows, an agent has to shell out for and then parse prose.

    That is the difference between a tool a person runs a few times a day and one an agent
    leans on constantly: the agent needs a door, and the door is MCP - line-delimited JSON-RPC
    over stdin and stdout. No new dependency, no network, no daemon; the same graph, asked a
    different way.

    Written before the server existed, so every one of these was red first."""

    def rpc(self, *messages, cwd=None):
        """Speak to the server the way a client does: one JSON object per line, replies the
        same. Returns the parsed replies, in order."""
        payload = "".join(json.dumps(m) + "\n" for m in messages)
        r = subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "mcp"],
                           input=payload, capture_output=True, text=True,
                           cwd=cwd or self.dir, timeout=120)
        out = []
        for line in r.stdout.splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out, r

    def build_a_tree(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/core.py", "def load():\n    return 1\n\n\ndef save(x):\n    return x\n")
        self.write("pkg/app.py", "from pkg.core import load\n\n\ndef run():\n    return load()\n")
        # `build` writes the graph itself; the server is a separate process reading it from
        # disk, which is exactly how a real client will meet it.
        return self.graph()

    # A method rather than a class attribute: a mutable one needs a ClassVar annotation to
    # satisfy the linter, and a fresh dict per call is what every caller here wants anyway.
    # Taken from the module rather than written out again, so the protocol version lives in
    # exactly one place and the no-dates rule has one line to make an exception for.
    def hello(self):
        return {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": codegraph.MCP_PROTOCOL, "capabilities": {},
                           "clientInfo": {"name": "test", "version": "0"}}}

    # ------------------------------------------------------------------ it speaks the protocol
    def test_it_answers_initialize(self):
        self.build_a_tree()
        replies, r = self.rpc(self.hello())
        self.assertTrue(replies, r.stderr[-400:])
        self.assertEqual(replies[0]["id"], 1)
        self.assertIn("serverInfo", replies[0]["result"])
        self.assertIn("protocolVersion", replies[0]["result"])

    def test_it_lists_its_tools(self):
        self.build_a_tree()
        replies, _ = self.rpc(self.hello(),
                              {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in replies[-1]["result"]["tools"]]
        self.assertIn("codegraph_callers", names)
        self.assertIn("codegraph_blast", names)

    def test_every_tool_says_what_it_takes(self):
        """A tool with no schema is a tool an agent guesses at, and a guessed argument is a
        failed call the model then tries to reason its way out of."""
        self.build_a_tree()
        replies, _ = self.rpc(self.hello(),
                              {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        for t in replies[-1]["result"]["tools"]:
            with self.subTest(tool=t["name"]):
                self.assertTrue(t.get("description"), t["name"])
                self.assertEqual(t["inputSchema"]["type"], "object")

    # ------------------------------------------------------------------------ it answers
    def test_it_answers_who_calls_this(self):
        self.build_a_tree()
        replies, r = self.rpc(self.hello(),
                              {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "codegraph_callers",
                                          "arguments": {"name": "load"}}})
        body = json.dumps(replies[-1])
        self.assertIn("pkg/app.run", body, r.stderr[-400:])

    def test_it_answers_what_would_break(self):
        self.build_a_tree()
        replies, _ = self.rpc(self.hello(),
                              {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                               "params": {"name": "codegraph_blast",
                                          "arguments": {"name": "load"}}})
        self.assertIn("pkg/app.run", json.dumps(replies[-1]))

    def test_an_answer_is_text_an_agent_can_read(self):
        """MCP hands content back as typed parts. A raw Python repr in there is a thing the
        model has to decode before it can use it."""
        self.build_a_tree()
        replies, _ = self.rpc(self.hello(),
                              {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                               "params": {"name": "codegraph_where",
                                          "arguments": {"name": "save"}}})
        content = replies[-1]["result"]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("core.py", content[0]["text"])

    # ------------------------------------------------------------- it fails like a server
    def test_an_unknown_method_is_an_error_not_a_crash(self):
        self.build_a_tree()
        replies, r = self.rpc(self.hello(),
                              {"jsonrpc": "2.0", "id": 6, "method": "does/not/exist"})
        self.assertIn("error", replies[-1])
        self.assertEqual(r.returncode, 0, "a bad request must not take the server down")

    def test_an_unknown_tool_is_an_error_not_a_crash(self):
        self.build_a_tree()
        replies, _ = self.rpc(self.hello(),
                              {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                               "params": {"name": "codegraph_nonsense", "arguments": {}}})
        self.assertIn("error", replies[-1])

    def test_a_broken_line_does_not_stop_the_next_one(self):
        """A client that writes half a line, or a stray newline, must not end the session.
        The agent has no way to tell a crashed server from a slow one."""
        payload = "not json at all\n" + json.dumps(self.hello()) + "\n"
        self.build_a_tree()
        r = subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "mcp"],
                           input=payload, capture_output=True, text=True,
                           cwd=self.dir, timeout=120)
        replies = [json.loads(x) for x in r.stdout.splitlines() if x.strip()]
        self.assertTrue(any(m.get("id") == 1 for m in replies), r.stdout[-300:])

    def test_a_notification_gets_no_reply(self):
        """A JSON-RPC message with no id is a notification. Answering one is a protocol error
        and some clients hang waiting for a response they will never match."""
        self.build_a_tree()
        replies, _ = self.rpc(self.hello(),
                              {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual([m.get("id") for m in replies], [1])

    def test_a_missing_graph_is_an_answer_not_a_traceback(self):
        """The commonest first run: an agent asks before anything has been built."""
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        replies, r = self.rpc(self.hello(),
                              {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                               "params": {"name": "codegraph_callers",
                                          "arguments": {"name": "load"}}}, cwd=empty)
        self.assertEqual(r.returncode, 0)
        said = json.dumps(replies[-1]).lower()
        self.assertIn("build", said, said[:300])

    # ------------------------------------------------------------------------- the controls
    def test_it_writes_nothing(self):
        """It reports and changes nothing - the same promise the CLI makes."""
        self.build_a_tree()
        before = {p: os.stat(os.path.join(dp, p)).st_mtime
                  for dp, _, fs in os.walk(self.dir) for p in fs}
        self.rpc(self.hello(), {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                              "params": {"name": "codegraph_blast",
                                         "arguments": {"name": "load"}}})
        after = {p: os.stat(os.path.join(dp, p)).st_mtime
                 for dp, _, fs in os.walk(self.dir) for p in fs}
        self.assertEqual(before, after)

    def test_nothing_but_json_goes_to_stdout(self):
        """stdout IS the protocol. One stray print and the client cannot parse the stream."""
        self.build_a_tree()
        _, r = self.rpc(self.hello(),
                        {"jsonrpc": "2.0", "id": 10, "method": "tools/list"})
        for line in r.stdout.splitlines():
            if line.strip():
                json.loads(line)                     # raises if anything else was printed


class TheShapeOfAFileWithoutItsBody(Sandbox):
    """An agent asked to change one function reads the whole file to find it. A 900-line module
    costs 900 lines of context to learn six signatures.

    `codegraph shape` returns the signatures and nothing else: what a file offers, at what line,
    without the code in between. Parsed on demand rather than stored in the graph, so it is
    right even when the graph is stale - and reading one file is cheaper than the staleness
    check would be.

    Written before the command existed."""

    SOURCE = (
        '"""A store, and how to open one."""\n'
        "import os\n"
        "\n"
        "\n"
        "class Store:\n"
        '    """Holds things."""\n'
        "\n"
        "    def __init__(self, path: str) -> None:\n"
        "        self.path = path\n"
        "        self.cache = {}\n"
        "\n"
        "    @property\n"
        "    def size(self) -> int:\n"
        "        return len(self.cache)\n"
        "\n"
        "    async def flush(self, *, force: bool = False):\n"
        "        SENTINEL_INSIDE_A_BODY = 1\n"
        "        return SENTINEL_INSIDE_A_BODY\n"
        "\n"
        "\n"
        "def load(path: str, *, strict: bool = False) -> Store:\n"
        "    return Store(path)\n"
    )

    def shape_of(self, target, extra=()):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = codegraph._main(["shape", target, *extra])
        return rc, out.getvalue()

    def setUp(self):
        super().setUp()
        self.write("pkg/__init__.py", "")
        self.write("pkg/store.py", self.SOURCE)
        self.graph()

    # ------------------------------------------------------------------------- what it shows
    def test_it_names_every_definition(self):
        _, text = self.shape_of(os.path.join(self.dir, "pkg/store.py"))
        for name in ("Store", "__init__", "size", "flush", "load"):
            with self.subTest(name=name):
                self.assertIn(name, text)

    def test_it_keeps_the_signature(self):
        """The signature is the whole point: an agent has to know what to pass without opening
        the file."""
        _, text = self.shape_of(os.path.join(self.dir, "pkg/store.py"))
        self.assertIn("path: str", text)
        self.assertIn("strict: bool = False", text)
        self.assertIn("-> Store", text)

    def test_it_shows_a_decorator_because_it_changes_the_call(self):
        """`size` is a property. Calling it as a method is the mistake this prevents."""
        _, text = self.shape_of(os.path.join(self.dir, "pkg/store.py"))
        self.assertIn("@property", text)

    def test_it_says_async(self):
        _, text = self.shape_of(os.path.join(self.dir, "pkg/store.py"))
        self.assertIn("async def flush", text)

    def test_it_gives_a_line_number_to_jump_to(self):
        _, text = self.shape_of(os.path.join(self.dir, "pkg/store.py"))
        self.assertRegex(text, r"\b21\b.*def load|def load.*\b21\b")

    def test_the_body_is_not_in_it(self):
        """The control that decides whether this saves anything at all."""
        _, text = self.shape_of(os.path.join(self.dir, "pkg/store.py"))
        self.assertNotIn("SENTINEL_INSIDE_A_BODY", text)

    def test_a_method_reads_as_belonging_to_its_class(self):
        """Flat, `__init__` could be anybody's."""
        _, text = self.shape_of(os.path.join(self.dir, "pkg/store.py"))
        lines = [l for l in text.splitlines() if "__init__" in l or "class Store" in l]
        self.assertEqual(len(lines), 2, text)
        # Measured AFTER the line number, not from the start of the line: every row begins with
        # a right-aligned number, so the leading whitespace is the same on both and the first
        # version of this compared 5 against 5 and called it a failure of the code.
        bare = [re.sub(r"^\s*\d+ {2}", "", l) for l in lines]
        depth = [len(b) - len(b.lstrip()) for b in bare]
        self.assertGreater(depth[1], depth[0], text)

    # -------------------------------------------------------------------- how it is asked
    def test_it_takes_a_module_id_too(self):
        """An agent that has been reading graph output has ids, not paths."""
        rc, text = self.shape_of("pkg/store")
        self.assertEqual(rc, 0, text)
        self.assertIn("def load", text)

    def test_it_is_also_an_mcp_tool(self):
        replies = []
        payload = "".join(json.dumps(m) + "\n" for m in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "codegraph_shape", "arguments": {"name": "pkg/store"}}}))
        r = subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "mcp"],
                           input=payload, capture_output=True, text=True,
                           cwd=self.dir, timeout=120)
        for line in r.stdout.splitlines():
            if line.strip():
                replies.append(json.loads(line))
        self.assertIn("def load", json.dumps(replies[-1]), r.stderr[-300:])

    # ------------------------------------------------------------------- when it cannot
    def test_a_file_that_does_not_parse_says_so(self):
        self.write("pkg/broken.py", "def oops(:\n")
        rc, text = self.shape_of(os.path.join(self.dir, "pkg/broken.py"))
        self.assertEqual(rc, 1)
        self.assertIn("parse", text.lower() + " ")

    def test_a_missing_file_says_so(self):
        rc, text = self.shape_of(os.path.join(self.dir, "pkg/nope.py"))
        self.assertEqual(rc, 1)
        self.assertNotIn("Traceback", text)

    def test_a_file_with_nothing_in_it_is_not_an_error(self):
        """An empty module is a normal thing to meet, and "no definitions" is an answer."""
        self.write("pkg/empty.py", "# nothing here yet\n")
        rc, text = self.shape_of(os.path.join(self.dir, "pkg/empty.py"))
        self.assertEqual(rc, 0, text)


class WhatTheAnswerSavedYou(Sandbox):
    """codegraph reports its resolution rate - a number about itself. It never reports the
    number a user actually cares about: how much reading this answer replaced.

    An agent has no way to know that asking was cheaper than opening the files, so it opens the
    files anyway. The saving is only real if it is stated, and only honest if the baseline is
    stated with it - which is `you would otherwise have read these files whole`, an assumption,
    written down as one.

    Written before the footer existed."""

    def setUp(self):
        super().setUp()
        self.write("pkg/__init__.py", "")
        self.write("pkg/core.py", "def load():\n    return 1\n" + "# padding\n" * 300)
        self.write("pkg/app.py", "from pkg.core import load\n\n\ndef run():\n    return load()\n"
                                 + "# padding\n" * 200)
        self.graph()

    def call(self, tool, **args):
        payload = "".join(json.dumps(m) + "\n" for m in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": tool, "arguments": args}}))
        r = subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "mcp"],
                           input=payload, capture_output=True, text=True,
                           cwd=self.dir, timeout=120)
        replies = [json.loads(x) for x in r.stdout.splitlines() if x.strip()]
        return replies[-1]["result"]["content"][0]["text"], r

    def test_the_shape_of_a_file_says_what_it_replaced(self):
        with open(os.path.join(self.dir, "pkg/core.py"), "rb") as fh:
            real = fh.read().count(b"\n") + 1        # counted, not assumed: the first version
        text, _ = self.call("codegraph_shape", name="pkg/core")   # of this hardcoded 302 and
        self.assertIn(str(real), text, text[:300])   # the file was 303
        self.assertRegex(text.lower(), r"instead of|rather than|whole")

    def test_a_list_of_callers_says_which_files_it_saved_opening(self):
        text, _ = self.call("codegraph_callers", name="load")
        self.assertRegex(text.lower(), r"\bfile|line", text[:300])

    def test_the_baseline_is_named_not_implied(self):
        """A saving with no stated baseline is a marketing number. The claim here is `you would
        otherwise have read these files whole`, and it has to be readable as that."""
        text, _ = self.call("codegraph_shape", name="pkg/core")
        self.assertRegex(text.lower(), r"reading .*whole")

    def test_nothing_is_claimed_for_an_empty_answer(self):
        """`no symbol named that` saved nobody anything, and a footer under it is noise that
        makes the next real one easier to skip."""
        text, _ = self.call("codegraph_callers", name="definitely_not_here")
        self.assertNotRegex(text.lower(), r"instead of|saved")

    def test_the_answer_is_still_the_first_thing(self):
        """A footer under the answer, never mixed into it: the caller ids have to stay
        readable line by line."""
        text, _ = self.call("codegraph_callers", name="load")
        self.assertTrue(text.splitlines()[0].startswith("pkg/app.run"), text[:200])

    def test_a_file_it_cannot_size_is_left_out_rather_than_guessed(self):
        """A graph can outlive the files it names. An estimate built on a file that is gone is
        a wrong number, and a smaller baseline is the honest answer."""
        os.remove(os.path.join(self.dir, "pkg/core.py"))
        _, r = self.call("codegraph_where", name="run")
        self.assertNotIn("Traceback", r.stderr, r.stderr[-300:])

    # ------------------------------------------------------------------------- the control
    def test_the_command_line_is_unchanged_unless_asked(self):
        """Every script anybody has written against this parses the current output. The footer
        is opt-in on the CLI for that reason, and default only where an agent reads it."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            codegraph._main(["callers", "load"])
        self.assertEqual(out.getvalue().strip(), "pkg/app.run")

    def test_and_it_appears_when_asked(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            codegraph._main(["callers", "load", "--saved"])
        self.assertRegex(out.getvalue().lower(), r"instead of|whole")


class AnAnswerThatCouldNotSeeEverything(Sandbox):
    """The graph is never stale - every query rebuilds it if the tree moved, so `is this
    current?` already has an answer and a command to ask it would be decoration.

    The real gap is next door, and it is worse: a file that will not PARSE is skipped. The
    build records it, the command line prints `skipped ...` to stderr, and over MCP it vanishes
    entirely. So an agent halfway through an edit asks who calls a function, gets a complete
    looking list, and never learns that one file in the tree was not read - which is exactly
    where the caller it is about to break would be.

    An incomplete answer that looks complete is the failure this tool exists to prevent."""

    def setUp(self):
        super().setUp()
        self.write("pkg/__init__.py", "")
        self.write("pkg/core.py", "def load():\n    return 1\n")
        self.write("pkg/app.py", "from pkg.core import load\n\n\ndef run():\n    return load()\n")
        self.graph()

    def call(self, tool="codegraph_callers", **args):
        payload = "".join(json.dumps(m) + "\n" for m in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": tool, "arguments": args or {"name": "load"}}}))
        r = subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "mcp"],
                           input=payload, capture_output=True, text=True,
                           cwd=self.dir, timeout=120)
        replies = [json.loads(x) for x in r.stdout.splitlines() if x.strip()]
        return replies[-1]["result"]["content"][0]["text"], r

    def break_one(self, rel="pkg/broken.py"):
        self.write(rel, "def half_edited(:\n    return load()\n")

    def test_the_agent_is_told_a_file_could_not_be_read(self):
        self.break_one()
        text, _ = self.call()
        self.assertIn("broken.py", text, text[:300])

    def test_and_told_what_that_means_for_the_answer(self):
        """A filename on its own is a curiosity. What matters is that the list it just read
        may be missing something."""
        self.break_one()
        text, _ = self.call()
        self.assertRegex(text.lower(), r"incomplete|may be missing|not read")

    def test_it_matters_most_when_the_answer_is_empty(self):
        """`no callers` and `no callers, and one file was not read` are different facts, and
        an agent acts on the first by deleting something."""
        self.break_one()
        text, _ = self.call(name="run")
        self.assertIn("broken.py", text, text[:300])

    def test_the_answer_is_still_the_first_thing(self):
        self.break_one()
        text, _ = self.call()
        self.assertTrue(text.splitlines()[0].startswith("pkg/app.run"), text[:200])

    def test_many_unreadable_files_do_not_swamp_the_answer(self):
        """Twenty names above a one-line answer is a different kind of unusable."""
        for i in range(20):
            self.break_one(f"pkg/broken{i}.py")
        text, _ = self.call()
        self.assertLess(len(text.splitlines()), 12, text)
        self.assertIn("20", text)

    # ------------------------------------------------------------------------- the control
    def test_a_tree_it_read_completely_says_nothing(self):
        """If it appears on every answer it is wallpaper, and the one that matters gets
        skipped with the rest."""
        text, _ = self.call()
        self.assertNotRegex(text.lower(), r"could not|incomplete|not read")
