# Account setup on your computer

The JARVIS secure setup window accepts account details locally and masks secret
fields. Do not send API hashes, API keys, login codes, or passwords in the chat.

## Telegram app credentials

1. Open [Telegram API development tools](https://my.telegram.org/apps) using the
   **Get Telegram app credentials** button in JARVIS.
2. Sign in with a Telegram account you own. Telegram normally sends the login code
   in the Telegram app. Complete that authentication yourself.
3. Choose **API development tools**. If no application exists, create one using
   app title `JARVIS`, a short name such as `jarvispersonal2026`, platform `Desktop`,
   and a description such as `Personal coding assistant`. Leave optional URL fields
   blank unless Telegram requires one.
4. Copy **App api_id** into **Telegram app API ID**, and **App api_hash** into
   **Telegram app API hash** in the local JARVIS window.
5. In the owner field, enter your **personal** Telegram `@username` or numeric user
   ID. This is the account JARVIS may answer or call.
6. Click **Log in spare Telegram account**. Enter the **spare** account's phone
   number, new login code, and any 2FA password in the local prompts. The owner and
   spare accounts must be different. A BotFather bot token is not a substitute.

An application API ID/hash identifies the Telegram API application. The login in
step 6 decides which user account operates the calling bridge. You do not need to
publish an app or buy Telegram Premium for this setup.

After successful login, open **Controls → Start / restart** to load the saved
configuration. Calls remain disabled until the controlled connection test.

Reference: [Telegram's API ID instructions](https://core.telegram.org/api/obtaining_api_id).

## Gemini free-tier voice (optional)

1. Click **Get a Gemini API key** to open [Google AI Studio](https://aistudio.google.com/apikey).
2. Sign in with your Google account and complete any required account eligibility
   steps yourself.
3. Create an API key in a project with **no billing account attached**. Do not add a
   payment method, activate paid billing, or select an already billed project.
4. Paste the key into the masked **Gemini API key** field in JARVIS.
5. Check the free-project confirmation only after verifying that project state, then
   click **Save settings** and restart JARVIS from Controls.

The key belongs to the API project; a consumer Gemini subscription is not required
by JARVIS. Free-model access and quotas depend on Google's availability for the
account and region. JARVIS cannot determine billing state from a key alone.

If no suitable free tier is available, leave the key blank and the confirmation
unchecked. The installed local speech fallback can be tested with Telegram once
the Telegram login is complete. It is slower than cloud voice.

References: [Gemini API keys](https://ai.google.dev/gemini-api/docs/api-key),
[pricing/free-tier information](https://ai.google.dev/gemini-api/docs/pricing).
