# Validation evidence

Date: 2026-11-08, Europe/Istanbul.

## Observed results

| Check | Result |
|---|---|
| Offline unit/integration suite | 122 tests passed in the installed Python 3.12 environment |
| Codex initialize/account/list probe | Passed against installed `0.167.0-alpha.3.2` |
| Codex auth type | `chatgpt`; no credentials printed |
| Existing chat discovery | 43 unique chats and 22 accessible project folders in the bounded metadata catalog; no chat bodies read |
| Live Codex reasoning | Worker/reviewer approval pipeline and project-discovery conversation exercised using existing ChatGPT sign-in |
| Protocol compatibility | Inspected generated thread/turn/command schemas |
| Dependency locks | 28 runtime distributions and three build distributions pinned |
| Dependency installation | Installed in isolated Python 3.12 environment; pip check passed |
| Telegram login or calls | Authenticated; incoming call and requested callback observed. Owner reports stable connection but static voice; audio quality remains pending retest |
| Gemini requests | Live audio and supervisor tool call succeeded; direct PCM sample transcribed correctly, with no clipped samples |
| Local speech model execution | Piper synthesis and whisper recognition exercised using generated speech |
| Windows login startup registration | Registered and running, non-elevated; canonical owner state directory |

The tests use temporary projects, fake providers and fake Codex events. The Windows
DPAPI test encrypts/decrypts a fixture string, not a real credential. No test contacts
Telegram, Google or OpenAI. The separately invoked Codex probe reads account type
and thread metadata without starting an agent turn. Separately authorized live
diagnostics do start model turns; these are distinct from the offline test suite.

## Voice repair follow-up

The owner's listening report takes precedence over interpreting the internal call
ledger as proof that Telegram physically disconnected. The owner reported static
speech and an incorrect claim that Codex projects were inaccessible.

Measured Windows asyncio 10 ms waits averaged 15.85 ms. Native playback now uses
high-resolution waits and a byte-counted 120 ms producer window. The diagnostic
clock averaged 10.01 ms across 100 intervals (p95 10.47 ms). A synthetic PCM
pipeline test verifies exact sample preservation across the window and interrupts
release discarded-frame credits. This does not establish audible phone quality.

Voice setup now receives current Codex connection facts and project names. The
supervisor discovers bounded, deduplicated chat/project metadata and permits voice
selection only from that catalog. A live Gemini-to-supervisor-to-Codex probe made
one tool call, received a project-list response and returned spoken audio. No task
was started. Project edits still require exact text/local approval. The repaired
phone audio needs another owner listening check; automatic calling remains off.

## Visible task follow-up — 2026-11-09, Europe/Istanbul

The ShopWise task `3cd2aa0e5c1e` and its persisted Codex turn were inspected.
The worker executed local inspection commands and completed a static security
review; it explicitly reported no executed tests. No JARVIS verification command
proposal or result exists for that task. The original chat was named and a
supported desktop deep-link opening was requested, without rerunning the review.

The owner selected automatic task start with its Codex chat opened. Local config
now uses `codex_task_mode = "visible"`. New workers and independent reviewers are
named and their chats are opened; opening failures stop submission or interrupt
an accepted turn. Receipts distinguish queued, accepted, frontend-draft and
verification states. Voice facts include command evidence and verification
results. The offline suite now passes 59 tests, including opening order,
failure without a hidden fallback, draft-only operation and verification evidence.
Actual foreground rendering is not established by an OS link-opening receipt.

## Personal assistant follow-up — 2026-11-09, Europe/Istanbul

- The owner selected Google Calendar and restaurant websites as the first integrations.
- Five personal-feature distributions were hash-locked; four new distributions
  were installed in the isolated environment (typing-extensions was already present).
  `pip check` passed. Existing Edge is used; no second browser binary was downloaded.
- 83 offline tests pass. New coverage includes timezone conversion, ICS escaping,
  calendar conflicts/rescheduling, note/preferences access, protected document
  folders, conditional/recurring reminders, startup delivery deferral, notification
  channel selection, recovery without replay, OAuth PKCE/state validation, sync
  deduplication/cancellation, routine restrictions, and reservation evidence and
  one-time commit behavior. Tests never make real bookings or use actual accounts.
- A headless Edge integration fixture observed form values, rejected an unconfirmed
  button submission and observed a synthetic `TEST-1234` confirmation after an
  explicit confirmed click. This was a local HTML fixture, not a restaurant site.
- A separate visible browser profile read a public page and a live Codex planner
  returned an evidence-based explanation with its observed source URL. No forms
  were submitted in that public-browser check.
- A live conversational check mapped “remind me in two hours” to a durable reminder
  in a temporary database, without creating a coding task. That database was removed;
  it will not notify the owner later. Planning threads disable the shell tool and
  use the existing subscription sign-in; another live structured-planning probe passed.
- Google OAuth is implemented and tested with a local mock callback/token exchange.
  The owner's actual Google Calendar is not connected or live-tested. Complete the
  setup in `PERSONAL_ASSISTANT.md` before testing event creation, moves and cancellation.
- Real restaurant adapters/acceptance remain site-specific. Login, CAPTCHA, payment
  controls, embedded booking flows and ambiguous confirmations may require owner
  handoff. No real restaurant reservation or Google Calendar event was created.
- Existing British voice, Telegram owner restriction, visible Codex execution and
  separate code approvals remain configured. No automatic blocker calling or
  unsolicited scheduled briefing was enabled by these changes.

## Project discovery repair — 2026-11-09, Europe/Istanbul

The old catalog inferred project folders from at most four pages of recent chat
history. It never consulted the registered project list. Its default 25-item
updated-time pagination missed AIDemo's three chat records in a live comparison,
although a broader single-page query returned them. Default provider/source
filtering also excluded other kinds of chats. These were unsuitable inputs for
an authoritative project inventory.

Discovery now reads registered local workspace metadata independently of history,
retains sidebar labels and unavailable/multiple roots, and refreshes when metadata
changes. Configured project roots are a labelled fallback, and unreadable metadata
retains the last valid snapshot with a warning. The installed runtime has no
project-list API; the desktop metadata adapter is read-only and isolated for future
format changes. No Codex database, settings or project files are modified.

Chat metadata now explicitly includes all source kinds/providers, uses immutable
creation-order pagination and detects repeated cursors/partial results. Unassigned
historical directories are kept separate from registered projects. Twelve new
regression tests cover these behaviors; 95 tests pass overall.

A live catalog comparison against the desktop's `list_projects` connector found
all 14 local projects with matching project IDs, labels and paths, with no missing
projects and no discovery warnings. AIDemo was available at its actual root. A
separate ephemeral conversation correctly identified AIDemo and its path without
starting a task or changing the selected project. Registered local projects remain
distinct from ChatGPT cloud projects and remote-host workspaces.

## Existing-chat delivery repair — 2026-11-09, Europe/Istanbul

Follow-up: frontend prompts now contain only the owner's requested text, with no
JARVIS marker, role label or execution boundary appended. Local request IDs and
prompt hashes remain in receipts. Verification compares transcript occurrence
counts before/after Send and reconciles only new history turns, so an earlier
identical request cannot confirm a later send. The 124-test suite passes. Live
service task `706ce5a0b75a` sent the plain diagnostic prompt and received
`CLEAN-PROMPT-VERIFIED`; the persisted user message exactly matched the requested
text (apart from Codex's trailing newline). This supersedes the marker behavior
described in the historical validation below.

The earlier app-server repair described below was insufficient for desktop-owned
chats. It is superseded by the native interface transport validated here:

- Root failure reproduced in production logs: `already has an active writer`.
  JARVIS's separate app-server could not resume a chat owned by the desktop.
- Default existing-chat delivery now uses Windows UI Automation, the visible
  composer and Send. Metadata/history remain read-only; no target resume/turn
  request is used for submission. The stale Windows protocol association is avoided.
- Existing fixture chat `01a1bd89-a7d7-7d61-a835-93b96ba6ebbf` received both a
  local service `/send` command and a natural-language request routed through
  the same supervisor used by voice. The actual requests and exact replies
  `FRONTEND-INTEGRATED-VERIFIED-C` and `FRONTEND-VOICE-ROUTING-VERIFIED-D` were
  observed in the native interface screenshot and persisted desktop history.
- Live tasks `92335cb93b43` and `416ebc2c04a3` record interface echo receipts and
  native turn IDs. The scheduler reconciles replies without acquiring a writer.
  A reader can briefly reconstruct an active desktop turn as interrupted; that
  observation is treated as unconfirmed until the actual completed reply persists.
- Native command search opened AIDemo's supervisor and ShopWise's existing chat;
  Copy chat link confirmed each exact UUID. These were navigation-only checks.
- Unicode and a registered binary clipboard format both survived a real Copy chat
  link cycle; the prior clipboard was restored. Existing-draft, wrong-chat, Stop
  button, uncertain-send, no-backend-fallback and status reconciliation regression
  checks are covered by the 122-test suite. `pip check` passes.

These checks exercised text commands and the voice supervisor's natural-language
route. A new Telegram audio call was not placed during this repair. Codex must be
open on an unlocked interactive Windows desktop. This validation does not assert
that every future desktop UI version will preserve the same accessibility controls.

Historical app-server-only repair (retained for context):

The conversational router previously had no existing-chat send/select action, and
the legacy handoff path required a watch entry that did not exist. Existing chats
were also renamed by the generic visible-worker path. These application defects
were fixed without loosening code-edit approval or subscription-only execution.

Voice/text routing now resolves exact chats, resumes the same thread with a
task-scoped owner grant and preserves its title. `/chat`, `/send` and the legacy
`/handoff` path share this behavior. Receipts wait for actual turn/steer acceptance;
pending/unknown outcomes are distinguished, persisted and not replayed. Internal
conversation planning now uses ephemeral threads. Fifteen new regression tests
cover selection/routing, no watch prerequisite, no new worker replacement,
ambiguity/busy refusal, accepted receipts, active-worker steering, title retention,
project switching and uncertain/restarted sends; 110 tests pass overall.

A real persistent diagnostic chat was seeded, its client closed, and a separate
supervisor connection resolved its title through the conversational router and
sent a second prompt into that exact thread. Both turns persisted, the original
title remained unchanged and the second reply contained
`DELIVERY-FOLLOWUP-RECEIVED`. The desktop `read_thread` connector also returned both
turns and that marker. This was a harmless fixture; no business chat, code file,
reservation or calendar item was changed by the live test. Evidence is saved in
`.runtime/diagnostics/chat-delivery-fixture/evidence.json`. Simultaneous sending
from independent Codex clients remains outside the ownership guarantee.

## Automated coverage

- Wrong, expired, stale, rejected, duplicate and replayed file approvals.
- Path traversal, reserved Windows paths, hardlinks and case-insensitive collisions.
- Validation of every file before the first write, and approved content readback.
- Owner-only calls, disabled calling, quiet hours, single-call gate, frequency limits.
- No automatic redial and restart reconciliation of calls/tasks.
- PCM resampling across chunk boundaries, bounded playback and interruption clearing.
- Coding → independent review → owner approval; reviewer concerns and clarification.
- Refusal of Codex API-key billing, direct model file/command grants and active-chat handoff.
- Call hangup does not cancel workers; repeated messages do not execute twice.
- Voice cannot approve edits; cloud voice cannot open without free-tier confirmation.
- Verification commands need exact approval, use scoped execution, and cannot replay.
- Ambiguous command outcomes remain uncertain and cancelled tasks cannot execute.

## Required live acceptance checklist

Record software versions, date, observed result and owner confirmation for each:

1. Install the isolated Python 3.12 environment; run `pip check`, import probes and
   the complete offline suite. Confirm no global package/config changes.
2. Confirm native Windows sandbox readiness and a real read-only worker. Attempted
   unapproved file writes and tool permission escalation must be rejected.
3. Configure and authenticate the spare Telegram account; verify foreign-account
   messages/calls cannot enter the command or media path.
4. With automatic calls off, explicitly request one short private call. Confirm
   intelligible inbound and outbound speech by listening, not only byte counters.
5. Interrupt at the start/middle/end of assistant speech and measure audible cutoff.
   Target under 500 ms. Measure ordinary response onset; target about two seconds
   in cloud mode and report local mode separately.
6. Confirm Gemini free-tier account/project state outside JARVIS, then exercise a
   non-sensitive conversation, delegation and quota/unavailability fallback.
7. Validate whisper.cpp/Piper on the actual laptop, including noise/echo, CPU/RAM,
   thermal behavior and sustained utterances. Unusable local voice must preserve text.
8. Run a bounded coding task, reject one proposal, approve another, and approve a
   specific verification command. Verify exact files, outcomes and reported status.
9. Monitor one selected existing chat; validate waiting flags and resolved blockers.
   Hand over only an idle chat and verify read-only settings and absence of competing
   desktop turns. Cross-client continuation is not established by the listing probe.
10. Enable automatic calls deliberately and create a harmless test blocker. Verify
    quiet hours, coalescing, frequency limits, no redial and resolution before dialing.
11. Test remote hangup, network loss, native child crash, provider failure, service
    restart and Windows sleep/wake. No stale operation may be repeated automatically.
12. Only after foreground checks pass, review/register login startup; verify tray
    controls, single-instance behavior, non-elevated execution and removal.

Do not mark the release operational until these checks pass. No live latency,
speech quality, transport reliability, or free-tier eligibility is claimed yet.
