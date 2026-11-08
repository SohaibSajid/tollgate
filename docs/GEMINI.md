# Test Tollgate with Gemini on Windows 11

This integration sends real text requests to **Gemini 3.5 Flash-Lite**. It uses Google's free-tier rates: input and output cost $0. Google lists this model on its Free tier as of **22 October 2026**. Your project's quotas and access still apply.

Tollgate cannot determine your Google project's billing tier from its API key. **Use a project that shows Free tier in Google AI Studio, and do not enable billing for this test.** A paid project can incur Google charges even though this configuration records $0 in Tollgate. Paid Gemini pricing is intentionally unsupported by this adapter.

## 1. Update your existing project

Stop the running gateway with **Ctrl+C**. Keep a backup of your current project. Replace the following files under `C:\FYP\FYP\gateway`, preserving the folder names:

| File under gateway | Action | Purpose |
|---|---|---|
| `manage.py` | Replace | Save Gemini credentials and select active providers |
| `models.json` | Replace | Register the reviewed Gemini model and free rates |
| `.env.example` | Replace | Add the new configuration defaults |
| `app/config.py` | Replace | Read Gemini settings |
| `app/main.py` | Replace | Apply the model's usage limits |
| `app/routing/providers.py` | Replace | Expose only enabled, configured providers |
| `app/services/live_provider.py` | Replace | Route Gemini requests to its adapter |
| `app/services/gemini_provider.py` | Add | Count input, stream Gemini text, and normalize final usage |
| `app/ui/dashboard.js` | Replace | Show free-tier rates and guide zero-balance testing |
| `app/ui/dashboard.html` | Replace | Update the customer instructions and retain the feature-tag fix |

Keep your existing **`.env`**, databases, and virtual environment. `.env.example` is only a template. No new runtime dependency is required. The optional `tests/test_gemini.py` adds the Gemini verification suite.

For a fresh installation, the current `install_tollgate.py` creates the complete project in a new or empty directory. It refuses to overwrite an existing project. Install `gateway/requirements.txt` in that new project's Python 3.12 virtual environment before continuing.

## 2. Create a Google API key

Open [Google AI Studio's API keys page](https://aistudio.google.com/apikey). Sign in and create a key in a project whose billing tier is **Free**. You may need to create or import a project first. Use the newly created key rather than an old unrestricted key.

Check that the project remains on Free tier. Do not select an upgrade or attach billing. Use ordinary test prompts: Google's free-tier terms allow content to be used to improve its products.

## 3. Configure Tollgate

In PowerShell, run:

```powershell
cd C:\FYP\FYP\gateway

$env:TOLLGATE_MODE = "live"
$env:TOLLGATE_ENABLED_PROVIDERS = '["gemini"]'
$env:TOLLGATE_DATABASE_PATH = "data/gemini-test.db"

.\.venv\Scripts\python.exe manage.py setup --mode live --providers gemini --database-path data/gemini-test.db
.\.venv\Scripts\python.exe manage.py set-provider gemini
```

The three environment settings override any simulator settings left in this terminal. `setup` also saves these choices in `.env` for future terminals. Gemini is the only enabled provider, even if an OpenAI key is still saved. Tollgate never falls back to another provider.

At **Provider key (hidden input)**, paste your **Google Gemini API key**, then press Enter. No characters are displayed while entering it; that is expected. Keep this key in your terminal, not in chat or the Playground.

Continue:

```powershell
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py doctor
.\.venv\Scripts\python.exe manage.py admin-key
.\.venv\Scripts\python.exe manage.py serve
```

`doctor` should list `gemini-3.5-flash-lite`; it checks local configuration, not Google's credentials or billing. `admin-key` displays the **Tollgate owner key** for dashboard sign-in. It is different from the Gemini key. Your existing owner key is preserved by setup.

The new `data/gemini-test.db` keeps this experiment separate from your old balances. Your existing database is not deleted. You do not need to run the simulator server.

## 4. Send one small request

1. Open [Tollgate](http://127.0.0.1:8000/). Press **Ctrl+F5** to load the updated dashboard.
2. Sign in with the owner key. The badge should read **GEMINI · FREE-TIER RATES**.
3. Open **Customers & keys**, create a customer named `Gemini Test`, and skip **Grant credits**.
4. Issue a virtual key allowing `gemini-3.5-flash-lite`. Copy the generated `tg_...` key. Tollgate also fills it into the Playground.
5. Open **Playground**, select that model, keep **Maximum output tokens** at **256**, and leave streaming enabled.
6. Send: `Say hello and explain an API gateway in one short sentence.`
7. After receiving the answer, click **Inspect request status**. If the worker has not finished yet, inspect again after a moment.

Expected result:

- A real Gemini answer appears incrementally.
- Final input and output token counts are recorded from Google's usage metadata.
- The request reaches `settled` with `cost_usd: "0.000000"`.
- The customer's balance remains $0, with no payment or credit grant required.

This validates the real-provider connection and zero-cost accounting. Use the simulator's nonzero test prices to demonstrate deductions, insufficient balance, and simultaneous requests competing for credits. Free Gemini requests cannot demonstrate a nonzero bill.

## 5. Understand errors without spending money

| Result | What to do |
|---|---|
| `gemini_quota_exhausted` / HTTP 429 | Check your active limits in AI Studio. Wait for the applicable limit to reset; do not upgrade billing for this test. |
| Authentication or access error | Check the key, its project, API restrictions, model access, and supported region. Restart after saving a replacement key. |
| Gemini is missing from the model list | Confirm that all updated files are in place, run setup and set-provider again, then restart. |
| `price_review_required` | Review Google's current free-tier availability before updating `reviewed_on` and `price_version` in `models.json`. The default review interval is 30 days. |
| `unresolved` after interrupted generation | The final usage is unknown. This adapter has no automatic Gemini history recovery. Keep the record for review; do not invent usage or repeatedly click Send. Its free-tier reservation is $0. |

## What the code does

1. Validates the customer key, model permission, and text request.
2. Calls Gemini `countTokens` with the same conversation and system instruction used for generation. Inputs above Tollgate's 40,000-token limit are rejected.
3. Atomically creates the request and its $0 reservation, with the saved price version.
4. Sends one native Gemini streaming request with minimal thinking and the requested output limit.
5. Forwards text and waits for a completed stream with final usage. Cumulative usage is replaced, never added across chunks. Reported thinking tokens are included in output once; thought content is not forwarded.
6. Saves final usage and a durable job together. The worker records one $0 ledger charge and finishes the request.

Google's maximum reported output allowance is retained for validating thinking plus visible tokens; the user's requested generation limit is still sent upstream. This allowance is enabled only for the reviewed free-rate model. Text chat is supported; tools, images, audio, caching requests, and paid Gemini pricing are outside this update.

## Verification and sources

The update passed mocked Gemini protocol tests and the existing local accounting tests. These tests do not contact Google and cannot prove that your key or project is eligible. The short request above is your live acceptance test. See `VALIDATION.md` for the exact checks and remaining deployment gates.

Official sources reviewed on 22 October 2026:

- [Google pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini 3.5 Flash-Lite limits](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)
- [API keys](https://ai.google.dev/gemini-api/docs/api-key)
- [Token counting reference](https://ai.google.dev/api/tokens)
- [Streaming and final usage reference](https://ai.google.dev/api/generate-content)
- [Thinking configuration](https://ai.google.dev/gemini-api/docs/thinking)
- [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
