-- 0004_messages.sql
-- Append-only synced messages. IDs are client-generated ULIDs (text).
-- No UPDATE/DELETE path: conversation deletion happens via the
-- conversations.deleted_at tombstone (rows cascade if the conversation
-- row itself is hard-deleted, and if the auth user is deleted).

create table public.messages (
    id text primary key,
    conversation_id text not null references public.conversations (id) on delete cascade,
    user_id uuid not null references auth.users (id) on delete cascade,
    role text not null check (role in ('user', 'assistant', 'system', 'tool')),
    content jsonb not null,
    seq bigint not null,
    device_id text references public.devices (id) on delete set null,
    created_at timestamptz not null,
    unique (conversation_id, seq, id)
);

comment on table public.messages is
    'Append-only message sync. id and device_id are client-generated ULIDs (text).';

create index messages_user_id_created_at_idx
    on public.messages (user_id, created_at);
