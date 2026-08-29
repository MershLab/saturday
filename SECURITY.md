# Security policy

Saturday executes model-driven tools on your machine, so security reports
matter more here than in a typical repo.

## Supported versions

| Version | Supported |
|---|---|
| latest release | yes |
| older releases | best effort — upgrade first |

## Reporting a vulnerability

Email **security@saturlabs.dev** (or open a GitHub security advisory via
**Security → Advisories → Report a vulnerability**). Please do not open a
public issue for anything exploitable.

Include: affected version/commit, reproduction steps or a trajectory export,
and your assessment of impact. We aim to acknowledge within 48 hours.

## Scope notes

- The desktop app binds to loopback with a per-launch token and pins
  Host/Origin; reports about **remote** compromise of a default-instance
  setup are especially welcome.
- The `serve` surface requires a bearer token by design; the Telegram gateway
  refuses to start without a chat allowlist. Reports that bypass these
  fail-closed defaults are in scope.
- The file tools sandbox against workspace escape; the network tools guard
  against SSRF (loopback / private / link-local / cloud-metadata, including
  across redirects). Escape reports are in scope.
- Prompt-injection behavior in general is known and partially mitigated
  (detection layer, privileged-write blocking, project trust gate); novel
  bypasses of those specific controls are in scope.

## Hardening your own deployment

Run unattended workloads with `SATURDAY_SANDBOXED=1` inside a container, keep
`--allow` allowlists on the gateway, and leave destructive-action guardrails
on (they block when no approver exists, by design). See "Safety notes" in the
README.
