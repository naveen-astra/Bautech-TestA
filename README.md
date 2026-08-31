# Bautech Sentinel

An autonomous QA agent for the Bautech Android app. It reads the 85-case
regression sheet as the testers wrote it, plans each case, drives the app
through Maestro, observes what actually happened, and returns PASS / FAIL /
BLOCKED with the evidence behind the call.

## Status

**The whole pipeline is built and tested end to end. It has never touched a real
device**, because no Bautech APK and no working login exist yet — see
[What is still needed](#what-is-still-needed).

| Component | State |
|---|---|
| Test sheet extraction (85 cases) | done |
| Sheet parser | done |
| Plan schema and invariants | done |
| Permission oracle (role x action) | done |
| Screen map loader, lint, verification gate | done, **0/41 targets verified** |
| Plan compiler (LLM, cached) | done, not yet run against a live model |
| Differential probe generation | done |
| Maestro renderer + observation protocol | done |
| Verifier and negative-case adjudicator | done |
| Verdict engine and reporting | done |
| Local backend | done, never run against a device |
| BrowserStack backend | written against the docs, **never exercised** |
| `run.py` single command | done |

### Prove it without a device

```bash
python tools/selftest.py         # 31 checks: the judging is sound
python tools/component_tests.py  # 46 checks: bad input is refused
python tools/roundtrip.py        # the full pipeline, three app behaviours
python run.py --dry-run          # plan and render the real suite, run nothing
```

`selftest.py` covers every way a verdict can go wrong. The ones that matter are
the inverted cases: a broken selector, a screen we never reached, evidence we
failed to capture. **None of them is allowed to produce a PASS.** If that ever
changes, the suite can manufacture green results and nothing else here is worth
anything.

`roundtrip.py` runs two real cases against three simulated app behaviours —
healthy, seeded regression, and broken automation — and checks each lands on the
right verdict *and* the right failure class. Only the device is faked: the flow
is really rendered, the log is really parsed by the code that will read
`maestro.log`, and the verdict is really decided by the adjudicator.

## The idea in one paragraph

An LLM plans; deterministic code executes and judges. Each sheet row is compiled
once into a `TestPlan` — persona segments, capability calls, and the assertions
its Expected column implies — and cached on disk. Execution is plain Maestro
with no model in the loop. Judging is arithmetic and string work. The model is
consulted again only for genuinely semantic expectations and for writing up a
diagnosis. A re-run of an unchanged suite makes zero LLM calls.

This split is not a preference. BrowserStack runs Maestro as an uploaded batch —
you upload the app, upload zipped flows, start a build, and poll — so nothing can
decide taps live on a cloud device. Everything has to be planned up front.

## Architecture

Nine stages. The model appears in exactly one of them, and everything
downstream of it is arithmetic and string work.

```mermaid
flowchart TD
    SHEET["tests/bautech_suite.csv<br/>85 cases, as the testers wrote them"]
    PARSE["parser.py<br/>sheet to RawTestCase"]
    COMPILE["compiler.py<br/>the only LLM step"]
    CACHE[".plan_cache/<br/>keyed on a hash of the row"]
    PROBE["probes.py<br/>attach the control persona"]
    RENDER["renderer.py<br/>TestPlan to Maestro YAML"]
    MAP["config/screen_map.yaml<br/>selectors, screens, forms"]
    PERS["config/personas.yaml<br/>credentials from the environment"]
    BACKEND{"backend"}
    LOCAL["local.py<br/>Maestro CLI"]
    BSTACK["browserstack.py<br/>upload, build, poll"]
    OBS["observation.py<br/>@@OBS lines to ObservedValue"]
    VERIFY["verifier.py<br/>deterministic assertion checking"]
    ADJ["adjudicator.py<br/>the prohibition ladder"]
    ORACLE["sentinel/oracle/<br/>role x action, from the spec"]
    VERDICT["verdict.py<br/>PASS / FAIL / BLOCKED"]
    REPORT["report.py<br/>report.md, report.html,<br/>defects.md, results.json"]

    SHEET --> PARSE --> COMPILE
    COMPILE <--> CACHE
    COMPILE --> PROBE --> RENDER
    MAP --> RENDER
    PERS --> RENDER
    RENDER --> BACKEND
    BACKEND --> LOCAL
    BACKEND --> BSTACK
    LOCAL --> OBS
    BSTACK --> OBS
    OBS --> VERIFY --> VERDICT --> REPORT
    VERIFY --> ADJ --> VERDICT
    ORACLE --> ADJ

    classDef llm fill:#fde68a,stroke:#b45309,color:#3f2d00
    classDef data fill:#e0e7ff,stroke:#4338ca,color:#1e1b4b
    classDef out fill:#dcfce7,stroke:#15803d,color:#052e16
    class COMPILE llm
    class SHEET,MAP,PERS,ORACLE,CACHE data
    class REPORT out
```

| Stage | Module | Decided by | Output |
|---|---|---|---|
| parse | `parser.py` | code | `RawTestCase` per row, or a hard error |
| compile | `compiler.py` | **LLM**, cached | `TestPlan`: segments, capability calls, assertions |
| probe | `probes.py` | oracle lookup | the control half of each differential |
| render | `renderer.py` | code | Maestro YAML, credentials passed as parameters |
| execute | `backends/` | Maestro | `maestro.log`, screenshots |
| observe | `observation.py` | code | `ObservedValue` per `@@OBS` line |
| verify | `verifier.py` | code | one result per assertion |
| adjudicate | `adjudicator.py` | code + oracle | prohibition upheld, or automation failure |
| judge | `verdict.py` | code | `CaseResult` with evidence and failure class |
| report | `report.py` | code, LLM for prose | four artifacts per run |

### The trust boundary

The model is asked what a case *means* — which persona, which capabilities, what
the Expected column is asserting. It is never asked whether a case passed. That
question is settled by comparing recorded observations against declared
assertions, which is why a re-run of an unchanged suite makes zero LLM calls and
why an identical log always yields an identical verdict.

Two structural consequences follow. A plan names intent and never a selector, so
the compiler cannot invent UI it has never seen. And an assertion declares the
observations it consumes, so a missing observation is an automation failure by
construction rather than by anyone remembering to check.

## Proving a negative

Nineteen of the 85 cases assert that something must *not* happen. The trap is
that "I could not open Site B" is equally consistent with a mistyped selector,
and a system that reads absence as success will award a PASS to a suite pointed
at the wrong app.

So a prohibition never passes on absence alone. It has to clear a ladder:

| Rung | What it establishes |
|---|---|
| reachability | we proved we were on the right screen |
| affordance | the control was not there |
| **differential** | the same probe *does* find it for a persona who is permitted |
| enforcement | if the control existed, attempting it was refused |
| state | nothing actually changed |

The differential rung is the one that makes absence provable. To claim a Site
Engineer cannot see "Add Site", we run the identical probe as an Admin. Admin
sees it and the Engineer does not — the selector works and the app is enforcing.
Neither sees it — our selector is broken, and the honest answer is an automation
failure, not a pass. `config/screen_map.yaml` records which spec action each
control implements, and `sentinel/oracle/` knows who the spec permits, so the
control persona is looked up rather than hardcoded.

Where no persona is permitted the action at all — TC-080, "no DMs in V1" — the
differential rung is unavailable, and the case comes back BLOCKED with that
reason rather than a PASS we cannot defend.

## The sheet is the input

`tests/bautech_suite.csv` carries the same five columns the testers already use:

```
ID, Test case, Who, Steps to replicate, Expected result
```

Regenerate it from the source Word document with:

```bash
python tools/extract_suite.py "path/to/Testing Day-Wise_word.docx"
```

Two things are deliberately discarded on the way in:

- **The P/F and Remarks columns.** They record a previous manual run. Letting
  them reach the compiler would hand it the answers.
- **The `[MUST BE BLOCKED]` hints in 13 of the titles.** Whether a case is a
  prohibition is read from the Expected column instead. Only 13 of the 19
  prohibitions are tagged — TC-019, TC-056, TC-064, TC-068, TC-072 and TC-077
  hide theirs in prose like "Engineer must NOT have this option" — so a
  tag-reading agent silently misses six of them. Stripping the hint also makes
  the claim testable: the polarity we produce is provably a reading of the
  expectation rather than an echo of a label.

### Adding TC-086

Open `tests/bautech_suite.csv` in Excel, add a row, save:

```
TC-086, Add cement stock, Site Engineer, Material -> Add purchase -> Cement 50 bags -> Save, Stock increases by 50
```

Then re-run. No code changes. The compiler plans the new row against the same
capabilities as everything else. Rewording an existing row works the same way —
the plan cache is keyed on a hash of the row, so changed wording is a cache miss
that gets re-planned.

## Layout

```
run.py                    the single command
config/
  screen_map.yaml         all UI knowledge - selectors, screens, forms
  personas.yaml           who signs in, and how the OTP is obtained
tests/bautech_suite.csv   the 85 cases, as the testers wrote them
sentinel/
  schema.py               the TestPlan contract and its invariants
  parser.py               sheet -> RawTestCase
  compiler.py             LLM: RawTestCase -> TestPlan, cached
  probes.py               attaches the control half of each differential probe
  screen_map.py           logical target -> Maestro selector
  renderer.py             TestPlan -> Maestro YAML, and the @@OBS protocol
  observation.py          logs -> ObservedValue
  verifier.py             deterministic assertion checking
  adjudicator.py          the prohibition ladder
  verdict.py              assertion results -> PASS / FAIL / BLOCKED
  report.py               report.md, report.html, defects.md, results.json
  backends/               local (Maestro CLI) and browserstack (batch API)
  oracle/                 role x action permission matrix from the spec
tools/
  extract_suite.py        rebuild the CSV from the Word sheet
  selftest.py             prove the judging is sound
  component_tests.py      prove bad input is refused
  roundtrip.py            drive the whole pipeline without a device
```

### How observations get home

A flow prints each observation as one JSON line behind a marker:

```
@@OBS {"key":"stock_before","raw":"500 bags","anchor":true}
```

`observation.py` reads those back out of `maestro.log` locally, or out of
BrowserStack device logs in the cloud. Stdout is the one channel both
environments preserve, so there is no separate transport to arrange.

The flows are written defensively for a specific reason: a bare `assertVisible`
that fails aborts the flow, and an aborted flow reports nothing. A test proving
a *negative* has to observe the absence, not die of it. So every check is a
conditional that records what it saw and continues, and judging happens
afterwards in Python where it can be reasoned about. `anchor` is recorded rather
than asserted for the same reason: a screen we never reached must arrive at the
adjudicator as a fact, so it can be called an automation failure instead of
being scored as a pass.

Three design rules are enforced by the code rather than by convention:

1. **A plan names intent, never mechanics.** Steps say `stock_level`, not a
   selector. Only the screen map knows what that looks like, so the compiler
   cannot invent UI it has never seen, and a UI change is a data edit.
2. **An assertion declares the observations it consumes.** If one is missing at
   verification time, the result is an automation failure. Structurally, we
   cannot pass a test we did not actually look at.
3. **No verdict without evidence.** `CaseResult` refuses to construct without at
   least one piece, and refuses a non-PASS without a failure classification
   separating a Bautech defect from our own breakage.

## What is still needed

Nothing device-facing can start until these arrive:

1. **The Bautech APK.** Not present on this machine.
2. **A login route that works unattended.** Auth is OTP-only — phone `+91` or
   email, plus Google; there is no password. This is the single biggest risk to
   the whole project, because an agent that cannot log itself in cannot run
   anything unattended. The clean fix is Firebase test phone numbers with fixed
   OTP codes, which needs one config change from Navicon. The fallback, which
   needs nothing from them, is a small relay that polls an IMAP mailbox and
   serves the code over HTTP for Maestro's built-in `http.get` to fetch mid-flow.
3. **Three test accounts** (Owner / Admin / Site Engineer) in a dedicated test
   company, with Site A and Site B already distinct.

A BrowserStack App Automate account is needed for Phase 7, not before.

## Honest limits

- **Every screen-map selector is a guess.** `screen map: 0/41 targets verified`.
  They were seeded from screenshots and the product guide, not from a running
  build. The renderer refuses to emit a flow for an unverified target unless
  `SENTINEL_ALLOW_UNVERIFIED=1`, so a run cannot quietly rest on a guess.
- **The compiler has never been run against a live model.** It is tested against
  a stubbed client: schema flattening, caching, cache invalidation on reword,
  rejection of unknown targets, and the repair round-trip all work. Plan quality
  on the real 85 is unmeasured, and `ANTHROPIC_API_KEY` is not set here.
- **The BrowserStack backend has never been exercised.** It is written against
  BrowserStack's documented Maestro API with no account to test it. Expect the
  log-retrieval path to need adjusting on first contact — and that path is what
  the cloud run's evidence depends on entirely.
- **The screen map lint reports 11 unusable anchors.** Eleven screens are
  anchored on the same text as the control that navigates to them, so the check
  would pass whether or not the screen ever opened. Those anchors prove nothing
  and must be replaced from a real hierarchy dump before any verdict on those
  screens means anything. `run.py` prints them as warnings on every run.
- **Some cases may be genuinely untestable** through the interfaces available.
  Billing changes that take effect next cycle (TC-027), pause/unpause billing
  (TC-028, TC-029), an hours threshold that has to be crossed (TC-055) and quiet
  hours that have to elapse (TC-084) all need either real payment, time travel,
  or a long wait. The compiler is instructed to mark these
  `needs_unavailable_interface` and name what is missing rather than substitute
  a weaker check and claim the original.
- **No runtime measurement exists yet.** The two-hour budget is a projection
  from assumed per-case timings, and will not be trustworthy until a real case
  has been timed on a real device.
