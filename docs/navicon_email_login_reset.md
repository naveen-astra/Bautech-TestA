Subject: Bautech app — login fails for accounts with existing company data (reproducible on real device + BrowserStack)

Hi team,

While finishing BrowserStack integration for the Sentinel testing agent, I ran into a login
issue in the Bautech app itself that I can now reproduce reliably, on two independent devices,
so I wanted to flag it directly rather than keep working around it.

**What happens**

For an account that already belongs to a company (tested with both the Owner and Admin test
numbers), the login flow gets all the way through OTP entry and verification — the OTP is sent,
entered, and "Verify OTP" is tapped — and then the app resets to the very first screen a fresh
install shows ("Select Your Language"), instead of landing on the real home/company screen.
No error message is shown either time.

**What doesn't fail**

The Site Engineer test number (<Site Engineer test number>), which is not yet a member of any company, logs in
successfully every time and lands cleanly on the "Create Company / Join Company" screen. So the
mechanical login flow itself — phone entry, OTP dispatch, OTP verification — is confirmed working;
the reset only happens for accounts that have existing company data to load afterward.

**Why I'm confident this isn't an automation or environment issue**

- Reproduced 4 times across two accounts (Owner: <Owner test number>, Admin: <Admin test number>), on two different
  environments: BrowserStack cloud devices and a real physical phone (Samsung Galaxy A22) connected
  directly over USB, driven manually step-by-step.
- Every prior step in the same flow (language screen, phone entry, OTP dispatch, OTP entry, the
  "Verify OTP" button itself) is confirmed to work correctly — verified against real matched UI
  elements at each step, not assumed.
- The one account that reliably succeeds (Site Engineer, no company yet) never hits the reset.

**My best guess, for what it's worth**

The pattern (works for an account with nothing to load, fails for an account with real company
data) points toward something in the post-login data-hydration step — fetching sites, approvals,
etc. for the authenticated user — failing or timing out, with the app's error handling resetting
all the way to onboarding rather than showing a retry option or an error message.

**One more, possibly related data point**

On the Site Engineer account (no company), the "Create Company" button on the explore screen does
not respond to taps at all — no navigation, no dialog, no error, confirmed both with a normal touch
and via Maestro's own tap mechanism landing on the real, clickable element. I don't know whether
this is a permission restriction (Site Engineer role not permitted to create a company) or the same
underlying issue as the login reset — worth checking on your end.

**What I'd ask**

1. Is this a known issue, or can you reproduce it on your end with the Owner/Admin test numbers?
2. Is there a way to see server-side logs for what happens right after OTP verification for these
   two accounts — a failed API call, a timeout, anything that would confirm the hydration-failure
   theory?
3. For the Create Company question — is that action restricted to certain roles, or should it work
   for Site Engineer too?

Attached: a screenshot of the reset on the real phone, a frame from the BrowserStack session video
showing the identical reset, and the successful Site Engineer login for contrast. Happy to send the
full device logs and screen recordings too if useful.

Thanks,
Naveen
