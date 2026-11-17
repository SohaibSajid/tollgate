# Client portal: self-service accounts

Businesses that want to use Tollgate sign up themselves at **`<your Tollgate address>/portal`**, bring their own AI provider key and create their own API keys. The operator only approves accounts and watches usage.

## For a client business

| step | where | what happens |
|---|---|---|
| 1. Create an account | `/portal` → Create an account | Business name, email, password (at least 10 characters). A customer wallet is created for you. |
| 2. Wait for approval | — | With the default sign-up mode the operator approves new accounts. Sign-in shows "waiting for approval" until then. |
| 3. Add your provider key | Portal → Your provider key | Save your own Gemini, OpenAI or Anthropic key. It is stored encrypted; only the last 4 characters are shown again. Your requests use this key, so the provider bills **your** account. |
| 4. Create an API key | Portal → API keys | A `tg_` key, shown once. Store it on your server only, never in a mobile app or website. |
| 5. Integrate | Portal → Integrate | Base URL and a ready example. Send a new `Idempotency-Key` per message and your user's ID in `X-Tollgate-End-User`. Details: [API_GUIDE.md](API_GUIDE.md), [integrations/INTEGRATION_MODES.md](integrations/INTEGRATION_MODES.md), [integrations/CLIENT_PROMPT.md](integrations/CLIENT_PROMPT.md). |
| 6. Watch usage | Portal → Requests & users | Every request with its end user, whose key paid, state and tokens, plus totals per end user. |

**Fail-open:** if Tollgate cannot be reached, your backend may call the provider directly with your own key (mode 3 in [INTEGRATION_MODES.md](integrations/INTEGRATION_MODES.md)). Those calls are not tracked by Tollgate.

## For the operator

| setting (`gateway/.env`) | values | meaning |
|---|---|---|
| `TOLLGATE_SIGNUP_MODE` | `approval` (default), `open`, `closed` | Whether new accounts wait for approval, can sign in at once, or cannot sign up |
| `TOLLGATE_PROVIDER_KEY_POLICY` | `customer_first` (default), `customer_only` | `customer_only` refuses any client without its own provider key, so the operator never pays a provider |
| `TOLLGATE_CREDENTIALS_KEY` | created by `python manage.py credentials-key` | Encrypts saved provider keys. Back it up with the database; the standby relay needs the same value |

Dashboard → Customers & keys → **Client accounts**: approve, suspend or reactivate. Suspending signs the account out of the portal; its `tg_` keys keep working until you revoke them or freeze the customer. A new account starts with a $0 budget: free-tier Gemini needs none; for priced models grant a spending budget under **Grant credits**.

## Security

- Passwords are hashed with scrypt and a per-account salt. Login errors do not reveal whether an email exists.
- Portal sessions are HttpOnly, SameSite=Strict cookies that expire after `TOLLGATE_SESSION_HOURS`. Every change needs the session's CSRF token and the configured Origin.
- Sign-up and login are rate limited per IP address; portal actions per account.
- Accounts only ever see and change their own keys, provider keys and requests.

## Not included yet

Email verification, password reset and two-factor login need an email service. Until then the operator approves accounts by hand, and a client who forgets a password asks the operator to suspend the old account and signs up again.
