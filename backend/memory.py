"""The query_memory tool: agents write SQL against the memory.* views to
recall past chats, runs, plans, and tool results.

Model-written SQL is untrusted (a repository file could carry injected
instructions), so it is contained three ways:
1. It must parse as a single SELECT that only reads memory.* views and only
   calls allow-listed functions — nothing like set_config, pg_sleep, or
   current_setting, so it can't widen its own scope.
2. It runs as the gitshow_memory role, which can read nothing but those
   views; the views only show the signed-in user's rows.
3. It runs in a read-only transaction with a statement timeout."""

import json
import uuid

import sqlglot
from sqlglot import exp

import db

ROW_LIMIT = 200
STATEMENT_TIMEOUT = "5s"

VIEWS = {
    "conversations": "id uuid, title text, repo text, created_at, updated_at, is_current bool (the chat you're in now)",
    "messages": "id bigint (increasing), conversation_id uuid, run_id uuid, role 'user'|'assistant', content text, created_at, in_current_conversation bool",
    "conversation_summaries": "conversation_id uuid, summary text, updated_at",
    "agent_runs": "id uuid, conversation_id uuid, user_request text, repo text, mode 'reader'|'writer'|'planner' (how the orchestrator handled it), status, error, started_at, completed_at",
    "plans": "run_id uuid, version int, goal text, reasoning text, steps jsonb [{id, agent, instruction, status}], done bool, created_at",
    "agent_steps": "id uuid, run_id uuid, step_number int, plan_version, plan_step_id, agent 'orchestrator'|'reader'|'writer'|'planner'|'verifier', instruction text, status, result text, started_at, completed_at",
    "tool_calls": "id uuid, run_id uuid, step_id uuid, tool_name text, arguments jsonb, result jsonb, success bool, duration_ms int, created_at",
    "proposed_actions": "id, conversation_id uuid, run_id uuid, tool text, arguments jsonb, status 'pending'|'executing'|'executed'|'cancelled'|'failed', result jsonb, created_at, resolved_at",
}

ALLOWED_FUNCTIONS = {
    # aggregates & windows
    "COUNT", "SUM", "AVG", "MIN", "MAX", "STRING_AGG", "GROUP_CONCAT", "ARRAY_AGG", "BOOL_OR",
    "BOOL_AND", "ROW_NUMBER", "RANK", "DENSE_RANK", "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE",
    # text
    "LOWER", "UPPER", "LENGTH", "SUBSTRING", "LEFT", "RIGHT", "TRIM", "CONCAT", "REPLACE",
    "POSITION", "STRPOS", "STR_POSITION", "SPLIT_PART", "SPLITPART", "INITCAP", "REGEXP_LIKE",
    "REGEXP_REPLACE", "REGEXP_EXTRACT",
    # conditionals & casts
    "COALESCE", "NULLIF", "GREATEST", "LEAST", "CAST", "TRY_CAST", "IF", "ABS", "ROUND",
    # dates
    "NOW", "CURRENT_TIMESTAMP", "CURRENT_DATE", "DATE_TRUNC", "TIMESTAMP_TRUNC", "EXTRACT",
    "TO_CHAR", "TIME_TO_STR", "AGE", "INTERVAL",
    # json
    "JSON_EXTRACT", "JSON_EXTRACT_SCALAR", "JSONB_EXTRACT", "JSONB_EXTRACT_SCALAR",
    "JSONB_ARRAY_LENGTH", "JSON_ARRAY_LENGTH", "ARRAY_SIZE", "JSONB_ARRAY_ELEMENTS",
    "JSON_ARRAY_ELEMENTS", "JSONB_TYPEOF", "JSONB_CONTAINS", "EXPLODE", "UNNEST",
}

FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command, exp.Merge,
    exp.Set, exp.Into, exp.Lock, exp.Copy, exp.Alter, exp.Grant, exp.Transaction, exp.Commit,
    exp.Rollback, exp.Use, exp.Describe, exp.Pragma, exp.Analyze,
)


def _function_name(node) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).upper()
    return node.sql_name().upper()


def validate(sql: str) -> str:
    """Return a normalized, safe version of sql, or raise ValueError with a
    message the model can act on."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise ValueError(f"SQL didn't parse: {exc}") from None
    if len(statements) != 1:
        raise ValueError("Send exactly one SELECT statement.")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise ValueError("Only SELECT queries are allowed.")

    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise ValueError(f"{type(node).__name__} is not allowed; only read with SELECT.")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        name, schema = table.name.lower(), (table.db or "").lower()
        if table.catalog:
            raise ValueError(f"{table.sql()} is not allowed.")
        if schema == "" and name in cte_names:
            continue
        if schema in ("", "memory") and name in VIEWS:
            continue
        if isinstance(table.this, exp.Func):
            continue  # a table function, e.g. jsonb_array_elements(...); checked below
        raise ValueError(f"Unknown table {table.sql()}. You can only read: {', '.join(sorted(VIEWS))}.")

    for func in tree.find_all(exp.Func):
        if isinstance(func, (exp.Case, exp.If, exp.Paren)):
            continue
        name = _function_name(func)
        if name not in ALLOWED_FUNCTIONS:
            raise ValueError(f"Function {name.lower()} is not allowed in memory queries.")

    # Qualify view names so they can't resolve to anything else.
    for table in tree.find_all(exp.Table):
        if table.name.lower() in VIEWS and not table.db and table.name.lower() not in cte_names:
            table.set("db", exp.to_identifier("memory"))
    return tree.sql(dialect="postgres")


def run_query(user_id, conversation_id, sql: str) -> dict:
    if user_id is None:
        return {"error": "Memory is only available to signed-in users."}
    try:
        safe_sql = validate(sql)
    except ValueError as exc:
        return {"error": str(exc)}
    uid = int(user_id)
    cid = str(uuid.UUID(str(conversation_id)))
    # One round trip: every value interpolated here is an int or a
    # validated UUID, and safe_sql was regenerated from the parse tree, so
    # it can't smuggle in a second statement.
    script = (
        "begin read only;"
        f" set local statement_timeout = '{STATEMENT_TIMEOUT}';"
        f" set local gitshow.user_id = '{uid}';"
        f" set local gitshow.conversation_id = '{cid}';"
        " set local role gitshow_memory;"
        " set local search_path = memory;"
        f" select * from ({safe_sql}) as memory_result limit {ROW_LIMIT + 1};"
    )
    with db.connection() as conn:
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute(script)
                    columns, rows = None, []
                    while True:
                        if cur.description:
                            columns = [d.name for d in cur.description]
                            rows = cur.fetchall()
                        if not cur.nextset():
                            break
                except Exception as exc:
                    return {"error": f"Query failed: {str(exc).splitlines()[0]}"}
                finally:
                    cur.execute("rollback")
        finally:
            conn.autocommit = False
    truncated = len(rows) > ROW_LIMIT
    rows = rows[:ROW_LIMIT]
    return {
        "columns": columns,
        "rows": json.loads(json.dumps([list(r) for r in rows], default=str)),
        "row_count": len(rows),
        "truncated": truncated,
    }


TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_memory",
        "description": (
            "Recall the user's saved history with one read-only SQL SELECT (PostgreSQL). Use it "
            "when the answer may be in earlier chats or runs — what was decided, which files were "
            "changed, what a past plan or tool call returned — rather than refetching from GitHub "
            "or asking the user again. The current chat's summary is already in your context; "
            "query for details it doesn't have. Only these views exist (all rows are the user's "
            "own):\n"
            + "\n".join(f"- {name}({cols})" for name, cols in VIEWS.items())
            + f"\nAt most {ROW_LIMIT} rows come back. Prefer ORDER BY created_at DESC with a LIMIT, "
            "select only needed columns, and use ILIKE for text search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A single SELECT over the views above"},
                "purpose": {"type": "string", "description": "A few words on what you're looking for, shown to the user"},
            },
            "required": ["sql", "purpose"],
        },
    },
}
