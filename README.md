# JARVIS

A Windows-native Telegram voice assistant and Codex supervisor. It uses your existing
Codex subscription, a free-tier Gemini voice project, and optional local speech.
Project edits require an exact, owner-approved proposal.

**Status:** application code and offline tests are implemented. Codex discovery has
been checked on the installed desktop runtime. Telegram media, Gemini access, local
speech, and existing-chat handoff require live validation before operational use.
Nothing installs, logs in, registers startup, or places a call merely by importing
the package or running `doctor`.

## What is included

- SQLite task state, restart recovery, command deduplication, selected-chat monitoring.
- Separate read-only coding and review workers; one worker by default, maximum two.
- Owner-reviewed file replacements bound to original file hashes and exact content.
- Separately approved verification commands, limited by the Codex project sandbox.
- Incoming, requested outgoing, and automatic blocker calls with quiet hours and limits.
- Isolated NTgCalls/PyTgCalls transport and raw PCM audio; no virtual audio devices.
- Gemini Live voice, local whisper.cpp/Piper fallback, interruption-aware playback.
- Windows DPAPI credentials, tray controls, and an explicit login-startup installer.

See [validation evidence](docs/VALIDATION.md), [security and limits](docs/SECURITY.md),
and [architecture](docs/IMPLEMENTATION.md).

## Offline checks

The core and tests need only Python 3.12 or newer; no package installation is needed:

```powershell
python -B -m jarvis doctor
python -B -m unittest discover -s tests -v
```

The dependency lock targets **Windows x64 / Python 3.12**. Tests on a newer Python
version do not validate the native calling dependencies on that version.

The optional Codex probe starts a restricted local app-server, reads account type
and chat-list metadata, and closes it. It does not start a model turn:

```powershell
python -B -m jarvis probe-codex --authorize-local-server
```

## Live environment setup — explicit installation step

Use a normal user PowerShell in this repository after approving installation.
If Python 3.12 is missing, install the official user-scoped Python 3.12 distribution
first; do not replace existing Python installations.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-build-win-py312.lock
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --require-hashes -r requirements-win-py312.lock
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-frontend-win-py312.lock
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

PyAES 1.6.1 is the one source-only dependency. Its source archive is hash-pinned;
building it uses the separately pinned build tools. All other selected artifacts are
wheels. The locks were resolved from public metadata without executing package code.
They still need installation-time validation. To regenerate deliberately:

```powershell
python -B scripts/lock_dependencies.py --authorize-downloads
```

Run from the checkout rather than installing JARVIS globally.

## Configure and authenticate

Copy `config.example.toml` to the gitignored `config.local.toml`. Set `owner_id` to
your personal account's numeric Telegram ID and `telegram_api_id` to the app ID for
the spare account. Keep calls, automatic calls and cloud voice disabled initially.

Store secrets using hidden prompts, never chat messages or command-line arguments:

```powershell
.\.venv\Scripts\python.exe -m jarvis secret set telegram_api_hash
.\.venv\Scripts\python.exe -m jarvis secret set gemini_api_key
.\.venv\Scripts\python.exe -m jarvis login-telegram --authorize-login
```

Login contacts Telegram and asks for the **spare** account's phone, code, and any 2FA
password. The session is encrypted with Windows DPAPI. The owner account cannot be
used as both sides of a call.

Use a Gemini API project **without attached billing** and confirm that its free tier
provides `gemini-3.8-live` to your account. Only then set
`free_cloud_confirmed = true`. This boolean is your attestation, not automatic proof
of the provider's billing/account state. JARVIS has no paid provider fallback.

For local fallback, obtain reviewed Windows whisper.cpp and Piper binaries and the
`base.en` recognition and `en_US-lessac-medium` synthesis models. Set their executable
and model paths in the config. No models are automatically downloaded. A missing
local engine leaves text control available and ends an unusable call.

## Start and validate

```powershell
.\.venv\Scripts\python.exe -m jarvis serve
```

With calls disabled, verify Telegram text control, Codex read-only permissions, and
the proposal workflow first. `/help` lists commands. No automatic call should be
enabled before the controlled call checks in `docs/VALIDATION.md` pass.

After authorizing one live-call test, set `calls_enabled = true`, restart, and send
`/call` from the owner account. This permits incoming calls from that owner too.
Validate speech in both directions and interruptions before enabling
`automatic_calls = true`.

Default automatic policy: blockers wait 60 seconds; quiet hours 23:00–09:00 Istanbul;
maximum two attempts/hour and six/day; no automatic redial. Routine completion uses
Telegram text. Calls end after 15 minutes; `/extend 300` adds five minutes up to a
one-hour hard cap. Hanging up leaves coding tasks running.

## Owner controls

| Command | Behavior |
|---|---|
| `/status` | Task facts, voice state, selected chats |
| `/project C:\path\project` | Select the project for spoken task requests |
| `/task C:\path\project \| instruction` | Start a read-only coding task |
| `/run TASK` | Explicitly resume an idle task |
| `/answer TASK clarification` | Record an answer or steer an active turn |
| `/cancel TASK` | Cancel work; independent of hangup |
| `/chats [CURSOR]` | List existing Codex chats with pagination |
| `/watch THREAD` / `/unwatch THREAD` | Select or remove monitoring |
| `/handoff THREAD instruction` | Explicitly continue an idle, selected chat if permissions can be verified |
| `/approvals` | List outstanding edit proposals |
| `/preview PROPOSAL` | Full diff and digest; oversized diffs require local review |
| `/approve PROPOSAL SHA256` | Apply exactly the reviewed content once |
| `/reject PROPOSAL` | Reject without editing the project |
| `/verify TASK \| ["python", "-m", "unittest"]` | Propose an exact verification command and scope |
| `/approve-command ID SHA256` | Execute that command within the project sandbox |
| `/call`, `/hangup`, `/extend SECONDS` | Call controls |
| `/quiet on` / `/quiet off` | Additional quiet override; scheduled quiet hours still apply |
| `/stop` | Stop JARVIS |

The same owner commands can be submitted locally, without a public HTTP server:

```powershell
.\.venv\Scripts\python.exe -m jarvis send '/status'
```

For large proposals, review and approve locally:

```powershell
.\.venv\Scripts\python.exe -m jarvis preview PROPOSAL
.\.venv\Scripts\python.exe -m jarvis approve PROPOSAL EXACT_SHA256
```

Voice can discuss tasks, start read-only work in the selected project, answer
questions, cancel a task, and end a call. **Voice cannot approve edits or verification
commands.** Those require an owner Telegram text command or local action.

## Windows login startup

First validate foreground operation and install the tray dependencies. Review the
exact task before registering it:

```powershell
.\.venv\Scripts\python.exe -m jarvis startup preview
.\.venv\Scripts\python.exe -m jarvis startup install --authorize-startup-change
```

This creates a per-user, non-elevated Scheduled Task, uses `pythonw.exe` when
available, and launches the tray controller. It does not force wake or alter sleep
settings. Remove it with `startup remove --authorize-startup-change`.

The service needs the laptop awake, online and signed in. Keep it plugged in for
long sessions. The tray can send status, call, hang up, set quiet mode, or stop.

## Recovery and retention

State is under `%LOCALAPPDATA%\JARVIS`, protected by owner/System ACLs. On restart,
in-flight tasks require inspection; ambiguous edits/commands are marked uncertain;
calls and commands are never blindly replayed. No automatic retry loop exists.

`purge` applies retention (seven days for events/inbox and 30 for terminal task and
approval history). `purge --all-history` explicitly removes history while keeping
credentials; stop active work first. Audio is not archived. Local speech engines use
temporary WAV/text files in the protected state directory, deleted on normal
completion; inspect that directory after a forced process/power failure.

## Personal assistant features

Browser research, appointments/reminders, Google Calendar authorization and sync,
reservation preparation/confirmation tracking, preferences, dictated notes,
document summaries, daily briefings, follow-ups and routines are implemented.
Use natural language by voice or Telegram, or `/help-personal` for exact commands.
Long operations return activity receipts and send their results to Telegram.

See [personal assistant setup and usage](docs/PERSONAL_ASSISTANT.md), especially
Google Calendar setup and the distinction between a prepared reservation and a
verified booking. Google account access and each real restaurant website still
need live acceptance; no real reservation was created during implementation.

Additional dependencies are installed/hash-locked separately in
`requirements-personal-win-py312.lock`. The browser uses installed Edge with a
separate persistent profile. Automatic blocker calling remains independently
controlled by existing settings.

## Visible Codex work

Project discovery uses Codex's registered local workspace metadata independently
of chats. Empty projects, custom sidebar names and multiple roots are preserved.
Historical chat folders are listed separately, so temporary work directories do
not become fake projects. `/projects` shows source/availability diagnostics;
`/projects refresh` refreshes them, and `/project AIDemo` selects a project by name.
The current runtime lacks a project-list API, so a read-only desktop metadata
adapter supplies the registry; configured roots are an explicitly labelled
fallback. JARVIS never edits Codex's settings or state to discover projects.

Chat listing includes all source kinds/providers and uses creation-order cursors.
Its bounded/partial history status does not restrict the registered project list.
Local project discovery does not imply access to ChatGPT cloud project files or
projects on another computer.

This installation uses `codex_task_mode = "visible"`: JARVIS automatically starts
an owner-requested task through Codex app-server and requests opening its named
desktop chat. The frontend receives the persisted prompt, worker command history
and replies; JARVIS does not simulate keystrokes. The app must be installed and its
`codex://` handler registered. An opening failure stops submission or interrupts an
already accepted turn; JARVIS never silently falls back to a hidden worker.

`/open TASK` reopens the linked chat. `/task-status TASK` includes its chat link,
worker command records and separately approved verification results. A finished
static review is not a passed test. Verification claims require recorded command
execution and an exit code; inspect the scope of each result.

To continue an existing chat, say its title and the prompt, or use:

```text
/chat Terra Supervisor — EMG Website
/send Review the current plan without editing files.
/send EXACT_CHAT_ID | Your follow-up prompt
```

An explicit send does not require `/watch`. Existing chat titles are preserved;
JARVIS navigates the desktop interface, checks the actual chat link, enters the
prompt in its composer and presses Send. It never acquires a backend writer for
these sends. Chat search also reaches chats hidden from the sidebar.
Ambiguous names require a specific title/ID. Follow-ups use the selected chat;
switching projects clears a selection from another project. `/handoff THREAD
instruction` remains an alias for sending to that existing chat.

With `existing_chat_transport = "interface"` (default), receipts report `sent: true`
only after a new occurrence of the exact prompt appears in that chat's transcript
and the composer clears. Only the owner's prompt is sent; tracking IDs and status
metadata stay in JARVIS. Read-only
history polling then records the native turn ID and response separately. Desktop
queueing follows Codex's own controls; JARVIS never presses Stop. The desktop must
be open in an unlocked interactive Windows session. Existing drafts are preserved,
and loss of focus stops input. Pending or uncertain sends are not automatically
replayed. Inspect `/task-status TASK` and the actual chat before using
`/chat-resolve TASK sent` or `/chat-resolve TASK not-sent`; resolution itself does
not resend anything. Internal conversational planning is ephemeral so it does not
create extra sidebar chats. New coding tasks remain visible and persistent.

Optional `frontend` mode prepares a composer draft and requires the owner to press
Send. It starts no background worker. `background` retains the previous headless
behavior. Conversation reasoning and audio processing remain background services
in all modes; the execution mode applies to coding task submission.

## Upstream references

- [Codex app-server](https://learn.chatgpt.com/docs/app-server)
- [Telegram calls](https://core.telegram.org/api/calls)
- [NTgCalls](https://github.com/pytgcalls/ntgcalls), [PyTgCalls](https://github.com/pytgcalls/pytgcalls)
- [Gemini Live](https://ai.google.dev/gemini-api/docs/live-guide)
- [whisper.cpp](https://github.com/ggml-org/whisper.cpp), [Piper](https://github.com/OHF-Voice/piper1-gpl)

Research references informed the architecture; JARVIS does not vendor those projects.
Review each dependency and model license before redistribution.

