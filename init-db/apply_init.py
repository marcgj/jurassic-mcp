from __future__ import annotations

import time
from pathlib import Path

try:
    import ibm_db as ifx  # type: ignore[import-untyped]
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("ibm_db is required for init sidecar") from exc

from jurassic_mcp.config import load_config
from jurassic_mcp.db import connect


def _split_sql_script(sql_text: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []

    for line in sql_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        current.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(current).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            current = []

    tail = "\n".join(current).strip().rstrip(";").strip()
    if tail:
        statements.append(tail)

    return statements


def _database_exists(cfg_path: str, db_name: str) -> bool:
    cfg = load_config(cfg_path)
    with connect(cfg.informix, "sysmaster") as conn:
        stmt = ifx.exec_immediate(
            conn,
            f"SELECT name FROM sysdatabases WHERE name = '{db_name}'",
        )
        if not stmt or isinstance(stmt, bool):
            return False
        row = ifx.fetch_assoc(stmt)
        return isinstance(row, dict) and str(row.get("name", "")).strip() == db_name


def main() -> int:
    cfg_path = "/app/config.yml"
    target_db = "stores_demo"
    script_path = Path("/work/init.sql")

    for _ in range(60):
        try:
            break
        except Exception:
            time.sleep(2)
    else:
        print("[informix-init] Informix not ready after timeout")
        return 1

    sql_text = script_path.read_text(encoding="utf-8")
    statements = _split_sql_script(sql_text)

    cfg = load_config(cfg_path)
    with connect(cfg.informix, "sysmaster") as conn:
        if _database_exists(cfg_path, target_db):
            print("[informix-init] dropping existing stores_demo")
            ifx.exec_immediate(conn, f"DROP DATABASE {target_db}")

        print("[informix-init] applying init.sql")
        for statement in statements:
            ifx.exec_immediate(conn, statement)

    print("[informix-init] init.sql applied successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
