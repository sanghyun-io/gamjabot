# Source preview scope and verification

This repository is an experimental source preview of GamjaBot, prepared for a
separate repository with fresh history. The publication was prepared to exclude
historical issue discussions, release assets, installed application databases,
credentials, personal work journals and payment records. No personal credentials
or payment records were identified in the scoped checks; those checks do not
guarantee absence. Historical private development material is outside the
publication scope.

The application is not a verified independent installation or a production-ready
release. No installer, embedded browser, executable, native signing identity or
personal certificate is distributed here. Native packaging scripts remain
platform-specific and may require developer-owned signing and runtime setup.
The desktop application's end-to-end flows and macOS/Windows builds have not
been verified for this public snapshot.

## Checks performed for this preview

Validation runs on an isolated Linux ARM64 / Python 3.12 / Node 24 environment.
The frontend TypeScript/Vite build and selected authentication, localization and
UI tests have been checked. Python JWT verification and engine authentication
use a synthetic test environment. These checks do not establish full platform,
provider, native credential store or installer compatibility. The private
release's cross-language wire receipt is not distributed; the public Python
transport tests use explicitly synthetic vectors instead.

An initial broad frontend test run was stopped after repeated failures so that
compiler and synthetic-fixture prerequisites could be corrected. A later Chat
sample still failed while waiting for a mocked websocket handler. The complete
frontend suite has not passed, and no full-suite success is claimed. This remains
a development limitation to investigate before a supported application release.

## Known development dependency advisories

The publication audit reports four moderate findings in the Storybook development
chain: Storybook / react-docgen plugin / glob 13.x / minimatch 10.x. No high or
critical npm finding was reported at that audit time. The resolved Python audit
reported no known vulnerabilities at that time; this is not a continuing guarantee.

- glob: [GHSA-3g3m-2fjr-6pw4](https://github.com/advisories/GHSA-3g3m-2fjr-6pw4)
- minimatch: [GHSA-g3hv-75wg-m249](https://github.com/advisories/GHSA-g3hv-75wg-m249)

These findings concern developer tooling with attacker-controlled glob patterns
or directories under specific options; applicability to application runtime was
not established. Use trusted source/configuration in isolated development
environments. The current plugin's dependency range does not accept the patched
major versions. Upgrade the compatible upstream chain and recheck Storybook
before treating these advisories as resolved; they are not suppressed by the audit.

## Artwork and licensing

The favicon and native conversation mark were authored for this source preview.
Native icon files are reproducible with `python3 scripts/generate_icons.py`, which
uses only Python's standard library. Unverified historical image assets are not
included. The project uses Apache-2.0; dependency licenses remain separate as
explained in THIRD_PARTY_NOTICES.md.
