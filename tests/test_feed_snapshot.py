"""px-feed-snapshot must not touch anyone else's work in the shared checkout (#190).

`bin/px-feed-snapshot` runs from cron every 30 minutes *inside the live working
repo* on the Pi, which is the same tree every other agent and human there edits.
The defects these tests pin were all one shape — the job acting on state it did
not create:

* `git commit` with no pathspec committed **whatever was already in the index**,
  so another writer's staged work rode out in a `data: update feed snapshot`
  commit and was pushed;
* `git pull --rebase` rewrote the shared branch under a working tree somebody
  else was using;
* `git push … || true` made every one of those failures invisible.

So the properties under test are negative ones, and the assertions are on what
did *not* happen: another writer's bytes are still theirs, the index is still
theirs, the remote still has the commit it had, and nothing was rebased, stashed,
reset or cleaned. Positive assertions appear where a silence would be ambiguous
(a refusal must be reported, and a delivery must be recorded) — an untested
"did nothing" is indistinguishable from a job that silently stopped running.

Every test drives a real temporary git repository with a real bare `origin`,
because the whole subject is git's behaviour on a shared checkout: a mocked git
would only test this file's beliefs about git.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "px-feed-snapshot"
COMPONENT = "px-feed-snapshot"
DST_REL = "site/data/feed.json"

FEED_OLD = {"posts": [{"ts": "2026-09-01T00:00:00Z", "thought": "old", "mood": "calm"}],
            "updated": "2026-09-01T00:00:00Z"}
FEED_NEW = {"posts": [{"ts": "2026-09-28T00:00:00Z", "thought": "new", "mood": "curious"}],
            "updated": "2026-09-28T00:00:00Z"}


def _feed_bytes(feed: dict) -> str:
    return json.dumps(feed, indent=2) + "\n"


def _run(args, cwd, env=None):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                          env=env, check=False)


class SnapshotRepo:
    """A checkout with a bare `origin`, shaped like the Pi's shared tree."""

    def __init__(self, root: Path):
        self.root = root
        self.work = root / "work"
        self.remote = root / "origin.git"
        self.state = self.work / "state"
        self.dst = self.work / DST_REL
        self.logs = self.work / "logs"
        # `bin/px-feed-snapshot` resolves PROJECT_ROOT from its own path and
        # sources the px-env beside it, so the script under test runs from a
        # copy inside this repository — the real file, the real px-env, in a
        # throwaway tree. Pointing PROJECT_ROOT at something else would test a
        # script that does not exist.
        self.bin = self.work / "bin"
        self.script = self.bin / SCRIPT.name

    def git(self, *args, cwd=None, check=True):
        out = _run(["git", *args], cwd or self.work)
        if check and out.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {out.stderr}")
        return out

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    def remote_head(self) -> str:
        return self.git("--git-dir", str(self.remote), "rev-parse", "refs/heads/master").stdout.strip()

    def paths_in(self, sha: str) -> list[str]:
        out = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", sha).stdout
        return [p for p in out.splitlines() if p]

    def commit_all(self, message: str = "base"):
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def write_feed(self, feed: dict):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "feed.json").write_text(_feed_bytes(feed), encoding="utf-8")

    def write_feed_raw(self, text: str):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "feed.json").write_text(text, encoding="utf-8")

    def health(self) -> dict:
        path = self.state / "health" / f"{COMPONENT}.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def env(self) -> dict:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            # px-env prepends $PROJECT_ROOT/src to the PYTHONPATH it inherits, so
            # this is what lets the script's helper import pxh.health at all.
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "PROJECT_ROOT": str(self.work),
            "PX_STATE_DIR": str(self.state),
            "LOG_DIR": str(self.logs),
            # The script's helpers run under the interpreter handed to it; the
            # system python3 cannot import pxh on this host, so pin the venv.
            "PX_SNAPSHOT_PYTHON": sys.executable,
        }
        # px-env sources $PROJECT_ROOT/.env when it exists. Nothing here should
        # read the operator's real credentials, so the opt-out is explicit.
        env["PX_ENV_SKIP_DOTENV"] = "1"
        return env

    def run_snapshot(self, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        assert SCRIPT.exists(), f"missing {SCRIPT}"
        env = self.env()
        if extra_env:
            env.update(extra_env)
        return _run(["bash", str(self.script)], cwd=self.work, env=env)


@pytest.fixture
def repo(tmp_path) -> SnapshotRepo:
    root = tmp_path / "repo"
    root.mkdir()
    r = SnapshotRepo(root)
    # The script and its px-env are copied verbatim from the repository, so the
    # tests exercise the file that ships, not a fixture that resembles it.
    r.bin.mkdir(parents=True)
    for name in (SCRIPT.name, "px-env"):
        source = REPO_ROOT / "bin" / name
        assert source.exists(), f"missing {source}"
        shutil.copy2(source, r.bin / name)

    _run(["git", "init", "-q", "--bare", "-b", "master", str(r.remote)], cwd=root)
    _run(["git", "init", "-q", "-b", "master", str(r.work)], cwd=root)
    # The runtime paths the real `.gitignore` covers must be ignored here too —
    # a tracked copy would make every test start from a dirty checkout for
    # reasons that have nothing to do with the behaviour under test. `state/`
    # holds the source feed, `logs/` and `state/health/` are where this job
    # writes its own log and health record (both gitignored in the repository).
    (r.work / ".git" / "info" / "exclude").write_text(
        "state/feed.json\nstate/health/\nlogs/\n", encoding="utf-8"
    )
    r.git("config", "user.email", "test@example.com")
    r.git("config", "user.name", "test")
    r.git("config", "commit.gpgsign", "false")

    (r.work / "site" / "data").mkdir(parents=True)
    r.dst.write_text(_feed_bytes(FEED_OLD), encoding="utf-8")
    (r.work / "a.txt").write_text("a\n", encoding="utf-8")
    r.state.mkdir(parents=True, exist_ok=True)
    r.write_feed(FEED_OLD)
    r.commit_all()

    r.git("remote", "add", "origin", str(r.remote))
    r.git("push", "-q", "-u", "origin", "master")
    return r


def _foreign_commit(repo: SnapshotRepo, text: str = "foreign\n") -> str:
    """Put someone else's commit on `origin/master`, as another writer would."""
    other = repo.root / "other"
    if not other.exists():
        _run(["git", "clone", "-q", str(repo.remote), str(other)], cwd=repo.root)
        for args in (("config", "user.email", "other@example.com"),
                     ("config", "user.name", "other")):
            _run(["git", *args], cwd=other)
    path = other / "foreign.txt"
    path.write_text(text, encoding="utf-8")
    _run(["git", "add", "foreign.txt"], cwd=other)
    _run(["git", "commit", "-qm", "foreign work"], cwd=other)
    _run(["git", "push", "-q", "origin", "master"], cwd=other)
    return repo.remote_head()


def _reject_pushes(repo: SnapshotRepo, script: str = "#!/bin/sh\nexit 1\n"):
    hook = repo.remote / "hooks" / "pre-receive"
    hook.write_text(script, encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------------------
# Unrelated work in the shared checkout
# ---------------------------------------------------------------------------


def test_a_foreign_staged_file_is_not_swept_into_the_snapshot_commit(repo):
    """The reported defect: `git commit` with no pathspec commits the index."""
    (repo.work / "a.txt").write_text("staged by another writer\n", encoding="utf-8")
    repo.git("add", "a.txt")
    before = repo.head()
    repo.write_feed(FEED_NEW)

    result = repo.run_snapshot()

    assert result.returncode != 0, "a foreign staged file must stop the run"
    assert "unrelated staged changes" in result.stderr
    assert "a.txt" in result.stderr
    assert repo.head() == before, "no commit may be created at all"
    # Their staging is exactly where they left it, and still uncommitted.
    assert repo.git("diff", "--cached", "--name-only").stdout.split() == ["a.txt"]
    assert (repo.work / "a.txt").read_text(encoding="utf-8") == "staged by another writer\n"
    assert repo.remote_head() == before


def test_a_dirty_checkout_is_refused_and_left_alone(repo):
    """`git pull --rebase` in a dirty tree was defect 2; the answer is to refuse."""
    (repo.work / "a.txt").write_text("edited, not staged\n", encoding="utf-8")
    before = repo.head()
    repo.write_feed(FEED_NEW)

    result = repo.run_snapshot()

    assert result.returncode != 0
    assert "unrelated uncommitted changes" in result.stderr
    assert repo.head() == before
    assert (repo.work / "a.txt").read_text(encoding="utf-8") == "edited, not staged\n"
    assert repo.dst.read_text(encoding="utf-8") == _feed_bytes(FEED_OLD), \
        "the destination must not be replaced once the run has refused"


def test_an_untracked_file_is_refused(repo):
    (repo.work / "scratch.txt").write_text("someone is working here\n", encoding="utf-8")
    repo.write_feed(FEED_NEW)

    result = repo.run_snapshot()

    assert result.returncode != 0
    assert "untracked files present" in result.stderr
    assert "scratch.txt" in result.stderr
    assert (repo.work / "scratch.txt").read_text(encoding="utf-8") == "someone is working here\n"


def test_newline_in_an_untracked_path_cannot_hide_it(repo):
    """Git's NUL separator must survive names that contain newlines."""
    hidden = repo.work / "\nsite" / "data" / ".feed.json.tmp"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("foreign\n", encoding="utf-8")
    repo.write_feed(FEED_NEW)
    before = repo.head()

    result = repo.run_snapshot()

    assert result.returncode != 0
    assert "untracked files present" in result.stderr
    assert repo.head() == before
    assert hidden.read_text(encoding="utf-8") == "foreign\n"


def test_a_refusal_is_reported_on_the_health_board(repo):
    """Silence would read as "the cron job is fine" — the #190 defect in reverse."""
    (repo.work / "scratch.txt").write_text("x\n", encoding="utf-8")
    repo.write_feed(FEED_NEW)

    assert repo.run_snapshot().returncode != 0

    rec = repo.health()
    assert rec, "a refusal must leave a health record"
    assert rec["consecutive_failures"] >= 1
    assert "untracked files present" in rec["last_error"]


# ---------------------------------------------------------------------------
# Only its own commit may be made, and only its own commit pushed
# ---------------------------------------------------------------------------


def test_the_snapshot_commit_contains_only_the_feed(repo):
    repo.write_feed(FEED_NEW)

    result = repo.run_snapshot()

    assert result.returncode == 0, result.stderr
    assert repo.paths_in("HEAD") == [DST_REL]
    assert repo.git("log", "-1", "--format=%s").stdout.strip() == "data: update feed snapshot"
    assert repo.remote_head() == repo.head()


def test_a_commit_hook_that_stages_a_foreign_file_is_caught_before_the_push(repo):
    """The one insertion point no pre-check can see, so the commit is verified."""
    hooks = repo.work / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'hooked\\n' > hooked.txt\n"
        "git add hooked.txt\n",
        encoding="utf-8",
    )
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    before = repo.head()
    repo.write_feed(FEED_NEW)

    result = repo.run_snapshot()

    assert result.returncode != 0
    assert "unexpected paths" in result.stderr
    assert "nothing was pushed" in result.stderr
    assert repo.remote_head() == before, "a poisoned commit must never reach origin"


def test_commit_hook_failure_is_reported_as_undelivered(repo):
    hook = repo.work / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho hook-refused >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    repo.write_feed(FEED_NEW)
    before = repo.head()

    result = repo.run_snapshot()

    assert result.returncode == 2
    assert "commit of site/data/feed.json failed" in result.stderr
    assert "hook-refused" in result.stderr
    assert repo.head() == before
    assert repo.remote_head() == before
    assert repo.health()["consecutive_failures"] >= 1

    hook.unlink()
    retry = repo.run_snapshot()
    assert retry.returncode == 0, retry.stderr
    assert repo.paths_in("HEAD") == [DST_REL]
    assert repo.remote_head() == repo.head()
    assert repo.health()["consecutive_failures"] == 0


def test_foreign_commits_are_never_rebased_or_pushed_over(repo):
    """A non-fast-forward is left for a human: no rebase, no force, no reset.

    The push is refused by git itself — this job pushes a named commit with a
    plain (never forced) refspec — and the refusal is *reported* rather than
    swallowed by `|| true`, which is the whole of defect 3.
    """
    foreign = _foreign_commit(repo)
    repo.write_feed(FEED_NEW)
    local_before = repo.head()

    result = repo.run_snapshot()

    assert result.returncode == 2, "an undelivered snapshot has its own exit status"
    assert "push to origin/master failed" in result.stderr
    assert repo.remote_head() == foreign, "the other writer's work must stand"
    assert repo.health().get("consecutive_failures", 0) >= 1
    # Not rebased, not reset: the local branch still points at the commit it had
    # plus our own snapshot commit, whose parent is the commit we started from.
    assert repo.git("log", "-1", "--format=%s").stdout.strip() == "data: update feed snapshot"
    assert repo.git("rev-parse", "HEAD~1").stdout.strip() == local_before
    assert repo.paths_in("HEAD") == [DST_REL]


def test_another_writers_local_commit_is_never_pushed(repo):
    """The case the sha-scoped push is really for: a commit in the shared tree.

    Another writer commits *locally* while the snapshot job is running, and this
    job's commit lands on top of theirs. Pushing the branch tip would publish
    their unfinished work — so every commit between origin/master and HEAD must
    be this job's own before anything is offered to the remote.
    """
    (repo.work / "a.txt").write_text("their work in progress\n", encoding="utf-8")
    repo.git("commit", "-qam", "wip: someone else's commit")
    their_commit = repo.head()
    repo.write_feed(FEED_NEW)
    remote_before = repo.remote_head()

    result = repo.run_snapshot()

    assert result.returncode == 2
    assert "not this job's commit" in result.stderr, \
        "the refusal must name their commit rather than push the branch tip"
    assert "wip: someone else's commit" in result.stderr
    assert repo.remote_head() == remote_before, "their commit must not be published"
    # Their commit and their working tree are untouched.
    assert repo.git("rev-parse", "HEAD~1").stdout.strip() == their_commit
    assert (repo.work / "a.txt").read_text(encoding="utf-8") == "their work in progress\n"


def test_newline_path_cannot_disguise_a_foreign_commit(repo):
    hidden = repo.work / "\nsite" / "data" / "feed.json"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("foreign\n", encoding="utf-8")
    repo.git("add", str(hidden.relative_to(repo.work)))
    repo.git("commit", "-qm", "data: update feed snapshot")
    foreign = repo.head()
    remote_before = repo.remote_head()

    result = repo.run_snapshot()

    assert result.returncode == 2
    assert "not this job's commit" in result.stderr
    assert repo.head() == foreign
    assert repo.remote_head() == remote_before


# ---------------------------------------------------------------------------
# Delivery: retried, reported, and never silent
# ---------------------------------------------------------------------------


def test_a_failed_push_exits_nonzero_and_records_the_reason(repo):
    _reject_pushes(repo)
    repo.write_feed(FEED_NEW)

    result = repo.run_snapshot()

    assert result.returncode == 2, "an undelivered snapshot is its own exit status"
    assert "push to origin/master failed" in result.stderr
    rec = repo.health()
    assert rec.get("consecutive_failures", 0) >= 1
    assert "push" in rec.get("last_error", "")
    assert rec.get("last_success_ts") is not None or "last_success_ts" not in rec


def test_fetch_failure_with_no_local_commit_is_not_reported_healthy(repo):
    """A stale origin/master ref cannot prove the remote is up to date."""
    repo.git("remote", "set-url", "origin", str(repo.root / "missing.git"))
    before = repo.head()

    result = repo.run_snapshot()

    assert result.returncode == 2
    assert "git fetch origin master failed" in result.stderr
    assert repo.head() == before
    assert repo.health()["consecutive_failures"] >= 1
    assert "fetch" in repo.health()["last_error"]


def test_remote_ahead_without_a_local_snapshot_is_not_called_delivered(repo):
    foreign = _foreign_commit(repo)
    before = repo.head()

    result = repo.run_snapshot()

    assert result.returncode == 2
    assert "behind or divergent" in result.stderr
    assert repo.head() == before
    assert repo.remote_head() == foreign
    assert repo.health()["consecutive_failures"] >= 1


def test_an_undelivered_commit_is_retried_when_the_feed_bytes_match(repo):
    """The retry must key on delivery, not on the feed having changed again.

    The snapshot commit already exists and `site/data/feed.json` already holds
    the source bytes, so a byte comparison has nothing to do — and if that were
    the only question asked, the commit would never be delivered.
    """
    _reject_pushes(repo)
    repo.write_feed(FEED_NEW)

    first = repo.run_snapshot()
    assert first.returncode == 2
    stranded = repo.head()
    assert repo.paths_in("HEAD") == [DST_REL]
    assert repo.dst.read_text(encoding="utf-8") == _feed_bytes(FEED_NEW)

    # The push failure is fixed (the hook is gone); nothing else changes.
    (repo.remote / "hooks" / "pre-receive").unlink()

    second = repo.run_snapshot()

    assert second.returncode == 0, second.stderr
    assert repo.head() == stranded, "the retry reuses the existing commit"
    assert repo.remote_head() == stranded, "the stranded snapshot is delivered"
    assert repo.health()["consecutive_failures"] == 0
    assert repo.health()["last_success_ts"]


def test_missing_source_does_not_clear_an_undelivered_failure(repo):
    _reject_pushes(repo)
    repo.write_feed(FEED_NEW)
    assert repo.run_snapshot().returncode == 2
    stranded = repo.head()
    failures = repo.health()["consecutive_failures"]
    (repo.state / "feed.json").unlink()

    result = repo.run_snapshot()

    assert result.returncode == 2
    assert "source feed is absent while 1 local commit" in result.stderr
    assert repo.head() == stranded
    assert repo.remote_head() != stranded
    assert repo.health()["consecutive_failures"] == failures + 1


def test_missing_source_with_no_pending_snapshot_is_a_no_op(repo):
    (repo.state / "feed.json").unlink()
    before = repo.head()

    result = repo.run_snapshot()

    assert result.returncode == 0
    assert "no source feed" in result.stderr
    assert repo.head() == before
    assert repo.remote_head() == before
    assert repo.health() == {}


def test_a_successful_delivery_records_success(repo):
    repo.write_feed(FEED_NEW)

    assert repo.run_snapshot().returncode == 0

    rec = repo.health()
    assert rec["consecutive_failures"] == 0
    assert rec["last_success_ts"]
    assert repo.head() in rec.get("last_success_detail", {}).get("note", "") or \
        rec.get("last_success_detail")


def test_a_no_op_run_delivers_nothing_and_succeeds(repo):
    before = repo.head()

    result = repo.run_snapshot()

    assert result.returncode == 0, result.stderr
    assert repo.head() == before
    assert repo.remote_head() == before
    assert repo.remote_head() == before
    assert "nothing to deliver" in result.stderr


# ---------------------------------------------------------------------------
# The source and the destination
# ---------------------------------------------------------------------------


def test_a_truncated_source_feed_never_replaces_the_destination(repo):
    """The destination is committed, so a bad copy would be a published one."""
    repo.write_feed_raw('{"posts": [{"ts": "x"')
    before = repo.head()
    dst_before = repo.dst.read_text(encoding="utf-8")

    result = repo.run_snapshot()

    assert result.returncode != 0
    assert "not a valid feed envelope" in result.stderr
    assert repo.dst.read_text(encoding="utf-8") == dst_before
    assert repo.head() == before


def test_a_source_feed_without_a_thought_is_refused(repo):
    repo.write_feed_raw(json.dumps({"posts": [{"ts": "x", "thought": "  "}]}) + "\n")

    result = repo.run_snapshot()

    assert result.returncode != 0
    assert "not a valid feed envelope" in result.stderr
    assert "thought" in result.stderr


def test_a_pre_existing_operator_edit_to_the_destination_is_not_overwritten(repo):
    """Their edit is not ours to resolve, and not ours to lose to a cp()."""
    repo.dst.write_text(_feed_bytes(FEED_OLD).replace("old", "hand-edited"), encoding="utf-8")
    repo.write_feed(FEED_NEW)
    before = repo.head()

    result = repo.run_snapshot()

    assert result.returncode != 0
    assert "not overwriting another writer's change" in result.stderr
    assert "hand-edited" in repo.dst.read_text(encoding="utf-8")
    assert repo.head() == before
    assert repo.remote_head() == before


def test_a_staged_destination_edit_is_not_replaced_even_if_worktree_is_clean(repo):
    repo.dst.write_text(_feed_bytes(FEED_NEW), encoding="utf-8")
    repo.git("add", DST_REL)
    staged_blob = repo.git("rev-parse", f":{DST_REL}").stdout.strip()
    repo.dst.write_text(_feed_bytes(FEED_OLD), encoding="utf-8")
    repo.write_feed(FEED_NEW)
    before = repo.head()

    result = repo.run_snapshot()

    assert result.returncode != 0
    assert "pre-existing staged edit" in result.stderr
    assert repo.head() == before
    assert repo.git("rev-parse", f":{DST_REL}").stdout.strip() == staged_blob
    assert repo.dst.read_text(encoding="utf-8") == _feed_bytes(FEED_OLD)


def test_a_destination_edit_during_copy_is_refused(repo):
    """A second guard catches an edit after the initial clean check."""
    wrapper_dir = repo.root / "wrappers"
    wrapper_dir.mkdir()
    cp = wrapper_dir / "cp"
    cp.write_text(
        "#!/bin/sh\n"
        "printf 'operator edit\\n' > \"$SNAPSHOT_TEST_DEST\"\n"
        "exec /bin/cp \"$@\"\n",
        encoding="utf-8",
    )
    cp.chmod(0o755)
    repo.write_feed(FEED_NEW)
    before = repo.head()

    result = repo.run_snapshot({
        "PATH": str(wrapper_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "SNAPSHOT_TEST_DEST": str(repo.dst),
    })

    assert result.returncode != 0
    assert "changed during snapshot preparation" in result.stderr
    assert repo.dst.read_text(encoding="utf-8") == "operator edit\n"
    assert repo.head() == before


def test_an_uncommitted_copy_of_the_same_feed_is_finished_not_refused(repo):
    """The previous run's own copy is not a foreign edit — it is this job's tail.

    A run killed between the copy and the commit leaves the destination equal to
    the source and uncommitted; refusing that would wedge the cron job forever.
    """
    repo.dst.write_text(_feed_bytes(FEED_NEW), encoding="utf-8")
    repo.write_feed(FEED_NEW)

    result = repo.run_snapshot()

    assert result.returncode == 0, result.stderr
    assert repo.paths_in("HEAD") == [DST_REL]
    assert repo.remote_head() == repo.head()
