# Licensing and minimum CI

Project-authored code and documentation in this repository are covered by the
root [LICENSE](../LICENSE), including files without an individual source header.
[NOTICE](../NOTICE) identifies the project attribution. Existing third-party
notices remain authoritative for their respective material. Dependencies are
not relicensed or bundled by this change. Protocol names, external contracts,
and quoted or independently licensed material are not claimed as project-owned.
KOL-v1 is a custom license, not an OSI approval or an SPDX-listed license.

## Minimum CI scope

The always-triggered **Minimum CI** job runs on pull requests, main pushes,
merge groups, and manual dispatch. It checks tracked Python/JSON syntax, local
documentation links, and 82 or more offline regression tests. A missing,
reduced, failed, or skipped selected suite fails the job. Existing specialized
workflows remain in place.

This is a baseline, not full test coverage, a secret scan, or a production
readiness claim. The selected suites validate the Codex approval bridge and approval portal with fixtures. Live authentication, Codex, services, and hardware are not exercised.
Adding this workflow does not configure required branch checks or change bypass
permissions. Administrators must select its check separately if enforcement is
wanted. No runtime or service configuration is changed.
