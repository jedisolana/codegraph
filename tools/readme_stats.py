#!/usr/bin/env python3
"""Rewrite the stats block in README.md with what the tool currently reports.

The README publishes a `stats` block as real output and a test compares every figure to a
fresh build, because the block used to be hand-written and eight of its eleven numbers had
drifted. The block measures this whole repository, so adding a test moves it - which makes
refreshing it a routine step rather than an event, and a routine step should be one command.

    python3 tools/readme_stats.py          # rewrite the block
    python3 tools/readme_stats.py --check  # report whether it is stale, change nothing

It rewrites three things and nothing else: the json block, and the two sentences underneath
that quote the resolution rate back in prose. Those move together on purpose - a block that
updates while the paragraph beside it still says the old number is the failure this exists to
prevent, not a smaller version of it.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(HERE, "README.md")
BLOCK = re.compile(r"```json\n(\{.*?\n\})\n```", re.S)


def measure():
    out = tempfile.mkdtemp()
    env = {**os.environ, "CODEGRAPH_OUT": os.path.join(out, "g.json"),
           "CODEGRAPH_CACHE": os.path.join(out, "c.json")}
    r = subprocess.run([sys.executable, os.path.join(HERE, "codegraph.py"), "build", HERE],
                       capture_output=True, text=True, env=env, cwd=out)
    if r.returncode != 0:
        sys.exit(f"the build failed:\n{r.stderr}")
    return json.loads(r.stdout)


def render(d):
    lines, cur = [], ""
    for k, v in sorted(d["edge_confidence"].items(), key=lambda kv: -kv[1]):
        piece = f'"{k}": {v}, '
        if len(cur) + len(piece) > 74:
            lines.append(cur.rstrip())
            cur = ""
        cur += piece
    lines.append(cur.rstrip().rstrip(","))
    conf = ("\n" + " " * 22).join(lines)
    return (f'{{\n  "call_edges": {d["call_edges"]},\n'
            f'  "call_sites": {d["call_sites"]},\n'
            f'  "edge_confidence": {{{conf}}},\n'
            f'  "resolved_to_one_def": {d["resolved_to_one_def"]},\n'
            f'  "could_have_been_resolved": {d["could_have_been_resolved"]},\n'
            f'  "resolution_rate": {d["resolution_rate"]}\n}}')


def main(argv):
    check = "--check" in argv
    with open(README, encoding="utf-8") as f:
        text = f.read()
    found = BLOCK.search(text)
    if not found:
        sys.exit("README.md no longer contains a json stats block")
    d = measure()
    fresh = render(d)
    if json.loads(found.group(1)) == json.loads(fresh):
        print("the block is current")
        return 0
    if check:
        print("the block is STALE - run tools/readme_stats.py to rewrite it")
        return 1
    text = BLOCK.sub("```json\n" + fresh + "\n```", text, count=1)
    text = re.sub(r"That 0\.\d+ says", f'That {d["resolution_rate"]} says', text, count=1)
    text = re.sub(r"it placed \d+%", f'it placed {round(d["resolution_rate"] * 100)}%', text,
                  count=1)
    with open(README, "w", encoding="utf-8") as f:
        f.write(text)
    print(f'rewrote the block: call_edges {d["call_edges"]}, rate {d["resolution_rate"]}')
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
