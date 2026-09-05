"""Tests for codegraph.

The tool ships its own `--selftest`, and that is deliberate: a single file you can copy into a
repo should be able to prove itself there, with no test runner and no checkout. This suite runs
that selftest as one case and then covers what it cannot -- the CLI, the on-disk contract, the
cache, and the honesty of the confidence labels -- plus the best available fixture, which is
codegraph reading its own source.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
        self.assertEqual((e.get("dst"), e["confidence"]), (None, "EXTERNAL"))

    def test_a_method_on_an_unknown_object_stays_external(self):
        self.write("m.py", "def write():\n    return 1\n")
        self.write("u.py", "def r(fh):\n    return fh.write('x')\n")
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
        self.assertGreater(s["in_tree_resolution_rate"], 0.5, s)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
