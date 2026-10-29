"""GitLab MR acceptance: exact head, CE reviewer gate, and forge classification.

The GitLab path is exercised end to end through the real lifecycle
(``kb.complete_task``) against a local HTTP server speaking GitLab API v4, so a
private project's merge request is accepted or refused by the same code that
runs in production — only the base URL is swapped.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pr_acceptance as kpa
from hermes_cli.kanban_db_connect import connect

HEAD = "a" * 40
OTHER = "b" * 40
MR_URL = "https://git.coio.uk/grp/proj/-/merge_requests/5"
REVIEW_LABEL = kpa.GITLAB_REVIEW_LABEL


@pytest.fixture
def gitlab(tmp_path, monkeypatch):
    state = {
        "head": HEAD, "state": "opened", "pipelines": [{"id": 10, "sha": HEAD, "status": "success"}],
        "jobs": [{"id": 1, "name": "pytest", "status": "success", "allow_failure": False},
                 {"id": 2, "name": "optional-scan", "status": "failed", "allow_failure": True}],
        "labels": [REVIEW_LABEL], "author": 1,
        "label_events": [{"action": "add", "label": {"name": REVIEW_LABEL}, "user": {"id": 2}}],
        "requests": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            state["requests"].append(self.path)
            if state.get("http_status"):
                self.send_error(state["http_status"])
                return
            if path == "/api/v4/projects/grp%2Fproj":
                value = {"id": 1, "path_with_namespace": "grp/proj", "only_allow_merge_if_pipeline_succeeds": True}
            elif path == "/api/v4/projects/1/merge_requests/5":
                value = {"sha": state["head"], "state": state["state"], "target_branch": "main",
                         "labels": state["labels"], "author": {"id": state["author"]},
                         "blocking_discussions_resolved": True}
            elif path == "/api/v4/projects/1/merge_requests/5/pipelines":
                value = state["pipelines"]
            elif path.startswith("/api/v4/projects/1/pipelines/") and path.endswith("/jobs"):
                value = state["jobs"]
            elif path == "/api/v4/projects/1/merge_requests/5/resource_label_events":
                value = state["label_events"]
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(kpa, "_GITLAB_API", f"http://127.0.0.1:{server.server_port}/api/v4/")
    monkeypatch.setenv("GITLAB_TOKEN", "test-token-not-a-real-secret")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GL_TOKEN", raising=False)
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _receipt(conn, tid):
    rows = [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
    assert rows, "no acceptance receipt was persisted"
    return rows[-1]


def _complete(conn, contract=MR_URL, published=MR_URL, **kwargs):
    tid = kb.create_task(conn, title="gitlab", completion_contract=contract, **kwargs)
    ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": published})
    return tid, ok


@pytest.mark.platforms("linux")
def test_gitlab_mr_at_exact_head_passes(gitlab):
    with connect() as conn:
        tid, ok = _complete(conn)
        receipt = _receipt(conn, tid)
        assert ok is True, receipt
        assert kb.get_task(conn, tid).status == "done"
        assert receipt["forge"] == "gitlab"
        assert receipt["head_sha"] == HEAD
        assert receipt["pipeline"]["sha"] == HEAD
        # Optional jobs (allow_failure) are not gates.
        names = [c["name"] for c in receipt["checks"]]
        assert "pytest" in names and "optional-scan" not in names
        gate = next(c for c in receipt["checks"] if "reviewer-approval" in c["name"])
        assert gate["classification"] == "success"
        assert gate["approvers"] == [2]


@pytest.mark.platforms("linux")
def test_pipeline_for_another_commit_is_not_acceptance(gitlab):
    """Exact-head discipline: a green pipeline whose sha != the MR head fails the contract."""
    gitlab["pipelines"] = [{"id": 11, "sha": OTHER, "status": "success"}]
    with connect() as conn:
        tid, ok = _complete(conn)
        receipt = _receipt(conn, tid)
        assert ok is False
        assert kb.get_task(conn, tid).status != "done"
        assert receipt["classification"] == "missing"
        assert receipt["pipeline"] is None
        assert "head SHA" in receipt["detail"]


@pytest.mark.platforms("linux")
def test_pipeline_on_old_head_is_stale(gitlab):
    """A pipeline that exists for the head is fine; if the MR head moves first, the
    evidence collected for the old sha is never transferred to the new head."""
    with connect() as conn:
        tid, ok = _complete(conn)
        assert ok is True
    # Now the MR head moves to OTHER while only the old pipeline exists.
    gitlab.update(head=OTHER, pipelines=[{"id": 10, "sha": HEAD, "status": "success"}])
    with connect() as conn:
        tid, ok = _complete(conn)
        receipt = _receipt(conn, tid)
        assert ok is False
        assert receipt["classification"] == "missing"


@pytest.mark.platforms("linux")
def test_failed_required_job_fails_the_contract(gitlab):
    gitlab["jobs"] = [{"id": 1, "name": "pytest", "status": "failed", "allow_failure": False}]
    with connect() as conn:
        tid, ok = _complete(conn)
        receipt = _receipt(conn, tid)
        assert ok is False
        assert receipt["classification"] == "failure"


@pytest.mark.platforms("linux")
def test_reviewer_gate_requires_the_label(gitlab):
    gitlab["labels"] = []
    with connect() as conn:
        tid, ok = _complete(conn)
        receipt = _receipt(conn, tid)
        assert ok is False
        gate = next(c for c in receipt["checks"] if "reviewer-approval" in c["name"])
        assert gate["classification"] == "missing"
        assert "not applied" in gate["conclusion"]


@pytest.mark.platforms("linux")
def test_reviewer_gate_rejects_a_self_applied_label(gitlab):
    """CE cannot enforce approval rules, so the gate is 'labelled by someone else';
    a label the author applied to their own MR must not pass."""
    gitlab["label_events"] = [{"action": "add", "label": {"name": REVIEW_LABEL}, "user": {"id": 1}}]
    with connect() as conn:
        tid, ok = _complete(conn)
        receipt = _receipt(conn, tid)
        assert ok is False
        gate = next(c for c in receipt["checks"] if "reviewer-approval" in c["name"])
        assert gate["classification"] == "missing"
        assert "only by the MR author" in gate["conclusion"]


@pytest.mark.platforms("linux")
def test_missing_gitlab_token_is_auth_and_never_leaks(gitlab, monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    with connect() as conn:
        tid, ok = _complete(conn)
        receipt = _receipt(conn, tid)
        assert ok is False
        assert receipt["classification"] == "auth"
        assert "GITLAB_TOKEN" in receipt["detail"]
        assert "test-token-not-a-real-secret" not in json.dumps(receipt)
        assert gitlab["requests"] == []  # never reached the API without a token


@pytest.mark.platforms("linux")
def test_rejected_token_is_auth_not_infra(gitlab):
    """A 401 from the forge is a bad/expired token to fix, never a retryable infra blip."""
    gitlab["http_status"] = 401
    with connect() as conn:
        tid, ok = _complete(conn)
        receipt = _receipt(conn, tid)
        assert ok is False
        assert receipt["classification"] == "auth"
        assert "401" in receipt["detail"]


def test_unrecognised_forge_url_is_refused_at_declaration():
    with pytest.raises(ValueError, match="not a supported forge"):
        kpa.validate_contract("https://bitbucket.org/acme/repo/pull-requests/1")
    with pytest.raises(ValueError, match="not a supported forge"):
        kpa.validate_contract("https://gitlab.com/acme/repo/-/merge_requests/1")


def test_supported_contract_shapes_are_accepted():
    for value in ("local-only", "acme/repo", "https://github.com/acme/repo/pull/7",
                  MR_URL, "https://git.coio.uk/grp/sub/proj/-/merge_requests/12"):
        assert kpa.validate_contract(value) == value


# --- misclassification: a plan refusal is not a credentials problem ---------

@pytest.mark.platforms("posix")
def test_github_pro_refusal_on_branch_rules_is_not_reported_as_auth(tmp_path, monkeypatch):
    """The documented defect: a private repo on free GitHub answers the branch-rules
    query with 403 'Upgrade to GitHub Pro'. That is a plan answer, not a login to fix."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"""#!{sys.executable}
import json, sys
endpoint = sys.argv[2]
if endpoint == "graphql":
    print(json.dumps({{"data": {{"repository": {{"pullRequest": {{
        "headRefOid": "{HEAD}", "baseRefName": "main", "state": "OPEN",
        "baseRef": {{"branchProtectionRule": None}}}}}}}}}}))
    sys.exit(0)
if "/rules/branches/" in endpoint:
    sys.stderr.write("gh: Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)\\n")
    sys.exit(1)
sys.stdout.write("[]")
""")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="private", completion_contract="acme/private")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/private/pull/7"})
        receipt = _receipt(conn, tid)
    assert receipt["classification"] == "forge", receipt
    assert "not a credentials problem" in receipt["detail"]
    assert "credentials/access" not in receipt["detail"]
