# Choosing a Hermes surface / reaching the backend from another machine

Use when the user asks how to talk to Hermes from somewhere else, wants a
richer chat window than the one they have, or cannot connect a client to a
remote backend. Covers surface selection and the Desktop → remote dashboard
connection.

## 1. Pick the surface by what the user is missing, not by what they use now

| Surface | Start | Gets you | Does NOT give you |
|---|---|---|---|
| Web dashboard chat | `hermes dashboard --host 0.0.0.0 --port 9119` | admin panel (channels, MCP, webhooks, memory, profiles) + chat in the browser from any machine | **it is a PTY, not a chat UI**: the page runs `hermes --tui` over a PTY socket and renders it with xterm.js. No file attachments, Enter sends (no soft newline), mouse selection is captured by the terminal, no prompt queue/preview. |
| Desktop app | `hermes desktop` (alias `hermes gui`) | real React chat composer: multi-line, drag-and-drop / pasted attachments, selection + copy, prompt history and queue, artifact preview, session list, ⌘K palette | needs an app installed on the client machine; no Linux release bundles (build locally) |
| Ink TUI / CLI | `hermes --tui`, `hermes` | full terminal experience, works over plain SSH | no attachments beyond `@file:` refs; Windows Terminal/SSH keeps normal select+paste |
| IDE (ACP) | `hermes acp` | chat inside VS Code / Zed / JetBrains, editor context, attachments | setup per editor |
| Messaging channel | `hermes gateway` + Telegram/Discord/Slack/… | attachments and multi-line from a phone, notifications, works from anywhere | poor for browsing files on the host; separate long-running process from the dashboard |

Rule: when a user complains that the browser chat "behaves like a console",
do not debug it — that is what it is. Offer Desktop, and explain the trade-off.

Client install packages: macOS `.dmg` and Windows (`Microsoft Store` MSIX —
Win 11 22H2+, or `Hermes-Setup.exe`) from https://hermes-agent.nousresearch.com/.
There is **no remote-only client download**: install the normal package and
point it at the remote backend instead of its bundled local runtime.

## 2. Connecting Desktop to a remote backend (VM / homelab / VPS)

The remote backend **is** the `hermes dashboard` (or headless `hermes serve`)
process on that host — Desktop attaches to it and never starts it.

1. Bind it reachable: `hermes dashboard --no-open --host 0.0.0.0 --port 9119`.
2. Make sure an auth provider exists. A non-loopback bind fails closed without
   one. Password auth lives in `dashboard.basic_auth` (username +
   `password_hash` + `secret`) or as `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` /
   `_PASSWORD` / `_SECRET` in `.env`; OAuth via `hermes dashboard register`.
3. Verify the gate before touching the client:
   `curl -s http://HOST:9119/api/status | jq '.auth_required, .auth_providers, .auth_flows'`
   → `true`, `["basic"]`, `["cookie","native_pkce"]`.
4. In Desktop: **Settings → Gateways → Remote gateway** → URL
   (`http://HOST:9119`) → **Sign in**. Password-only gateways get the embedded
   login page for the dashboard's own credentials; the resulting session is
   reused for the chat socket and the secret goes to the OS keyring. There is
   no token to copy.
5. Pick the profile in the sidebar — same `state.db`, so sessions, skills and
   memory are the ones the CLI/TUI wrote.

## 3. Pitfalls that break the remote connection

- **Bind host must equal the URL the client uses.** A loopback bind rejects
  remote peers at the socket layer no matter what credentials are supplied,
  and the DNS-rebinding guard rejects a Host header that does not match. Use
  the same address in `--host` and in the client URL.
- **A manually started dashboard dies with its shell.** If the process parent
  is an interactive `bash`/terminal (check `ps -o pid,ppid,cmd` and
  `hermes serve --status`), closing that window kills the backend and the
  client loses chat mid-turn. Put it under systemd:
  `ExecStart=… -m hermes_cli.main dashboard --host 0.0.0.0 --port 9119 --no-open`,
  `EnvironmentFile=%h/.hermes/.env`, `Restart=always`,
  `RestartPreventExitStatus=78` (78 = deliberate "host already served by PID …"
  refusal — parking there beats a restart loop). One backend serves a whole
  host; add `--isolated` for a profile-dedicated server.
- **WebSocket close codes are the diagnosis, not the symptom:** 4401 = the WS
  ticket did not authenticate, 4403 = request guard rejected Host/peer. Grab
  `desktop.log` (Settings → Gateways → Open logs) plus the backend log from the
  same retry window.
- **Client and backend update on separate clocks.** A newer app against an
  older backend warns about the backend; `hermes update` fixes it but restarts
  the gateway, so never suggest it mid-work. The skew also runs the other way —
  newer backend, older renderer — and silently disables everything that needs
  the renderer to call *back*: `clarify` cards submit into nothing, an in-chat
  widget's `window.hermes.send` / `data-hermes-send` never arrives, and the
  preview bridge answers `desktop_preview read` with "no page is loaded" and
  `drive_preview` with "No Hermes Desktop window answered". Probe the channel
  **before** designing anything that must return an answer:
  `curl -s --max-time 3 http://127.0.0.1:9222/json/version` (packaged builds
  close CDP), then `desktop_preview action=read` and `drive_preview
  action=elements`. If no channel answers, collect the answers in the message
  instead: a numbered option list the user can reply to as one line (`1-1,
  2-1`) beats a form that cannot report back. When a question card returns with
  no answers twice, stop re-sending it and ask in text; say once that updating
  the app restores cards and widgets, and do not retry them in the same session.
- **Paths resolve where the backend is.** Artifacts and relative paths on a
  remote connection are opened on the gateway host, not the client machine;
  use the file tree rather than "open containing folder".

## 4. Answering capability questions against the installed build

The installed git checkout carries the full, version-matched docs tree:
`~/.hermes/hermes-agent/website/docs/**`. Grep it before answering, and prefer
it over the website when the version matters (`hermes --version`):

```bash
cd ~/.hermes/hermes-agent && grep -rln "remote gateway" website/docs/
grep -rn "Connecting Hermes Desktop to a remote backend" -A 40 \
  website/docs/user-guide/features/web-dashboard.md
hermes <command> --help        # flags of the installed build
```

To confirm a UI path actually exists in this build (rather than trusting
memory of the docs), grep the client source: `apps/desktop/src/app/settings/`
for a settings page, `apps/desktop/electron/*.ts` for the flow behind it, and
`hermes_cli/web_server*.py` / `web/` for dashboard routes.
