# Sharing Hermes and its skills with a team

Use when several people want the same skill library, or when the question is
"can my colleagues use this agent without stepping on each other". Pick the
topology first, then the distribution mechanism.

## Topology decision

| Option | Isolation | Cost | Use when |
|---|---|---|---|
| One host, one profile, shared login | None — shared files, processes, browser tabs | cheapest | never for real work |
| One host, one profile per person | Config/skills/memory/sessions separated; **filesystem, terminal and browsers still shared** | one machine | light Q&A, no heavy tool runs |
| One host per person (VM or container) | Full | one machine per person | anything involving browsers, builds, or audited evidence |

Size the host against the *tool mix*, not the chat: a browser-driven run costs
~0.5–1.5 GB for Chromium (double it when a headed Chrome plus Xvfb is needed to
screenshot DevTools), LibreOffice another ~0.5 GB. 4 vCPU / 7 GB RAM holds one
heavy agent comfortably and starts thrashing at two or three. A practical rule
of thumb is 8–16 GB RAM per concurrent browser-using agent.

## Profile distributions — the supported way to share skills

A profile can be published as a git repository ("distribution"): the unit of
sharing is the whole agent recipe, not a folder of skills copied by hand.

```
fstek-agent/
├── distribution.yaml   # name, version, required env
├── SOUL.md             # persona/role rules
├── config.yaml         # model, approvals, toolsets — no secrets
├── skills/             # the class-level skills that carry the procedure
└── .gitignore          # .env, auth.json, memories/, sessions/, state.db
```

- Author side: `git push` + version tag.
- Consumer side: `hermes profile install <git-url> --alias <name>`, then
  `hermes profile update` to pull improvements.
- **`profile install` does not seed bundled skills.** `install_distribution()`
  only bootstraps dirs and copies the payload; `sync_skills()` runs at profile
  *create*, at `hermes update`, and on every CLI/TUI/gateway start. So a
  distribution can ship just its authored skills — the rest of the catalog
  arrives at the consumer's first launch. Vendoring all N bundled skills instead
  pins nothing: `skills/.*` (including `.bundled_manifest`) is gitignored, so
  sync treats each shipped copy as untracked — byte-identical to upstream gets
  baselined and then overwritten by the next upstream change, differing copies
  are kept forever with a "you already have a local skill by this name" warning
  (`hermes skills reset <name>` to align). Never publish the manifest to force
  pinning: it makes sync see copy==origin and silently replace the author's own
  edits with stock versions.
- **Anchor `.gitignore` runtime paths with a leading `/`.** The documented
  template lists `hermes-agent/` unanchored, which also matches the
  `skills/**/hermes-agent/` skill directory and silently drops it from the repo.
  Verify with `git ls-files 'skills/**/SKILL.md' | wc -l` against the intended
  count before committing, and `comm -13 <(git ls-files | sort) <(find . -type f
  -not -path './.git/*' | sed 's|^./||' | sort)` for anything left untracked.
- Secrets, memories and session history are **not** transferred by design: each
  person keeps their own keys and their own agent memory, while skills, SOUL and
  procedures are common. Say this explicitly — it is the property that makes the
  model safe for a team.
- `hermes profile export` produces a one-off `.tar.gz` for handing an agent over
  without git; it has no update path, so treat it as a snapshot, not a channel.
- Change flow: skill edits arrive as pull requests against the same repository,
  with a version bump and a changelog line. One person curates; everyone else
  consumes. Otherwise two divergent copies of the same skill appear within weeks.

## Many people on one host (when that is the chosen topology)

```bash
hermes profile create colleague1 --clone-from fstek-agent
hermes profile create colleague2 --clone-from fstek-agent
```

- One multiplexed gateway (default) serves every profile; it keeps state
  isolated per profile, but each profile still needs its own bot credentials.
- Restrict who may talk to which profile with the platform allowlist rather than
  hoping nobody finds the bot.
- Route the terminal into a container per profile (Docker backend) when people
  share a host — that is what separates their files and processes. Profiles
  alone do **not**: one `~`, one shell, one browser profile.
- Give each person a working directory (`~/work/<profile>/`) so artifacts and
  screenshots cannot overwrite each other.
- Serialise heavy runs: one browser-driven session at a time on a 4-core host,
  even when everything else is isolated.

## The trap that has nothing to do with Hermes

When the work targets a shared mutable environment (test stand, staging
cluster), parallel testing corrupts the *results*, not just the tooling: audit
log checks see other people's events, account-management steps change the
accounts a colleague is testing, lockout counters trip under someone else's
attempts. Evidence collected that way is not reproducible and will not survive
review. Either give each tester their own instance or put the runs in a queue
(kanban, or simply a schedule) — isolation of agents does not buy isolation of
the system under test.
