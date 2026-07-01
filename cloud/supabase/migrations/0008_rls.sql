-- 0008_rls.sql
-- Row Level Security for every table in this schema.
--
-- Model:
--   * profiles / devices / conversations : select + insert + update own rows
--   * messages       : select + insert own rows ONLY (append-only; deletion
--                      happens via the conversations.deleted_at tombstone)
--   * subscriptions  : select own row only; all writes via service role
--   * savings_daily  : select + insert + update own rows
--   * feature_flags  : select for anon + authenticated; no client writes
--
-- The service role bypasses RLS entirely (Supabase default), so edge
-- functions using SUPABASE_SERVICE_ROLE_KEY are unaffected.

-- ---------------------------------------------------------------------------
-- profiles
-- ---------------------------------------------------------------------------
alter table public.profiles enable row level security;

create policy "profiles_select_own"
    on public.profiles for select
    to authenticated
    using (auth.uid() = id);

create policy "profiles_insert_own"
    on public.profiles for insert
    to authenticated
    with check (auth.uid() = id);

create policy "profiles_update_own"
    on public.profiles for update
    to authenticated
    using (auth.uid() = id)
    with check (auth.uid() = id);

-- ---------------------------------------------------------------------------
-- devices
-- ---------------------------------------------------------------------------
alter table public.devices enable row level security;

create policy "devices_select_own"
    on public.devices for select
    to authenticated
    using (auth.uid() = user_id);

create policy "devices_insert_own"
    on public.devices for insert
    to authenticated
    with check (auth.uid() = user_id);

create policy "devices_update_own"
    on public.devices for update
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- conversations
-- ---------------------------------------------------------------------------
alter table public.conversations enable row level security;

create policy "conversations_select_own"
    on public.conversations for select
    to authenticated
    using (auth.uid() = user_id);

create policy "conversations_insert_own"
    on public.conversations for insert
    to authenticated
    with check (auth.uid() = user_id);

create policy "conversations_update_own"
    on public.conversations for update
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- messages (append-only: no update, no delete policies)
-- ---------------------------------------------------------------------------
alter table public.messages enable row level security;

create policy "messages_select_own"
    on public.messages for select
    to authenticated
    using (auth.uid() = user_id);

-- Insert requires both owning the row AND owning the target conversation
-- (defense in depth against writing into another user's conversation id).
create policy "messages_insert_own"
    on public.messages for insert
    to authenticated
    with check (
        auth.uid() = user_id
        and exists (
            select 1
            from public.conversations c
            where c.id = conversation_id
              and c.user_id = auth.uid()
        )
    );

-- ---------------------------------------------------------------------------
-- subscriptions (read own row; writes via service role only)
-- ---------------------------------------------------------------------------
alter table public.subscriptions enable row level security;

create policy "subscriptions_select_own"
    on public.subscriptions for select
    to authenticated
    using (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- savings_daily
-- ---------------------------------------------------------------------------
alter table public.savings_daily enable row level security;

create policy "savings_daily_select_own"
    on public.savings_daily for select
    to authenticated
    using (auth.uid() = user_id);

create policy "savings_daily_insert_own"
    on public.savings_daily for insert
    to authenticated
    with check (auth.uid() = user_id);

create policy "savings_daily_update_own"
    on public.savings_daily for update
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- feature_flags (world-readable, no client writes; grants tightened in 0007)
-- ---------------------------------------------------------------------------
alter table public.feature_flags enable row level security;

create policy "feature_flags_select_all"
    on public.feature_flags for select
    to anon, authenticated
    using (true);
