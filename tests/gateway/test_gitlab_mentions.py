"""GitLab mention routing contracts."""

from gateway.platforms.gitlab_mentions import extract_gitlab_mention, render_gitlab_mention_prompt


def _payload(text):
    return {
        "object_kind": "note",
        "object_attributes": {"note": text, "id": 91, "iid": 7, "url": "https://gitlab.test/p/-/issues/7"},
        "project": {"id": 12, "path_with_namespace": "group/project", "web_url": "https://gitlab.test/group/project"},
        "user": {"id": 44, "username": "cohan", "name": "Cohan"},
    }


def test_exact_allow_listed_mention_preserves_routing_context():
    context = extract_gitlab_mention(_payload("Please fix this, @builder"), "Note Hook", {"builder": "builder"})
    assert context is not None
    assert context["profile"] == "builder"
    assert context["project_id"] == 12
    assert context["object_iid"] == 7
    assert context["author"] == "cohan"
    assert "@builder" in render_gitlab_mention_prompt(context)


def test_email_style_and_unknown_mentions_are_ignored():
    assert extract_gitlab_mention(_payload("mail a@builder.test"), "Note Hook", {"builder": "builder"}) is None
    assert extract_gitlab_mention(_payload("@unknown please help"), "Note Hook", {"builder": "builder"}) is None


def test_only_supported_gitlab_event_types_are_scanned():
    assert extract_gitlab_mention(_payload("@builder"), "Push Hook", {"builder": "builder"}) is None


def test_issue_and_merge_request_descriptions_are_supported():
    for event_type in ("Issue Hook", "Merge Request Hook"):
        payload = _payload("")
        payload["object_attributes"] = {"description": "@ops inspect this"}
        result = extract_gitlab_mention(payload, event_type, {"ops": "ops"})
        assert result is not None
        assert result["profile"] == "ops"
