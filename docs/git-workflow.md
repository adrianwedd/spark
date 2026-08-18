# Git Workflow

**Owns:** branching, staging discipline, and how work is identified.

---

## Invariant

### `master` is trunk and the only code truth

Branch new work off `master`. `origin` is `git@github.com:adrianwedd/spark.git`.

**The Pi's live tree tracks `master`, and Cloudflare Pages auto-deploys
`site/` from it.** A merge to `master` is a deploy to a real robot and a
publish to a public website. There is no staging environment between them.

### GitHub Issues are work identity

A change's identity is its issue number, not its branch name, not a plan
document, and not a line in a memory file. Reference the issue in the commit
or PR so the reasoning stays findable after the branch is deleted.

Branch names follow `type/short-slug` — `fix/audio-sink-policy-gate`,
`feat/px-brain-persistent-session`, `docs/constitution-and-canonical-map`.

Commits use Conventional Commits with a scope: `fix(policy):`, `feat(mind):`,
`docs(prompt):`, `test(brain):`, `chore:`. Subject lines here state **what
became true**, not what was done — `a session the sink cannot read is not a
session without quiet mode`.

### Never blanket-stage

**Forbidden, without exception:**

```
git add -A
git add .
git add -u
git commit -a
git commit --all
```

**Stage exact owned paths**, then inspect what you staged:

```bash
git add path/to/file.py path/to/test_file.py docs/thing.md
git diff --cached
```

This is not style. The working tree on this robot routinely carries unrelated
dirty work — a half-finished experiment, a live-tuned constant, a runtime
artifact a daemon just dropped. Blanket staging sweeps all of it into someone
else's commit, and on a repository whose `state/` holds a child's session data
it can commit things that must never be published.

**Preserve unrelated dirty work.** If you find changes you did not make,
leave them. If they textually overlap the file you must change, commit them
*first* as their own clearly-labelled commit rather than absorbing them.

### Never end a task with a dirty tree

Code, tests, and documentation land in **one** commit, and then it is pushed.
A change that is committed but not pushed does not exist to anyone else; a
change whose docs land in a later commit is a change whose docs will not land.

### Runtime state is never committed

See [operations/state-and-runtime](operations/state-and-runtime.md). If
`git status` shows a `state/` file you did not create, it is a daemon's output
— do not stage it, and check whether it should be gitignored.

### Do not commit or push unless asked

Committing is an outward-facing action on a repository that deploys on merge.

---

## Working on the robot

The checkout at `/home/pi/picar-x-hacking` **is** the running robot. Daemons
are reading these files while you edit them.

- Changing a `bin/tool-*` takes effect on the next invocation, immediately.
- Changing a `src/pxh/` module takes effect when the owning daemon restarts.
- Changing `docs/prompts/spark-*.md` requires killing the tmux session —
  prompts bake in at launch, and `KillMode=process` means a service restart
  will not reload them. See
  [architecture/resident-brain](architecture/resident-brain.md).

### Worktrees

`.worktrees/` is gitignored and excluded from pytest collection. Use a worktree
when work needs isolation from the live tree — but remember the live daemons
still run from the main checkout.

## Multi-model QA

Independent review of a diff, run in parallel and synthesised:

```bash
hermes -z "QA prompt" 2>&1
agy --dangerously-skip-permissions --add-dir /path/to/repo --print-timeout 10m --print "QA prompt" 2>&1
gemini -p "QA prompt" 2>&1
echo "QA prompt" | codex exec --full-auto - 2>&1
```

**`agy --print` takes the prompt as its value, and must come last.** An earlier
spelling put `--print` first with the prompt trailing, so `--print` consumed
`--dangerously-skip-permissions` as its value, the prompt was never read, and
agy answered a question about the flag and exited 0.

That is the dangerous failure mode for a review tool: **a QA run that returns
cleanly having reviewed nothing looks exactly like a pass.** Check that the
output discusses your actual code before believing it. `agy`'s default timeout
is 5m, which is short for a whole-diff review.

Give `agy` a named file list and a ranked list of what to look for. It does
markedly worse with "review this branch".

---

## Why it looks like this

*History, not rule.*

The blanket-staging prohibition is written this strongly because the failure is
silent and asymmetric: `git add -A` succeeds, the commit looks clean in
`git log --oneline`, and the unrelated file only surfaces when someone else
bisects to it weeks later. The cost of `git add <paths>` is a few seconds; the
cost of the alternative is unbounded.

The one-commit rule for code + tests + docs came from documentation drift that
this very restructure exists to undo: docs promised for "a follow-up commit"
reliably did not arrive, and CLAUDE.md accumulated claims that outlived the
code they described.
