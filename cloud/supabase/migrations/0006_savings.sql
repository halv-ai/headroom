-- 0006_savings.sql
-- Aggregate token/cost savings telemetry, one row per user per day.
-- Numbers only -- no prompt content ever lands here.

create table public.savings_daily (
    user_id uuid not null references auth.users (id) on delete cascade,
    day date not null,
    tokens_before bigint not null default 0,
    tokens_after bigint not null default 0,
    tokens_saved bigint not null default 0,
    usd_saved numeric(12, 4) not null default 0,
    requests int not null default 0,
    primary key (user_id, day)
);

comment on table public.savings_daily is
    'Aggregate savings telemetry. Numeric counters only; never prompt content.';

-- Atomic upsert-accumulate used by the report-savings edge function.
-- SECURITY INVOKER: runs as the calling (authenticated) role so the RLS
-- policies from 0008 still apply; auth.uid() comes from the caller JWT.
create or replace function public.report_savings(
    p_day date,
    p_tokens_before bigint,
    p_tokens_after bigint,
    p_tokens_saved bigint,
    p_requests int,
    p_usd_saved numeric default 0
)
returns void
language sql
security invoker
set search_path = ''
as $$
    insert into public.savings_daily
        (user_id, day, tokens_before, tokens_after, tokens_saved, usd_saved, requests)
    values
        (auth.uid(), p_day, p_tokens_before, p_tokens_after, p_tokens_saved, p_usd_saved, p_requests)
    on conflict (user_id, day) do update set
        tokens_before = savings_daily.tokens_before + excluded.tokens_before,
        tokens_after  = savings_daily.tokens_after  + excluded.tokens_after,
        tokens_saved  = savings_daily.tokens_saved  + excluded.tokens_saved,
        usd_saved     = savings_daily.usd_saved     + excluded.usd_saved,
        requests      = savings_daily.requests      + excluded.requests;
$$;

revoke all on function public.report_savings(date, bigint, bigint, bigint, int, numeric) from public;
revoke all on function public.report_savings(date, bigint, bigint, bigint, int, numeric) from anon;
grant execute on function public.report_savings(date, bigint, bigint, bigint, int, numeric) to authenticated;
grant execute on function public.report_savings(date, bigint, bigint, bigint, int, numeric) to service_role;
