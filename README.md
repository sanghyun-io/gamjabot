# GamjaBot

GamjaBot is an experimental multi-agent application. Its source contains a Rust core host, a Python engine, and a React/Tauri interface intended to work together. Agents can connect to model providers and collaborate in separate conversations. This repository is a source-only preview, not a packaged release.

## Status and scope

- The public source snapshot has not yet been validated as a complete independent installation. There are no public binaries or installers in this repository. Platform support, clean installation, and provider integrations are unverified for this snapshot.
- A supported public build and run recipe is not yet available. The existing native packaging scripts depend on platform-specific runtimes, signing and resource contracts; they are not a general-purpose setup script. `backend/run.py` expects the core host to supply local runtime settings and is not a standalone launch instruction.
- This preview may change without compatibility guarantees. Use synthetic data and an isolated development environment while investigating it; do not expose an experimental local service to the network or use it for sensitive work.

## Source layout

- `core-host/`: Rust host and CLI.
- `backend/`: Python engine and tests.
- `frontend/`: React interface and Tauri host.
- `platform-helper/` and the credential broker directories: platform integration components.
- `scripts/`: development and packaging utilities, some of which assume the original native environment.

Frontend scripts and dependency declarations are in `frontend/package.json` and `frontend/package-lock.json`; Python requirements are in `backend/requirements.txt` and `backend/requirements-build.txt`; Rust crate manifests and locks are in their respective directories. These files are starting points for contributors, not proof that the full application can be built or run from this snapshot. See [CONTRIBUTING.md](CONTRIBUTING.md) for review and test expectations and [the publication scope](docs/publication-scope.md) for checks and known limitations.

## Frontend development

Use Node.js 24 and the committed npm lockfile in an isolated development checkout:

```sh
cd frontend
npm ci
npm run build
```

This builds the web assets. It does not package or start the complete desktop
application. The optional `npm run storybook` command serves component fixtures
on the loopback interface; review the known development dependency advisories in
[the publication scope](docs/publication-scope.md) before using it. Do not connect
component fixtures to personal accounts or production data.

## Data and external services

The intended desktop runtime stores application data locally; exact paths and credential storage depend on the platform and configuration. Conversations, attachments, browser profiles, diagnostics, and credentials can be sensitive. Features may contact the model provider or external service selected by the user, and provider use can incur charges under the provider's terms. Review the code, configuration and service permissions before enabling integrations. Do not publish local databases, diagnostic files, API keys, tokens or payment information in issues or pull requests.

See [SECURITY.md](SECURITY.md) for vulnerability reporting. Public issue reports are for non-sensitive bugs and feature requests.

## License

The project is licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for applicable notices. Third-party components may have their own licenses.

감자봇은 여러 에이전트의 협업을 위한 실험적인 소스 공개판입니다. 이 스냅샷의 독립 설치와 플랫폼 지원은 아직 검증되지 않았으며 설치 파일은 제공하지 않습니다.
