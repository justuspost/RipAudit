# License decision

**Decision: MIT** (selected by the maintainer). The options considered are kept below for reference. Unraid Community Applications requires an OSI-approved license for the repository contents; MIT satisfies this.

| License | Summary | Trade-offs for RipAudit |
|---|---|---|
| MIT | Very permissive; keep the copyright notice | Simplest and most familiar; allows closed-source forks and commercial reuse without sharing changes; no explicit patent grant |
| Apache-2.0 | Permissive with an explicit patent grant and patent-retaliation clause; requires stating changes | Slightly more paperwork (NOTICE file); compatible with GPLv3 but not GPLv2 |
| GPL-3.0 | Copyleft: distributed modifications must be released under GPL-3.0 | Keeps forks open; distributing a modified container image obliges source availability; discourages some commercial reuse |
| AGPL-3.0 | GPL-3.0 plus source obligations when users interact with it over a network | Strongest protection against hosted closed forks; least attractive to contributors and companies |

Dependency compatibility: all Python runtime dependencies use MIT, BSD-3-Clause, Apache-2.0, PSF-2.0, or MPL-2.0 licenses (see [dependency-licenses.md](dependency-licenses.md)), which are compatible with any of the options above.

**Recommendation:** MIT or Apache-2.0 for a small self-hosted utility that you want people to adopt and contribute to. Choose Apache-2.0 if an explicit patent grant matters to you; choose GPL-3.0 if you want modified redistributions to stay open.
