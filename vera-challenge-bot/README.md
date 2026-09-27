# Vera Challenge Bot — README

## Approach

The composer is **deterministic and rule-based, not an LLM call**. Every trigger
`kind` has a small template function in `composer.py` that pulls specific facts
straight out of the pushed context — a digest headline + source citation, a
performance delta, a competitor's name and distance, a customer's real open
slots and offer price — and assembles one message with one CTA. If a kind is
unrecognized, or a trigger arrives with an empty/placeholder payload (the
dataset generator emits some triggers as `{"placeholder": true, ...}` with no
real fact attached), the composer falls back to grounding on the merchant's or
customer's *own* real snapshot data (performance numbers, visit history)
rather than ever inventing a number, date, or headline. That fallback path
matters more than it might look — a meaningful fraction of the official 30
canonical test pairs land on these placeholder-payload triggers, and the first
version of this composer was printing literal `"None"` into the message body
until I caught it in testing.

`conversation_handlers.py` is a small keyword/regex state machine for
`/v1/reply`, covering the four things the brief explicitly calls out:
auto-reply detection (exits after a second consecutive canned reply, having
tried once to re-engage), explicit intent handoff (an affirmative reply routes
straight to "doing it now," never back to a qualifying question), graceful
exit on a hard decline, and a polite redirect on off-topic/hostile messages
without breaking character.

## Why rule-based instead of an LLM prompt

Three reasons, in order of how much they mattered:

1. **Determinism is a hard requirement** ("same input → same output"), and a
   rule-based composer gets that for free with zero temperature/sampling risk.
2. **Latency and reliability budget** — `/v1/tick` has a 30s timeout and can
   be called with up to 20 triggers at 10 req/s; a template lookup is
   sub-millisecond per action, so there's no risk of ever timing out or
   burning API quota mid-test.
3. **Anti-fabrication is scored explicitly** and is the easiest way to lose
   points. A template that only ever interpolates fields that are actually
   present in the context structurally cannot hallucinate a fact — it can
   only ever be *generic* when a field is missing, which is the safe failure
   mode.

The tradeoff is real: an LLM composer would likely read more naturally on
truly novel trigger kinds the judge injects post-submission, and would do a
better job of *dynamically* deciding tone shifts and connector phrasing than
my keyword-based Hindi-English code-mix heuristic does. If I had more time
I'd make this hybrid — rule-based extraction of the grounding facts (so the
anti-fabrication guarantee holds), then a single constrained LLM call with
temperature=0 to phrase the final sentence, with the rule-based version as a
timeout/failure fallback.

## What additional context would have helped most

- A `now` timestamp reliably available to the composer (not just to
  `/v1/tick`) so days-since-X triggers computed from raw dates rather than
  requiring the trigger's own `payload.days_since_X` field, which is exactly
  the field that's missing on placeholder triggers.
- Category-level `voice.connector_phrases` (a few canned Hindi-English
  transition phrases per category) instead of my having to hand-write three
  generic ones — the categories already carry `tone_examples`, extending
  that pattern to connectors would have removed my biggest hand-rolled guess.
- A `customer_aggregate.chronic_conditions` / molecule-level list surfaced
  on the merchant snapshot for pharmacies, so `chronic_refill_due` triggers
  without a full payload could still ground on real molecule data instead of
  the generic "your regular medicines" fallback.

## Files

| File | Purpose |
|---|---|
| `bot.py` | FastAPI server, the 5 required endpoints |
| `composer.py` | `compose()` — the pure deterministic composition function |
| `conversation_handlers.py` | `respond()` — multi-turn reply state machine |
| `generate_submission.py` | Produces `submission.jsonl` from the 30 canonical test pairs, calling `composer.compose()` directly (no HTTP) |
| `submission.jsonl` | The 30 required output lines |
| `requirements.txt`, `Dockerfile`, `Procfile` | Deploy artifacts |

## Running locally

```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Then push the base dataset and try a tick — see `examples/api-call-examples.md`
in the challenge pack, or the commands in the PR description / commit history
of this repo for the exact `curl`/Python snippets used during development.

## Known limitations

- Language handling is a binary hi-en-mix-or-not heuristic keyed off
  `languages`/`language_pref` containing `"hi"` — it doesn't detect a
  mid-conversation code-switch (challenge-brief.md §12.4, called out as an
  open/extra-credit challenge).
- The reply state machine is keyword-based; it will miss intent phrased in
  ways not in its pattern list. It's deliberately biased toward *not*
  mis-firing "auto-reply detected" or "hard decline" on an ambiguous message,
  since those two are the highest-cost false positives.
- No LLM call means no adaptive phrasing for a genuinely novel trigger kind
  the judge injects — the generic fallback will always be grammatically
  clean and non-fabricating, but it will read more like a template on kinds
  outside the ones enumerated in the dataset.
