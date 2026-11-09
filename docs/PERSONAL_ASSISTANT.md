# JARVIS personal assistant

Implemented 2026-11-09 for Europe/Istanbul. Browser research, a durable local
calendar, reminders, preferences, notes, document summaries, routines, reservation
workflows and activity receipts share the existing voice/Telegram service.

## Start here

Call JARVIS or send ordinary text to its Telegram account. For example:

- “Research vegetarian restaurants in Kadıköy and send me the sources.”
- “Remind me tomorrow at 10:00 to review ShopWise.”
- “Remember that I prefer vegetarian restaurants.”
- “Make a note titled meeting ideas: …”
- “What appointments do I have this week?”
- “Give me my daily briefing.”
- “Run start my workday.”

JARVIS asks for missing details. Long actions return an activity ID first, then
send their actual result to Telegram. An active call also receives a short spoken
completion update. “Accepted” is not “completed.” `/activity ID` shows evidence;
`/action-cancel ID` requests cancellation. External work already in progress may
finish before cancellation; inspect its final receipt.

Use `/help-personal` for all commands. The control window has Personal help,
Daily briefing, Activity history and Connect Google Calendar buttons.

## Browser

JARVIS opens a visible, separate Edge profile, retained under its protected state
directory. It does not reuse or modify the normal Edge/Chrome profile. Choose
`browser_channel = "chrome"` in local configuration to use installed Chrome, or
`"chromium"` if the compatible Playwright browser has been separately installed.

`/search QUERY` opens results; `/research QUERY` reads pages and produces a cited
answer. `/browse URL` and `/browser-read` show page text and observed controls.
`/browser-close` closes the JARVIS session. Research cannot fill or submit forms.
Navigation is restricted to public HTTP(S) destinations. Passwords, authentication
codes, payments and CAPTCHAs are handled directly by the owner in the browser.

Optional `/browser-attach http://127.0.0.1:9222` connects to a Chromium debugging
session the owner has deliberately enabled. JARVIS creates its own tab in that
session; it does not take over existing tabs or enable remote debugging itself.
Close the current JARVIS browser before attaching. The independent profile is the
working default and avoids requiring this advanced setup.

## Google Calendar setup

Local appointments and reminders work immediately. Google Calendar needs its own
OAuth authorization; a Gemini API key is not calendar authorization.

1. In [Google Cloud Console](https://console.cloud.google.com/), select your project
   and enable the **Google Calendar API**.
2. Configure Google Auth Platform / OAuth consent for your personal app. If it is
   in Testing, add your own Google account as a test user.
3. Create an **OAuth client ID**, choose **Desktop app**, and download its JSON file.
4. Open JARVIS controls and click **Connect Google Calendar**. Select that JSON file.
5. Complete Google's authorization in your system browser. JARVIS requests calendar
   event access, validates the callback state/PKCE, and stores tokens with Windows
   DPAPI. Do not paste client secrets, login codes or tokens into Telegram.
6. Run `/calendar-sync`; then `/agenda`. The service polls the connected primary
   calendar approximately every 15 minutes, covering yesterday through 90 days ahead.

Standard Calendar API use has no additional API charge within Google's quotas.
JARVIS does not enable billing. OAuth apps in testing may need reauthorization.
Account authorization and live Google read/write operations still need the owner's
setup; mocked integration tests do not establish access to an actual account.

`/appointment START | END | TITLE | LOCATION` creates a **local** appointment and
a reminder 30 minutes beforehand if that time is still in the future. For example:

```text
/appointment 2030-05-10T15:00+03:00 | 2030-05-10T16:00+03:00 | Dentist | Kadıköy
```

Use your actual requested dates, not the example date. Overlaps are reported
before creation. `/calendar-push ID` explicitly creates/updates that appointment
in Google. `/reschedule ID | START | END` moves it; `/cancel-appointment ID` cancels
it. Linked Google events are updated remotely; local-only events stay local.
`/calendar-export` writes an ICS file that can be imported into another calendar.

## Reminders, follow-ups and daily briefings

```text
/remind 2030-05-10T14:00+03:00 | Leave for the dentist
/snooze REMINDER_ID | 2030-05-10T14:15+03:00
/dismiss REMINDER_ID
/briefing-at 08:30
/briefing-at off
```

Advanced options are available by voice or `/assistant`:

```json
{"op":"remind","title":"Review my plan","due":"2030-05-10T09:00+03:00","repeat":"daily","channel":"both"}
```

Channels are `telegram`, `desktop`, `both` or `call`; recurrence is `none`, `daily`
or `weekly`. A reminder can include `"task":"JARVIS_TASK_ID"` to suppress it when
that coding task is completed or cancelled. Reminder calls respect quiet hours,
existing calls, cooldown and frequency limits, and fall back to Telegram. They do
not enable automatic blocker calls globally.

The laptop must be awake and JARVIS running. Pending reminders catch up once after
wake/restart. Claimed-but-interrupted sends become `uncertain`; failed/uncertain
notifications are not blindly replayed. `/retry-reminder ID` explicitly schedules
a retry ten minutes later. A notification receipt means the channel accepted the
request, not proof that the owner read/heard it. Google's own notifications depend
on the calendar's notification settings.

## Restaurant reservations

Supply venue, date/time, party size, requirements and optionally the booking URL:

```text
/reserve VENUE | START_ISO | END_ISO | 2 | https://restaurant.example/reservations | Vegetarian options
```

JARVIS opens the venue's site, reads the page and prepares available booking steps.
It stops for an explicit observed browser-step confirmation. `/reservation-submit
ID` executes that step once. Changing the form or page invalidates the saved step.
Use `/reservation-continue ID` after completing a login or other manual step.

A possible final submission with no verifiable confirmation becomes `uncertain`,
not “booked”; it cannot automatically submit again. `/reservation-check ID` only
checks the current page. A successful receipt requires a concrete reference and
an exact matching quote from the observed page. A verified booking creates a local
appointment/reminder and, if connected, attempts to add it to Google Calendar.
Calendar failure does not erase a real booking confirmation.

The generic workflow has been tested with a local browser fixture. No real venue
has been booked during implementation. Restaurant sites with embedded frames,
unusual controls, CAPTCHA, payment requirements or delayed confirmations may need
manual completion or a site adapter. Do not treat the engine as universal booking
support until the particular site has passed a live check. Stopping a JARVIS job
does not cancel a reservation at the venue.

## Preferences, notes, documents and routines

`/remember KEY | VALUE`, `/memories`, `/forget KEY` manage preferences explicitly.
Credential keys are rejected. `/note TITLE | BODY`, `/notes QUERY` and `/delete-note
ID` manage dictated or typed notes.

`/documents QUERY` searches filenames within selected document folders (Documents
by default). `/documents-root FOLDER` adds a folder, with the newly added folder
searched first. `/document-summary ABSOLUTE_PATH` extracts TXT/Markdown/CSV, Word
DOCX or PDF text and produces a source-referenced summary. Links/junctions, hidden
protected folders, credential filenames, files over 10 MiB and encrypted PDFs are
excluded. Summaries are bounded to 60,000 extracted characters/100 PDF pages and
report truncation. Scanned PDFs without text require OCR, which is not installed.
Only explicitly requested document text is sent to the existing Codex account.

`/routine start my workday` gives a briefing and opens the selected Codex project
chat. `/routine-save NAME | JSON_STEPS` creates up to eight allowed steps, including
briefing, agenda, browsing/research, notes/document discovery and opening the
selected project. Routines cannot contain booking commits, arbitrary shell
commands or code approval. `/routines` inspects them; `/routine-delete NAME` removes
one. A partially failed routine records completed steps without replaying them.

Preferences, notes, local calendars, routines and receipts live in the existing
owner-protected SQLite database. OAuth secrets use DPAPI. They are not all encrypted
at rest. Completed activity/reminder history is retained for 90 days; unresolved
uncertain actions and booking receipts are preserved for inspection. Browser login
state stays in the separate protected browser profile.

## Dependencies and evidence

The feature dependencies are hash-locked in
`requirements-personal-win-py312.lock`; install them into the existing Python 3.12
environment with `pip install --require-hashes -r requirements-personal-win-py312.lock`.
Installed Edge is used, so a second browser binary download is unnecessary.

See `VALIDATION.md` for observed results and outstanding live acceptance checks.

References: [Playwright browser sessions](https://playwright.dev/python/docs/api/class-browsertype),
[Google desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app),
[Google Calendar quotas](https://developers.google.com/workspace/calendar/api/guides/quota).
