# Security policy

## Reporting a vulnerability

If you believe you have found a security vulnerability in claudeStruct, please
**do not** file a public GitHub issue. Instead, report it privately so we can
fix the issue before it is disclosed.

- Preferred channel: [GitHub Security Advisories](https://github.com/tonyandclaw/claudeStruct/security/advisories/new) (private).
- Fallback: email the maintainers (address listed on the repo's GitHub profile).

When reporting, please include:

- A description of the issue and its impact.
- Steps to reproduce, ideally with a minimal proof-of-concept.
- Affected versions or commit SHAs.
- Any suggested mitigation, if you have one.

We aim to acknowledge within **3 business days** and provide an initial
assessment within **10 business days**. Coordinated disclosure timing is
negotiated per report.

## Scope

In scope:

- The `claudestruct` (Python), `claw-squad` (TypeScript), and `claw-sandbox`
  (Go) source trees in this repository.
- Default configuration files shipped under `src/claudestruct/integrations/`
  and `claw-squad/`.
- Container images published to `ghcr.io/tonyandclaw/claudestruct`.

Out of scope:

- Vulnerabilities in upstream dependencies (report those upstream; we will
  ship a pinned-version mitigation once the upstream fix is available).
- Issues that require an attacker to already have local code execution as the
  same user running claudeStruct (e.g. reading `.env` files, modifying the
  user's shell rc).
- The `claw-sandbox` binary's `--no-network` flag on platforms that lack
  CAP_SYS_ADMIN — this is documented as a defense-in-depth control, not a
  guarantee, in the sandbox's per-run isolation report.

## Handling secrets in logs

claudeStruct never logs `ANTHROPIC_API_KEY`, `SLACK_*_TOKEN`, or
`GITHUB_TOKEN` values. If you observe a leak in a log file, treat it as a
security report.

## Supported versions

We patch security issues on the latest minor release line. Older lines may
receive backports on a best-effort basis.

## Threat model

For the technical breakdown of trust boundaries, STRIDE per surface,
tenant-isolation rules, and accepted risks, see
[`docs/threat-model.md`](docs/threat-model.md). A PR that violates a
documented invariant in that doc is a security defect; a PR that lands
in a stated "out of scope" lane is not (but might still be a useful
hardening).
