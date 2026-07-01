# Headroom Desktop — Cloud (Supabase)

Cloud backend skeleton for the Headroom Desktop SaaS. The desktop app does all
LLM proxying **locally**; this backend does only:

- **Auth** (Supabase Auth; `profiles` auto-provisioned by trigger)
- **Conversation sync** (`devices`, `conversations`, `messages` — append-only,
  tombstone deletes, client-generated ULID ids stored as `text`)
- **Stripe billing** (`subscriptions` mirror + `create-checkout`,
  `billing-portal`, `stripe-webhook` edge functions)
- **Aggregate savings telemetry** (`savings_daily` — strictly numeric
  counters, enforced server-side by `report-savings`; prompt content never
  leaves the user's machine except as their own synced conversation rows)

## Layout

```
cloud/
  supabase/
    config.toml            # CLI config (verify_jwt=false for stripe-webhook)
    migrations/            # 0001..0008, plain SQL, Postgres 15
    functions/
      _shared/             # stripe client, supabase clients, CORS helpers
      create-checkout/
      billing-portal/
      stripe-webhook/      # deploy with --no-verify-jwt
      report-savings/
  tests/
    rls.test.sql           # transactional RLS assertions (always rolls back)
```

## One-time setup

```bash
# From the repo root (config lives in cloud/supabase):
cd cloud

supabase login                       # or export SUPABASE_ACCESS_TOKEN
supabase link --project-ref <PROJECT_REF>
```

## Deploy database

```bash
cd cloud
supabase db push                     # applies migrations/0001..0008
```

## Secrets (edge functions)

```bash
cd cloud
supabase secrets set \
  STRIPE_SECRET_KEY=sk_live_... \
  STRIPE_WEBHOOK_SECRET=whsec_... \
  STRIPE_PRICE_ID=price_... \
  SITE_URL=https://app.headroom.example   # optional; checkout redirect base
```

`SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `SUPABASE_SERVICE_ROLE_KEY` are
injected automatically by the platform — do not set them yourself.

## Deploy edge functions

```bash
cd cloud
supabase functions deploy create-checkout
supabase functions deploy billing-portal
supabase functions deploy report-savings
# Stripe calls this one directly; it authenticates via webhook signature:
supabase functions deploy stripe-webhook --no-verify-jwt
```

`config.toml` also sets `[functions.stripe-webhook] verify_jwt = false` so
local `supabase functions serve` behaves the same way.

## Stripe wiring

1. Create a recurring Price in Stripe; put its id in `STRIPE_PRICE_ID`.
2. Add a webhook endpoint in the Stripe dashboard pointing at
   `https://<PROJECT_REF>.functions.supabase.co/stripe-webhook`
   with events: `checkout.session.completed`,
   `customer.subscription.updated`, `customer.subscription.deleted`.
3. Put the endpoint's signing secret in `STRIPE_WEBHOOK_SECRET`.

### Local development

```bash
cd cloud
supabase functions serve             # requires Docker for the edge runtime

# In another terminal, forward Stripe events to the local function:
stripe listen --forward-to http://127.0.0.1:54321/functions/v1/stripe-webhook
# Use the whsec_... printed by `stripe listen` as STRIPE_WEBHOOK_SECRET
# (supabase functions serve reads it from ./supabase/functions/.env or
#  `supabase secrets set` on the linked project).

stripe trigger checkout.session.completed   # smoke-test the handler
```

## RLS tests

`cloud/tests/rls.test.sql` is self-contained: it seeds two users, asserts
isolation / append-only / read-only invariants, and **always rolls back**.

```bash
# Local stack (after `supabase db reset`):
psql "postgresql://postgres:postgres@127.0.0.1:54322/postgres" \
     -v ON_ERROR_STOP=1 -f tests/rls.test.sql

# Hosted (use the direct connection string, not the pooler):
psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f tests/rls.test.sql
```

Success = exit code 0 and a final `NOTICE: ALL RLS TESTS PASSED`.

## CI/CD

`.github/workflows/cloud-deploy.yml` pushes migrations and deploys functions
on pushes to `main` that touch `cloud/**`. It requires these repo secrets and
skips itself cleanly when they are absent (e.g. on forks):

- `SUPABASE_ACCESS_TOKEN` — personal access token for the CLI
- `SUPABASE_PROJECT_REF`  — the project ref used by `supabase link`
- `SUPABASE_DB_PASSWORD`  — database password for `db push`

## Data-model decisions

- `conversations.id`, `messages.id`, `devices.id` are **`text`** primary keys:
  the desktop app generates ULIDs client-side (offline-first sync).
  `user_id` stays `uuid` referencing `auth.users`.
- `messages` is append-only at the policy level (no UPDATE/DELETE policies);
  conversation deletion is a tombstone (`conversations.deleted_at`).
- `subscriptions` is written only by the service role (webhook/checkout
  functions); clients can read their own row to gate features.
- `savings_daily` accumulates via the `report_savings` SECURITY INVOKER RPC,
  so RLS still applies to telemetry writes.
