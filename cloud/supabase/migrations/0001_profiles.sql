-- 0001_profiles.sql
-- User profile row, auto-created when a user signs up.

create table public.profiles (
    id uuid primary key references auth.users (id) on delete cascade,
    display_name text,
    created_at timestamptz not null default now()
);

comment on table public.profiles is
    'One row per auth user. Auto-inserted by trigger on auth.users.';

-- Auto-insert a profile row whenever a user is created in auth.users.
-- SECURITY DEFINER: the trigger fires as supabase_auth_admin, which has no
-- grants on public tables; definer (postgres) does.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
    insert into public.profiles (id, display_name)
    values (
        new.id,
        coalesce(
            new.raw_user_meta_data ->> 'display_name',
            nullif(split_part(coalesce(new.email, ''), '@', 1), '')
        )
    )
    on conflict (id) do nothing;
    return new;
end;
$$;

create trigger on_auth_user_created
    after insert on auth.users
    for each row
    execute function public.handle_new_user();
