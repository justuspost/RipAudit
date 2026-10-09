# Security policy

## Supported versions

RipAudit is pre-release. Security fixes are made on `main` until the first tagged release.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's **Report a vulnerability** form on the repository's Security tab. Do not open a public issue. Include the version or commit, your deployment (Unraid, Compose, or other), steps to reproduce, and the impact. Never include real tokens; describe them instead.

You can expect an acknowledgement within 7 days. Please allow time for a fix before public disclosure.

## Scope

In scope: the RipAudit application, container image, and Unraid template. Out of scope: Plex, TMDB, Tdarr, ntfy, Unraid, and Docker themselves, and deployments that expose the WebUI directly to the internet against the [deployment guidance](docs/security.md).
