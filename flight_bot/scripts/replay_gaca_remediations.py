"""Read-only audit of saved verified GACA outcomes and their next actions."""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from flight_bot.gaca_status import interpret_gaca_remediation


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    uri = f"file:{args.database.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """SELECT id,complaint_id,reference,case_status,response_text,status
           FROM gaca_status_checks
           WHERE response_text IS NOT NULL AND trim(response_text)<>''
           ORDER BY id DESC LIMIT ?""",
        (max(1, min(args.limit, 1000)),),
    ).fetchall()
    output = []
    for row in rows:
        result = dict(row)
        decision = interpret_gaca_remediation(
            result["response_text"],
            case_status=result.get("case_status") or "",
        )
        output.append({
            "check_id": result["id"],
            "complaint_id": result["complaint_id"],
            "reference": result["reference"],
            "check_status": result["status"],
            **asdict(decision),
        })
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
