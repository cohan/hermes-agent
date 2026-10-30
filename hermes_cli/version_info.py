"""Canonical runtime identity for Hermes.

Resolution order:
1. Install stamp (``install-stamp.json``) — written at build time by
   ``scripts/write_install_stamp.py`` for every packager (Docker, Nix, and
   the desktop app). The stamp is authoritative
   for packaged builds.
2. Live git — for unstamped source/dev installs with a ``.git`` directory.
3. Unknown — no stamp and no git. The provenance is unknown.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

from hermes_cli.steward import UPDATE_MECHANISMS
from hermes_cli.update_channel import STABLE_TAG_RE


@dataclass(frozen=True)
class VersionInfo:
    base_version: str
    derived_version: str
    distance: int | None
    commit: str | None
    branch: str | None
    source: Literal["build", "commit-build", "ci", "docker", "fallback", "git", "local", "nix", "unknown"]
    dirty: bool = False
    commit_date: int | None = None
    distribution: Literal["docker", "nix", "desktop-app"] | None = None

    @property
    def display_version(self) -> str:
        """``<base>+<distance>`` when a release is known, else the derived identity.
        Keep a tagless checkout's ``git.<sha>`` rather than reducing it to unknown.
        """
        if self.base_version == "unknown":
            return self.derived_version
        return _derived_version(self.base_version, self.distance)


def _derived_version(
    base_version: str,
    distance: int | None,
    dirty: bool = False,
    short_commit: str | None = None,
) -> str:
    if distance and distance > 0:
        suffix = f"{distance}.g{short_commit}" if short_commit else str(distance)
        return f"{base_version}+{suffix}{'.dirty' if dirty and short_commit else ''}"
    if dirty and distance is None:
        return f"{base_version}+?"
    return base_version


def _run_git(repo_dir: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3, cwd=str(repo_dir),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (result.stdout or "").strip()
    return value if result.returncode == 0 and value else None


def _resolve_repo_dir() -> Path | None:
    """Use the executing checkout before a profile's optional clone."""
    repo_dir = Path(__file__).parent.parent.resolve()
    if (repo_dir / ".git").exists():
        return repo_dir
    # The PROCESS home: this is the running code's identity and is cached
    # process-wide, so a profile's context-local override must not pick it.
    from hermes_constants import get_process_hermes_home

    candidate = get_process_hermes_home() / "hermes-agent"
    if (candidate / ".git").exists():
        return candidate
    return None


def _parse_nonnegative(value: str | None) -> int | None:
    try:
        parsed = int(value or "")
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _calver_release_version(repo_dir: Path) -> tuple[str, int] | None:
    """The version the nearest CalVer release shipped, and the commits since it.

    Releases before semver tags existed are tagged ``vYYYY.M.D`` only; the
    version users actually run is in that tag's pyproject. Without this, a
    checkout past such a release would compare as "unknown" against plugins'
    ``requires_hermes``.
    """
    described = _run_git(repo_dir, "describe", "--tags", "--long", "--match", "v2[0-9][0-9][0-9].*", "HEAD")
    if not described:
        return None
    tag, count, _ = described.rsplit("-", 2)
    distance = _parse_nonnegative(count)
    try:
        project = tomllib.loads(_run_git(repo_dir, "show", f"{tag}:pyproject.toml") or "").get("project", {})
    except tomllib.TOMLDecodeError:
        return None
    version = project.get("version")
    if distance is None or not isinstance(version, str) or not STABLE_TAG_RE.fullmatch(f"v{version}"):
        return None
    return version, distance


# --- Install stamp reader ---------------------------------------------------

def _resolve_stamp_file() -> Path | None:
    """The executing tree's stamp (pm.paths.install_stamp_path owns the location)."""
    from pm.paths import install_stamp_path, repo_root

    p = install_stamp_path(repo_root())
    return p if p.is_file() else None


def _stamp_version_info() -> VersionInfo | None:
    """Read provenance from a build-time install stamp."""
    stamp_file = _resolve_stamp_file()
    if stamp_file is None:
        return None
    try:
        raw = stamp_file.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict) or "commit" not in data:
        return None

    # updateMechanism is required in every stamp. A stamp without it means
    # the writing build lane must be fixed, not tolerated.
    if data.get("updateMechanism") not in UPDATE_MECHANISMS:
        raise RuntimeError(
            f"install-stamp.json at {stamp_file} is missing a valid "
            f"'updateMechanism' (one of {', '.join(UPDATE_MECHANISMS)}). The "
            "build lane that wrote this stamp must pass --update-mechanism to "
            "scripts/write_install_stamp.py (or bake the field directly)."
        )
    # A light artifact ships no Python runtime. Reading a light stamp from
    # a Python process means the artifact was mispackaged.
    if data.get("payload") == "light":
        raise RuntimeError(
            f"install-stamp.json at {stamp_file} marks this artifact as 'light' "
            "(no agent runtime). No Python process can legitimately run from a "
            "light artifact — this build is mispackaged."
        )

    commit = data.get("commit") or None
    if not commit or set(commit) == {"0"}:
        # All-zero placeholder = fallback stamp, not real provenance.
        return None
    stamp_source = str(data.get("source") or "")
    if stamp_source == "git" and (stamp_file.parent / ".git").exists():
        live_commit = _run_git(stamp_file.parent, "rev-parse", "HEAD")
        if live_commit and live_commit != commit:
            return None

    base_version = data.get("baseVersion") or "unknown"
    display_version = data.get("displayVersion") or base_version
    distance = data.get("distance")
    if isinstance(distance, str):
        distance = _parse_nonnegative(distance)

    # ``source`` describes build provenance, while ``distribution`` identifies
    # the package form users installed. Keep both facts intact for support.
    source = (
        cast(Literal["build", "commit-build", "ci", "docker", "fallback", "git", "local", "nix", "unknown"], stamp_source)
        if stamp_source in {"commit-build", "ci", "docker", "fallback", "git", "local", "nix"}
        else "build"
    )
    distribution = data.get("distribution")
    if distribution not in {"docker", "nix", "desktop-app"}:
        distribution = None

    commit_date = data.get("commitDate")
    if not isinstance(commit_date, int):
        commit_date = None

    return VersionInfo(
        base_version,
        display_version,
        distance if isinstance(distance, int) else None,
        commit,
        data.get("branch") or None,
        source,
        bool(data.get("dirty")),
        commit_date,
        distribution,
    )


# --- Git provenance (source/dev installs) -----------------------------------


def _working_tree_dirty(repo_dir: Path, *, include_untracked: bool = False) -> bool:
    try:
        # -uno: skip the untracked-file scan. This runs on the startup-banner
        # path, and a full working-tree walk costs real time on large or cold
        # checkouts. Same semantics as write_install_stamp.py.
        # The 3 s timeout kills a slow status (a partial clone lazily fetching a tree, a cold or
        # scanned disk), and a status killed while refreshing the index strands .git/index.lock:
        # the update's own merge then dies on "File exists" (#132089). A read-only probe takes
        # no optional lock.
        status_command = ["git", "--no-optional-locks", "status", "--porcelain"]
        if not include_untracked:
            status_command.append("-uno")
        dirty_result = subprocess.run(
            status_command,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=3,
            cwd=str(repo_dir),
            check=False,
        )
        return dirty_result.returncode == 0 and bool((dirty_result.stdout or "").strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _display_version_for(info: VersionInfo, dirty: bool) -> str:
    """Re-derive ``display_version`` when only the dirty flag changed."""
    short_commit = info.commit[:7] if info.commit else None
    if info.base_version == "unknown" and short_commit:
        return f"git.{short_commit}{'.dirty' if dirty else ''}"
    return _derived_version(info.base_version, info.distance, dirty, short_commit)


def _git_version_info(repo_dir: Path, *, include_untracked: bool = False) -> VersionInfo:
    commit = _run_git(repo_dir, "rev-parse", "HEAD")
    # A detached HEAD has no branch. Leave the field None: every formatter
    # already prints the commit separately and handles a missing branch.
    branch = _run_git(repo_dir, "branch", "--show-current")
    commit_date_raw = _run_git(repo_dir, "log", "-1", "--format=%ct", "HEAD")
    commit_date: int | None = None
    if commit_date_raw and commit_date_raw.isdigit():
        commit_date = int(commit_date_raw)
    dirty = _working_tree_dirty(repo_dir, include_untracked=include_untracked)

    tags = _run_git(repo_dir, "tag", "--merged", "HEAD", "--list", "v[0-9]*")
    releases = [
        tag[1:]
        for tag in (tags or "").splitlines()
        if STABLE_TAG_RE.fullmatch(tag)
    ]
    base_version = (
        max(releases, key=lambda value: tuple(int(part) for part in value.split(".")))
        if releases else "unknown"
    )
    distance = _parse_nonnegative(
        _run_git(repo_dir, "rev-list", "--count", f"v{base_version}..HEAD")
    ) if releases else None
    if not releases:
        base_version, distance = _calver_release_version(repo_dir) or ("unknown", None)
    short_commit = _run_git(repo_dir, "rev-parse", "--short=7", "HEAD")
    if base_version == "unknown" and short_commit:
        display_version = f"git.{short_commit}{'.dirty' if dirty else ''}"
    else:
        display_version = _derived_version(base_version, distance, dirty, short_commit)

    return VersionInfo(
        base_version, display_version, distance, commit, branch, "git", dirty, commit_date
    )


# --- Disk cache for the git probe -------------------------------------------
# A checkout's git identity only changes on a commit, a checkout, or an update,
# but the probe shells out to git six or more times and the CLI start path runs
# it in every new process (pm/worker.py included). On a small host that keeps
# cores warm permanently. Cache the resolved identity, keyed on a stat-only
# signature of the git metadata it depends on, so the steady state spawns no
# git at all. The dirty flag is the one field that can change without touching
# git metadata (editing a tracked file does not update .git/index), so it is
# re-checked on a longer interval of its own and the rest is served from disk.

_GIT_CACHE_SCHEMA = 1
_DIRTY_REFRESH_SECONDS = 900.0
_git_cache_memo: tuple[str, tuple[str, ...], bool, VersionInfo] | None = None


def _git_cache_path() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "cache" / "version_info.json"


def _git_dir(repo_dir: Path) -> Path | None:
    """The metadata directory, following a worktree/submodule ``.git`` file."""
    marker = repo_dir / ".git"
    try:
        if marker.is_dir():
            return marker
        if marker.is_file():
            text = marker.read_text(encoding="utf-8", errors="replace").strip()
            if text.startswith("gitdir:"):
                target = Path(text.split(":", 1)[1].strip())
                return target if target.is_absolute() else (repo_dir / target).resolve()
    except OSError:
        return None
    return None


def _path_signature(path: Path, base: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return f"{-1}"
    try:
        name = str(path.relative_to(base))
    except ValueError:
        name = str(path)
    return f"{name}:{st.st_mtime_ns}:{st.st_size}"


def _git_signature(repo_dir: Path) -> tuple[str, ...] | None:
    """Stat-only fingerprint of the inputs the git identity depends on.

    Deliberately spawns no subprocess: HEAD, the refs (loose and packed), the
    index (dirty flag) and pyproject.toml (CalVer fallback) cover every field
    the probe produces except the working-tree contents themselves.
    """
    git_dir = _git_dir(repo_dir)
    if git_dir is None:
        return None
    # A linked worktree's HEAD and index live in its own gitdir, while the refs
    # and packed-refs it resolves against live in the common dir (commondir).
    common_dir = git_dir
    try:
        raw = (git_dir / "commondir").read_text(encoding="utf-8", errors="replace").strip()
        if raw:
            candidate = Path(raw)
            common_dir = candidate if candidate.is_absolute() else (git_dir / candidate).resolve()
    except OSError:
        pass
    parts = [_path_signature(repo_dir / ".git", repo_dir)]
    seen: set[Path] = set()
    for directory in (git_dir, common_dir):
        if directory in seen:
            continue
        seen.add(directory)
        for name in ("commondir", "HEAD", "index", "packed-refs", "ORIG_HEAD"):
            parts.append(_path_signature(directory / name, directory))
        refs = directory / "refs"
        try:
            for path in sorted(refs.rglob("*")):
                if path.is_file():
                    parts.append(_path_signature(path, directory))
        except OSError:
            parts.append("refs:unreadable")
    parts.append(_path_signature(repo_dir / "pyproject.toml", repo_dir))
    return tuple(parts)


def _info_to_json(info: VersionInfo) -> dict:
    return {field: getattr(info, field) for field in VersionInfo.__dataclass_fields__}


def _info_from_json(data: object) -> VersionInfo | None:
    if not isinstance(data, dict):
        return None
    fields = VersionInfo.__dataclass_fields__
    kwargs = {key: data[key] for key in fields if key in data}
    if not {"base_version", "derived_version", "source"} <= set(kwargs):
        return None
    try:
        return VersionInfo(**kwargs)  # type: ignore[arg-type]
    except TypeError:
        return None


def _read_git_cache() -> dict | None:
    try:
        raw = _git_cache_path().read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("schema") == _GIT_CACHE_SCHEMA else None


def _write_git_cache(repo_dir: Path, signature: tuple[str, ...], include_untracked: bool, info: VersionInfo) -> None:
    payload = {
        "schema": _GIT_CACHE_SCHEMA,
        "repoDir": str(repo_dir),
        "signature": list(signature),
        "includeUntracked": include_untracked,
        "dirtyCheckedAt": time.time(),
        "info": _info_to_json(info),
    }

    # A plain temp-file + replace, not utils.atomic_json_write: importing utils
    # drags the YAML stack onto the CLI start path this cache exists to keep cheap.
    path = _git_cache_path()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _git_version_info_cached(repo_dir: Path, *, include_untracked: bool = False) -> VersionInfo:
    """``_git_version_info`` behind a disk cache that survives the process."""
    global _git_cache_memo
    signature = _git_signature(repo_dir)
    if signature is None:
        # Not a recognizable git dir (or unreadable): probe directly, no cache.
        return _git_version_info(repo_dir, include_untracked=include_untracked)

    memo = _git_cache_memo
    if memo is not None and memo[0] == str(repo_dir) and memo[1] == signature and memo[2] == include_untracked:
        return memo[3]

    cached = _read_git_cache()
    if (
        cached is not None
        and cached.get("repoDir") == str(repo_dir)
        and tuple(cached.get("signature") or ()) == signature
        and bool(cached.get("includeUntracked")) == include_untracked
    ):
        info = _info_from_json(cached.get("info"))
        if info is not None:
            checked_at = cached.get("dirtyCheckedAt")
            age = time.time() - checked_at if isinstance(checked_at, (int, float)) else None
            if age is not None and age < _DIRTY_REFRESH_SECONDS:
                _git_cache_memo = (str(repo_dir), signature, include_untracked, info)
                return info
            dirty = _working_tree_dirty(repo_dir, include_untracked=include_untracked)
            if dirty != info.dirty:
                info = replace(info, dirty=dirty, display_version=_display_version_for(info, dirty))
            _write_git_cache(repo_dir, signature, include_untracked, info)
            _git_cache_memo = (str(repo_dir), signature, include_untracked, info)
            return info

    info = _git_version_info(repo_dir, include_untracked=include_untracked)
    _write_git_cache(repo_dir, signature, include_untracked, info)
    _git_cache_memo = (str(repo_dir), signature, include_untracked, info)
    return info


# --- Cache + public API -----------------------------------------------------

_cached_version_info: VersionInfo | None = None


def _reset_version_info_cache() -> None:
    """Test-only cache reset."""
    global _cached_version_info, _git_cache_memo
    _cached_version_info = None
    _git_cache_memo = None


def get_version_info() -> VersionInfo:
    """Return cached provenance from install stamp, git, or unknown."""
    global _cached_version_info
    if _cached_version_info is not None:
        return _cached_version_info

    # 1. Install stamp (packaged builds: Docker, Nix)
    # A malformed stamp (missing/illegal updateMechanism, mispackaged "light"
    # payload) raises RuntimeError — the build lane that wrote it must be
    # fixed, but a crashing CLI is the wrong failure mode for every uncached
    # caller. Degrade to "unknown" and let the authoring lane's own tests
    # surface the malformed stamp.
    try:
        info = _stamp_version_info()
    except RuntimeError:
        info = None

    # 2. Live git (source/dev installs)
    if info is None:
        repo_dir = _resolve_repo_dir()
        if repo_dir is not None:
            info = _git_version_info_cached(repo_dir)

    # 3. Unknown — no stamp, no git
    if info is None:
        info = VersionInfo("unknown", "unknown", None, None, None, "unknown")

    _cached_version_info = info
    return info


def get_code_identity(refresh: bool = False) -> dict:
    """Return the running install's code identity as a flat dict.

    Shape: ``{"sha": full sha | None, "short_sha": str | None, "version":
    base package version | None, "source": str, "commit_date": epoch s | None}`` — what the update
    receipt, runtime inventory, and gateway status stamping consume.
    Backed by :func:`get_version_info` (install stamp first, live git
    second, unknown third), so every consumer shares one resolution
    policy. Returned dicts are fresh copies, never the shared cache.

    ``refresh=True`` drops the per-process cache first — the updater uses
    it to re-read identity after swapping the checkout out from under the
    running process.
    """
    global _cached_version_info
    if refresh:
        _cached_version_info = None
    info = get_version_info()
    return {
        "sha": info.commit,
        "short_sha": info.commit[:8] if info.commit else None,
        "version": info.base_version,
        "source": info.source,
        "commit_date": info.commit_date,
    }
