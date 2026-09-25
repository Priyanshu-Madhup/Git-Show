"""Storage for the persistent repository index: repositories,
repository_trees (one per branch, stamped with its commit), and
repository_files."""

from db import connection


def upsert_repository(github_repo_id, owner, name, default_branch, private) -> int:
    with connection() as conn:
        row = conn.execute(
            """
            insert into repositories (github_repo_id, owner, name, full_name, default_branch, private)
            values (%s, %s, %s, %s, %s, %s)
            on conflict (github_repo_id) do update
                set owner = excluded.owner,
                    name = excluded.name,
                    full_name = excluded.full_name,
                    default_branch = excluded.default_branch,
                    private = excluded.private,
                    updated_at = now()
            returning id
            """,
            (github_repo_id, owner, name, f"{owner}/{name}", default_branch, private),
        ).fetchone()
    return row[0]


def find_repository(full_name) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            """
            select id, github_repo_id, owner, name, default_branch
            from repositories where lower(full_name) = lower(%s)
            order by updated_at desc limit 1
            """,
            (full_name,),
        ).fetchone()
    if not row:
        return None
    return dict(zip(("id", "github_repo_id", "owner", "name", "default_branch"), row))


def lookup(full_name, branch=None) -> dict | None:
    """The repository and its indexed tree for branch (default branch if
    None), in one round trip. tree is None if that branch isn't indexed."""
    with connection() as conn:
        row = conn.execute(
            """
            select r.id, r.github_repo_id, r.owner, r.name, r.default_branch,
                   t.id, t.commit_sha, t.tree_sha, t.truncated, t.file_count
            from repositories r
            left join repository_trees t
                on t.repository_id = r.id and t.branch = coalesce(%s, r.default_branch)
            where lower(r.full_name) = lower(%s)
            order by r.updated_at desc
            limit 1
            """,
            (branch, full_name),
        ).fetchone()
    if not row:
        return None
    record = dict(zip(("id", "github_repo_id", "owner", "name", "default_branch"), row[:5]))
    tree_id, commit_sha, tree_sha, truncated, file_count = row[5:]
    record["tree"] = (
        {"id": tree_id, "commit_sha": commit_sha, "tree_sha": tree_sha, "truncated": truncated, "file_count": file_count}
        if tree_id
        else None
    )
    return record


def get_tree(repository_id, branch) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            """
            select id, commit_sha, tree_sha, truncated, file_count, updated_at
            from repository_trees where repository_id = %s and branch = %s
            """,
            (repository_id, branch),
        ).fetchone()
    if not row:
        return None
    return dict(zip(("id", "commit_sha", "tree_sha", "truncated", "file_count", "updated_at"), row))


def replace_tree(repository_id, branch, commit_sha, tree_sha, truncated, entries) -> int:
    """Swap in a freshly fetched tree for this branch in one transaction, so
    readers never see a half-written index."""
    with connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """
                insert into repository_trees
                    (repository_id, branch, commit_sha, tree_sha, truncated, file_count)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (repository_id, branch) do update
                    set commit_sha = excluded.commit_sha,
                        tree_sha = excluded.tree_sha,
                        truncated = excluded.truncated,
                        file_count = excluded.file_count,
                        updated_at = now()
                returning id
                """,
                (repository_id, branch, commit_sha, tree_sha, truncated, len(entries)),
            ).fetchone()
            tree_id = row[0]
            conn.execute("delete from repository_files where tree_id = %s", (tree_id,))
            with conn.cursor() as cur:
                with cur.copy(
                    "copy repository_files (tree_id, path, type, size, sha, parent_path) from stdin"
                ) as copy:
                    for e in entries:
                        copy.write_row((tree_id, e["path"], e["type"], e["size"], e["sha"], e["parent_path"]))
    return tree_id


def list_directory(tree_id, parent_path) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """
            select path, type, size from repository_files
            where tree_id = %s and parent_path = %s
            order by type desc, path
            """,
            (tree_id, parent_path),
        ).fetchall()
    return [{"path": p, "type": t, "size": s} for p, t, s in rows]


def all_entries(tree_id, limit) -> list[tuple]:
    """Every path in the tree as (path, type, size), for the tree viewer."""
    with connection() as conn:
        return conn.execute(
            "select path, type, size from repository_files where tree_id = %s order by path limit %s",
            (tree_id, limit),
        ).fetchall()


def find_files(tree_id, pattern) -> list[dict]:
    """Case-insensitive match on the path. '*' works as a wildcard;
    anything else is a substring match."""
    like = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("*", "%")
    if "%" not in like:
        like = f"%{like}%"
    with connection() as conn:
        rows = conn.execute(
            """
            select path, type, size from repository_files
            where tree_id = %s and path ilike %s
            order by length(path), path
            """,
            (tree_id, like),
        ).fetchall()
    return [{"path": p, "type": t, "size": s} for p, t, s in rows]


def overview(tree_id) -> dict:
    """Top-level entries plus per-extension counts: enough for a planner to
    orient itself without the whole tree in its context."""
    with connection() as conn:
        top = conn.execute(
            """
            select path, type from repository_files
            where tree_id = %s and parent_path = ''
            order by type desc, path
            """,
            (tree_id,),
        ).fetchall()
        dirs = conn.execute(
            """
            select path from repository_files
            where tree_id = %s and type = 'dir' and path not like '%%/%%/%%'
            order by path
            """,
            (tree_id,),
        ).fetchall()
        exts = conn.execute(
            """
            select lower(substring(path from '\\.([A-Za-z0-9]+)$')) as ext, count(*)
            from repository_files where tree_id = %s and type = 'file'
            group by 1 order by 2 desc limit 12
            """,
            (tree_id,),
        ).fetchall()
    return {
        "top_level": [f"{p}/" if t == "dir" else p for p, t in top],
        "directories": [d[0] + "/" for d in dirs],
        "file_types": {e or "(none)": c for e, c in exts},
    }
