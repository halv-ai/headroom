-- cloud/tests/rls.test.sql
-- RLS assertions for the Headroom Desktop cloud schema.
--
-- HOW TO RUN (manual, against a database with migrations 0001-0008 applied):
--
--   Local stack:   supabase db reset   (applies migrations), then:
--                  psql "postgresql://postgres:postgres@127.0.0.1:54322/postgres" \
--                       -v ON_ERROR_STOP=1 -f cloud/tests/rls.test.sql
--
--   Hosted:        psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f cloud/tests/rls.test.sql
--
-- The whole file runs in ONE transaction and always ROLLS BACK -- it never
-- leaves data behind. Each assertion raises an exception on failure, so a
-- clean exit (status 0, final NOTICE "ALL RLS TESTS PASSED") means success.
--
-- Style note: plain SQL DO-block assertions (pgTAP not required). If you have
-- pgTAP installed you can port each block to ok()/throws_ok() one-to-one.

begin;

-- ---------------------------------------------------------------------------
-- Fixtures: two users created directly in auth.users (superuser context).
-- The 0001 trigger must auto-create their profiles.
-- ---------------------------------------------------------------------------
insert into auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
values
  ('00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-000000000000',
   'authenticated', 'authenticated', 'alice@test.local', 'x', now(), now()),
  ('00000000-0000-0000-0000-00000000000b', '00000000-0000-0000-0000-000000000000',
   'authenticated', 'authenticated', 'bob@test.local', 'x', now(), now());

do $$
begin
  if (select count(*) from public.profiles
      where id in ('00000000-0000-0000-0000-00000000000a',
                   '00000000-0000-0000-0000-00000000000b')) <> 2 then
    raise exception 'FAIL: profiles trigger did not auto-create rows on auth.users insert';
  end if;
  raise notice 'PASS: profiles auto-created by trigger';
end $$;

-- Seed data as superuser (bypasses RLS): one conversation+message per user.
insert into public.devices (id, user_id, name, platform, app_version)
values ('01DEVALICE0000000000000000', '00000000-0000-0000-0000-00000000000a', 'mbp', 'darwin', '1.0.0');

insert into public.conversations (id, user_id, title, created_at, updated_at)
values
  ('01CONVALICE000000000000000', '00000000-0000-0000-0000-00000000000a', 'alice conv', now(), now()),
  ('01CONVBOB00000000000000000', '00000000-0000-0000-0000-00000000000b', 'bob conv', now(), now());

insert into public.messages (id, conversation_id, user_id, role, content, seq, created_at)
values
  ('01MSGALICE0000000000000000', '01CONVALICE000000000000000',
   '00000000-0000-0000-0000-00000000000a', 'user', '{"text":"hi"}', 1, now()),
  ('01MSGBOB000000000000000000', '01CONVBOB00000000000000000',
   '00000000-0000-0000-0000-00000000000b', 'user', '{"text":"yo"}', 1, now());

insert into public.subscriptions (user_id, stripe_customer_id, status)
values
  ('00000000-0000-0000-0000-00000000000a', 'cus_alice', 'active'),
  ('00000000-0000-0000-0000-00000000000b', 'cus_bob', 'canceled');

insert into public.savings_daily (user_id, day, tokens_saved)
values ('00000000-0000-0000-0000-00000000000b', current_date, 999);

-- ---------------------------------------------------------------------------
-- Helper: impersonate a user the way PostgREST does.
-- ---------------------------------------------------------------------------
-- (set_config with is_local=true scopes it to this transaction)

-- =========================== AS ALICE =======================================
select set_config('request.jwt.claims',
  '{"sub":"00000000-0000-0000-0000-00000000000a","role":"authenticated"}', true);
set local role authenticated;

do $$
begin
  -- profiles: sees own row only
  if (select count(*) from public.profiles) <> 1 then
    raise exception 'FAIL: profiles visible across users';
  end if;
  -- conversations: own only
  if (select count(*) from public.conversations) <> 1
     or exists (select 1 from public.conversations where user_id <> auth.uid()) then
    raise exception 'FAIL: conversations leak across users';
  end if;
  -- messages: own only
  if (select count(*) from public.messages) <> 1 then
    raise exception 'FAIL: messages leak across users';
  end if;
  -- devices: own only
  if (select count(*) from public.devices) <> 1 then
    raise exception 'FAIL: devices leak across users';
  end if;
  -- subscriptions: own row only
  if (select count(*) from public.subscriptions) <> 1
     or exists (select 1 from public.subscriptions where stripe_customer_id = 'cus_bob') then
    raise exception 'FAIL: subscriptions leak across users';
  end if;
  -- savings_daily: bob's row invisible
  if (select count(*) from public.savings_daily) <> 0 then
    raise exception 'FAIL: savings_daily leak across users';
  end if;
  -- feature_flags: readable
  if not exists (select 1 from public.feature_flags where key = 'chatgpt_oauth') then
    raise exception 'FAIL: feature_flags not readable by authenticated';
  end if;
  raise notice 'PASS: per-user SELECT isolation (alice)';
end $$;

-- Alice can insert/update her own rows.
insert into public.conversations (id, user_id, title, created_at, updated_at)
values ('01CONVALICE200000000000000', auth.uid(), 'alice conv 2', now(), now());
update public.conversations set title = 'renamed'
 where id = '01CONVALICE200000000000000';
insert into public.messages (id, conversation_id, user_id, role, content, seq, created_at)
values ('01MSGALICE2000000000000000', '01CONVALICE200000000000000',
        auth.uid(), 'assistant', '{"text":"ok"}', 1, now());

-- Alice CANNOT insert a row owned by bob.
do $$
begin
  begin
    insert into public.conversations (id, user_id, title, created_at, updated_at)
    values ('01CONVEVIL0000000000000000',
            '00000000-0000-0000-0000-00000000000b', 'spoof', now(), now());
    raise exception 'FAIL: inserted conversation as another user';
  exception
    when insufficient_privilege or check_violation then
      raise notice 'PASS: cannot insert conversation for another user';
  end;
end $$;

-- Alice CANNOT insert a message into bob's conversation, even with her user_id.
do $$
begin
  begin
    insert into public.messages (id, conversation_id, user_id, role, content, seq, created_at)
    values ('01MSGEVIL00000000000000000', '01CONVBOB00000000000000000',
            auth.uid(), 'user', '{"text":"spoof"}', 99, now());
    raise exception 'FAIL: inserted message into another user''s conversation';
  exception
    when insufficient_privilege or check_violation then
      raise notice 'PASS: cannot write into another user''s conversation';
  end;
end $$;

-- messages are append-only: UPDATE and DELETE must affect 0 rows (no policy).
do $$
declare n int;
begin
  update public.messages set content = '{"text":"tampered"}' where true;
  get diagnostics n = row_count;
  if n <> 0 then raise exception 'FAIL: messages UPDATE allowed (append-only violated)'; end if;

  delete from public.messages where true;
  get diagnostics n = row_count;
  if n <> 0 then raise exception 'FAIL: messages DELETE allowed (append-only violated)'; end if;
  raise notice 'PASS: messages append-only (no update/delete)';
end $$;

-- subscriptions are read-only for clients: writes must affect 0 rows / fail.
do $$
declare n int;
begin
  update public.subscriptions set status = 'hacked' where true;
  get diagnostics n = row_count;
  if n <> 0 then raise exception 'FAIL: subscriptions UPDATE allowed to client'; end if;

  begin
    insert into public.subscriptions (user_id, status) values (auth.uid(), 'self-serve');
    raise exception 'FAIL: subscriptions INSERT allowed to client';
  exception
    when insufficient_privilege or unique_violation then
      raise notice 'PASS: subscriptions writes blocked for clients';
  end;
end $$;

-- feature_flags: no client writes.
do $$
declare n int;
begin
  begin
    insert into public.feature_flags (key, enabled) values ('evil', true);
    raise exception 'FAIL: feature_flags INSERT allowed to client';
  exception
    when insufficient_privilege then null;
  end;
  update public.feature_flags set enabled = true where key = 'chatgpt_oauth';
  get diagnostics n = row_count;
  if n <> 0 then raise exception 'FAIL: feature_flags UPDATE allowed to client'; end if;
  raise notice 'PASS: feature_flags read-only for clients';
exception
  when insufficient_privilege then
    raise notice 'PASS: feature_flags read-only for clients';
end $$;

-- savings_daily: report_savings RPC accumulates for auth.uid().
select public.report_savings(current_date, 100, 40, 60, 3, 0.0123);
select public.report_savings(current_date, 100, 40, 60, 3, 0.0123);
do $$
begin
  if not exists (
    select 1 from public.savings_daily
    where user_id = auth.uid() and day = current_date
      and tokens_before = 200 and tokens_after = 80
      and tokens_saved = 120 and requests = 6 and usd_saved = 0.0246
  ) then
    raise exception 'FAIL: report_savings did not upsert-accumulate correctly';
  end if;
  raise notice 'PASS: report_savings accumulates own row';
end $$;

-- =========================== AS ANON ========================================
reset role;
select set_config('request.jwt.claims', '{"role":"anon"}', true);
set local role anon;

do $$
begin
  if (select count(*) from public.feature_flags) < 1 then
    raise exception 'FAIL: feature_flags not readable by anon';
  end if;
  raise notice 'PASS: feature_flags readable by anon';
end $$;

do $$
begin
  -- Every user-data table must be empty (or error) for anon.
  if (select count(*) from public.conversations) <> 0
     or (select count(*) from public.messages) <> 0
     or (select count(*) from public.subscriptions) <> 0
     or (select count(*) from public.savings_daily) <> 0
     or (select count(*) from public.profiles) <> 0
     or (select count(*) from public.devices) <> 0 then
    raise exception 'FAIL: anon can read user data';
  end if;
  raise notice 'PASS: anon sees no user data';
exception
  when insufficient_privilege then
    raise notice 'PASS: anon sees no user data (denied at grant level)';
end $$;

reset role;

do $$ begin raise notice 'ALL RLS TESTS PASSED'; end $$;

rollback;
