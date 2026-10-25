"""Board metadata (``board.json``) and board lifecycle management for the Kanban DB:
read/write of per-board display metadata, board creation/discovery/archival, and the
``board.json``-as-identity-marker rule.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from typing import Optional
from pathlib import Path

from hermes_cli import kanban_quiesce as _quiesce


def _refuse_if_held(normed: str, db_path: Path, *, action: str, command: str) -> None:
    """Refuse an operation that would move/replace the file under a live holder.

    ``remove_board`` renames the board *directory*, and ``create_board`` runs a
    schema pass on an existing board; either one under another process's open
    handle is the #90950 split brain — that process keeps writing the old inode
    (or a path that no longer resolves) and a later ``connect()`` recreates an
    empty ``kanban.db``.

    ``live_holders`` returns ``None`` when the ``/proc`` scan cannot run; that
    is "cannot prove quiesced" and must fail closed, never be read as "free".
    """
    holders = _quiesce.live_holders(db_path)
    if holders is None:
        raise ValueError(
            f"refusing to {action} board {normed!r}: this host's /proc scan is "
            f"unavailable, so it cannot be proved that no process holds "
            f"{db_path.name} open. Run this on the host that owns the board."
        )
    if holders:
        raise ValueError(
            f"refusing to {action} board {normed!r}: {db_path.name} or its "
            f"WAL/SHM is still held open by PID(s) "
            f"{', '.join(str(pid) for pid in sorted(holders))}. Stop them "
            f"(`hermes gateway stop`) and re-run: {command}"
        )


def _dir_holds_board(d: Path) -> bool:
    # ``board.json`` is the identity marker: archive/hard-delete both leave the
    # directory without it, and a stale ``connect(board=slug)`` used to leave a
    # ``kanban.db``-only stub that resurfaced in the board list as an empty
    # active board (#43243). Discovery must therefore require the metadata
    # file; a DB-only directory is a stub to ignore, never a board.
    return (d / "board.json").exists()


def board_metadata_path(board: Optional[str] = None) -> Path:
    """``board.json`` path — display metadata only; the directory slug is the identity."""
    return _kb.board_dir(_kb._slug_or_default(board)) / "board.json"


def _default_board_display_name(slug: str) -> str:
    """``atm10-server`` -> ``Atm10 Server``."""
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part) or slug


def read_board_metadata(board: Optional[str] = None) -> dict:
    """``board.json`` merged over defaults, plus ``slug`` and ``db_path``. Never
    raises — a missing/malformed file yields the synthesized entry."""
    slug = _kb._slug_or_default(board)
    meta: dict[str, Any] = {
        "slug": slug,
        "name": _default_board_display_name(slug),
        "description": "",
        "icon": "",
        "color": "",
        "default_workdir": None,
        # Project scope: new tasks inherit it (deterministic worktree + branch).
        "project_id": None,
        "created_at": None,
        "archived": False,
    }
    try:
        p = board_metadata_path(slug)
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                # Never let the metadata file claim a different slug than
                # its directory — trust the filesystem.
                raw["slug"] = slug
                meta.update(raw)
    except (OSError, json.JSONDecodeError):
        pass
    meta["db_path"] = str(_kb.kanban_db_path(slug))
    return meta


def write_board_metadata(
    board: Optional[str], *, name: Optional[str] = None, description: Optional[str] = None,
    icon: Optional[str] = None, color: Optional[str] = None, archived: Optional[bool] = None,
    default_workdir: Optional[str] = None, project_id: Optional[str] = None,
) -> dict:
    """Create/update ``board.json``; unmentioned fields are preserved, ``created_at``
    set on first write. ``project_id``/``default_workdir``: ``None`` = unchanged,
    "" = clear (``project_id`` is not validated here)."""
    _kb._assert_not_delegated_child_mutation()
    slug = _kb._slug_or_default(board)
    meta = read_board_metadata(slug)
    # db_path is derived on every read; never persist it into board.json.
    meta.pop("db_path", None)
    if name is not None:
        meta["name"] = str(name).strip() or _default_board_display_name(slug)
    for key, value in (("description", description), ("icon", icon), ("color", color)):
        if value is not None:
            meta[key] = str(value)
    if archived is not None:
        meta["archived"] = bool(archived)
    for key, value in (("default_workdir", default_workdir), ("project_id", project_id)):
        if value is not None:
            meta[key] = str(value) if value else None
    if not meta.get("created_at"):
        meta["created_at"] = int(time.time())
    path = board_metadata_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    meta["db_path"] = str(_kb.kanban_db_path(slug))
    return meta


def _forget_live_board(normed: str, board_dir: Path) -> None:
    """Drop the current-board pointer and schema cache *after* the path is gone.

    Both must happen only once the rename has succeeded: clearing the pointer
    before a rename that then fails would leave the user on ``default`` with
    the board still present, and dropping the cache early lets a concurrent
    ``connect()`` re-run the schema pass against the old inode.
    """
    if _kb.get_current_board() == normed:
        _kb.clear_current_board()
    _kb._INITIALIZED_PATHS.discard(str((board_dir / "kanban.db").resolve()))


def _assert_board_quiesced(normed: str, db_path: Path, *, action: str, command: str) -> None:
    """Refuse board lifecycle mutations while the board is busy or held open.

    Two independent gates, both fail-closed:

    * ``assert_path_quiesced`` names every card that is ``running`` or still
      holds a ``claim_lock`` — moving or replacing the file under it would
      convert a live run to ``released`` and lose the worker's claim.
    * ``_refuse_if_held`` catches a gateway/worker that merely holds the file
      open with no in-flight card; a rename under that handle is the #90950
      split brain (the holder keeps writing a deleted inode and a later
      ``connect()`` recreates an empty board).
    """
    _quiesce.assert_path_quiesced(
        db_path, board_label=f"board {normed!r}", command=command, action=action,
    )
    _refuse_if_held(normed, db_path, action=action, command=command)


def _create_db_atomically(db_path: Path) -> None:
    """Build a brand-new board DB in a staging file, then publish by rename.

    The live path is never opened for a schema pass: ``init_db`` runs against
    the staging copy, ``publish`` integrity-checks it and ``os.replace``s it
    into place. Nothing ``cp``/``mv``s over an existing live file and no
    sidecar is unlinked by hand — the staging artifacts are cleaned up by
    ``discard_staging`` after the rename.
    """
    staging = _quiesce.staging_path(db_path)
    try:
        _kb.init_db(db_path=staging)
        _quiesce.publish(staging, db_path, board_label=f"board {db_path.parent.name!r}")
    finally:
        _quiesce.discard_staging(staging)


def create_board(
    slug: str, *, name: Optional[str] = None, description: Optional[str] = None,
    icon: Optional[str] = None, color: Optional[str] = None, default_workdir: Optional[str] = None,
    project_id: Optional[str] = None,
) -> dict:
    """Create board dir + DB + metadata (``mkdir -p`` semantics: existing board returns its metadata).

    The DB is only ever *built* in a staging file and published by atomic
    rename. An existing, non-empty DB is not opened or migrated here at all:
    ``create_board`` on a live board is a metadata-only write. That keeps the
    ``hermes kanban boards create <existing>`` path from becoming a second
    schema writer against a file the dispatcher is using (#35240).
    """
    normed = _kb._require_slug(slug)
    db_path = _kb.kanban_db_path(normed)
    board_dir = _kb.board_dir(normed)
    exists = _dir_holds_board(board_dir)
    has_live_db = db_path.exists() and db_path.stat().st_size > 0

    if exists and has_live_db:
        # Re-creating an existing board is a metadata refresh only. Touching
        # the DB (init/migration) while another process holds it is a second
        # writer outside the dispatcher's discipline, so refuse rather than
        # open it.
        _assert_board_quiesced(
            normed, db_path,
            action="re-create",
            command=f"hermes kanban boards create {normed}",
        )
    # Explicit creation clears any archived tombstone at this slug (left by
    # remove_board(archive=True)) — otherwise _kb.init_db() below would rightly
    # refuse to recreate the archived board's DB (#43243).
    meta = write_board_metadata(
        normed, name=name, description=description, icon=icon, color=color,
        default_workdir=default_workdir, project_id=project_id, archived=False,
    )
    if not has_live_db:
        # New board, or a stub whose ``kanban.db`` is missing/zero bytes: build
        # the schema in staging and publish it atomically. list_boards() sees
        # the DB immediately after the rename.
        _create_db_atomically(db_path)
    return meta


def list_boards(*, include_archived: bool = True) -> list[dict]:
    """Metadata for every board: ``default`` first (always present), then
    ``boards/<slug>/`` dirs holding a ``kanban.db`` or ``board.json``, sorted."""
    entries = [read_board_metadata(_kb.DEFAULT_BOARD)]
    seen = {_kb.DEFAULT_BOARD}
    root = _kb.boards_root()
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            try:
                normed = _kb._normalize_board_slug(child.name)  # skip junk dirs, don't raise
            except ValueError:
                continue
            if not normed or normed in seen or not _dir_holds_board(child):
                continue
            meta = read_board_metadata(normed)
            if meta.get("archived") and not include_archived:
                continue
            entries.append(meta)
            seen.add(normed)
    return entries


def remove_board(slug: str, *, archive: bool = True) -> dict:
    """Archive (to ``boards/_archived/<slug>-<ts>/``) or delete a board;
    ``default`` cannot be removed. Returns ``{"slug", "action", "new_path"}``."""
    _kb._assert_not_delegated_child_mutation()
    normed = _kb._require_slug(slug)
    if normed == _kb.DEFAULT_BOARD:
        raise ValueError("the 'default' board cannot be removed")
    d = _kb.board_dir(normed)
    if not d.exists():
        raise ValueError(f"board {normed!r} does not exist")

    db_path = _kb.kanban_db_path(normed)
    action = "archive" if archive else "delete"
    command = f"hermes kanban boards rm {normed}" + ("" if archive else " --delete")
    # Both modes replace or remove the board's directory — and with it the
    # ``kanban.db`` inode and its ``-wal``/``-shm``. A card that is still
    # running would keep writing through the old handle while the path stops
    # resolving; a later ``connect(board=slug)`` then creates an empty DB. The
    # quiesce gate names the blocking cards, and the holder scan catches a
    # gateway/worker that holds the file without an in-flight card.
    _quiesce.assert_path_quiesced(
        db_path, board_label=f"board {normed!r}", command=command, action=action,
    )
    _refuse_if_held(normed, db_path, action=action, command=command)

    if archive:
        # Capture display metadata before the move so the tombstone below keeps
        # the user's board name instead of falling back to a title-cased slug.
        prior_meta = read_board_metadata(normed)
        archive_root = _kb.boards_root() / "_archived"
        archive_root.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        target = archive_root / f"{normed}-{ts}"
        suffix = 1
        while target.exists():  # rapid double-archive
            target = archive_root / f"{normed}-{ts}-{suffix}"
            suffix += 1
        d.rename(target)
        _forget_live_board(normed, d)
        # Leave an ``archived`` tombstone at the original slug. Stale dashboard
        # tabs / gateway pollers can keep calling connect(board=slug) after the
        # archive; without a marker the resurrect-guard cannot tell an archived
        # slug from a brand-new one and an empty board would reappear (#43243).
        write_board_metadata(
            normed,
            name=prior_meta.get("name"),
            description=prior_meta.get("description"),
            icon=prior_meta.get("icon"),
            color=prior_meta.get("color"),
            default_workdir=prior_meta.get("default_workdir"),
            project_id=prior_meta.get("project_id"),
            archived=True,
        )
        return {"slug": normed, "action": "archived", "new_path": str(target)}
    import shutil
    # Delete by rename first, then remove the already-detached directory. The
    # live path stops resolving in one atomic step, so a concurrent connect()
    # either sees the whole board or no board — it never opens a half-removed
    # directory. Sidecars travel with the directory; nothing unlinks
    # ``kanban.db``/``-wal``/``-shm`` in place.
    trash_root = _kb.boards_root() / "_trash"
    trash_root.mkdir(parents=True, exist_ok=True)
    trash = trash_root / f"{normed}-{int(time.time())}-{os.getpid()}"
    suffix = 1
    while trash.exists():
        trash = trash_root / f"{normed}-{int(time.time())}-{os.getpid()}-{suffix}"
        suffix += 1
    d.rename(trash)
    _forget_live_board(normed, d)
    shutil.rmtree(trash)
    return {"slug": normed, "action": "deleted", "new_path": ""}


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb
