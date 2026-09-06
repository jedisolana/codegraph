#!/usr/bin/env python3
"""Break codegraph one small way at a time and see whether the suite notices.

A test suite that never fails is not evidence of anything. This changes the tool - flips a
comparison, swaps an `and` for an `or`, drops a `not`, moves a number by one - and runs the
suite against each change. Every mutation should make something go red. One that does not is a
behaviour nothing is checking.

    python3 tools/mutation.py            # a sample of sixty
    python3 tools/mutation.py 200        # a bigger sample
    python3 tools/mutation.py 200 7      # ...with a particular seed
    python3 tools/mutation.py 839 0 250  # ...skipping the first 250 of it

The third number is where to start, and it exists because a full pass takes hours and hours is
long enough to be killed - by a machine running short of memory, or by whoever needs the
laptop. The sample is drawn from the seed before anything is skipped, so the same count and
seed always describe the same list, and a run that stopped after 250 continues with `250`
rather than starting again.

It edits codegraph.py in place and puts it back, so it refuses to start unless git says the
tree is clean, and it verifies the file byte for byte before it exits. A full run takes hours;
do that on a `git clone` of the repository rather than the copy you are working in, because
while it runs the file on disk is a broken one.
"""
import ast
import contextlib
import hashlib
import os
import random
import resource
import shutil
import signal
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


# A mutant that loops does not just waste ninety seconds - it ALLOCATES while it spins, and
# nothing here bounds that. Two of them were left running on a 16GB machine and reached 460GB
# and 422GB of address space between them, which pushed every byte of swap out and took the
# whole desktop down with it.
#
# Two separate failures produced that, and both are fixed below:
#
#   1. the child outlived its parent.  `subprocess.run(timeout=)` kills the child when the
#      TIMEOUT fires, but nothing kills it when the HARNESS ITSELF is killed - a Ctrl-C, a
#      `pkill`, an out-of-memory kill. The child is then orphaned onto init and, being an
#      infinite loop, never stops. It is started in its own session now and the whole group
#      is killed, from the timeout path and from a signal handler both.
#   2. nothing capped what it could allocate.  A ceiling is what makes a runaway mutant fail
#      instead of taking the machine with it - waiting ninety seconds is far too long to
#      notice 400GB of address space being asked for.
CHILD_MEMORY_CAP = 4 * 1024 ** 3          # generous for a test suite, fatal to a runaway loop
_running: list[subprocess.Popen] = []


def _cap_child():
    """Runs in the child between fork and exec."""
    os.setsid()                            # its own process group, so the whole tree is killable
    with contextlib.suppress(Exception):
        resource.setrlimit(resource.RLIMIT_AS, (CHILD_MEMORY_CAP, CHILD_MEMORY_CAP))


def _kill_group(proc):
    """Kill the child AND anything it started. The suite runs the CLI as a subprocess, so
    killing only the direct child leaves grandchildren behind."""
    with contextlib.suppress(Exception):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=10)


# The child watches for its own parent dying. A signal handler covers Ctrl-C and an ordinary
# `pkill`, and covers nothing at all against SIGKILL - which cannot be caught, and which is
# what an out-of-memory kill and an impatient `kill -9` both send. The parent is then gone and
# an infinite-loop mutant runs until the machine does. Nothing outside the child can fix that,
# so the child checks: reparented onto init means the harness is dead and there is no one left
# to report to.
CHILD = """
import os, sys, threading, time, unittest
def _orphan_watch():
    while True:
        if os.getppid() == 1:
            os._exit(137)
        time.sleep(2)
threading.Thread(target=_orphan_watch, daemon=True).start()
unittest.main(module=None, argv=["mutant", "discover", "-s", "tests", "-q", "--failfast"])
"""


def _run_suite(timeout):
    """The suite, in its own process group, under a memory ceiling, killed as a group."""
    proc = subprocess.Popen([sys.executable, "-c", CHILD],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, preexec_fn=_cap_child)
    _running.append(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise
    finally:
        if proc in _running:
            _running.remove(proc)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _on_signal(signum, _frame):
    """Do not leave a spinning child behind on the way out."""
    for proc in list(_running):
        _kill_group(proc)
    sys.exit(128 + signum)


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
        start = int(argv[2]) if len(argv) > 2 else 0
    except ValueError:
        # It used to raise the ValueError itself, so `--help` - the first thing anybody types
        # at an unfamiliar script - answered with a traceback out of int().
        sys.exit(f"usage: mutation.py [count] [seed] [start]   whole numbers; got {' '.join(argv)!r}\n"
                 f"       mutation.py --help                    what this does and why")
    if want < 1:
        sys.exit("a run of no mutations proves nothing")
    marker_path = os.path.join(os.path.dirname(TARGET), ".mutation-running")
    if os.path.exists(marker_path):
        # Say what happened, rather than leaving "commit or stash" to be read as advice about
        # work somebody did themselves.
        with open(marker_path, encoding="utf-8") as fh:
            where = fh.readline().strip()
        sys.exit(f"a previous run was killed before it could put codegraph.py back.\n"
                 f"  the file on disk is a mutated, comment-stripped rewrite of the real one\n"
                 f"  restore it:  git checkout -- codegraph.py\n"
                 f"  or from:     {where}\n"
                 f"  then remove: {marker_path}")
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
    # Sample first, THEN skip. Drawing a shorter sample for a later slice would draw a
    # different set, and the point of the third argument is that the list does not move.
    sample = random.sample(every, min(want, len(every)))
    drawn = len(sample)
    if start:
        if start >= drawn:
            sys.exit(f"nothing to do: the sample is {drawn} long and starts at {start}")
        sample = sample[start:]
    print(f"{len(every)} possible mutations; trying {len(sample)}"
          + (f" (of {drawn}, skipping the first {start})" if start else ""), flush=True)
    survivors, hung, killed, t0 = [], [], 0, time.time()
    tried = 0

    # A breadcrumb, because the restore below is in a `finally` and a `finally` does not run
    # when the process is killed - which is how a long run actually ends when a machine runs
    # short of memory. What it leaves behind is not one flipped comparison: every mutation is
    # written back with ast.unparse, so the file on disk has lost the shebang and every comment
    # in it. That is a 2,500-line diff and nothing on screen to say why.
    #
    # Written on the LAST line before the try that removes it. An earlier version wrote it
    # beside the backup, several exits higher up, so `mutation.py 10 0 99` - which does nothing
    # at all - left the breadcrumb behind and the next run reported a crash that never
    # happened. A marker for "this is in progress" has to be created where the progress does.
    marker = os.path.join(os.path.dirname(TARGET), ".mutation-running")
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write(f"{backup}\n{before}\n")
    for _sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(_sig, _on_signal)
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
                r = _run_suite(90)
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
        with contextlib.suppress(OSError):
            os.remove(marker)
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
