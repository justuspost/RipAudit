# Security and deployment

## Design

| Control | Implementation |
|---|---|
| Least privilege | Runs as UID 99 / GID 100 by default; no root, privileged mode, Docker socket, capabilities, or GPU required. Works with `--read-only`, `--cap-drop ALL`, and `no-new-privileges`. |
| Read-only media | Media is mounted `:ro`. RipAudit has no code path that writes, renames, or deletes media. |
| Path confinement | Paths are resolved with `realpath` and must stay inside a configured media root; symlinks pointing outside a root are skipped; Plex path mappings reject `..`. |
| Subprocess safety | FFprobe runs from an argument list (no shell) with a `file:` prefix, a timeout, closed stdin, and bounded concurrency. |
| Authentication | First-run account creation requires a one-time token stored in appdata, proving local control of the server. No default password exists. |
| Passwords | scrypt (N=2^15, r=8, p=1) with per-user random salt; minimum length 12. |
| Sessions | Signed, HTTP-only, `SameSite=Strict` cookies with a 12-hour lifetime; optional `Secure` flag for HTTPS. The signing key is generated per installation. |
| CSRF | Per-session token required on every state-changing form. |
| Brute force | Five failed sign-ins per client address in 15 minutes triggers a temporary lockout. |
| Secrets | Masked in the UI, never returned by exports or diagnostics, redacted from all logs and stored errors, and `settings.json` is written atomically with mode 0600. Environment-supplied secrets are never written to disk. |
| Browser hardening | CSP (`default-src 'self'`, no inline script), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin`. Templates auto-escape. |
| Exports | CSV cells beginning with `=`, `+`, `-`, or `@` are neutralized against spreadsheet formula injection. |
| Supply chain | Pinned Python dependencies, SHA-pinned GitHub Actions, Dependabot, `pip-audit`, and Trivy image scanning in CI. |

## Outbound connections

An administrator configures the Plex, ntfy, and webhook URLs, and RipAudit connects to them from inside your network. This is intentional (Plex usually lives on the LAN), but it means anyone with an administrator account can make RipAudit send requests to internal addresses. URLs must use http or https, must not embed credentials or tokens, and link-local addresses (such as cloud metadata endpoints) are rejected.

## Deployment guidance

- Keep RipAudit on your LAN. Do not forward its port to the internet.
- For remote access, use a VPN (for example WireGuard or Tailscale) or a reverse proxy that terminates TLS, then enable **Secure cookies only** and restart.
- Example Nginx location:

  ```nginx
  location / {
      proxy_pass http://192.168.1.10:8080;
      proxy_set_header Host $host;
  }
  ```

- RipAudit does not trust `X-Forwarded-For`; the sign-in lockout counts by the proxy's address when behind a proxy.

## Threat notes

| Threat | Mitigation | Residual risk |
|---|---|---|
| Unauthenticated LAN user takes over first run | Setup token in appdata | Anyone with appdata access can read it (by design) |
| Malicious file names | No shell; `file:` prefix; auto-escaped HTML | — |
| Symlink or `..` escape from media roots | realpath confinement | — |
| Token leakage via logs | Header-based Plex auth; global log redaction; HTTP library logging suppressed | Secrets shorter than 4 characters are not substring-redacted |
| Unmounted share interpreted as mass deletion | Unavailable-root detection and two-scan confirmation | — |
| Stolen session cookie | HTTP-only, SameSite=Strict, 12-hour lifetime | No server-side session revocation list yet |
| Single administrator account | — | No multi-user roles in the MVP |

Report vulnerabilities as described in [SECURITY.md](../SECURITY.md).
