# Security

## Reporting a problem

Report vulnerabilities privately through GitHub's **Security → Report a vulnerability** facility for this repository. Avoid public issues containing an exploit, credentials, or private account data.

## Threat model

piDeck displays private project metadata, playback information, and Slack messages. Its security boundary covers untrusted network peers, other local OS users without access to its private files, malicious browser origins, and event-only agent integrations. It does not protect against a compromised owner account, administrator/root access, a compromised OS/browser/provider, or someone looking at the physical display. Hiding a page is a layout preference, not an access-control boundary: authorized displays receive the configured source state.

## Authentication and transport

- Servers bind to loopback by default. Direct network listeners require TLS. Remote hooks and viewers require HTTPS with normal certificate validation; hook and provider requests refuse redirects.
- `PIDECK_TOKEN` is a display administrator credential. `PIDECK_HOOK_TOKEN` only posts agent events; it cannot read Slack snapshots, create display sessions, change settings, or control playback. Do not configure the same value for both credentials.
- Real localhost clients authenticate too. A proxy cannot inherit access merely by using a loopback connection or Host header. Explicit trusted-proxy IPs are required to accept forwarded HTTPS/client metadata; wildcard proxy trust is rejected.
- Browser bootstrap credentials use fragments, never query parameters. They are exchanged for expiring HttpOnly, SameSite=Strict cookies, Secure on HTTPS, and a separate origin-confined session proof. Cookies alone cannot authenticate another localhost service that shares the hostname but uses a different port. Proofs stay in tab session storage, never persistent local storage. WebSockets carry the proof as a subprotocol and periodically recheck revoked/expired sessions. The server selects only the non-secret `pideck-v1` subprotocol.
- Browser mutations and WebSockets enforce the expected origin including its scheme. Disconnect revokes the current session; active displays rotate their sessions periodically. Server restarts invalidate browser sessions.
- The first secured startup rotates legacy display credentials. Reconnect displays and install hooks using the new event-only token. Legacy `?token=` links and remote HTTP connections are refused.

## Secrets and privacy

- Frozen Windows/macOS applications use Windows Credential Manager/macOS Keychain. The `.env` import file is permission-restricted before reading, imported secrets are removed after successful storage, and account secrets are never bundled in installers. Configuration remains separate from application files.
- Source and Pi deployments retain private `.env` files. Atomic writes restrict temporary files before secret bytes are written. Unix files use mode 600; Windows private files receive an explicit owner/SYSTEM ACL. Existing environment files are tightened too. Hook settings contain only the limited event credential.
- Claude notification and assistant text require explicit message opt-in. Full working paths require separate path opt-in. Local provider credentials and transcript text are not included in usage events. Account usage collection reads local sign-ins only on the agent's computer; the Pi service disables local discovery.
- Application/server log filters redact URL tokens, bootstrap fragments, session subprotocol proofs, and bearer strings. HTTP API responses are not cached. Slack's recent feed remains in memory rather than a message database.

## Availability and browser protections

API authentication precedes JSON parsing. API bodies are limited to 16 KiB with a five-second read deadline. Attempts are limited to 600 per peer per minute with bounded bookkeeping; session and display counts are bounded. WebSocket input is limited, and display sends have independent timeouts. Frontend alert/history queues are bounded. External text is escaped and the UI uses a restrictive script/content policy, framing denial, MIME protection, and a no-referrer policy.

The Pi installer creates a non-login `pideck` account, installs root-owned code under `/opt/pideck`, and keeps private writable state under `/var/lib/pideck`. The systemd service denies privilege escalation and protects the filesystem, home directories, devices, and kernel settings. It never runs as root.

## Builds and remaining boundaries

CI runs regression checks, dependency audits, and native bundle smoke tests. Desktop/Pi dependency locks include hashes; desktop artifacts include dependency versions, source commit, interpreter version, audit results, and SHA-256 checksums. OS runtimes, provider services, and browser engines need their own updates and are not covered by Python dependency audits.

Initial desktop installers are unsigned test builds because publisher certificates are not configured. They may trigger OS warnings; checksums provide integrity comparison, not publisher authentication. Optional Windows signing and macOS signing/notarization require externally managed credentials. Never place signing keys, passwords, or account credentials in the repository.
