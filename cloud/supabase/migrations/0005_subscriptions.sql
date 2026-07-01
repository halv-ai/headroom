-- 0005_subscriptions.sql
-- Stripe subscription state, one row per user.
-- Written exclusively by the service role (stripe-webhook / create-checkout
-- edge functions); clients get read-only access via RLS in 0008.

create table public.subscriptions (
    user_id uuid primary key references auth.users (id) on delete cascade,
    stripe_customer_id text unique,
    stripe_subscription_id text,
    status text,
    price_id text,
    current_period_end timestamptz,
    updated_at timestamptz not null default now()
);

comment on table public.subscriptions is
    'Stripe billing state mirror. Writes come only from edge functions using the service role.';

create index subscriptions_stripe_subscription_id_idx
    on public.subscriptions (stripe_subscription_id);
