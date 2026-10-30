"""Workspace kind is validated at card CREATION, not discovered at spawn.

Eight cards were filed with a workspace the dispatcher can never resolve and
burned their retry budget before anyone saw it:

  dir       /home/cohan/.hermes/panel                       (a repo root)
  dir       /home/cohan/.hermes/panel/.worktrees/t_docnight (inside a repo)
  dir       /home/cohan/.hermes/zabbix
  dir       .../zabbix/.worktrees/t_29e117d4
  worktree  /tmp                                            (not a repo)
  worktree  /tmp
  worktree  (empty)
  worktree  (empty)

Both shapes are refused now at ``kanban_db.create_task``, with a message that
names the field and the fix.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        yield kb, conn
    finally:
        conn.close()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "README").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                    "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], check=True)
    return root


@pytest.fixture
def plain_dir(tmp_path):
    d = tmp_path / "shared"
    d.mkdir()
    return d


def _refused(kb, conn, **kwargs):
    with pytest.raises(ValueError) as exc:
        kb.create_task(conn, title="probe", assignee="ops", **kwargs)
    return str(exc.value)


# --- (a) dir pointing at a repo ------------------------------------------------

def test_dir_on_repo_root_is_refused_and_names_worktree(board, repo):
    kb, conn = board
    msg = _refused(kb, conn, workspace_kind="dir", workspace_path=str(repo))
    assert str(repo) in msg
    assert "workspace_kind=worktree" in msg
    assert "allow_repo_dir" in msg


def test_dir_inside_repo_is_refused(board, repo):
    kb, conn = board
    nested = repo / ".worktrees" / "t_docnight"
    nested.mkdir(parents=True)
    msg = _refused(kb, conn, workspace_kind="dir", workspace_path=str(nested))
    assert "workspace_kind=worktree" in msg


def test_dir_inside_repo_that_does_not_exist_yet_is_refused(board, repo):
    kb, conn = board
    msg = _refused(kb, conn, workspace_kind="dir",
                   workspace_path=str(repo / "src" / "new-thing"))
    assert "workspace_kind=worktree" in msg


def test_dir_repo_is_allowed_with_the_explicit_opt_out(board, repo):
    kb, conn = board
    tid = kb.create_task(conn, title="read-only", assignee="ops",
                         workspace_kind="dir", workspace_path=str(repo),
                         allow_repo_dir=True)
    task = kb.get_task(conn, tid)
    assert (task.workspace_kind, task.workspace_path) == ("dir", str(repo))


def test_dir_without_a_path_is_refused(board):
    kb, conn = board
    msg = _refused(kb, conn, workspace_kind="dir", workspace_path=None)
    assert "workspace_path" in msg


# --- (b) worktree with a path that is not in a repo ----------------------------

def test_worktree_on_tmp_is_refused(board):
    kb, conn = board
    msg = _refused(kb, conn, workspace_kind="worktree", workspace_path="/tmp")
    assert "/tmp" in msg
    assert "git repo" in msg
    assert "scratch" in msg


def test_worktree_on_a_plain_directory_is_refused(board, plain_dir):
    kb, conn = board
    msg = _refused(kb, conn, workspace_kind="worktree", workspace_path=str(plain_dir))
    assert str(plain_dir) in msg


def test_worktree_with_no_path_is_refused_when_the_board_has_no_repo(board, monkeypatch):
    kb, conn = board
    monkeypatch.setattr(kb, "_board_meta_for", lambda board=None: {})
    msg = _refused(kb, conn, workspace_kind="worktree", workspace_path=None)
    assert "git repo" in msg and "project" in msg


def test_worktree_on_a_relative_path_is_refused(board):
    kb, conn = board
    msg = _refused(kb, conn, workspace_kind="worktree", workspace_path="repo")
    assert "absolute" in msg


# --- legitimate shapes still create -------------------------------------------

def test_scratch_still_creates(board):
    kb, conn = board
    tid = kb.create_task(conn, title="probe", assignee="ops")
    assert kb.get_task(conn, tid).workspace_kind == "scratch"


def test_dir_on_a_genuinely_shared_directory_still_creates(board, plain_dir):
    kb, conn = board
    tid = kb.create_task(conn, title="probe", assignee="ops",
                         workspace_kind="dir", workspace_path=str(plain_dir))
    assert kb.get_task(conn, tid).workspace_path == str(plain_dir)


def test_worktree_on_a_real_repo_root_is_still_created_as_a_worktree(board, repo):
    kb, conn = board
    tid = kb.create_task(conn, title="probe", assignee="ops",
                         workspace_kind="worktree", workspace_path=str(repo))
    task = kb.get_task(conn, tid)
    assert task.workspace_kind == "worktree"
    # The canonical <repo>/.worktrees/<id> checkout is materialized at spawn;
    # creation stores the repo anchor it will branch from.
    assert Path(task.workspace_path) == repo


def test_project_linked_worktree_still_creates(board, repo, monkeypatch):
    """A project link supplies the repo, so an empty workspace_path is fine."""
    kb, conn = board
    monkeypatch.setattr(
        kb, "_resolve_project_link",
        lambda *a, **kw: ("proj", object(), str(repo), "worktree"),
    )
    tid = kb.create_task(conn, title="probe", assignee="ops",
                         workspace_kind="worktree", project_id="proj")
    assert kb.get_task(conn, tid).workspace_kind == "worktree"


def test_existing_linked_worktree_checkout_as_dir_is_still_allowed(board, repo, tmp_path):
    """Mirror of the spawn-time exemption: an existing linked checkout is isolated."""
    kb, conn = board
    wt = tmp_path / "elsewhere"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q",
                    str(wt), "-b", "wt/probe"], check=True)
    tid = kb.create_task(conn, title="probe", assignee="ops",
                         workspace_kind="dir", workspace_path=str(wt))
    assert kb.get_task(conn, tid).workspace_kind == "dir"


# --- every filing surface goes through the same validator ---------------------

def _cli_create(argv):
    """Drive the real `hermes kanban create` path, including its ValueError handler."""
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    return kc.kanban_command(root.parse_args(["kanban", "create", *argv]))


def test_cli_create_refuses_and_prints_the_fix(board, repo, capsys):
    assert _cli_create(["PROBE", "--assignee", "ops",
                        "--workspace", f"dir:{repo}"]) != 0
    err = capsys.readouterr().err
    assert "workspace_kind=worktree" in err


def test_cli_create_accepts_the_opt_out(board, repo):
    assert _cli_create(["PROBE", "--assignee", "ops",
                        "--workspace", f"dir:{repo}", "--allow-repo-dir"]) == 0


def test_cli_create_refuses_a_repo_less_worktree(board, capsys):
    assert _cli_create(["PROBE", "--assignee", "ops",
                        "--workspace", "worktree:/tmp"]) != 0
    assert "git repo" in capsys.readouterr().err


def test_kanban_create_tool_refuses_and_returns_structured_error(board, repo):
    from tools import kanban_tools

    out = kanban_tools._handle_create({
        "title": "PROBE", "assignee": "ops",
        "workspace_kind": "dir", "workspace_path": str(repo),
    })
    payload = json.loads(out)
    assert payload.get("error")
    assert "workspace_kind=worktree" in json.dumps(payload)


def test_kanban_create_tool_accepts_the_opt_out(board, plain_dir):
    from tools import kanban_tools

    out = kanban_tools._handle_create({
        "title": "PROBE", "assignee": "ops",
        "workspace_kind": "dir", "workspace_path": str(plain_dir),
        "allow_repo_dir": True,
    })
    assert json.loads(out).get("task_id")
