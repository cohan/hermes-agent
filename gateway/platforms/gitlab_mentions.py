"""Opt-in GitLab mention extraction for the generic webhook adapter."""

from __future__ import annotations

import re
from typing import Any


SUPPORTED_EVENT_TYPES = frozenset({"Issue Hook", "Merge Request Hook", "Note Hook"})
_MENTION_RE = re.compile(r"(?<![\w@])@([a-z][a-z0-9_-]{0,31})(?![\w@])")


def _comment_text(payload: dict[str, Any], event_type: str) -> str:
    attrs = payload.get("object_attributes")
    if not isinstance(attrs, dict):
        return ""
    if event_type == "Note Hook":
        value = attrs.get("note", "")
    else:
        value = attrs.get("description", "")
    return value if isinstance(value, str) else ""


def extract_gitlab_mention(payload: Any, event_type: str, mapping: Any) -> dict[str, Any] | None:
    """Return one allow-listed mention context, or None for unsupported/unmatched input.

    Mapping keys are exact GitLab usernames. Only issue, merge-request and note hooks are
    accepted, and only comment/description text is scanned; URLs and arbitrary payload fields
    cannot create a route.
    """
    if event_type not in SUPPORTED_EVENT_TYPES or not isinstance(payload, dict) or not isinstance(mapping, dict):
        return None
    text = _comment_text(payload, event_type)
    if not text:
        return None
    for username in _MENTION_RE.findall(text):
        profile = mapping.get(username)
        if isinstance(profile, str) and profile.strip():
            project_value = payload.get("project")
            project = project_value if isinstance(project_value, dict) else {}
            attrs_value = payload.get("object_attributes")
            attrs = attrs_value if isinstance(attrs_value, dict) else {}
            user_value = payload.get("user")
            user = user_value if isinstance(user_value, dict) else {}
            return {
                "username": username,
                "profile": profile.strip(),
                "event_type": event_type,
                "comment": text,
                "project_id": project.get("id"),
                "project_path": project.get("path_with_namespace"),
                "project_url": project.get("web_url"),
                "author": user.get("username") or user.get("name"),
                "author_id": user.get("id"),
                "object_kind": payload.get("object_kind"),
                "object_id": attrs.get("id"),
                "object_iid": attrs.get("iid"),
                "object_url": attrs.get("url") or attrs.get("web_url"),
            }
    return None


def render_gitlab_mention_prompt(context: dict[str, Any], custom_prompt: str = "") -> str:
    """Render stable, non-secret routing context while retaining the original comment verbatim."""
    import json

    fields = json.dumps(context, ensure_ascii=False, sort_keys=True, indent=2)
    suffix = f"\n\nOperator route instructions:\n{custom_prompt}" if custom_prompt else ""
    return f"GitLab mention routed to @{context['username']} from {context.get('author') or 'unknown'}:\n{fields}{suffix}"
