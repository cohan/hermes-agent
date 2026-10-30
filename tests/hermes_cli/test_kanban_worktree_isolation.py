"""Per-task worktree isolation for decompose siblings.

Decompose children used to inherit the root's literal ``workspace_path``,
so every sibling of a worktree-kind root pointed at the SAME checkout —
and ``_resolve_worktree_workspace``'s existing-checkout shortcut reused it
on whatever branch was there, letting sibling workers run concurrently in
one directory on one branch (cross-task provenance corruption, no lock).

Two-part fix under test:
- ``decompose_triage_task`` leaves worktree children's ``workspace_path``
  unset so each child materializes its own ``<repo>/.worktrees/<child-id>``.
- ``_resolve_worktree_workspace`` falls back to a fresh per-task worktree
  when the requested path is occupied by another task's branch (heals
  pre-existing rows that still carry a shared path).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_graph import decompose_triage_task
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw


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
        [
            "git", "-C", str(cwd),
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _add_worktree(repo: Path, target: Path, branch: str) -> Path:
    _git(repo, "worktree", "add", str(target), "-b", branch, "HEAD")
    return target


def test_decompose_worktree_children_get_own_workspace(kanban_home):
    with kbc.connect() as conn:
        root = kb.create_task(conn, title="build the feature", triage=True)
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', "
            "workspace_path='/repo/.worktrees/root' WHERE id = ?",
            (root,),
        )
        conn.commit()

        child_ids = decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "spec it", "assignee": "alice", "parents": []},
                {"title": "implement it", "assignee": "bob", "parents": [0]},
            ],
            author="decomposer",
        )
        assert child_ids is not None and len(child_ids) == 2

        for cid in child_ids:
            row = conn.execute(
                "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
                (cid,),
            ).fetchone()
            assert row["workspace_kind"] == "worktree"
            # Each child resolves its own <repo>/.worktrees/<child-id> at
            # dispatch; the root's literal path must never be shared.
            assert row["workspace_path"] is None




def test_resolve_worktree_falls_back_when_path_occupied(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    occupied = _add_worktree(repo, repo / ".worktrees" / "sibling", "wt/sibling")

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="second sibling",
            workspace_kind="worktree",
            workspace_path=str(occupied),  # inherited shared/stale path
        )
        task = kb.get_task(conn, tid)

    workspace, branch = kbw._resolve_worktree_workspace(task)
    assert workspace == (repo / ".worktrees" / tid).resolve()
    assert branch == f"wt/{tid}"
    # The sibling's checkout is untouched, still on its own branch.
    assert (occupied / "README.md").exists()
    head = subprocess.run(
        ["git", "-C", str(occupied), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "wt/sibling"


def _legacy_worktree_card(conn, *, title, workspace_path, branch_name=None):
    """A worktree card as it exists BEFORE creation-time validation.

    ``create_task`` now refuses a repo-less worktree outright (see
    tests/hermes_cli/test_kanban_workspace_contract.py). The spawn-time
    refusal still has to hold for rows already on the board — the eight cards
    filed before that check — so these tests forge the row.
    """
    tid = kb.create_task(conn, title=title)
    conn.execute(
        "UPDATE tasks SET workspace_kind = 'worktree', workspace_path = ?, "
        "branch_name = ? WHERE id = ?",
        (workspace_path, branch_name, tid),
    )
    conn.commit()
    return tid


def test_worktree_path_outside_a_repo_is_reported_as_unavailable(kanban_home, tmp_path):
    """No repo above the named path: the resolver flags it for a downgrade
    instead of claiming the path is a broken repo."""
    plain = tmp_path / "not-a-repo" / "wt-target"

    with kbc.connect() as conn:
        tid = _legacy_worktree_card(conn, title="work outside git", workspace_path=str(plain))
        task = kb.get_task(conn, tid)

    with pytest.raises(kbw.WorktreeUnavailable):
        kbw._resolve_worktree_workspace(task)


def test_worktree_without_a_repo_stays_a_worktree(kanban_home, tmp_path):
    """There is no downgrade path any more: a worktree task whose path has no
    repo is reported unavailable and the card stays a worktree card. Turning it
    into a shared ``dir`` is the collision hazard the kind exists to prevent —
    see tests/hermes_cli/test_kanban_work_guard.py for the dispatcher-side proof."""
    plain = tmp_path / "not-a-repo" / "wt-target"

    with kbc.connect() as conn:
        tid = _legacy_worktree_card(
            conn, title="work outside git", workspace_path=str(plain),
            branch_name="wt/ghost",
        )
        task = kb.get_task(conn, tid)
        with pytest.raises(kbw.WorktreeUnavailable):
            kbw._resolve_worktree_workspace(task)
        # Still a worktree card, and no downgrade event was written.
        row = conn.execute(
            "SELECT workspace_kind, branch_name FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row["workspace_kind"] == "worktree"
        assert row["branch_name"] == "wt/ghost"
        assert not conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'workspace_downgraded'",
            (tid,),
        ).fetchone()


def test_dispatch_refuses_a_repo_less_worktree_card_instead_of_downgrading(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """End to end through the dispatcher: a worktree card whose (board-inherited)
    path is not a git repo is a broken COLLISION-SAFETY contract. It must not be
    spawned as a shared ``dir`` card — no worker is placed, and the failure is
    recorded for the orchestrator to repair the project link."""
    from hermes_cli import kanban_db_dispatch as kbd

    non_repo = kanban_home / "not-a-repo"
    non_repo.mkdir()
    monkeypatch.setattr(
        kbw._kb, "read_board_metadata", lambda *a, **k: {"default_workdir": str(non_repo)}
    )

    seen: list[tuple[str, str]] = []

    def _fake_spawn(task, workspace, board=None):
        seen.append((task.workspace_kind, str(workspace)))
        return 0  # falsy pid: dispatch records no worker_pid

    with kbc.connect() as conn:
        tid = _legacy_worktree_card(conn, title="worktree, no repo", workspace_path=None)
        conn.execute("UPDATE tasks SET assignee = 'alice' WHERE id = ?", (tid,))
        conn.commit()
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn)

        assert seen == [], "a repo-less worktree card was spawned into a shared workspace"
        assert not res.spawned
        row = conn.execute(
            "SELECT workspace_kind, branch_name, last_failure_error FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
        # Still a worktree card — never silently switched to a shared dir.
        assert row["workspace_kind"] == "worktree"
        assert row["branch_name"] is None
        assert "workspace:" in (row["last_failure_error"] or "")


def test_board_default_workdir_that_is_not_a_repo_is_refused_not_scratch(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """A card with no path of its own (a decompose child, say) on a board whose
    default_workdir is not a repo has nowhere to branch. Scratch is a shared
    workspace, so the guard refuses rather than dropping a code card into it."""
    from hermes_cli import kanban_db_dispatch as kbd

    non_repo = kanban_home / "not-a-repo"
    non_repo.mkdir()
    monkeypatch.setattr(
        kbw._kb, "read_board_metadata", lambda *a, **k: {"default_workdir": str(non_repo)}
    )

    seen: list[tuple[str, str]] = []

    def _fake_spawn(task, workspace, board=None):
        seen.append((task.workspace_kind, str(workspace)))
        return 0

    with kbc.connect() as conn:
        tid = _legacy_worktree_card(conn, title="worktree, no path", workspace_path=None)
        conn.execute("UPDATE tasks SET assignee = 'alice' WHERE id = ?", (tid,))
        conn.commit()
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn)

        assert seen == [], "a repo-less worktree card was dropped into scratch"
        assert not res.spawned
        assert conn.execute(
            "SELECT workspace_kind FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["workspace_kind"] == "worktree"

