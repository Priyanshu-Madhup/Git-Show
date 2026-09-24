-- Git Show schema. Idempotent: safe to re-run via `python db/migrate.py`.
--
-- Every table has row level security enabled with no policies, so the
-- project's publishable (anon) key can't read or write anything through the
-- Supabase REST API. The backend connects directly as the postgres role,
-- which bypasses RLS.

create table if not exists users (
    id          bigserial primary key,
    github_id   bigint unique not null,
    login       text not null,
    name        text,
    avatar_url  text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- The session cookie value is never stored; only its SHA-256 hash is, so a
-- leaked table dump can't be replayed as a login. The GitHub access token is
-- encrypted with TOKEN_ENCRYPTION_KEY before it's written.
create table if not exists sessions (
    token_hash              text primary key,
    user_id                 bigint not null references users(id) on delete cascade,
    encrypted_access_token  text not null,
    created_at              timestamptz not null default now(),
    expires_at              timestamptz not null
);
create index if not exists sessions_user_id_idx on sessions(user_id);
create index if not exists sessions_expires_at_idx on sessions(expires_at);

-- id is the client-generated chat_id. user_id is null for signed-out chats.
create table if not exists conversations (
    id          uuid primary key,
    user_id     bigint references users(id) on delete cascade,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index if not exists conversations_user_id_idx on conversations(user_id);

create table if not exists conversation_summaries (
    conversation_id  uuid primary key references conversations(id) on delete cascade,
    summary          text not null,
    updated_at       timestamptz not null default now()
);

-- Write tool calls proposed by the model, awaiting human confirmation.
create table if not exists pending_actions (
    id               text primary key,
    session_hash     text not null references sessions(token_hash) on delete cascade,
    conversation_id  uuid references conversations(id) on delete set null,
    tool             text not null,
    arguments        jsonb not null,
    status           text not null default 'pending'
                     check (status in ('pending', 'executing', 'executed', 'cancelled', 'failed')),
    result           jsonb,
    created_at       timestamptz not null default now(),
    expires_at       timestamptz not null,
    resolved_at      timestamptz
);
create index if not exists pending_actions_session_idx on pending_actions(session_hash);

-- Chat history, shown in the sidebar. title comes from the first user
-- message; repo is the last repository selected in that chat, restored when
-- the chat is reopened.
alter table conversations add column if not exists title text;
alter table conversations add column if not exists repo text;
create index if not exists conversations_user_updated_idx on conversations(user_id, updated_at desc);

-- steps is the tool timeline shown above an assistant reply. An assistant
-- message that proposed a write links to it, so reopening the chat can show
-- the Confirm/Cancel card (still pending) or its outcome.
create table if not exists messages (
    id                 bigserial primary key,
    conversation_id    uuid not null references conversations(id) on delete cascade,
    role               text not null check (role in ('user', 'assistant')),
    content            text not null,
    steps              jsonb not null default '[]'::jsonb,
    pending_action_id  text references pending_actions(id) on delete set null,
    created_at         timestamptz not null default now()
);
create index if not exists messages_conversation_idx on messages(conversation_id, id);

-- Summaries fold in messages up to summarized_through (a messages.id).
-- Anything newer is passed to the agents verbatim, so a slow or failed
-- summary update never loses a turn.
alter table conversation_summaries add column if not exists summarized_through bigint not null default 0;

-- --- repository index ------------------------------------------------------
-- Shared across users: a repo's file listing is the same for everyone who
-- can see it. Access is checked per request with the user's own token (the
-- head-commit lookup that also detects a stale index), never from this table.
create table if not exists repositories (
    id              bigserial primary key,
    github_repo_id  bigint unique not null,
    owner           text not null,
    name            text not null,
    full_name       text not null,
    default_branch  text not null,
    private         boolean not null default false,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
create index if not exists repositories_full_name_idx on repositories(lower(full_name));

-- One indexed tree per branch, stamped with the commit it was built from.
create table if not exists repository_trees (
    id             bigserial primary key,
    repository_id  bigint not null references repositories(id) on delete cascade,
    branch         text not null,
    commit_sha     text not null,
    tree_sha       text not null,
    truncated      boolean not null default false,
    file_count     integer not null default 0,
    updated_at     timestamptz not null default now(),
    unique (repository_id, branch)
);

create table if not exists repository_files (
    tree_id      bigint not null references repository_trees(id) on delete cascade,
    path         text not null,
    type         text not null check (type in ('file', 'dir')),
    size         bigint,
    sha          text,
    parent_path  text not null,
    primary key (tree_id, path)
);
create index if not exists repository_files_parent_idx on repository_files(tree_id, parent_path);

-- --- agent execution trace -------------------------------------------------
-- One run per user request (and its confirm/continue follow-ups). state holds
-- what the orchestrator needs to resume after a confirmation or a pause.
create table if not exists agent_runs (
    id               uuid primary key default gen_random_uuid(),
    conversation_id  uuid not null references conversations(id) on delete cascade,
    user_id          bigint references users(id) on delete cascade,
    user_request     text not null,
    repo             text,
    mode             text check (mode in ('utility', 'reader', 'planner')),
    status           text not null default 'running'
                     check (status in ('running', 'awaiting_confirmation', 'paused', 'completed', 'failed')),
    state            jsonb not null default '{}'::jsonb,
    error            text,
    started_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    completed_at     timestamptz
);
create index if not exists agent_runs_conversation_idx on agent_runs(conversation_id, started_at);

-- Every planner output is kept as a new version, so the trace shows how
-- the plan changed as results came in.
create table if not exists plans (
    id          bigserial primary key,
    run_id      uuid not null references agent_runs(id) on delete cascade,
    version     integer not null,
    goal        text,
    reasoning   text,
    steps       jsonb not null,
    done        boolean not null default false,
    created_at  timestamptz not null default now(),
    unique (run_id, version)
);

create table if not exists agent_steps (
    id             uuid primary key,
    run_id         uuid not null references agent_runs(id) on delete cascade,
    step_number    integer not null,
    plan_version   integer,
    plan_step_id   integer,
    agent          text not null check (agent in ('router', 'utility', 'reader', 'writer', 'planner', 'verifier')),
    instruction    text,
    status         text not null default 'running'
                   check (status in ('running', 'completed', 'failed', 'awaiting_confirmation', 'cancelled')),
    result         text,
    started_at     timestamptz not null default now(),
    completed_at   timestamptz
);
create index if not exists agent_steps_run_idx on agent_steps(run_id, step_number);

create table if not exists tool_calls (
    id           uuid primary key,
    run_id       uuid not null references agent_runs(id) on delete cascade,
    step_id      uuid not null references agent_steps(id) on delete cascade,
    tool_name    text not null,
    arguments    jsonb not null,
    result       jsonb,
    success      boolean,
    duration_ms  integer,
    created_at   timestamptz not null default now()
);
create index if not exists tool_calls_step_idx on tool_calls(step_id);

alter table pending_actions add column if not exists run_id uuid references agent_runs(id) on delete cascade;
alter table pending_actions add column if not exists tool_call_id uuid references tool_calls(id) on delete set null;
alter table pending_actions add column if not exists preview jsonb;

alter table messages add column if not exists run_id uuid references agent_runs(id) on delete set null;
alter table messages add column if not exists plan jsonb;
alter table messages add column if not exists can_continue boolean not null default false;

alter table users                  enable row level security;
alter table repositories           enable row level security;
alter table repository_trees       enable row level security;
alter table repository_files       enable row level security;
alter table agent_runs             enable row level security;
alter table plans                  enable row level security;
alter table agent_steps            enable row level security;
alter table tool_calls             enable row level security;
alter table sessions               enable row level security;
alter table conversations          enable row level security;
alter table conversation_summaries enable row level security;
alter table pending_actions        enable row level security;
alter table messages               enable row level security;

-- --- agent memory ------------------------------------------------------------
-- Agents can query saved history with SQL they write themselves (the
-- query_memory tool). That SQL runs under gitshow_memory, a role with no
-- access to anything except these views, which only ever show the
-- signed-in user's own rows (gitshow.user_id is set per query by the
-- backend; the query validator forbids functions like set_config /
-- current_setting, so a query can't change it). Sessions, tokens, and
-- other users' data are unreachable.
create schema if not exists memory;

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'gitshow_memory') then
        create role gitshow_memory nologin;
    end if;
end
$$;
grant gitshow_memory to postgres;

create or replace view memory.conversations with (security_barrier) as
select c.id, c.title, c.repo, c.created_at, c.updated_at,
       c.id = nullif(current_setting('gitshow.conversation_id', true), '')::uuid as is_current
from public.conversations c
where c.user_id = nullif(current_setting('gitshow.user_id', true), '')::bigint;

create or replace view memory.messages with (security_barrier) as
select m.id, m.conversation_id, m.run_id, m.role, m.content, m.created_at,
       m.conversation_id = nullif(current_setting('gitshow.conversation_id', true), '')::uuid as in_current_conversation
from public.messages m
join public.conversations c on c.id = m.conversation_id
where c.user_id = nullif(current_setting('gitshow.user_id', true), '')::bigint;

create or replace view memory.conversation_summaries with (security_barrier) as
select s.conversation_id, s.summary, s.updated_at
from public.conversation_summaries s
join public.conversations c on c.id = s.conversation_id
where c.user_id = nullif(current_setting('gitshow.user_id', true), '')::bigint;

create or replace view memory.agent_runs with (security_barrier) as
select r.id, r.conversation_id, r.user_request, r.repo, r.mode, r.status, r.error,
       r.started_at, r.completed_at
from public.agent_runs r
where r.user_id = nullif(current_setting('gitshow.user_id', true), '')::bigint;

create or replace view memory.plans with (security_barrier) as
select p.run_id, p.version, p.goal, p.reasoning, p.steps, p.done, p.created_at
from public.plans p
join public.agent_runs r on r.id = p.run_id
where r.user_id = nullif(current_setting('gitshow.user_id', true), '')::bigint;

create or replace view memory.agent_steps with (security_barrier) as
select s.id, s.run_id, s.step_number, s.plan_version, s.plan_step_id, s.agent, s.instruction,
       s.status, s.result, s.started_at, s.completed_at
from public.agent_steps s
join public.agent_runs r on r.id = s.run_id
where r.user_id = nullif(current_setting('gitshow.user_id', true), '')::bigint;

create or replace view memory.tool_calls with (security_barrier) as
select t.id, t.run_id, t.step_id, t.tool_name, t.arguments, t.result, t.success, t.duration_ms, t.created_at
from public.tool_calls t
join public.agent_runs r on r.id = t.run_id
where r.user_id = nullif(current_setting('gitshow.user_id', true), '')::bigint;

create or replace view memory.proposed_actions with (security_barrier) as
select a.id, a.conversation_id, a.run_id, a.tool, a.arguments, a.status, a.result,
       a.created_at, a.resolved_at
from public.pending_actions a
join public.conversations c on c.id = a.conversation_id
where c.user_id = nullif(current_setting('gitshow.user_id', true), '')::bigint;

revoke all on all tables in schema public from gitshow_memory;
grant usage on schema memory to gitshow_memory;
grant select on all tables in schema memory to gitshow_memory;

-- The orchestrator hands single-step writes straight to the Writer, and
-- records its own interpretation step; the Utility agent is gone.
alter table agent_runs drop constraint if exists agent_runs_mode_check;
alter table agent_runs add constraint agent_runs_mode_check
    check (mode in ('utility', 'reader', 'writer', 'planner'));
alter table agent_steps drop constraint if exists agent_steps_agent_check;
alter table agent_steps add constraint agent_steps_agent_check
    check (agent in ('router', 'orchestrator', 'utility', 'reader', 'writer', 'planner', 'verifier'));

-- GitHub App user tokens can expire (8 h) with a refresh token (6 months).
-- Both are stored encrypted so sessions can refresh instead of failing.
alter table sessions add column if not exists encrypted_refresh_token text;
alter table sessions add column if not exists access_token_expires_at timestamptz;
