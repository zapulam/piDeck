# Security

## Reporting a problem

Please report vulnerabilities privately through GitHub: **Security → Report a vulnerability** on this repository. Don't open a public issue for something exploitable. This is a hobby project, so there is no SLA, but reports are read and taken seriously.

## Threat model

piDeck is meant for a trusted home or office network. It assumes you control the Pi and the computers that talk to it, and it protects against other devices on the same network and against web pages opened in a browser on that network. It does not protect against someone who can already read your `.env` or sniff your network traffic (the hook speaks plain HTTP; use a VPN or tunnel on untrusted networks).

## What is in place

- Access control: loopback (the Pi's own kiosk, reached as `localhost`) or a shared bearer token, on the websocket and on every endpoint that changes anything. Tokens are compared in constant time.
- Origin checks on websockets and mutating requests, and a Host check on the loopback exemption (blocks cross-site requests and DNS rebinding).
- Input limits on agent events, and a cap on sessions and connected screens.
- Security headers, including a strict Content-Security-Policy.
- Secrets live in `.env` (gitignored, mode 600 when piDeck creates it) and `~/.config/pideck/hook.json` (mode 600). Slack messages are held in memory only.
- A sandboxed systemd unit, `pip-audit` in CI, and Dependabot.
