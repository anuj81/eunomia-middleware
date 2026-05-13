"""MySQL execution client. Reads connection params from settings.database.

The password must come from the DB_PASSWORD environment variable (or via the
prefixed EUNOMIA_DATABASE__PASSWORD form). It is never read from YAML.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import mysql.connector

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)


def _connection_kwargs(settings: Settings) -> Dict[str, Any]:
    db = settings.database
    if not db.password:
        logger.warning(
            "database.password is empty — set DB_PASSWORD in your .env "
            "or shell environment."
        )
    return {
        "host": db.host,
        "port": db.port,
        "user": db.user,
        "password": db.password or "",
        "database": db.name,
    }


async def execute_query(
    sql: str,
    settings: Optional[Settings] = None,
) -> List[Dict[str, Any]]:
    """Run the validated SQL on MySQL. Returns rows as a list of dicts.

    Non-JSON-serializable values (Decimal, datetime, ...) are coerced to str
    so the result can be handed directly to ``json.dumps``.
    """
    s = settings or get_settings()
    conn_kwargs = _connection_kwargs(s)

    def _run() -> List[Dict[str, Any]]:
        conn = mysql.connector.connect(**conn_kwargs)
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(sql)
            rows = cursor.fetchall()
            cursor.close()
        finally:
            conn.close()
        # Stringify non-JSON-safe values once here so downstream serialization
        # is trivial.
        return [
            {k: (str(v) if v is not None else None) for k, v in row.items()}
            for row in rows
        ]

    logger.debug(
        "Executing SQL on %s:%s/%s as %s",
        conn_kwargs["host"], conn_kwargs["port"],
        conn_kwargs["database"], conn_kwargs["user"],
    )
    return await asyncio.to_thread(_run)
