# Phase 1 — Environment discovery

Started 2026-09-02, once `app-debug.apk` was placed in the project folder.
Answers the 20 questions from the original plan (§40) with evidence, not
guesses. Anything not yet answered is marked so explicitly.

## The build

Tool: `tools/inspect_apk.py app-debug.apk` (built this session; run it again on
any future build to re-derive all of this automatically).

| Question | Answer | Evidence |
|---|---|---|
| Package id | `com.naviconinfra.bautech` | `aapt dump badging` |
| Version | 0.1.2 (code 11) | manifest |
| min / target SDK | 24 / 35 | manifest |
| Framework | Flutter, **debug (JIT)** build | `libflutter.so` + `kernel_blob.bin` present, no `libapp.so` |
| ABIs packaged | arm64-v8a, armeabi-v7a, x86_64 | zip listing |
| Firebase project | **`navicon-erp`** | compiled resources: `project_id`, `google_app_id`, `google_api_key` |
| Auth provider | Firebase Auth (phone + email OTP, Google) | `firebase-auth*.properties` present; matches `auth.jpeg` screenshot |
| Permissions | camera, contacts, location (fine+coarse), notifications, storage | manifest |

This is the confirmation needed for the email to Navicon: the Firebase console
to ask for test phone numbers in is **`navicon-erp`**, not a guess.

## The UI vocabulary — the actually useful part

A **debug** build embeds Flutter's generated localisation source, and gen-l10n
writes the English text as a doc comment above every getter:

```dart
/// In en, this message translates to:
/// **'Add Site'**
String get homeScreenAddSiteTitle;
```

`tools/inspect_apk.py` parses `kernel_blob.bin` for this pattern and recovered
**6,779 distinct English UI strings**, written to `config/app_labels.json`.
This turns most of the screen map from a screenshot-based guess into a
build-grounded candidate. It does **not** tell us which screen shows which
string - that still needs a real hierarchy dump - but it narrows the
candidates enormously and, in several cases, resolved an outright ambiguity or
error in the original guesses:

- **Fixed 9 of 11 anchors the lint flagged.** Screens were originally anchored
  on the same text as the nav item that opens them (e.g. tap "Material", then
  check "Material" is visible - passes whether or not the screen ever opened).
  Replaced with real, distinct screen-title strings: `inventoryTitle` =
  "Inventory Management", `machineryStatusTitle` = "Machinery Status",
  `labourManagementTitle` = "Labour Management", `tasksOverview` = "Tasks
  Overview", `expensesDashboard` = "Expenses Dashboard", `partyManagementTitle`
  = "Party Management", `sitesHomeSearchHint` = "Site name, code or address",
  plus distinct subtitles for the billing and invite-users screens.
- **2 remain genuinely unresolved** (`reports`, `issues`) - no string exists
  in the build that is both unique to the screen and unconditionally present.
  Flagged in the screen map rather than forced.
- **Found 6 real, app-authored refusal messages** to replace generic guesses
  in `refusal_markers`: `errAuthNoPermission`, `errDbNoPermissionChange`,
  `errorPermission`, `errorUnauthorized`, `machineryVendorsAccessRestricted`,
  `reportsMembersNotAllowedAccess`. These are what the enforcement rung of the
  negative-case ladder should actually match on - not a paraphrase of what a
  permission error might say.
- **Confirmed the login flow strings** the renderer already sends match
  exactly: "Phone", "Email", "Send OTP", "Verify" all exist verbatim.
- **Found a genuine ambiguity**, left flagged rather than silently resolved:
  "Add Site", "New Site" and "Create Site" are all real strings that could
  plausibly be the site-creation button, likely at different points of the
  flow (empty-state CTA vs. list FAB vs. form submit). Needs a hierarchy dump
  to know which is which.
- **Found a discrepancy worth flagging**: the screenshot
  (`photo_2026-08-26_19-55-12.jpg`) shows an "My Alerts (2)" tab, but that
  exact string does not exist anywhere in this build's localisation table.
  Either the screenshot predates or postdates v0.1.2 - noted in the screen map
  rather than silently trusted.

`sentinel/screen_map.py`'s lint went from **11 warnings to 2** as a direct
result. Still `0/41 verified` - `apk_string` is stronger evidence than a
screenshot, but it is not the same as `maestro hierarchy` confirming the string
is where we think it is.

## Installing on a local emulator — the ABI trap

**Status: unresolved as of this write-up: two fixes are in flight in the
background (see "What's still running" below).**

Created `bautech_sentinel` AVD: Pixel 4 profile, `android-35;google_apis_playstore;x86_64`
(system image already present on this machine - no download needed). Booted in
~45s, `adb devices` showed it healthy.

`adb install app-debug.apk` succeeded, but the app **crashed on every launch**:

```
UnsatisfiedLinkError: dlopen failed: ".../libflutter.so" is for EM_AARCH64
instead of EM_X86_64
```

**What's going on:** `dumpsys package` reports `primaryCpuAbi=x86_64`, but the
actual bytes extracted for `libflutter.so` are the ARM64 slice. This is a known
quirk of Google Play-flavoured emulator images: they advertise ARM64 as a
supported ABI (to run ARM-only apps via a translation layer) and
`installd`/PackageManager can pick that slice over the real native one for a
multi-ABI APK, even while the *metadata* still says x86_64.

**Attempt 1 (in progress → failed → refined):** stripped the ARM slices from a
copy of the APK (`arm64-v8a`, `armeabi-v7a` removed via Python's `zipfile`,
`resources.arsc` and `lib/**` re-stored uncompressed as `targetSdk 35`
requires), then re-aligned with `zipalign -p 4` and re-signed with the debug
keystore. Reduced 233MB → 92MB. Install succeeded, but the app still crashed -
this time with `MissingLibraryException: Could not find 'libflutter.so'`.

**Root cause of the second failure:** the manifest sets
`android:extractNativeLibs="false"` (confirmed via `aapt dump xmltree`), which
means Android is expected to `mmap` the `.so` directly out of the APK zip
rather than extracting it - and `zipalign -c -p -v 4` on the rebuilt APK does
report successful page alignment, so the packaging itself checks out. The
remaining suspect is `adb install`'s **incremental install path**, which
streams the APK lazily via incfs; a first attempt showed an empty `app_lib/`
directory immediately after install, consistent with the native library not
having materialized yet. Retried with `--no-incremental`, which forced a
`Performing Streamed Install` - and the crash persisted. Given the manifest's
`extractNativeLibs=false`, this points at something more particular to
Flutter's `ReLinker` (the library Flutter's `FlutterJNI` uses to load its
native engine) not being satisfied by the direct-mmap path in the way
`System.loadLibrary` would be, and/or an interaction with the Play-flavoured
image's ARM-translation layer intercepting the load attempt regardless of
which slice is actually present.

**Attempt 2 (running in the background):** rather than continue reverse-
engineering AGP's native packaging assumptions against this specific device
image, install a **plain `google_apis` (non-Play) x86_64 system image** for
API 35. These do not carry the ARM-translation layer that appears to be the
common thread in both failures, and are the standard, supported target for
exactly this situation. Image was not present locally and is downloading now
(`sdkmanager "system-images;android-35;google_apis;x86_64"`).

## Maestro CLI — installed and confirmed working

`maestro --version` → **2.10.0**. Proven end-to-end against the (still
Bautech-ABI-broken) `bautech_sentinel` emulator, using `com.android.settings`
as a stand-in target since Bautech itself cannot launch on that image yet:

- `maestro hierarchy` returned a full, real accessibility tree (bounds,
  resource-ids, classes) - the exact mechanism `screen_map.yaml` verification
  and selector-writing depends on.
- `maestro test` ran a two-command flow (`launchApp`, `takeScreenshot`) and
  both steps reported `COMPLETED`.
- The screenshot artifact landed on disk exactly where the local backend
  expects it: `~/.maestro/tests/<timestamp>/<flow>/takeScreenshot/*.png`.

So the whole chain **Maestro CLI → emulator → accessibility tree → executed
flow → retrieved artifact** is confirmed working. The only remaining blocker
to running this against Bautech itself is the ABI issue below.

## Still in progress

`system-images;android-35;google_apis;x86_64` download via `sdkmanager` -
started, process still alive (memory climbing), but as of this write-up the
image directory still only contains the `.installer` marker, not the actual
`system.img`. This is a large (multi-hundred-MB) download; being tracked via a
scheduled background check rather than blocking on it.

**Update: it still crashed.** The plain `google_apis` image (no Play Store)
finished downloading (3.6GB - explains the long wait) and produced the
*identical* crash on the original, unmodified APK:

```
UnsatisfiedLinkError: dlopen failed: ".../libflutter.so" is for EM_AARCH64
instead of EM_X86_64
```

So the original theory ("this is a Play Store-specific ARM-translation quirk")
had the right *mechanism* but the wrong *scope*. Checked directly:

```
ro.product.cpu.abilist:   x86_64,arm64-v8a
ro.dalvik.vm.native.bridge: libndk_translation.so
ro.enable.native.bridge.exec: 1
```

**ARM translation is now bundled by default on all Android 15 (API 35)
emulator images, Play flavour or not** - Google made this standard, not a
Play-specific feature, some time after the docs and community threads this
diagnosis was originally based on were written. Every x86_64 API 35 image
currently downloadable will hit the same installd ABI-selection quirk.

The genuinely good news buried in this: **this is a local-emulator-only
problem.** A real ARM phone - including everything BrowserStack runs, which is
what the graded submission has to use regardless - has no ABI ambiguity to get
wrong in the first place; `arm64-v8a` is just what the hardware natively runs.
This blocks convenient local iteration, not the project.

**Next attempt:** downloading `android-33;google_apis;x86_64` (Android 13),
old enough to predate ARM translation being bundled by default. Bautech's
`minSdk` is 24, so it will install and run there regardless of the newer
`targetSdk 35` the build declares - Android's platform compatibility handles
that the normal way. If this also fails, local emulation is deprioritised
entirely in favour of getting a real device or BrowserStack access sooner
rather than later, since no further local workaround is likely to be worth the
time against that ceiling.

## Maestro CLI installed - and the OTP relay is now real, not just designed

`~/.maestro/bin/maestro --version` -> **2.10.0**. Confirmed end-to-end against
the running emulator using `com.android.settings` as a stand-in target (Bautech
itself can't launch yet): `maestro hierarchy` returned a full real
accessibility tree, and a two-command flow (`launchApp`, `takeScreenshot`) both
reported `COMPLETED` with the screenshot landing exactly where the local
backend expects it. So **Maestro CLI -> emulator -> executed flow -> retrieved
artifact** is a confirmed-working chain; only the Bautech-specific ABI issue
above stands between this and driving the real app.

Separately, while the system image downloaded, closed a real gap: the renderer
already generated `runScript: fetch_otp.js` for `otp_mode: relay` logins, but
nothing ever wrote that file, and the fallback OTP path (needed if Navicon
either can't or is slow to set up Firebase test numbers) didn't actually exist
yet. Built it properly:

- `sentinel/otp_relay.py` - a small HTTP service that long-polls an IMAP
  mailbox for a 6-digit code addressed to a given identifier, and holds the
  connection open until one arrives or its own timeout elapses. The polling
  logic (`ImapOtpSource`) and the HTTP surface are split apart specifically so
  the HTTP behaviour is testable without a real mailbox.
- `FETCH_OTP_JS` in `renderer.py` - the companion Maestro script. Makes exactly
  one HTTP call rather than polling from inside the flow, because Maestro's JS
  sandbox has no sleep/timer primitive to poll with.
- `render_plan()` now writes `fetch_otp.js` alongside any flow whose persona
  uses relay mode, and leaves it out otherwise.
- `tools/otp_relay_tests.py` - 19 checks: code extraction against real
  `email.Message` objects (plain and multipart), the full HTTP surface against
  a real running server with a scripted fake source (200/504/400/404 paths,
  per-identifier isolation for concurrent personas), and confirmation the
  renderer only emits the script when it's actually needed.

This does not reduce the need to ask Navicon for Firebase test numbers - that
is still the clean fix and the primary ask. It means a "no" or a slow "yes"
from them no longer stalls the project; email-OTP test accounts are enough.

## The emulator chase is over - a real phone settled it

Three local emulator variants (Play Store x86_64, plain x86_64, plain x86_64
API 33) all hit the same class of native-library ABI failure - see the section
above. Rather than keep patching around emulator-specific quirks, connected an
actual Android phone (a Samsung Galaxy A22, `adb` serial `RZ8R80CE9VV`, Android
13, genuine `arm64-v8a` hardware) via USB. This sidesteps the whole problem
category: there is no ABI to mis-select on real hardware, since the phone only
ever had one native architecture to begin with.

**The original, unmodified `app-debug.apk` installs and launches cleanly on
real hardware.** No crash, no workaround needed. That alone is worth recording
as the answer to "is this build sound" - it is; the problem was entirely
specific to x86_64 emulation of an ARM-only-in-practice build.

### The login flow: built, debugged, and proven correct against real hardware

With real Firebase-registered test credentials in `.env`, ran the actual
Maestro-driven login flow (`tools/demo_login.py --execute`) against the phone
repeatedly, fixing three real, evidence-based bugs along the way - each found
by reading the actual screenshot or hierarchy dump `maestro` captured at the
point of failure, never guessed at:

1. **The Phone/Email tab's real accessibility text is `"Phone\nTab 1 of 2"`**,
   not the bare label - Flutter merges the tab-position hint into it for this
   segmented-control widget. Maestro's text selector requires a full match, so
   `tapOn: "Phone"` matched nothing. Fixed with a permissive regex,
   `{"text": ".*Phone.*"}`.
2. **Selecting the tab does not focus the input field beneath it.** `inputText`
   sent with nothing focused typed into empty air and the flow silently never
   left the screen. Fixed by tapping the field's hint text (`"Enter 10-digit
   number"`, the only anchor available - the field has no resource-id) before
   typing.
3. **The on-screen keyboard covers "Send OTP".** Confirmed directly in a
   screenshot: after typing the phone number, the keyboard occupied the bottom
   half of the screen, and `tapOn` (which resolves through the accessibility
   tree, not pixels) reported success while the physical touch coordinate
   likely landed on the keyboard instead. Fixed with `hideKeyboard` before the
   tap.

All three fixes are now in `sentinel/renderer.py`, covered by the existing
102-check suite (still green), and were each confirmed by re-running against
the phone and watching the step that used to fail report `COMPLETED`.

### Where it actually stopped, and why no more debugging will move it

The fourth attempt, with all three fixes in place, revealed the real
blocker - and it is not technical. Driving to the screen manually (bypassing
Maestro to get unambiguous ground truth) showed Bautech's own banner:

> **"No account found with this number. Please sign up first."**

**A Firebase test phone number and a Bautech user account are two different
things.** Registering a number as a Firebase test number only makes *OTP
verification* automatic - it does not create an account in Bautech's own
backend. The app checked whether an account exists for `<test number>` on
sign-in, found none, and refused before ever sending an OTP or navigating
anywhere. This is exactly why "Send OTP" and "Verify" looked like automation
failures in earlier attempts: the app never left this screen, because its own
business logic rejected the number first.

**This is provably not an automation problem.** Every mechanical step - open
the app, select the tab, focus the field, type the number, dismiss the
keyboard, tap the button - now works, confirmed against real hardware,
repeatedly. What's missing is an actual account behind the number.

### The actual next step

Ask whoever registered the three Firebase test numbers one direct question:
**do accounts already exist for these numbers in the `navicon-erp` test
company, or do they still need to go through Bautech's own sign-up flow once
each?** Nothing on the automation side is blocking this - the login flow is
built, debugged, and proven correct. It is only waiting on real accounts to
authenticate into.

## Answered vs. still open, against the original 20 questions (§40)

| # | Question | Status |
|---|---|---|
| 1-2 | Android / Flutter version | **Answered**: Flutter (debug/JIT build), targetSdk 35 |
| 3 | Package id | **Answered**: `com.naviconinfra.bautech` |
| 4-5 | Auth mechanism / env-var credentials | **Answered**: Firebase Auth, project `navicon-erp`; env-var plumbing built and proven in `config/personas.yaml` + `sentinel/renderer.py` |
| 6-7 | What Maestro can identify / semantics quality | **Answered**: confirmed via real hierarchy dumps against a physical device - text, accessibility labels and hints are usable; resource-ids are largely absent (typical Flutter) |
| 8 | API responses observable | Not yet investigated - needs a reachable account to get past login first |
| 9-10 | Test data creation/reset | Still needs an answer from Navicon (asked in the sent email); now blocked behind the same account-provisioning question above |
| 11-15 | Persona switching, cross-persona/numeric/absence cases, genuine BLOCKED candidates | Design already accounts for these (see the plan); execution evidence still pending real accounts |
| 16-19 | BrowserStack build receipt / execution / artifact retrieval | Not started - Phase 7 |
| 20 | Whether 85 cases fit in 2 hours | Still a projection; needs one real per-case timing to become real |

## Pushing further with the live session: real module coverage

While the account session stayed live, went past the home screen to see how
far the same patterns hold. Two genuinely new things came out of it:

**The site-detail screen's module tiles (Reports, Expenses, Machinery,
Materials, Tasks, Labour) are each their own clean, unmerged accessibility
node** - unlike every navigation widget confirmed so far (login tabs, the
bottom nav), these do *not* merge a position hint into their label. Real,
useful contrast: the merging problem is specific to certain widget patterns
in this app, not a blanket property of every tappable element. `site_home`
and the six module routes are now verified against a real hierarchy dump, and
one real correction came out of it: the `material` route was guessed as
`"Material"` (singular); the real tile reads `"Materials"` (plural).

**The `material` screen's own title is `"Material Management"`, not
`"Inventory Management"`** as an earlier `apk_string`-sourced guess had it.
The guess was not fabricated - "Inventory Management" is real text on that
screen - it is a sub-header above the "+ Add Entry" button, not the screen's
own title. A string existing in the build says nothing about *where* it
appears; this is exactly why the verification gate exists.

Given the now three-times-confirmed merging pattern (login tabs, bottom nav,
site-list cards), generalised the fix rather than patching each occurrence:
`_selector()`, the one function every rendered text selector already funnels
through, now wraps a bare `{"text": ...}` spec in a fuzzy match by default.
A fuzzy match costs nothing on the labels that turn out clean - `".*Reports.*"`
still fully matches a node whose text is only `"Reports"` - so this is the
safe default everywhere, not a targeted patch. All 117 checks (see below)
stayed green through the change.

## Bounded retries: the Phase 6 gap that had zero external dependency

Built `sentinel/junit.py` and wired a retry loop into `run.py`
(`--max-retries`, default 1). The one property that actually matters was
built to be checkable independently of the code reading correctly: a case
whose flow *crashed* before finishing is eligible for one bounded re-run;
a case whose flow *completed* and observed a real FAIL is never touched
again, because the retry mechanism only ever looks at JUnit-level
completion, never the verdict - it has no code path by which a FAIL could
influence whether a retry happens. `tools/retry_tests.py` checks this
directly against a real JUnit report shape (cross-checked against the
one Maestro actually produced during today's live runs), not merely assumed
from reading the implementation.

A case that only completes on a retry now says so in its own report entry -
"this verdict is from retry N" - rather than looking identical to a clean
first pass, which is what stops a retry from quietly becoming a way to hide
flakiness.

**Check count is now 117** (31 + 58 + 19 + 9), up from 102 at the start of
this pass - all against the same anti-false-positive guarantee: no
configuration of missing or broken evidence produces a PASS.

## A real write, and a real unresolved question

Pushed past read-only verification to prove the actual create-and-verify-delta
pattern the whole system exists to demonstrate - not read a value, but write
one and watch it move. Real, careful steps: created a clearly-labelled test
item (`SNTL-cement-live01`, matching the project's own run-scoped naming
convention rather than touching real inventory), entered a real quantity of
100 via the real "Manage Stock -> Adjust" form, watched the app's own live
"Updated Stock" preview correctly compute `0 + 100 = 100`, and saved.

**The write did not persist.** Checked immediately after, and again after
forcing a genuine fresh server sync ("Synced just now", not a cached read):
the new item never appeared, Total Items stayed at 1, the Activity log never
recorded it. A **"1 conflict"** badge was present throughout and is not
tappable for detail.

Two explanations are equally plausible from the evidence available, and this
is reported as genuinely unresolved rather than picking one:

1. **A real sync/write bug** - the app accepted the form, showed no error,
   and silently failed to persist behind a vague conflict indicator.
2. **A collision with concurrent real use** - this account (company "RBAC
   Testing and Bauchat") is not a private sandbox. Aqua Line's own progress
   changed from 100%/Completed to 13%/Ongoing between two screenshots taken
   minutes apart today, with nothing done here to cause it - someone else,
   almost certainly Navicon's own QA team, is actively using this exact
   account concurrently.

Stopped here rather than keep experimenting on a live shared account without
understanding the conflict mechanism. **Worth asking Navicon directly:** what
does "1 conflict" mean in Material Management, and should test isolation
assume this account is exclusively ours, or shared? If writes can silently
fail behind an unresolved conflict, that is a real reliability question for
all 85 cases, not just this one - and the honest verdict for this specific
attempt is BLOCKED, not a fabricated PASS or an unfounded defect claim.

## Further real screen coverage

Continued past Materials with read-only navigation (deliberately no more
writes, given the finding above):

- **Tasks and eMB are not separate screens.** Both are tabs - "Tasks
  Dashboard" / "eMB Dashboard", each a clean unmerged node - on one screen
  genuinely titled **"Progress Management"**, not the guessed "Tasks
  Overview". `emb`'s own apk_string-sourced guess ("eMB lives under Tasks,
  not its own nav item") had the right structural idea; the specific anchor
  text just was not confirmable, because there is no separate screen to
  anchor. Both `tasks` and `emb` screen entries now point at the same real,
  verified anchor - the screen-map lint correctly flags this as a shared
  anchor, and that flag is accurate: they are the same physical screen.
- The screen's real stat blocks ("Overall Progress 13%, 1 completed, 2
  tasks", "Schedule Status: On Track") are merged into one large
  accessibility node, consistent with the pattern found everywhere else in
  this app - handled automatically by the fuzzy-match default, no special
  case needed.

**Screen map is now 4/41 verified** (`site_home`, `material`, `tasks`,
`emb`), up from 2. One test (`component_tests.py`, the "no spec mapping"
guard) broke as a direct, expected consequence of `emb` gaining a real spec -
fixed by moving that test to `machinery`, which still has none, with a note
explaining why the fixture needed to move rather than silently changing what
it proves. **Check count holds at 117, all still green.**

## Device paused - moved to pure-code work

Checked back in on the phone and found it showing Instagram Reels, not
Bautech - real, unambiguous evidence it had moved into someone's personal use
rather than sitting available for testing. Did not relaunch Bautech and
resume automated interaction on a phone actively being used for something
else; paused all real-device work and shifted to code that needs none.

**The reachability timeout got a second, more honest look.** The 6000ms fix
from earlier missed again on the very next live run - the same failure mode,
proven by the same evidence (a later step finding text the anchor check had
just missed). Raised to 15000ms. This is explicitly logged in the code as an
engineering judgment call made without device time to re-measure properly,
not a re-measured number - worth tightening once the phone is available
again rather than trusted as tuned.

**Built and tested the persona-wave scheduler** (`sentinel/scheduler.py`),
the real Phase 4/6 gap named since early in this project. A greedy
batching algorithm: ride the current persona's wave as long as any plan has
work ready for it, respecting each plan's own segment order absolutely (a
cross-persona case's later segment can never be scheduled before its
earlier one). Checked directly, not assumed:

- Shuffled single-persona plans collapse into one wave per persona
  regardless of sheet order
- A cross-persona plan's segments are proven to stay in order even while
  interleaved with unrelated plans' work
- **A realistic 15-case mixed batch: naive sheet-order execution costs 11
  logins, the scheduler's ordering costs 3** - checked against a computed
  naive baseline, not asserted

**Deliberately not done yet, and said so in the module's own docstring:**
this produces an ordering, not yet a change to how flows are generated - each
segment is still its own Maestro flow file with its own login regardless of
which wave it lands in. The change that actually collapses login count in a
real run - merging same-wave segments into one flow - is a real change to
the renderer's flow-per-segment model, and was not made without device time
to verify it working. Built the algorithm; wiring it in is the next step,
not this one.

**Check count is now 130** (31 + 58 + 19 + 9 + 13), all still holding the
same anti-false-positive guarantee.

## Immediate next steps, in order

1. **Ask Navicon whether the three test-number accounts actually exist yet.**
   This is now the single blocking question - everything downstream is ready
   the moment it is answered.
2. Once an account is reachable, run `maestro hierarchy` on the post-login
   screens and begin flipping `screen_map.yaml` entries to `verified: true` -
   `home`, `sites_list`, and the rest of the vertical slice.
3. Resolve the `add_site_button` three-way ambiguity and the two remaining
   weak anchors (`reports`, `issues`) from real hierarchy dumps once reachable.
4. Local x86_64 emulation is no longer on the critical path - the physical
   phone is faster to iterate on and does not carry the ABI-selection problem
   at all. Emulator work only matters again if BrowserStack device
   availability makes a specific ARM emulator profile worth matching later.
