# Bautech Sentinel — Architecture Note

*The full reference is `README.md`; this is what a reviewer needs without reading it.*

## How it works

Sentinel reads Navicon's 85-row regression sheet as testers wrote it — persona, steps, expected
result — and produces a defensible `PASS`/`FAIL`/`BLOCKED` verdict with cited evidence, in two
modes over one shared judgment core.

**Mode A, the live thinking agent**, perceives the real screen over `adb` (filtered to the app
under test, rendered as a numbered element list), decides one action at a time via a free LLM
(Groq's `gpt-oss-120b` default; Ollama for fully offline), and re-observes after each step.
Elements are addressed by index, not constructed text selectors, which structurally eliminates the
fuzzy-matching and status-bar mis-tap bugs selector-based automation is prone to. It is genuinely
adaptive: stuck on a real app defect during a live run, it tried "Sign in with Google" unprompted.

**Mode B, the compiled Maestro suite**, plans each row *once* with an LLM into a structured, cached
`TestPlan` (capabilities and assertions, never raw selectors), renders it to Maestro YAML, and
executes as a batch — the only mode BrowserStack can run at all, since its App Automate integration
is upload → build → poll with no channel for a live decision mid-run. Planning is cached by content
hash, so an unchanged suite re-runs for zero LLM calls, and a reworded case is simply a cache miss
that replans automatically with no code change.

**The trust boundary is enforced by code:** the model says what a case *means*, never whether it
*passed*. `verifier.py`/`adjudicator.py` decide that deterministically from recorded observations
against declared assertions; a missing observation is an automation failure by construction, and a
`CaseResult` refuses to exist without cited evidence. 31 checks in `tools/selftest.py` prove no
configuration of missing evidence can produce a `PASS`.

**The hardest 20 points — proving a negative.** "I couldn't open Site B" is equally consistent with
a broken selector as real enforcement, so a prohibition never passes on absence alone: it clears a
five-rung ladder ending in a differential probe against a *permitted* persona — if nobody can see
the control, the honest verdict is "our selector is broken," never a pass credited to Bautech.
Measured: 15 of 19 official negatives correctly derived from Expected-column prose alone (the
compiler is never told which rows are negative), plus 7 bonus catches in compound rows.

## What it cannot do today

- **No live write action yet.** Every live run so far is read-only/exploratory; the formal verdict
  bridge (`mission.py`, 33 checks) is built and tested but unexercised live.
- **4 of 41 screen-map targets are verified** against a real hierarchy dump. The renderer refuses
  an unverified target, which is why ~38 cases are currently declined rather than weakened.
- **Persona switching is blocked by a real app defect, BAU-001** (`docs/defects.md`): Owner/Admin
  accounts reset to onboarding after OTP verification succeeds; Site Engineer is unaffected.
  Reproduced through three independent automation paths; reported, awaiting response.

## What breaks first at 500 cases

**The screen map** — hand-maintained YAML, each entry needing a real hierarchy dump to verify. At
500 cases the failure mode isn't runtime or cost, it's **verification throughput**: the same gate
that keeps the compiled path honest (`SENTINEL_ALLOW_UNVERIFIED`) caps how fast new coverage can be
trusted, since most growth is new *combinations* of screens rather than proportionally more of
them. The structural mitigation already exists — Mode A has no screen-map dependency at all — so
scaling means shifting a growing share of the suite onto the live agent, not verifying selectors
faster.

Secondary: `scheduler.py` batches same-persona segments into login waves (tested, now wired into
rendering) but doesn't yet merge a wave into one flow behind one shared login — deferred pending
device time to confirm it won't disturb the renderer's per-flow anchor-tracking.

## Cost

**Zero dollars per run, by design.** The brain, the compiler, and BrowserStack's App Automate all
run on free tiers; Maestro is open source. One HTTP adapter speaks the OpenAI-compatible protocol
shared by Groq, Ollama, OpenRouter, and LM Studio, so a hosted free tier and a fully offline local
model are the same code path with a different URL. Claude is opt-in only and fails loudly, naming
free alternatives, if selected without a key. Measured: compiling all 85 cases cost 33 calls,
118,327 tokens, 95 seconds — **$0.00** — and a repeat run costs nothing further, since compilation
is cached.

## Three-month roadmap

**Month 1.** Prove one real write action and one formal live verdict — both achievable now, no
blocked dependency. Test whether `logout()` (built, tested against a fixture, unverified on
hardware) survives BAU-001. Verify the next tranche of screen-map targets, prioritizing whichever
unlocks the most currently-declined cases.

**Month 2.** Merge wave segments into shared-login flows. Run the full 85-case suite live,
end to end, measured against the two-hour budget. Run the full suite on BrowserStack. Produce the
stable-build report.

**Month 3.** Run against Navicon's Day-13 seeded build with *zero* code changes — the guarantee
this design is built around — and produce the comparative report, the unattended screen recording,
and a live rewording walkthrough. In parallel, begin shifting screen-map-blocked cases onto the
live agent as its write-action and formal-verdict capabilities mature, so coverage growth stops
being bottlenecked on hand-verified selectors.
