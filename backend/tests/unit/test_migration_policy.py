"""Статическая проверка миграций: одна голова и отсутствие неявно разрушительных операций.

Операция, удаляющая данные, допускается в upgrade() только с комментарием
`# destructive-approved: <ссылка на согласование>` в той же миграции.
"""

from __future__ import annotations

import ast
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

VERSIONS = Path(__file__).resolve().parents[2] / "app" / "db" / "migrations" / "versions"
DESTRUCTIVE = {"drop_table", "drop_column", "drop_index", "drop_constraint", "execute", "alter_column"}


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


def test_no_unapproved_destructive_operations() -> None:
    offenders = []
    for path in sorted(VERSIONS.glob("*.py")):
        risky = _upgrade_calls(path) & DESTRUCTIVE
        if risky and "# destructive-approved:" not in path.read_text(encoding="utf-8"):
            offenders.append(f"{path.name}: {sorted(risky)}")
    assert not offenders, "Разрушительные операции без согласования: " + "; ".join(offenders)
