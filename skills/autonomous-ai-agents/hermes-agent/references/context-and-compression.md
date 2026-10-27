# Context size, compression and session hygiene

How Hermes decides when the conversation is too big, what survives a
compaction, which knob to turn, and how to read the real size of a session off
disk. Load this before advising a user to "just start a new chat" or before
tuning `compression.*`.

## The knobs and the effective trigger

Everything lives in the `compression:` section of `config.yaml`. Change it only
through the CLI (`hermes config set compression.<key> <value>`) — a stray indent
corrupts the file and can break a live gateway.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | true | master switch |
| `threshold` | 0.50 | fraction of the window that triggers compaction |
| `threshold_tokens` | unset | absolute token cap; the **lower** of cap and ratio wins |
| `target_ratio` | 0.20 | share of the effective threshold kept verbatim as the tail |
| `protect_first_n` / `protect_last_n` | 3 / 20 | head messages and trailing messages that are never summarised |
| `model` | auto | auxiliary model for summaries; `(auto)` = cheap lane |
| `micro_compact` | off | summarise a little each turn instead of one big pass |

`hermes config show` prints the **resolved** block — always confirm there
after a change; it states whether the cap or the ratio is smaller:

```
hermes config set compression.threshold_tokens 250000
hermes config show   # → "◆ Context Compression ... Token cap: 250,000 tokens (takes lower of ratio vs absolute)"
```

Threshold math (source: `agent/context_compressor.py`, `tail_token_budget`):

```
effective_trigger  = min(threshold * context_length, threshold_tokens if set)
tail_kept_verbatim = effective_trigger * target_ratio
max_summary_tokens = min(context_length * 0.05, internal ceiling)
```

Consequence that surprises people: **setting an absolute cap also shrinks the
verbatim tail**. A 1M-token model with `threshold: 0.50` would keep ~105K tokens
of recent dialogue; adding `threshold_tokens: 250000` drops the tail to ~50K. If
the user wants to compress earlier but keep more recent detail, raise
`target_ratio` (0.30 → 75K in that example) in the same request.

Small context windows get the ratio floored upward so tiny models do not
compact on every turn — do not assume `threshold: 0.50` means half the window on
a small model.

## Does a change apply to a running chat?

TUI and Desktop apply `compression.*` (and `model.context_length`) **at the start
of every turn**, in place — `tui_gateway/session_compression.py`,
`_apply_live_compression_config`, guarded by a config signature so it is a no-op
when nothing changed. No restart, no rebuild: say "applies from your next
message". Messaging gateways rebuild the agent on those keys instead, so there a
`gateway restart` may be needed. Verify in a live session by re-running
`hermes config show`, not by assuming.

## What a compaction keeps and loses

- User messages are kept verbatim; the assistant's middle of the dialogue
  (tool output, file reads, reasoning) is replaced by a summary block.
- Survives well: standing instructions, stated preferences, the shape of the
  task. Lost reliably: exact hashes, line numbers, part names, id lists,
  byte counts, specific file paths mentioned only once.
- Therefore the durable answer is **artifacts on disk**: whatever must be quoted
  later goes into a file (map/JSON/Markdown summary) the same turn it is learned,
  and is re-read after compaction instead of recalled from the transcript.
- Tell the user what to expect: a one-call pause, then a summary row in history.
  Configure a progress notice if they want it visible.

## Measuring a session (grounded, not guessed)

Model window — provider metadata cache, no network needed:

```python
json.load(open("~/.hermes/models_dev_cache.json"))[provider]["models"][model]["limit"]["context"]
```

Session accounting — `~/.hermes/state.db`, table `sessions`:
`message_count`, `tool_call_count`, `api_call_count`, `input_tokens`,
`output_tokens`, `cache_read_tokens`, `estimated_cost_usd`. Note that
`input_tokens` is **cumulative across API calls**, not the current prompt size —
do not present it as "how full the context is".

Approximating current context when heuristics are all you have: the `messages`
table has a `token_count` column that is commonly left at 0, so sum
`LENGTH(content)` for the session and divide by ~3.0–3.5 chars/token
(mixed Russian/English/code), then add the system prompt plus tool schemas
(roughly 30–40K tokens on a full toolset). Label it an estimate.

## When a new session is actually warranted

The token ceiling is almost never the real reason — quality degrades much
earlier than a 500K-token threshold fires. The trigger is a **task boundary**:

- one session per logical block (parse the document / run one test section /
  assemble the report), not per calendar day;
- start the next session with a short brief — artifact paths, host, which
  section — and let the skill carry the procedure, not the transcript;
- keep state in files, so a fresh session loses nothing;
- mid-session relief: `/compress here N` (keep the last N exchanges verbatim),
  `/compress <topic>`, `/new <name>`, `/resume`, `/status` for the live numbers.

## micro_compact trade-off

`micro_compact: true` + `micro_compact_every_n_turns: 5` keeps the context
flat without one long pause per compaction, at two costs: every pass breaks the
prompt cache, and older turns are summarised sooner. Worth it only for
sessions that habitually run past ~100–150K tokens; with cache reads priced at
a fraction of input the money is negligible, the fidelity loss is not.

## Cheaper than compacting

Most context bloat is tool output, not conversation. Keep large reads out of the
context in the first place: dump to a file and read slices, filter big JSON in
`execute_code` before printing, pass screenshots as paths rather than base64,
and quote file paths instead of pasting contents. Compaction is the fallback,
not the strategy.
