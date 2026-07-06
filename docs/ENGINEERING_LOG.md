# Engineering Log — Evaluation-Driven Debugging

> This is the honest story of building RepoPilot's agent loop. The headline is
> not a success rate — it is a method. Roughly 80% of the effort went into
> figuring out **what to fix**, not fixing it, and most of the interesting
> findings are *negative* results where measurement overturned an assumption.

RepoPilot turns a GitHub Issue into a fix PR: read the issue, locate code,
plan a patch, apply it in an isolated clone, run the tests, and reflect on
failures before retrying. Getting the loop to *work* is easy to demo on one
lucky bug. Getting an honest read on *why it fails* is the hard part, and that
is what this log is about.

## The evaluation subsystem came first

Before optimizing anything, we built `eval/failure_taxonomy.py`: it reads the
per-attempt `fix_attempts` already recorded in each eval run and classifies the
decisive (last-attempt) failure into a fixed set of categories —
`wrong_file_path`, `invalid_diff`, `search_not_found`, `test_failed`, `infra`,
`budget`, `empty_patch`. It also diffs two runs so we can see the category
delta between, say, model A and model B.

The payoff was immediate and humbling: **the taxonomy repeatedly contradicted
our hand-rolled guesses about the #1 bottleneck.** Every time we thought we
knew the dominant failure mode from eyeballing logs, the automated count said
something else. That is the entire reason the subsystem exists.

## Peeling failure attribution across four layers

The clearest example. Our headline failure mode changed *three times* under
forensic, per-sample debugging — each apparent cause turned out to be a phantom
masking the real one:

1. **"invalid_diff 5/10"** — a *routing bug*. The hallucination/dead-patch
   gates cleared `patch_edits` and set `current_phase`, but the router read
   `frame.recommended_action` (still `execute`) instead — so empty patches
   leaked to EXECUTE and produced empty-diff `git apply` failures. The diffs
   were not really invalid; there were no diffs.
2. **"wrong_file_path 5/10"** — an *empty-clone* phantom. Some worktrees were
   0-file / no-HEAD clones left over from an earlier blobless partial clone.
   Any apply failed with file-not-found. The model had actually picked the
   right files. Fix: validate worktree health before reuse.
3. **The truth: `search_not_found` 5/10** — the model mis-remembers exact
   source characters (e.g. writing a default value onto a keyword-only
   parameter). This was the real #1 all along, buried under two phantoms.

The lesson is not any one fix; it is that **without per-sample forensics and an
automated taxonomy, we would have "fixed" two phantoms and shipped.**

## Negative result: an elegant cross-repo memory that did nothing

We built a genuinely polished subsystem: cross-repo semantic recall — embeddings
(ONNX/fastembed), a vector index, and keyframe extraction — so the planner could
retrieve similar past fixes as templates (✅) or pitfalls (❌).

Then we ran the A/B experiment we should always run. With recall verifiably
firing (we added a `[recall] N episode(s) injected` log after discovering the
system had *zero* observability), the resolve rate went **1/10 → 0/10**. The
recalled episodes were relevant; they simply did not change the failure mode.

Small sample, high single-run variance — we do not claim "memory is harmful."
What we *can* say: a well-architected, well-tested memory system showed no
positive signal on our eval, and had never been validated by a single metric
before. **Elegant engineering is not the same as effective engineering.** The
question to ask first is "what is this worth in points," not "how clean is the
architecture."

## Negative result: the fix aimed at the confirmed #1 never fired

Having *data-confirmed* search-hallucination as the top failure, we built C1:
when a planned search block does not exist, feed the model back the **entire
real enclosing function/method/class** (resolved via AST), leaving almost no room
to re-hallucinate. Clean implementation, unit-tested, no regression.

Across two models and two eval rounds, **the gate that C1 hangs off triggered
zero times.** Forensics explained why:

- The stronger reasoning model frequently *stops without producing a patch at
  all* (`recommended_action=stop`), so there is nothing for the gate to check.
- The gate validates against `relevant_files[].content` (fetched via the GitHub
  Contents API at seed time), while the executor applies against the **cloned
  worktree on disk** — two independent content channels. Search-not-found then
  surfaces at the EXECUTE layer, not the PLAN gate.

So the confirmed "#1" was partly a property of one model's *lucky* runs, and the
gate's trigger window was far narrower than assumed. Measurement corrected
*what to fix* a second time: the real levers are (a) getting the planner to emit
a patch at all, and (b) unifying the gate's content source with the executor's.

## Counter-intuitive result: removing a contradiction, not switching formats

The PLAN prompt had accreted a self-contradictory instruction: one clause
*forced* `patch_edits` and *forbade* unified diffs; a later clause said to use
`patch` when `patch_edits` could not express the change. We relaxed it to a
neutral "use whichever fits."

On the gpt-5.5 targeted subset, resolved went **0/7 → 1/7**, and one sample
(`scrapy#5383`) went from a premature `stop` to a real DONE fix. But the
interesting part is *why*: the model **still emitted `patch_edits` every time —
zero unified diffs.** The win did not come from letting it use a format it
prefers. It came from removing a contradictory instruction that raised the
reasoning model's cognitive load until it gave up after ~200s. **Measurement
corrected the causal story, again — "loosen" ≠ "switch format."**

## Controlling for the model, not just the harness

To separate "harness problem" from "model capability," we swapped the planning
model (Gemini flash ↔ gpt-5.5) on the *same* seeded samples. Finding: gpt-5.5 is
10–20× slower per call, timeout-prone on this harness, and — on the samples we
could run — no better. That is a control-variable result, and it kept us from
misattributing harness friction to model weakness (and vice versa).

## Things we deliberately did *not* build

- **Streaming output** — evaluated, then deprioritized. It changes robustness
  and latency, not the model's tokens, so it can only help the ~1/10 timeout
  failures, versus ~4/10 for patch anchoring. Wrong lever to pull first.
- **Forcing `node_target`** — the AST node-anchoring path exists and is safe,
  but the model adopts it ~0% of the time; we kept it as an option rather than
  forcing it and distorting prompts.

## Honest caveats

Sample sizes are small (7–10), and both flash and gpt-5.5 have high
single-run variance, so individual deltas like 1/7 vs 0/7 are *not* statistically
significant. The value here is the *method* — hypothesis → instrument → falsify →
control variables → resist over-engineering — and the discipline of letting the
evaluation subsystem, not intuition, decide what gets built next.
