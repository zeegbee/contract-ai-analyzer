"""Статическая проверка миграций: одна голова и отсутствие неявно разрушительных операций.

Операция, удаляющая данные, допускается в upgrade() только с комментарием
`# destructive-approved: <ссылка на согласование>` в той же миграции.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

VERSIONS = Path(__file__).resolve().parents[2] / "app" / "db" / "migrations" / "versions"
DESTRUCTIVE = {"drop_table", "drop_column", "drop_index", "drop_constraint", "execute", "alter_column"}
# SQL, изменяющий или удаляющий данные/схему, — недопустим в миграции с пометкой `# non-destructive-sql:`.
FORBIDDEN_SQL = re.compile(
    r"\bDROP\s|\bDELETE\s+FROM\b|\bTRUNCATE\b|\bUPDATE\s+\S+\s+SET\b|\bALTER\s+TABLE\b|\bALTER\s+TYPE\b",
    re.IGNORECASE,
)


def _upgrade_calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            return {
                call.func.attr
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "op"
            }
    return set()


def test_single_head() -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(VERSIONS.parent))
    assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1


def _upgrade_strings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            nodes = ast.walk(node)
            return [n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    return []


def _non_destructive_sql(path: Path, risky: set[str]) -> bool:
    """`op.execute` с пометкой `# non-destructive-sql:` допустим, если SQL ничего не удаляет и не меняет."""
    if risky != {"execute"} or "# non-destructive-sql:" not in path.read_text(encoding="utf-8"):
        return False
    sql = "\n".join(_upgrade_strings(path))
    return not FORBIDDEN_SQL.search(sql)


def test_no_unapproved_destructive_operations() -> None:
    offenders = []
    for path in sorted(VERSIONS.glob("*.py")):
        risky = _upgrade_calls(path) & DESTRUCTIVE
        text = path.read_text(encoding="utf-8")
        if risky and "# destructive-approved:" not in text and not _non_destructive_sql(path, risky):
            offenders.append(f"{path.name}: {sorted(risky)}")
    assert not offenders, "Разрушительные операции без согласования: " + "; ".join(offenders)


def test_non_destructive_sql_marker_rejects_data_changes(tmp_path: Path) -> None:
    migration = tmp_path / "m.py"
    for sql, allowed in (
        ("CREATE TRIGGER t BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION f()", True),
        ("DROP TABLE documents", False),
        ("DELETE FROM documents", False),
        ("TRUNCATE audit_log", False),
        ("UPDATE users SET role = 'viewer'", False),
        ("ALTER TABLE users DROP COLUMN email", False),
    ):
        migration.write_text(
            f'# non-destructive-sql: test\ndef upgrade():\n    op.execute("{sql}")\n', encoding="utf-8"
        )
        assert _non_destructive_sql(migration, {"execute"}) is allowed, sql
