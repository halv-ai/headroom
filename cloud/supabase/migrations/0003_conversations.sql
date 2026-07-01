-- 0003_conversations.sql
-- Synced conversation metadata. IDs are client-generated ULIDs (text).
-- Deletion is a tombstone (deleted_at) so other devices can converge.

create table public.conversations (
    id text primary key,
    user_id uuid not null references auth.users (id) on delete cascade,
    title text not null default 'New chat',
    model text,
    provider text,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    deleted_at timestamptz
);

comment on table public.conversations is
    'Conversation sync metadata. id is a client-generated ULID (text). deleted_at is a tombstone.';

create index conversations_user_id_updated_at_idx
    on public.conversations (user_id, updated_at desc);
