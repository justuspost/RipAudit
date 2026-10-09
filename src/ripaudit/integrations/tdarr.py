"""Tdarr decode-health adapter — deliberately deferred.

Tdarr remains responsible for full-file decode checks. As of this release the
only Tdarr endpoint RipAudit's maintainers have verified in official
documentation is the server status endpoint (``/api/v2/status``). The
documentation does not describe a stable, supported interface for reading
per-file health-check results with distinct success / failure / cancelled /
configuration-error meanings.

Until such an interface is verified, RipAudit reports every file's decode-health
dimension as ``not_integrated`` and never reads Tdarr's internal database.
"""

STATUS = "not_integrated"
EXPLANATION = ("Decode health is checked by Tdarr and is not imported into RipAudit yet; "
               "a supported per-file results interface has not been verified.")
