#!/usr/bin/env python3
"""Break codegraph one small way at a time and see whether the suite notices.

A test suite that never fails is not evidence of anything. This changes the tool - flips a
comparison, swaps an `and` for an `or`, drops a `not`, moves a number by one - and runs the
suite against each change. Every mutation should make something go red. One that does not is a
behaviour nothing is checking.

    python3 tools/mutation.py            # a sample of sixty
    python3 tools/mutation.py 200        # a bigger sample
    python3 tools/mutation.py 200 7      # ...with a particular seed

It edits codegraph.py in place and puts it back, so it refuses to start unless git says the
tree is clean, and it verifies the file byte for byte before it exits. A full run takes hours;
do that on a `git clone` of the repository rather than the copy you are working in, because
while it runs the file on disk is a broken one.
"""
import ast
import hashlib
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "codegraph.py")

FLIP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt,
        ast.Gt: ast.LtE, ast.LtE: ast.Gt, ast.In: ast.NotIn, ast.NotIn: ast.In,
        ast.Is: ast.IsNot, ast.IsNot: ast.Is}


def candidates(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in FLIP:
            yield "compare", node.lineno, type(node.ops[0]).__name__
        elif isinstance(node, ast.BoolOp):
            yield "boolop", node.lineno, type(node.op).__name__
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            yield "not", node.lineno, ""
        elif (isinstance(node, ast.Constant) and isinstance(node.value, int)
              and not isinstance(node.value, bool)):
            yield "int", node.lineno, str(node.value)


class Mutator(ast.NodeTransformer):
    """Applies exactly one change, the first of its kind on the given line."""

    def __init__(self, kind, line):
        self.kind, self.line, self.done = kind, line, False

    def generic_visit(self, node):
        node = super().generic_visit(node)
        if self.done or getattr(node, "lineno", None) != self.line:
            return node
        if self.kind == "compare" and isinstance(node, ast.Compare) and len(node.ops) == 1:
            if type(node.ops[0]) in FLIP:
                node.ops = [FLIP[type(node.ops[0])]()]
                self.done = True
        elif self.kind == "boolop" and isinstance(node, ast.BoolOp):
            node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            self.done = True
        elif self.kind == "not" and isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            self.done = True
            return node.operand
        elif (self.kind == "int" and isinstance(node, ast.Constant)
              and isinstance(node.value, int) and not isinstance(node.value, bool)):
            self.done = True
            return ast.copy_location(ast.Constant(value=node.value + 1), node)
        return node


def clean_tree():
    git = shutil.which("git") or "git"
    r = subprocess.run([git, "status", "--porcelain", "--", "codegraph.py"],
                       cwd=ROOT, capture_output=True, text=True)
    return r.returncode == 0 and not r.stdout.strip()


def main(argv):
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__.strip())
        return 0
    try:
        want = int(argv[0]) if argv else 60
        seed = int(argv[1]) if len(argv) > 1 else 0
    except ValueError:
        # It used to raise the ValueError itself, so `--help` - the first thing anybody types
        # at an unfamiliar script - answered with a traceback out of int().
        sys.exit(f"usage: mutation.py [count] [seed]   both whole numbers; got {' '.join(argv)!r}\n"
                 f"       mutation.py --help           what this does and why")
    if want < 1:
        sys.exit("a run of no mutations proves nothing")
    if not clean_tree():
        sys.exit("codegraph.py has uncommitted changes - commit or stash them first, because "
                 "this rewrites the file and an interrupted run would be hard to tell apart")
    with open(TARGET, encoding="utf-8") as fh:
        text = fh.read()
    before = hashlib.sha256(text.encode()).hexdigest()
    backup = os.path.join(tempfile.mkdtemp(), "codegraph.py")
    shutil.copy(TARGET, backup)

    every = list(candidates(ast.parse(text)))
    random.seed(seed)
    sample = random.sample(every, min(want, len(every)))
    print(f"{len(every)} possible mutations; trying {len(sample)}", flush=True)
    survivors, hung, killed, t0 = [], [], 0, time.time()
    tried = 0
    try:
        for kind, line, detail in sample:
            m = Mutator(kind, line)
            changed = m.visit(ast.parse(text))
            if not m.done:
                continue                      # another candidate on that line got there first
            try:
                code = ast.unparse(ast.fix_missing_locations(changed))
            except Exception:
                continue                      # an unparseable mutation is not a mutation
            with open(TARGET, "w", encoding="utf-8") as fh:
                fh.write(code)
            # A short leash. The suite runs in about eight seconds, and some mutations do not
            # make it FAIL - they make it never finish. Flip the comparison that ends a `while`
            # and the tool loops for ever. At ten minutes each, one of those ended a run of
            # seven hundred by raising TimeoutExpired straight through this harness.
            try:
                r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests",
                                    "-q", "--failfast"],
                                   cwd=ROOT, capture_output=True, text=True, timeout=90)
            except subprocess.TimeoutExpired:
                hung.append((line, kind, detail))
                killed += 1          # a change that makes it spin for ever is one somebody notices
                tried += 1
                print(f"  HUNG      line {line}: {kind} {detail}", flush=True)
                continue
            tried += 1
            if r.returncode == 0:
                survivors.append((line, kind, detail))
                print(f"  SURVIVED  line {line}: {kind} {detail}", flush=True)
            else:
                killed += 1
            if tried % 25 == 0:
                # A run of seven hundred takes twenty minutes, and stdout is a pipe as often as
                # a terminal. Without this it prints nothing at all until it is finished, which
                # looks exactly like being stuck - as it did the first time it was left running.
                done = tried / len(sample)
                left = (time.time() - t0) * (1 - done) / done
                print(f"  {tried}/{len(sample)}  {killed} killed, {len(survivors)} survived"
                      f"  ~{left/60:.0f} min left", flush=True)
    finally:
        shutil.copy(backup, TARGET)
        with open(TARGET, encoding="utf-8") as fh:
            after = hashlib.sha256(fh.read().encode()).hexdigest()
        if after != before:
            sys.exit(f"codegraph.py was NOT restored - put it back from {backup}")
    print(f"{killed + len(survivors)} mutations in {time.time() - t0:.0f}s: "
          f"{killed} killed ({len(hung)} of them by hanging), {len(survivors)} survived",
          flush=True)
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
