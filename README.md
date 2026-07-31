# Codex cost preflight

This user-level `UserPromptSubmit` hook estimates a prompt before Codex sends it to a model.

## What happens

1. Submit a prompt normally.
2. The hook blocks that first submission before model dispatch and displays:
   - a draft-token range;
   - projected credit ranges for Sol, Terra, and Luna;
   - the selected model and a heuristic recommendation.
3. Submit the unchanged prompt again within 90 seconds to continue. You may change between Sol, Terra, and Luna because the preview already showed all three ranges.

The approval is one-shot. The hook stores only a SHA-256-derived filename plus timing and status metadata. It does not store the prompt.

## Estimate model

The rate card is dated July 31, 2026 and uses [standard Codex credits](https://learn.chatgpt.com/docs/pricing.md) per one million tokens:

| Model | Input | Cached input | Output |
| --- | ---: | ---: | ---: |
| Sol | 125 | 12.5 | 750 |
| Terra | 50 | 5 | 300 |
| Luna | 5 | 0.5 | 30 |

The hook uses the latest `last_token_usage` event in the current transcript when available. It otherwise applies a conservative context range. Draft tokenization, future output, tool loops, retries, subagents, cache behavior, Fast mode, reasoning effort, and attachments remain estimates.

These are Codex credit estimates, not dollar estimates. API-key sessions use API pricing instead.

## Installed locations

- Hook script: `$HOME/.codex/hooks/cost_preflight.py`
- Hook definition: `$HOME/.codex/hooks.json`
- Short-lived hash state: `$HOME/.codex/cost-preflight/state`

Codex may ask you to review and trust the new command hook the first time it discovers it. Review the command and approve it through the normal hook trust UI. Do not bypass hook trust.

## Configuration

The hook accepts these optional environment variables:

- `CODEX_COST_PREFLIGHT_TTL_SECONDS`: confirmation window, default `90`.
- `CODEX_COST_PREFLIGHT_MIN_CONFIRM_DELAY_SECONDS`: double-click protection, default `1`.
- `CODEX_COST_PREFLIGHT_STATE_DIR`: state directory override.

## Test

From this directory:

```sh
python3 -m unittest -v test_cost_preflight.py
```

## Disable or remove

For a temporary shutoff, enter `/hooks` in Codex and disable the cost preflight hook. Return to `/hooks` to turn it back on.

To remove it manually, delete only the `UserPromptSubmit` entry for this command from `$HOME/.codex/hooks.json`. Preserve any other hooks that may have been added later. The state directory can then be removed; it contains only short-lived hashes and timestamps.
