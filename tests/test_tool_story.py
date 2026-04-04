"""Story builder tool tests."""
import json, os, subprocess


def test_session_has_story_field(isolated_project, monkeypatch):
    """Session template includes obi_story_lines."""
    # Apply isolated env vars so load_session uses the temp session path
    for k, v in isolated_project["env"].items():
        monkeypatch.setenv(k, v)
    from pxh.state import load_session
    s = load_session()
    assert "obi_story_lines" in s
    assert s["obi_story_lines"] == []


def _run_story(action, extra_env=None, **params):
    """Helper to run tool-story with given action and params."""
    env = os.environ.copy()
    env["PX_DRY"] = "1"
    env["PX_BYPASS_SUDO"] = "1"
    if extra_env:
        env.update(extra_env)
    env["TOOL_ACTION"] = action
    for k, v in params.items():
        env[f"TOOL_{k.upper()}"] = str(v)
    r = subprocess.run(
        ["bash", "bin/tool-story"],
        capture_output=True, text=True, env=env,
        cwd=os.environ.get("PROJECT_ROOT", "."),
    )
    return r, json.loads(r.stdout.strip().split("\n")[-1])


def test_story_start(isolated_project):
    """Start action creates a new story with SPARK's opening line."""
    env = dict(isolated_project["env"])
    r, out = _run_story("start", extra_env=env)
    assert r.returncode == 0
    assert out["status"] == "ok"
    assert "line" in out
    assert len(out["line"]) > 10


def test_story_add(isolated_project):
    """Add action appends a line and SPARK responds."""
    env = dict(isolated_project["env"])
    _run_story("start", extra_env=env)
    r, out = _run_story("add", extra_env=env, text="And then a dragon appeared!")
    assert r.returncode == 0
    assert out["status"] == "ok"
    assert "line" in out


def test_story_read(isolated_project):
    """Read action returns current story."""
    env = dict(isolated_project["env"])
    _run_story("start", extra_env=env)
    r, out = _run_story("read", extra_env=env)
    assert r.returncode == 0
    assert out["status"] == "ok"
    assert "lines" in out
    assert len(out["lines"]) >= 1


def test_story_finish(isolated_project):
    """Finish action completes and saves story."""
    env = dict(isolated_project["env"])
    _run_story("start", extra_env=env)
    _run_story("add", extra_env=env, text="The robot found a treasure map.")
    r, out = _run_story("finish", extra_env=env)
    assert r.returncode == 0
    assert out["status"] == "ok"
    assert "saved" in out or "title" in out


def test_story_add_without_start(isolated_project):
    """Add without start returns error."""
    env = dict(isolated_project["env"])
    r, out = _run_story("add", extra_env=env, text="hello")
    assert out["status"] == "error"


def test_story_finish_without_start(isolated_project):
    """Finish without start returns error."""
    env = dict(isolated_project["env"])
    r, out = _run_story("finish", extra_env=env)
    assert out["status"] == "error"
