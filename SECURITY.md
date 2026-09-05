# Security

codegraph reads source code and writes a graph of it. That is a small surface, but it is not
nothing: you point it at a folder, and it opens every Python file it finds there.

## Reporting

Please **do not open a public issue** for a security problem. Use GitHub's private report:

**Security tab → Report a vulnerability**, on this repository. It goes only to the maintainer.

Say what you found, how to reproduce it, and what it lets someone do. You will get a reply.

## What it does, exactly

- **It never runs your code.** Files are parsed with the standard library's `ast` module and
  walked as a tree. Nothing is imported, `exec`'d, or evaluated — so a file that would delete
  your home directory when imported is, to this tool, a shape.
- **It never uses the network.** There is no HTTP client in it, no telemetry, no update check.
  It imports fourteen standard-library modules and nothing else.
- **It writes exactly two files**: `codegraph.json` and `codegraph.cache.json`, in the
  directory you run it from, or wherever `CODEGRAPH_OUT` and `CODEGRAPH_CACHE` point. Both are
  written to a temporary name and renamed into place, so an interrupted build cannot leave a
  half-written graph behind.
- **It does not follow symlinked directories.** A link to `/usr/lib` inside your project would
  otherwise turn a small repo into an unbounded walk. Broken links are skipped and counted.

## What the graph contains

Names, kinds, line numbers, and the relationships between them — **not your source text**. The
one thing worth knowing before you commit it: it also records the **absolute path of every file
it read**, which on most machines includes your username and your directory layout.

If you build a graph inside a repository, add this to `.gitignore`:

```
codegraph.json
codegraph.cache.json
```

## What is deliberately not restricted

**The folder you point it at.** `codegraph build <dir>` reads any directory you name, anywhere
on the machine. That is the feature, and the person running it is the person choosing what gets
read. There is no daemon, no server, and no port: it runs, it writes, it exits.

## Static analysis

A code scanner will flag the file operations here as "uncontrolled data used in a path
expression", because the paths come from arguments and environment variables. That is accurate
and it is the design: a tool you point at a directory takes the directory from you. What is
enforced is that it cannot wander out of the tree it was given — symlinked directories are not
followed, and the locations it reports are resolved against the roots it was actually built
from.
