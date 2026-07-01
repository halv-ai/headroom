-- 0002_devices.sql
-- Desktop app installs. IDs are client-generated ULIDs, stored as text.

create table public.devices (
    id text primary key,
    user_id uuid not null references auth.users (id) on delete cascade,
    name text,
    platform text,
    app_version text,
    last_seen_at timestamptz
);

comment on table public.devices is
    'Desktop app installations. id is a client-generated ULID (text).';

create index devices_user_id_idx on public.devices (user_id);
