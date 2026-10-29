"""Exact-head acceptance for explicitly declared PR / merge-request tasks.

Two forges are supported, selected by the *shape* of the contract — never by a
fallback that guesses:

- ``github.com`` — a bare ``OWNER/REPO`` slug (GitHub by definition, the original
  behaviour) or an exact ``https://github.com/O/R/pull/N`` URL.
- ``git.coio.uk`` — GitLab CE, an exact
  ``https://git.coio.uk/GROUP/PROJECT/-/merge_requests/N`` URL. Nested groups are
  allowed; the host list is :data:`GITLAB_HOSTS`.

Any other ``scheme://host/...`` value is refused at declaration time by
:func:`validate_contract` rather than being read against GitHub.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured run/status/contract under its lock.

``gh`` runs as the card's assignee profile (``profile_home``), not the ambient
login: :func:`_gh_env` resolves that profile's own credentials/config for the
subprocess — a multi-profile host's default ``gh`` login cannot read another
org's private repos (#122689). The GitLab token comes from the same seam
(``GITLAB_TOKEN`` in that profile's ``.env``) or, failing that, from that
profile's vault: a ``login`` item bound to ``https://git.coio.uk``. It is never
written into a receipt, a comment or a repository.

Exact-head discipline is forge-specific but has the same meaning: GitHub reads
check-runs/statuses for the pinned ``headRefOid``; GitLab reads the jobs of the
pipeline whose ``sha`` equals the merge request's head SHA, so a green pipeline
for some other commit is never acceptance.

GitLab CE has no merge-request approval rules (``/approval_rules`` is 404), so
the reviewer gate is the ``kanban-accepted`` label **applied by a user other
than the MR author**, verified through ``resource_label_events`` — the forge's
own audit trail of who applied it. Limitation: CE cannot permission-enforce an
approval rule, so this is an explicit human attestation recorded by the forge
rather than a gate the forge itself refuses to bypass.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_GITHUB_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")
# Backwards-compatible alias: the GitHub PR URL pattern other modules import.
_PR = _GITHUB_PR
_GITLAB_PROJECT = r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
_GITLAB_MR = re.compile(r"https://(git\.coio\.uk)/(" + _GITLAB_PROJECT + r")/-/merge_requests/([1-9][0-9]*)")
_HAS_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_SHA = re.compile(r"[0-9a-f]{40}")

GITLAB_HOSTS = ("git.coio.uk",)
#: Label a reviewer applies to a merge request to accept it (the GitLab CE gate).
GITLAB_REVIEW_LABEL = "kanban-accepted"
_GITLAB_API = "https://{host}/api/v4/"
_GITLAB_TIMEOUT = 30

# A plan/visibility refusal, not a credential refusal: this is the exact wording
# GitHub returns for branch rules on a private repo without Pro.
_PLAN_REFUSAL = re.compile(r"upgrade to github pro|make this repository public", re.I)

_RECOVERY = ("Fix required failures, rerun infrastructure checks or wait, then retry completion. "
             "Use kanban_block if human input is needed; receipts remain on the task event log.")


def _ref(value):
    """Parse a contract / ``published_pr`` value into a forge reference.

    Returns ``{"forge", "path", "number", "url"}`` for an exact PR/MR URL, or
    ``None`` for anything that is not one (a bare ``OWNER/REPO`` slug is GitHub
    publication without a known PR number).
    """
    if not isinstance(value, str):
        return None
    match = _GITHUB_PR.fullmatch(value)
    if match:
        return {"forge": "github", "path": match[1], "number": int(match[2]), "url": value}
    match = _GITLAB_MR.fullmatch(value)
    if match:
        return {"forge": "gitlab", "path": match[2], "number": int(match[3]), "url": value}
    return None


def validate_contract(value: str | None) -> str:
    if value is None or value == "local-only":
        return "local-only"
    if not isinstance(value, str):
        raise ValueError("completion_contract must be local-only, OWNER/REPO, or an exact PR/MR URL")
    if _REPO.fullmatch(value) or _ref(value):
        return value
    if _HAS_SCHEME.search(value):
        host = urlsplit(value).hostname or value
        raise ValueError(
            f"completion_contract URL host {host!r} is not a supported forge "
            f"(github.com, or a merge-request URL on {', '.join(GITLAB_HOSTS)})")
    raise ValueError("completion_contract must be local-only, OWNER/REPO, or an exact PR/MR URL")


def _receipt(published_pr: str | None) -> dict:
    return {"ok": False, "classification": "missing", "head_sha": None,
            "pr_url": published_pr, "checks": [], "recovery": _RECOVERY}


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #

class _GateAuthError(RuntimeError):
    """The forge refused the read at HTTP 401/403/404 (or GraphQL returned no
    repository): this profile's identity cannot see the repo — an identity
    problem to fix, not an infrastructure blip to retry."""


class _GateTokenMissing(_GateAuthError):
    """No GitLab token could be resolved for this profile."""


class _ForgeCapabilityError(RuntimeError):
    """The forge answered, but this plan/edition cannot answer the *query*
    (branch rules on a private repo without Pro, an unavailable endpoint).
    Never a credentials problem."""


# --------------------------------------------------------------------------- #
# identity / credentials
# --------------------------------------------------------------------------- #

def _api(endpoint: str, *, query: str | None = None, paginate: bool = False,
         profile_home: str | None = None, capability: bool = False):
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if query is not None:
        command += ["-f", "query=" + query]
    if paginate:
        command += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30,
                                check=True, env=_gh_env(profile_home))
    except subprocess.CalledProcessError as exc:
        # Never persist gh's stderr (credentials/host details); classify only.
        blob = f"{exc.stderr or ''}\n{exc.stdout or ''}"
        if _PLAN_REFUSAL.search(blob):
            raise _ForgeCapabilityError(
                f"GitHub plan/visibility refused {endpoint.split('?')[0]} "
                "(\"Upgrade to GitHub Pro or make this repository public\")") from None
        # 401/403/404 = the login cannot see this repository (wrong profile identity
        # or missing grant) — unless the caller already proved visibility and is
        # asking a plan-gated query, where 403/404 means "this plan cannot answer".
        denied = re.search(r"HTTP (40[134])", exc.stderr or "")
        if denied:
            if capability and denied[1] in {"403", "404"}:
                raise _ForgeCapabilityError(
                    f"HTTP {denied[1]} on {endpoint.split('?')[0]} — this plan/visibility "
                    "cannot answer that query") from None
            raise _GateAuthError(f"HTTP {denied[1]} on {endpoint.split('?')[0]}") from None
        if exc.returncode == 4:  # gh's authentication-required exit: this profile has no login
            raise _GateAuthError(f"gh has no login for {endpoint.split('?')[0]}") from None
        raise
    value = json.loads(result.stdout)
    if isinstance(value, dict) and value.get("errors"):
        raise ValueError("GitHub returned incomplete GraphQL evidence")
    return value


def _gh_env(profile_home: str | None) -> dict[str, str] | None:
    """Child env for ``gh``: the card's profile identity when one is resolvable.

    The completion boundary runs in the worker (assignee), the CLI, or a
    reviewer/dispatcher turn, so an ambient ``gh`` login is whichever process
    happened to call it (#122689). ``served_profile_child_env(inherit_credentials=True)``
    is the seam for "this child acts for that profile": it scrubs the launch
    profile's credential residue and overlays the target profile's own
    ``GH_TOKEN``/``GH_CONFIG_DIR`` (its ``.env`` + external secret sources).
    ``None`` keeps the ambient env — unassigned cards behave exactly as before.
    """
    if not profile_home:
        return None
    from tools.environments.local import _is_routed_home, hermes_subprocess_env, served_profile_child_env
    base = hermes_subprocess_env(inherit_credentials=True)
    routed = _is_routed_home(profile_home)
    if routed:
        # gh's config dir decides which login `gh api` uses, yet it is a path, not a
        # credential, so no scrub list sees it; the target's own value is overlaid from its .env.
        base.pop("GH_CONFIG_DIR", None)
    env = served_profile_child_env(base=base, target_home=profile_home, inherit_credentials=True)
    if routed and not (env.keys() & {"GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"}):
        # HOME/XDG_CONFIG_HOME are still the launch process's: without a login of its own the
        # child would fall through to ~/.config/gh/hosts.yml — the ambient login. Pin gh's config
        # to a profile-owned dir so it fails "not logged in" (exit 4 -> auth) instead.
        env["GH_CONFIG_DIR"] = str(Path(profile_home) / "gh")
    return env


def _profile_env(profile_home: str | None) -> dict[str, str]:
    """Env a forge child would run under: the profile's own credentials overlaid
    on the launch env, or the launch env itself for an unassigned card."""
    if not profile_home:
        return dict(os.environ)
    from tools.environments.local import hermes_subprocess_env, served_profile_child_env
    base = hermes_subprocess_env(inherit_credentials=True)
    return served_profile_child_env(base=base, target_home=profile_home, inherit_credentials=True)


def _gitlab_token(profile_home: str | None, assignee: str | None) -> str:
    """The GitLab token for this card: profile env first, then that profile's vault.

    Order: ``GITLAB_TOKEN``/``GL_TOKEN`` in the card profile's env (its ``.env``),
    then a ``login`` vault item bound to ``https://git.coio.uk``. The value never
    reaches a receipt, a log line or a repository; only "no token" is reported.
    """
    env = _profile_env(profile_home)
    token = (env.get("GITLAB_TOKEN") or env.get("GL_TOKEN") or "").strip()
    if token:
        return token
    token = _vault_gitlab_token(profile_home)
    if token:
        return token
    who = f"assignee profile {assignee!r}" if assignee else "the ambient environment"
    raise _GateTokenMissing(
        f"no GitLab token for {who}: set GITLAB_TOKEN in that profile's .env "
        f"(or add a vault login for https://{GITLAB_HOSTS[0]})")


def _vault_gitlab_token(profile_home: str | None) -> str | None:
    from hermes_constants import get_hermes_home
    home = Path(profile_home) if profile_home else Path(get_hermes_home())
    try:
        from agent.vault_store import VaultStore
    except Exception:
        return None
    store = VaultStore(home / "vault")
    try:
        items = [item for item in store.list_items()
                 if item.kind == "login" and (item.origin or "") == f"https://{GITLAB_HOSTS[0]}"]
    except Exception:
        return None
    if not items:
        return None
    if len(items) > 1:
        raise _GateTokenMissing(
            f"{len(items)} vault logins are bound to https://{GITLAB_HOSTS[0]} in {home.name}; "
            "keep exactly one GitLab token there or set GITLAB_TOKEN")
    try:
        return str(store.resolve_secret(items[0].id).get("password") or "") or None
    except Exception:
        return None


def _assignee_profile_home(assignee: str | None) -> str | None:
    """Home whose ``gh`` login must read the contract repo — the assignee's, resolved
    exactly as the dispatcher resolves the worker's home — or None (unassigned) so the
    ambient login is used. An assigned card whose profile cannot be resolved is an
    identity failure (``auth``), never a silent fall-through to the ambient login."""
    if not assignee:
        return None
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    try:
        return resolve_profile_env(normalize_profile_name(assignee))
    except (FileNotFoundError, ValueError):
        raise _GateAuthError(f"assignee profile {assignee!r} cannot be resolved") from None


# --------------------------------------------------------------------------- #
# GitLab (git.coio.uk, API v4)
# --------------------------------------------------------------------------- #

def _gl_api(endpoint: str, *, token: str, capability: bool = False, missing_ok: bool = False):
    url = _GITLAB_API.format(host=GITLAB_HOSTS[0]) + endpoint
    request = urllib.request.Request(url, headers={
        "PRIVATE-TOKEN": token, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_GITLAB_TIMEOUT) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # The error body is dropped: it can echo scopes/host detail but never the token.
        if exc.code == 401:
            raise _GateAuthError(
                f"HTTP 401 on {endpoint.split('?')[0]} — the GitLab token was rejected") from None
        if exc.code in (403, 404):
            if missing_ok:
                return None
            if capability:
                raise _ForgeCapabilityError(
                    f"HTTP {exc.code} on {endpoint.split('?')[0]} — this GitLab edition/visibility "
                    "cannot answer that query") from None
            raise _GateAuthError(f"HTTP {exc.code} on {endpoint.split('?')[0]}") from None
        raise OSError(f"GitLab HTTP {exc.code} on {endpoint.split('?')[0]}") from None
    except urllib.error.URLError as exc:
        raise OSError(f"GitLab unreachable: {endpoint.split('?')[0]}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise ValueError(f"GitLab returned non-JSON for {endpoint.split('?')[0]}") from None


def _classify_job(status: str | None) -> str:
    if status in {"success", "skipped"}:
        return "success"      # a conditional job that did not need to run is not a gate
    if status in {"created", "waiting_for_resource", "preparing", "pending", "running", "manual"}:
        return "pending"
    if status in {"failed", "canceled", "cancelled"}:
        return "failure"
    return "infra"


def _review_gate_ce(project_id: int, iid: int, mr: dict, *, token: str) -> dict:
    """GitLab CE reviewer gate: ``kanban-accepted`` applied by someone other than
    the author, read back from the label-event audit trail (CE has no approval rules)."""
    entry = {"name": f"reviewer-approval (label {GITLAB_REVIEW_LABEL} added by a non-author)",
             "head_sha": mr.get("sha"), "classification": "missing"}
    labels = set(mr.get("labels") or [])
    if GITLAB_REVIEW_LABEL not in labels:
        entry["conclusion"] = f"label {GITLAB_REVIEW_LABEL} is not applied"
        return entry
    events = _gl_api(f"projects/{project_id}/merge_requests/{iid}/resource_label_events"
                     "?per_page=100", token=token, missing_ok=True)
    if events is None:
        entry.update(classification="infra",
                     conclusion="label events unavailable — reviewer cannot be verified")
        return entry
    author = (mr.get("author") or {}).get("id")
    approvers = {(event.get("user") or {}).get("id") for event in events
                 if event.get("action") == "add"
                 and (event.get("label") or {}).get("name") == GITLAB_REVIEW_LABEL}
    approvers.discard(None)
    others = {a for a in approvers - {author} if a is not None}
    if not others:
        entry["conclusion"] = "label applied only by the MR author"
        return entry
    if mr.get("blocking_discussions_resolved") is False:
        entry.update(classification="pending", conclusion="unresolved blocking discussions")
        return entry
    entry.update(classification="success", conclusion="success", approvers=sorted(others))
    return entry


def _collect_gitlab(ref: dict, profile_home: str | None, assignee: str | None) -> dict:
    receipt = _receipt(ref["url"])
    receipt["forge"] = "gitlab"
    token = _gitlab_token(profile_home, assignee)
    project = _gl_api(f"projects/{quote(ref['path'], safe='')}", token=token)
    if not isinstance(project, dict) or "id" not in project:
        raise _GateAuthError(f"GitLab returned no project for {ref['path']}")
    project_id, iid = project["id"], ref["number"]
    mr = _gl_api(f"projects/{project_id}/merge_requests/{iid}", token=token)
    if not isinstance(mr, dict):
        raise _GateAuthError(f"GitLab returned no merge request !{iid} for {ref['path']}")
    sha = mr.get("sha") or (mr.get("diff_refs") or {}).get("head_sha")
    target = mr.get("target_branch")
    if not _SHA.fullmatch(sha or "") or mr.get("state") not in {"opened", "merged", "locked"}:
        raise ValueError("MR is closed or current head is unavailable")
    receipt["head_sha"] = sha
    receipt["base"] = target

    # Exact head: only a pipeline whose own sha equals the MR head SHA may be read.
    pipelines = _gl_api(f"projects/{project_id}/merge_requests/{iid}/pipelines?per_page=100",
                        token=token) or []
    head_pipelines = [p for p in pipelines if p.get("sha") == sha]
    jobs: list[dict] = []
    if head_pipelines:
        pipeline = max(head_pipelines, key=lambda p: p.get("id") or 0)
        receipt["pipeline"] = {"id": pipeline.get("id"), "sha": pipeline.get("sha"),
                               "status": pipeline.get("status")}
        jobs = _gl_api(f"projects/{project_id}/pipelines/{pipeline['id']}/jobs"
                       "?per_page=100&include_retried=false", token=token) or []
    else:
        receipt["pipeline"] = None

    # Only jobs the forge does not mark optional are gates; retries collapse to the
    # latest job id per name so an old failure cannot outvote a green rerun.
    latest: dict[str, dict] = {}
    for job in jobs:
        if job.get("allow_failure") or not job.get("name"):
            continue
        current = latest.get(job["name"])
        if current is None or (job.get("id") or 0) > (current.get("id") or 0):
            latest[job["name"]] = job
    receipt["required"] = [{"context": name} for name in sorted(latest)]
    if not latest:
        receipt["detail"] = ("No required pipeline job exists for the merge request head SHA; "
                             "a pipeline for another commit is not acceptance. Explicitly use a "
                             "local-only contract for non-CI tasks.")
        return receipt

    outcomes: list[str] = []
    for name in sorted(latest):
        job = latest[name]
        classification = _classify_job(job.get("status"))
        outcomes.append(classification)
        receipt["checks"].append({"name": name, "id": job.get("id"), "url": job.get("web_url"),
                                  "head_sha": sha, "classification": classification,
                                  "conclusion": job.get("status")})

    gate = _review_gate_ce(project_id, iid, mr, token=token)
    outcomes.append(gate["classification"])
    receipt["checks"].append(gate)

    # Re-read after collecting evidence: a head move invalidates everything above.
    current = _gl_api(f"projects/{project_id}/merge_requests/{iid}", token=token)
    if not isinstance(current, dict):
        raise _GateAuthError(f"GitLab stopped returning merge request !{iid} while collecting evidence")
    if (current.get("sha") or (current.get("diff_refs") or {}).get("head_sha")) != sha \
            or current.get("target_branch") != target \
            or current.get("state") not in {"opened", "merged", "locked"}:
        receipt.update(classification="stale",
                       detail="MR head/base changed while collecting evidence; retry.")
        return receipt
    receipt["classification"] = next((x for x in outcomes if x != "success"),
                                     "missing" if not outcomes else "success")
    receipt["ok"] = receipt["classification"] == "success"
    return receipt


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #

def _collect_github(ref: dict, profile_home: str | None) -> dict:
    repo, number = ref["path"], ref["number"]
    receipt = _receipt(ref["url"])
    receipt["forge"] = "github"
    owner, name = repo.split("/")
    query = f'{{repository(owner:{json.dumps(owner)},name:{json.dumps(name)}){{pullRequest(number:{number:d}){{headRefOid baseRefName state\n            baseRef{{branchProtectionRule{{requiredStatusChecks{{context app{{databaseId}}}}}}}}}}}}}}'
    repository = _api("graphql", query=query, profile_home=profile_home)["data"]["repository"]
    if repository is None:
        # A private repo the login cannot read resolves to null, not an error.
        raise _GateAuthError(f"HTTP 404 on graphql {repo}")
    pr = repository["pullRequest"]
    sha, branch = pr["headRefOid"], pr["baseRefName"]
    receipt["head_sha"] = sha
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or pr["state"] not in {"OPEN", "MERGED"}:
        raise ValueError("PR is closed or current head is unavailable")
    protection = (pr.get("baseRef") or {}).get("branchProtectionRule") or {}
    required = {(r["context"], (r.get("app") or {}).get("databaseId")) for r in protection.get("requiredStatusChecks", [])}
    # Visibility is already proven above, so a 403/404 here is this plan refusing the
    # branch-rules query (private repo without GitHub Pro) — not a credentials problem.
    rules = _api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100",
                 paginate=True, profile_home=profile_home, capability=True)
    for page in rules:
        for rule in page:
            if rule["type"] == "required_status_checks":
                required.update((r["context"], r.get("integration_id"))
                                for r in rule["parameters"]["required_status_checks"])
    receipt["required"] = [{"context": c, "app_id": a} for c, a in sorted(required, key=str)]
    if not required:
        receipt["detail"] = "No repository-required checks are configured; explicitly use a local-only contract for non-CI tasks."
        return receipt
    pages = _api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest",
                 paginate=True, profile_home=profile_home)
    runs = [run for page in pages for run in page["check_runs"]]
    if len({r["id"] for r in runs}) != pages[0]["total_count"]:
        raise ValueError("Incomplete check-run pagination")
    statuses = [{**s, "sha": sha} for page in _api(f"repos/{repo}/commits/{sha}/statuses?per_page=100",
                                                   paginate=True, profile_home=profile_home) for s in page]
    outcomes = []
    for context, app_id in sorted(required, key=str):
        matching = [r for r in runs if r["name"] == context and
                    (app_id in (None, -1) or r["app"]["id"] == app_id)]
        # A legacy status can satisfy an unpinned context, but never a check pinned to an app.
        legacy = [s for s in statuses if s["context"] == context] if app_id in (None, -1) else []
        selected = matching + ([max(legacy, key=lambda s: s["id"])] if legacy else [])
        if not selected:
            outcomes.append("missing")
            receipt["checks"].append({"name": context, "classification": "missing", "head_sha": sha})
        for check in selected:
            is_run = "conclusion" in check
            outcome = check.get("conclusion") if is_run else check["state"]
            classification = _classify(check, sha, outcome, is_run)
            outcomes.append(classification)
            receipt["checks"].append({"name": context, "id": check["id"],
                "url": check.get("html_url") or check.get("target_url"),
                "head_sha": check.get("head_sha", check.get("sha")),
                "classification": classification, "conclusion": outcome})
    # Re-read after all pages: old-head successes are never transferable.
    current = _api(f"repos/{repo}/pulls/{number}", profile_home=profile_home)
    if current["head"]["sha"] != sha or current["base"]["ref"] != branch or (current["state"] == "closed" and not current.get("merged")):
        receipt.update(classification="stale", detail="PR head/base changed while collecting evidence; retry.")
        return receipt
    receipt["classification"] = next((x for x in outcomes if x != "success"), "missing" if not outcomes else "success")
    receipt["ok"] = receipt["classification"] == "success"
    return receipt


def collect_acceptance(contract: str, published_pr: str | None,
                       assignee: str | None = None) -> dict:
    receipt = _receipt(published_pr)
    try:
        profile_home = _assignee_profile_home(assignee)
        declared = _ref(contract)
        url = contract if declared else published_pr
        ref = _ref(url or "")
        if not ref or (not declared and ref["path"] != contract) or (declared and published_pr and published_pr != contract):
            receipt["detail"] = "Supply metadata.published_pr matching the persisted completion contract."
            return receipt
        receipt["pr_url"] = ref["url"]
        if ref["forge"] == "gitlab":
            return _collect_gitlab(ref, profile_home, assignee)
        return _collect_github(ref, profile_home)
    except _GateTokenMissing as exc:
        receipt.update(classification="auth",
                       detail=f"GitLab acceptance cannot read the merge request ({exc}); "
                              "add that profile's GitLab token, then retry completion.")
        return receipt
    except _GateAuthError as exc:
        login = f"assignee profile {assignee!r}'s login" if assignee else "the ambient login"
        receipt.update(classification="auth",
                       detail=f"A forge refused the acceptance read ({exc}) as {login}; "
                              "fix that profile's credentials/access to the repository, then retry completion.")
        return receipt
    except _ForgeCapabilityError as exc:
        receipt.update(classification="forge",
                       detail=f"This forge/plan cannot answer the contract query ({exc}). "
                              "That is not a credentials problem: branch-rule evidence needs GitHub Pro "
                              f"for a private repo. Use a GitLab merge-request contract on {GITLAB_HOSTS[0]} "
                              "or declare local-only.")
        return receipt
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        # Never persist forge stderr (credentials/host details); the failed phase is actionable.
        receipt.update(classification="infra",
                       detail="Forge acceptance evidence unavailable or incomplete; check the forge token/API access and retry.")
        return receipt


def _classify(check: dict, sha: str, outcome: str | None, is_run: bool) -> str:
    if check.get("head_sha", check.get("sha")) != sha:
        return "stale"
    if is_run and check.get("status") != "completed":
        return "pending"
    return {"success": "success", "failure": "failure", "error": "infra", "pending": "pending"}.get(outcome, "infra")
