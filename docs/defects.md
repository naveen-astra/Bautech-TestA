# Bautech Sentinel — Defect List

**Project:** Bautech Sentinel, an autonomous QA agent for the Bautech Android app
**Prepared for:** Navicon InfraProjects
**Status as of this document:** live-hardware and BrowserStack findings to date; updated as the
suite runs further

This document lists every defect Sentinel has found, in two sections kept **structurally
separate on purpose**: findings about the Bautech app itself, and findings about Sentinel's own
automation. Conflating the two — reporting an automation bug as an app defect, or waving away a
real app defect as "probably our selector" — is the single failure mode the whole verification
design in this project exists to avoid (see `README.md`, §13 "Proving a Negative"). Every
entry below states which section it belongs to and why.

---

## Section A — Bautech application defects

### BAU-001 · Owner/Admin accounts reset to onboarding after OTP verification

| | |
|---|---|
| **Severity** | Blocker |
| **Status** | Reported to Navicon (`docs/navicon_email_login_reset.md`), awaiting response |
| **Affects** | Owner, Admin — any account already belonging to a company |
| **Does not affect** | Site Engineer — an account with no company yet |

**Symptom.** For an account that already belongs to a company, the login flow completes every
visible step correctly — phone number entered, OTP dispatched, code entered, "Verify OTP" tapped —
and then the app resets all the way back to the first screen a fresh install shows ("Select Your
Language"), instead of landing on the real home/company screen. No error message appears at any
point.

**The asymmetry that narrows the cause.** The Site Engineer test account, which has no company
data to load, logs in successfully every time and lands cleanly on "Create Company / Join Company."
This rules out the login mechanics themselves — phone entry, OTP dispatch, OTP verification are all
confirmed working, on every account — and points specifically at whatever happens *after*
verification succeeds: fetching the authenticated user's sites, approvals, and other company data.
The working theory, stated as a theory rather than a claim: a post-login data-hydration step is
failing or timing out for an account with real data to load, and the app's error handling resets
all the way to onboarding rather than surfacing a retry option or an error message.

**Reproduction — why this is reported with confidence.**

Reproduced on **two accounts** (Owner, Admin), across **two independent hardware environments**:

1. A physical Android device (Samsung Galaxy A22), connected directly over USB, driven manually
   step by step
2. A real BrowserStack App Automate session (Samsung Galaxy S22 · Android 12) — session artifacts,
   device logs, and video retrieved through the API

...and independently reconfirmed a third time through a completely different automation
mechanism — Sentinel's live thinking agent, which drives the app via raw `adb` and its own screen
perception rather than Maestro at all. All three paths hit the identical wall. Three structurally
independent code paths converging on the same failure is strong evidence this is a genuine defect
in the app rather than an artifact of any one testing tool.

Every step *before* the failure point was independently confirmed working — each element the flow
interacts with (the language screen, the phone-number field, "Send OTP," the OTP field, "Verify
OTP" itself) was matched against real, captured UI elements at each step, not assumed.

**A related, possibly connected observation.** On the Site Engineer account (the one that logs in
successfully), the "Create Company" button on the explore screen does not respond to any tap at
all — no navigation, no dialog, no error — confirmed both with a manual touch and via an automated
tap landing on the real, clickable element. Whether this is a deliberate permission restriction
(Site Engineer not permitted to create a company) or a second symptom of the same underlying issue
is an open question, listed here rather than guessed at.

**Impact on this project.** This is the single largest obstacle to a full 85-case unattended run:
it blocks any workflow that requires switching personas mid-run, and blocks re-establishing an
Owner or Admin session at all once logged out. Every other component of Sentinel — planning,
navigation, observation, judgment, reporting — is unaffected and has been proven independently of
this defect (see `README.md` §18).

**Corroborating detail, not part of the defect itself.** While genuinely stuck on the reset
screen during a live run, Sentinel's thinking agent — without being instructed to — tried "Sign in
with Google" as an alternative and successfully authenticated via a cached account. This confirms
the phone-OTP path specifically is where the defect lives, not authentication in general.

**Open questions for Navicon**, carried over from the original report:

1. Is this reproducible on your end with the same Owner/Admin test numbers? Is it a known issue?
2. Are server-side logs available for the moment right after OTP verification succeeds for these
   two accounts — a failed API call, a timeout, anything that would confirm or rule out the
   hydration-failure theory?
3. Is "Create Company" restricted to certain roles by design, or should it work for Site Engineer
   too?

---

## Section B — Automation failures

**None outstanding as of this document.**

Every automation-side defect found during development — a selector that turned out to be wrong, a
timing assumption that didn't hold, a race between the UI redrawing and the next check running —
was caught, diagnosed against real hardware evidence, fixed, and turned into a standing rule so the
same class of bug cannot recur silently. The full, evidence-cited list of these is kept in
`README.md`, §18.3 ("Findings that became standing rules"), because each one is tied to the exact
real-device evidence that proved it and the specific code that now prevents it — duplicating that
detail here would only let the two documents drift out of sync.

The headline items, for a reader who wants the shape of it without following the link:

- Flutter merging a whole widget's text into one accessibility node, breaking exact-match
  selectors (fixed: every text selector is fuzzy-matched by default; the live agent sidesteps this
  entirely by addressing elements by index instead of text)
- The on-screen keyboard covering a button the flow needed to tap next (fixed: keyboard dismissal
  is verified against real device state, `mInputShown`, never assumed)
- A reachability check running before a resumed screen had finished redrawing (fixed: every anchor
  check now waits before judging)
- BrowserStack upload timing out on a large APK over a slow link (fixed: the upload timeout scales
  with file size)

**Why this matters as its own section.** A defect list that only ever finds fault with the
automation, never with the app, is as suspicious as one that never finds fault with the automation
— both are signs the boundary between the two isn't actually being checked. The evidence that this
boundary is real and enforced, not just claimed, is in `README.md` §13 (the five-rung
prohibition ladder and its differential probe) and in `tools/selftest.py`'s own guarantee: no
configuration of missing or broken evidence is allowed to produce a `PASS`.

---

## How this list will be maintained

This document reflects findings to date, not a final count. As the remaining screen-map coverage
gap closes (`README.md` §21: 4 of 41 targets currently verified against real hardware) and a full
85-case run becomes possible, this list will be updated in place — new entries appended to whichever
section they belong in, with the same reproduction rigor as BAU-001 above: independent reproduction,
real-device evidence cited, and a clear statement of what has and has not been ruled out.

BAU-001 will move from "awaiting response" once Navicon replies, and this document will record
whichever of the two outcomes actually happens — confirmed as a real defect, or resolved with an
explanation Sentinel's evidence had not accounted for. Either outcome is reported here plainly.
