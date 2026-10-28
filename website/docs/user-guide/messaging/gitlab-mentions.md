# GitLab bot mentions

GitLab routing is opt-in per webhook route. It uses the existing generic webhook adapter and does
not grant the receiving profile GitLab API permissions.

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      routes:
        gitlab-mentions:
          secret: "replace-with-a-GitLab-signing-token"
          events: ["Issue Hook", "Merge Request Hook", "Note Hook"]
          gitlab_mentions:
            profiles:
              assistant: assistant
              builder: builder
              ops: ops
              researcher: researcher
            # Set true only while validating a fixture; no agent run is admitted.
            dry_run: false
          # Optional instructions appended after the preserved GitLab context.
          prompt: "Use the normal Hermes approval and permission boundaries."
```

GitLab should be configured with a signing token. Hermes validates the Standard Webhooks
`webhook-id`, `webhook-timestamp`, and `webhook-signature` headers, rejects stale timestamps,
and deduplicates the delivery id. The legacy `X-Gitlab-Token` path remains supported by the
underlying generic adapter but does not provide replay protection; use signing tokens for this
route.

## Supported event matrix

| GitLab event header | Payload text scanned | Result |
| --- | --- | --- |
| `Issue Hook` | `object_attributes.description` | route first allow-listed exact mention |
| `Merge Request Hook` | `object_attributes.description` | route first allow-listed exact mention |
| `Note Hook` | `object_attributes.note` | route first allow-listed exact mention |
| Any other event | none | ignored |

Only exact configured usernames route. `a@builder.test`, unknown usernames, and mentions in
unscanned fields are ignored. The prompt preserves the GitLab project id/path/URL, author id and
username, object id/iid/URL, event type, full comment text, and selected username.

## Dry-run readback

Send the same signed request to the route with `?dry_run=1`. A `200` response contains
`status: dry_run` and the parsed context, without admitting an agent run. The route remains
allow-listed and authenticated in dry-run mode.

Bot-to-bot mentions and `@cohan` can be enabled by adding explicit mappings. They do not bypass
normal Hermes approval gates: spending, destructive deletion, force-push, DNS/hosting/deploy,
job applications, and messages to outsiders still require their existing approvals.
