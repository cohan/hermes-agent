"""The work-collision guard: a kanban card must never silently land two workers
in the same directory.

The guard has two prongs, both in ``_dispatch_lane_task``:

* an explicit ``worktree`` card whose path has no git repo to branch from is a
  broken workspace CONTRACT — the dispatcher records the failure and
  retries/blocks the card. It must never fall back to a shared ``dir`` or
  ``scratch`` workspace (that is exactly the class of collision the worktree
  kind exists to prevent).
* a ``dir`` card whose path is inside a repo checkout (the main repo or a path
  under it) is the old collision hazard: two workers, one tree. Existing linked
  worktree checkouts are already isolated, so those stay valid.

These tests call the real dispatcher with a fake spawn so the whole
claim -> resolve -> (spawn | refuse) path is exercised.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=T", "-c", "user.email=t@e.x",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)],
                   check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _recorded_spawns() -> tuple[list, callable]:
    seen: list = []

    def _spawn(task, workspace, board=None):
        seen.append((task.id, str(workspace)))
        return 0  # falsy pid: dispatch records no worker_pid

    return seen, _spawn


def _row(conn, tid):
    return conn.execute(
        "SELECT workspace_kind, workspace_path, status, consecutive_failures, "
        "last_failure_error FROM tasks WHERE id = ?", (tid,),
    ).fetchone()


def test_worktree_card_without_a_repo_is_refused_not_downgraded(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """The headline case: a worktree card with no repo must NOT be spawned into
    a shared directory. It stays a worktree card, the failure is recorded, and
    no worker is placed anywhere."""
    non_repo = kanban_home / "not-a-repo"
    non_repo.mkdir()
    monkeypatch.setattr(
        kbw._kb, "read_board_metadata", lambda *a, **k: {"default_workdir": str(non_repo)}
    )
    seen, spawn = _recorded_spawns()

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="worktree, no repo", assignee="alice", workspace_kind="worktree",
        )
        before = _row(conn, tid)
        res = kbd.dispatch_once(conn, spawn_fn=spawn)

        assert seen == [], "a repo-less worktree card was spawned into a shared workspace"
        assert res.spawned == []
        row = _row(conn, tid)
        # The collision-prone downgrade must not have happened: the card is
        # still a worktree card, not silently switched to a shared dir/scratch.
        assert row["workspace_kind"] == "worktree"
        assert row["workspace_kind"] == before["workspace_kind"]
        # The card carries the failure diagnosis the orchestrator needs.
        assert row["consecutive_failures"] == 1
        assert "workspace:" in (row["last_failure_error"] or "")
        assert row["status"] == "ready"
        assert res.auto_blocked == []  # first failure: retried, not yet blocked


def test_repeat_failures_auto_block_the_repo_less_worktree_card(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """Retried, then blocked — never downgraded. The card ends up blocked so the
    orchestrator repairs the project link instead of two workers sharing a tree."""
    non_repo = kanban_home / "not-a-repo"
    non_repo.mkdir()
    monkeypatch.setattr(
        kbw._kb, "read_board_metadata", lambda *a, **k: {"default_workdir": str(non_repo)}
    )
    seen, spawn = _recorded_spawns()

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="worktree, no repo", assignee="alice", workspace_kind="worktree",
        )
        blocked = False
        for _ in range(kbd.DEFAULT_FAILURE_LIMIT + 1):
            res = kbd.dispatch_once(conn, spawn_fn=spawn)
            if tid in res.auto_blocked:
                blocked = True
                break

        assert seen == []
        assert blocked, "the card never auto-blocked — it would retry forever"
        row = _row(conn, tid)
        assert row["workspace_kind"] == "worktree"
        assert row["status"] == "blocked"


def test_dir_card_inside_a_repo_checkout_is_refused(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    """A `dir` card pointing at the main checkout is the duplicate-work hazard.
    Refuse it and name worktree as the fix."""
    repo = _make_repo(tmp_path)
    seen, spawn = _recorded_spawns()

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="dir inside a repo", assignee="alice",
            workspace_kind="dir", workspace_path=str(repo),
        )
        res = kbd.dispatch_once(conn, spawn_fn=spawn)

        assert seen == [], "a dir card inside a repo checkout was spawned"
        row = _row(conn, tid)
        err = row["last_failure_error"] or ""
        assert "worktree" in err, f"refusal did not name the fix: {err!r}"
        assert res.auto_blocked == []


def test_dir_card_pointing_at_a_linked_worktree_is_allowed(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    """The carve-out: a linked worktree checkout is already isolated, so a dir
    card aimed at one still runs."""
    repo = _make_repo(tmp_path)
    wt = repo / ".worktrees" / "wt-ok"
    _git(repo, "worktree", "add", str(wt), "-b", "wt/ok", "HEAD")
    seen, spawn = _recorded_spawns()

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="dir on a worktree", assignee="alice",
            workspace_kind="dir", workspace_path=str(wt),
        )
        kbd.dispatch_once(conn, spawn_fn=spawn)

        assert seen, "an isolated worktree checkout was wrongly refused"
        assert seen[0][1] == str(wt)


def test_scratch_card_is_unaffected(kanban_home, all_assignees_spawnable):
    """The guard is not a blanket refusal: an ordinary non-repo card still runs."""
    seen, spawn = _recorded_spawns()

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="support work", assignee="alice", workspace_kind="scratch",
        )
        kbd.dispatch_once(conn, spawn_fn=spawn)

        assert seen, "a plain scratch card was blocked by the guard"
