-- 0007_feature_flags.sql
-- Server-controlled feature flags, readable by everyone, writable by nobody
-- except the service role / dashboard.

create table public.feature_flags (
    key text primary key,
    enabled boolean not null default false,
    payload jsonb,
    updated_at timestamptz not null default now()
);

comment on table public.feature_flags is
    'Read-only (for clients) feature flags. Writes only via service role.';

-- Seed flags.
insert into public.feature_flags (key, enabled)
values ('chatgpt_oauth', false)
on conflict (key) do nothing;

-- Tighten table-level grants: Supabase grants broad privileges on public
-- tables by default; flags must be SELECT-only for client roles.
revoke all on table public.feature_flags from anon;
revoke all on table public.feature_flags from authenticated;
grant select on table public.feature_flags to anon;
grant select on table public.feature_flags to authenticated;
