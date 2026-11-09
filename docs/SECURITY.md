# Security boundaries

## Credentials and identity

Only the configured numeric Telegram owner ID is accepted, and only private
incoming messages are treated as owner commands. Commands are deduplicated by
account/chat/message identity. Credentials and the spare account session use
Windows DPAPI. State directories remove inherited ACLs and grant the current user
and System full access. Credential values are never command arguments.

JARVIS is a personal application, not a sandbox against other programs running as
the same Windows user. Those programs can act as that user and access DPAPI.
Protect the Windows account and do not share the state directory.

## Project changes

Workers start with read-only filesystem permissions, explicit user review routing,
disabled configured external MCP servers/plugins/hooks, and no API-key billing.
The adapter verifies the server's returned permissions before worker use. It never
grants a model-generated permission request. Unsupported permission profiles fail
closed; global Codex config is not changed.

The model returns file replacements, not executable patch commands. Approval binds
the project, relative paths, original SHA256 values, replacement text, and expiry.
The broker rejects path traversal, Windows device/ADS names, sensitive VCS paths,
links, junctions, hardlinks, duplicate paths and stale content. Each replacement
uses an atomic rename and readback. Approval applies once and cannot be granted by
the voice provider. Cancelled tasks cannot apply pending proposals.

Multi-file application is **not one filesystem transaction**. A crash/failure can
leave some approved files applied. The proposal is then uncertain, records the
known applied files when possible, and is never replayed automatically. Inspect
and create a new proposal for remaining changes. Concurrent external editors can
still race filesystem checks; avoid editing a project during application.

## Verification commands

A command requires a separate owner-approved exact argv/cwd/scope digest. It uses
the Codex `command/exec` sandbox with project writes permitted and network and
outside-project writes denied. No raw shell concatenation is used. Timeouts and
ambiguous results are never automatically retried. A zero exit status verifies
only the selected command; it does not prove all task requirements.

## Existing chats

Discovery and monitoring only read metadata/history. Existing-chat sends default
to native Windows UI Automation: verify the chat UUID using Copy chat link, preserve
drafts, paste the owner's request and invoke exactly one enabled Send control.
The helper accepts only the installed OpenAI.Codex desktop executable, checks
foreground focus and serializes input. Clipboard formats are copied/restored in
memory, without persisting their contents. A newer user clipboard entry is retained.
No target thread/resume, turn/start, private app state patch or permission change
occurs. Delivery requires a new occurrence of the prompt in the rendered
conversation and an empty composer. The sender adds no tracking markers, role
labels or execution instructions to the owner's prompt. History reconciliation
compares the prompt hash against turns absent from the pre-send baseline; legacy
marker-based receipts remain readable for tasks submitted before this change.

Frontend execution inherits the desktop chat's actual permissions and instructions.
JARVIS does not weaken or silently change the desktop's permissions. Review them
in Codex before delegating sensitive work. The separate legacy app-server transport
remains an explicit configuration option; it is never an automatic fallback.
Unknown acceptance after a timeout/restart blocks replay. History observations
report Codex responses separately from independently executed verification results.

## Voice and cost

Cloud voice receives audio and compact task context. Gemini requires explicit
confirmation of an account/project with no billing. JARVIS cannot independently
inspect Google billing from an API key and cannot guarantee that an externally
reconfigured provider project remains free. It does not automatically add billing,
purchase credits, or switch to paid voice providers.

Codex requires ChatGPT authentication before model turns. Subscription allowances
and existing credits may be consumed. Environment API keys are removed from the
worker process and API-key accounts are refused. No hidden API proxy is used.

Local speech is not fully offline intelligence: Codex still handles reasoning.
Local mode uses a simple energy-based VAD, so speakerphone echo and background noise
need real-device testing. It is a degraded fallback, not a promise of human-like
full-duplex conversation on this laptop.

## Failure handling and data

Native calling runs in a separate process, with stderr drained and bounded audio
queues. Faults hang up rather than redial. Parent shutdown terminates a stuck
transport process. Worker tasks continue across call hangup, but service shutdown
interrupts them and restart requires an explicit resume.

Raw audio is not archived. Local speech creates temporary audio/text files, removed
normally but potentially left by a power loss. SQLite events contain redacted
summaries and approval records can contain full proposed source. Redaction is a
defense in depth, not a guarantee that arbitrary source or prose contains no secret.
Review before sending source into any model or Telegram preview.

