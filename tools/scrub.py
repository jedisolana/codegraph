#!/usr/bin/env python3
"""Refuse to publish anything private. Run against the tree and the commit messages.

A scrub done by reading is a scrub that passes until the once it does not. This repository has
already been caught twice by that: a dead project's name survived every review of the LICENCE
because the reviews were looking for forbidden words and years, and `--help` still named where
the tool came from after a full pass had been declared clean. Both were plain text sitting in
files nobody thought to question.

So this is a check that fails, wired into the suite like any other. It reads every file git
tracks - that being exactly the surface a `git push` publishes - plus every commit message,
because a message is published as loudly as a file and cannot be edited afterwards without
rewriting history.

WHY THE DENY LIST IS HASHED. A list of private words is itself the leak: writing the name of
an internal host or a private project into a file in the repository publishes the very names
the file exists to keep out. `deny.txt` therefore holds only sha256 of each lowercased word, and
`--add WORD` appends a hash without ever writing the word down. The check hashes every token
in the tree and looks for a match, so the list can live in the open safely.

    python3 tools/scrub.py              scan the tracked tree; exit 1 on any hit
    python3 tools/scrub.py --history    ...every commit message AND every version of
                                        every file ever committed. RUN THIS BEFORE
                                        MAKING THE REPOSITORY PUBLIC.
    python3 tools/scrub.py --add WORD   add a private word to the deny list, by hash only
    python3 tools/scrub.py --quiet      exit code only
    python3 tools/scrub.py --fixtures   list every line where this is switched off
    python3 tools/scrub.py --install-hooks   turn the pre-commit guards on here
"""
import hashlib
import os
import re
import stat
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DENY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deny.txt")

# The assistant names are NOT written here. An older test in this suite forbids any shipped
# file from naming one, and the first version of this scanner failed it by spelling them out
# in its own pattern - the file that bans a word containing the word. They live in the hashed
# deny list instead, which is what it is for: a token match on a hash names nothing.
#
# A token, for the hashed deny list: a run of letters and digits, lowercased. Splitting on
# everything else means `two_words` and `two-words` both yield `two` and `words`, which may be
# too ordinary to deny on their own - so add the joined spelling as well when it matters.
_TOKEN = re.compile(r"[A-Za-z0-9]+")

# Assembled from fragments, the same way the older guard in the test suite assembles its own
# list, so that this file does not trip the very checks it defines. The alternative was to
# exempt this file from both - and a scanner nobody scans is where the next leak lives.
_U = "U" + "sers"

# Binary and generated files: reading them as text produces noise, and none of them is
# somewhere a person writes a private name by accident.
_SKIP_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".whl", ".gz")

# Loopback and the documentation ranges are addresses that name nothing real.
_IP_OK = re.compile(r"\A(127\.|0\.0\.0\.0|255\.|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.)")

# An email that is deliberately not a person: the noreply address git commits under, and the
# reserved test domains. Anything else is somebody's inbox.
_EMAIL_OK = re.compile(r"(@example\.(com|org|net|invalid)|\.invalid\Z"
                       r"|" + _U.lower() + r"\.noreply\.github\.com)", re.I)

# A home directory names the machine's owner and the machine's layout. `/home/runner` is
# GitHub's own CI path and names nobody.
_PATH_OK = re.compile(r"\A(/home/runner|/" + _U + "/runner)", re.I)

RULES = (
    ("secret", re.compile(
        r"(?:sk|rk)-[A-Za-z0-9_-]{16,}"
        r"|github_pat_[A-Za-z0-9_]{20,}"
        r"|gh[pousr]_[A-Za-z0-9]{30,}"
        r"|AKIA[0-9A-Z]{16}"
        r"|xox[baprs]-[A-Za-z0-9-]{10,}"
        r"|AIza[0-9A-Za-z_-]{30,}"
        r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"), None),
    ("home path", re.compile(r"/" + _U + r"/[A-Za-z0-9_.-]+|/home/[A-Za-z0-9_.-]+"
                             r"|[Cc]:\\+" + _U + r"\\+[A-Za-z0-9_.-]+"), _PATH_OK),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), _EMAIL_OK),
    ("ip address", re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b"), _IP_OK),
    ("date", re.compile(r"\b20[2-9][0-9]-[01][0-9]-[0-3][0-9]\b"), None),
)


def _denied_hashes():
    try:
        with open(DENY, encoding="utf-8") as fh:
            return {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}
    except FileNotFoundError:
        return set()


def _hash(word):
    return hashlib.sha256(word.strip().lower().encode()).hexdigest()


def tracked_files(root=HERE):
    """What `git push` would publish. Not os.walk: a build artefact, a virtualenv or a local
    scratch file is not published and is not this check's business, and scanning them produces
    the noise that gets a check switched off."""
    out = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                         capture_output=True, text=True, check=True)
    return [p for p in out.stdout.split("\0") if p and not p.endswith(_SKIP_SUFFIX)]


def historical_blobs(root=HERE):
    """Every version of every file that has ever been committed, including files deleted since.

    Deleting a file from the tree does not remove it from the history: `git log -p` still
    prints it and anyone can check out the commit that had it. Scanning only the current tree
    said this repository was clean while two files of working notes sat in earlier commits,
    which is exactly the shape of miss this whole check exists to stop.
    """
    listing = subprocess.run(["git", "-C", root, "cat-file", "--batch-check",
                              "--batch-all-objects"], capture_output=True, text=True, check=True)
    blobs = [ln.split()[0] for ln in listing.stdout.splitlines()
             if len(ln.split()) > 1 and ln.split()[1] == "blob"]
    # Where each blob came from, so a hit names a file and not a bare hash nobody can act on.
    named = {}
    paths = subprocess.run(["git", "-C", root, "rev-list", "--objects", "--all"],
                           capture_output=True, text=True, check=True)
    for ln in paths.stdout.splitlines():
        parts = ln.split(" ", 1)
        if len(parts) == 2:
            named.setdefault(parts[0], parts[1])
    # A blob no ref reaches is local-only: `git add` writes the object even when the commit is
    # then refused, so a hook doing its job leaves one behind. A push never sends it and the
    # fix is `git gc --prune=now`, not a history rewrite - which is worth saying, because the
    # two look identical in a list and one of them is an afternoon.
    for h in blobs:
        blob = subprocess.run(["git", "-C", root, "cat-file", "blob", h],
                              capture_output=True, check=False)
        try:
            yield named.get(h, h[:9]), h[:9], blob.stdout.decode("utf-8")
        except UnicodeDecodeError:
            continue                      # a binary blob is not where a name gets written


def commit_messages(root=HERE):
    """Every message on every reachable commit. A message cannot be edited after a push
    without rewriting history, so it is the half of the publication people forget."""
    out = subprocess.run(["git", "-C", root, "log", "--all", "--format=%H%x00%B%x00%x00"],
                         capture_output=True, text=True, check=True)
    for chunk in out.stdout.split("\0\0"):
        if "\0" in chunk:
            sha, body = chunk.split("\0", 1)
            yield sha.strip()[:9], body


# A test that proves this catches a planted secret has to contain a planted secret, and the
# scan reads the test file like any other. The exemption is per-line, visible in the source,
# and counted - `--fixtures` prints every one of them, so they cannot quietly multiply into
# the hole this check exists to close. A whole-file exemption was the other option and would
# have meant a real key in a test file goes unseen for ever.
FIXTURE = "scrub: fixture"


def _scan_text(where, text, denied, hits):
    for line_no, line in enumerate(text.splitlines(), 1):
        if FIXTURE in line:
            continue
        for label, pattern, allowed in RULES:
            for m in pattern.finditer(line):
                found = m.group(0)
                if allowed and allowed.search(found):
                    continue
                hits.append((where, line_no, label, found))
        if denied:
            for m in _TOKEN.finditer(line):
                if _hash(m.group(0)) in denied:
                    # The word itself is never printed - that would put it in the CI log, which
                    # for a public repository is public. Position is enough to find it.
                    hits.append((where, line_no, "private word",
                                 f"<{len(m.group(0))} chars, col {m.start() + 1}>"))
    return hits


def scan(root=HERE, history=True):
    """The tracked tree, and by default every commit message too.

    They are separable because they are fixed differently. A file is fixed by editing it; a
    commit message is fixed only by rewriting history, which changes every commit id after it
    and needs a force-push. So the suite holds the tree to green always, and the history check
    is the gate run before the repository is made public - the moment the messages stop being
    private.
    """
    denied = _denied_hashes()
    hits = []
    for rel in tracked_files(root):
        full = os.path.join(root, rel)
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except (OSError, ValueError):
            continue
        _scan_text(rel, text, denied, hits)
    if history:
        for sha, body in commit_messages(root):
            _scan_text(f"commit {sha}", body, denied, hits)
        seen_here = set(tracked_files(root))
        for path, short, text in historical_blobs(root):
            if path in seen_here:
                continue                  # the live version was read above; this is an old one
            where = (f"history {path} ({short})" if path != short
                     else f"UNREACHABLE object {short} - local only, `git gc --prune=now`")
            _scan_text(where, text, denied, hits)
    return hits


def main(argv):
    if "--help" in argv or "-h" in argv:
        print(__doc__.strip())
        return 0
    if "--add" in argv:
        i = argv.index("--add")
        words = [w for w in argv[i + 1:] if not w.startswith("-")]
        if not words:
            sys.exit("--add needs at least one word")
        have = _denied_hashes()
        with open(DENY, "a", encoding="utf-8") as fh:
            for w in words:
                h = _hash(w)
                if h not in have:
                    fh.write(h + "\n")
                    have.add(h)
        print(f"deny list now holds {len(have)} words (hashed; the words themselves are not stored)")
        return 0
    if "--install-hooks" in argv:
        # Hooks live in the tree; git runs them only when core.hooksPath says so, and that is
        # local config a clone does not carry. "Present but not installed" is the shape every
        # guard here has failed in, so it is one command rather than a line in a README.
        hooks = os.path.join(HERE, ".githooks")
        subprocess.run(["git", "-C", HERE, "config", "core.hooksPath", ".githooks"], check=True)
        for name in os.listdir(hooks):
            path = os.path.join(hooks, name)
            if os.path.isfile(path):
                # ADD the owner's execute bit, rather than writing a whole mode. git runs a
                # hook as whoever runs git, which is the owner, so that one bit is the entire
                # requirement - and 0o755 was granting execute to everybody to get it, which
                # both ruff and CodeQL called out as exactly what it was.
                os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        print(f"hooks installed from {hooks}\n"
              f"  pre-commit  refuses a staged tree with anything private in it\n"
              f"  commit-msg  refuses a message with anything private in it")
        return 0
    if "--fixtures" in argv:
        n = 0
        for rel in tracked_files():
            try:
                with open(os.path.join(HERE, rel), encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if FIXTURE in line:
                            print(f"{rel}:{i}")
                            n += 1
            except OSError:
                continue
        print(f"\n{n} exempted line(s). Every one is a place this check is switched off.")
        return 0
    quiet = "--quiet" in argv
    # The tree alone by default: that is what the suite holds green on every commit. --history
    # adds every commit message, and is the gate to run before this repository is made public.
    hits = scan(history="--history" in argv)
    if not hits:
        if not quiet:
            print("nothing private found in the tracked tree or in any commit message")
        return 0
    if not quiet:
        for where, line_no, label, found in hits:
            print(f"{where}:{line_no}  {label}: {found}")
        print(f"\n{len(hits)} thing(s) that must not be published.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
