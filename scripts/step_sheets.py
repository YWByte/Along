"""Export one contact sheet per task step from a read-only database, for human labelling of replays.

Output goes to --out (outside the project); the source data directory is never written.
"""

import argparse
import sqlite3
from pathlib import Path

from along.config import default_data_dir
from along.sheets import contact_sheet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--columns", type=int, default=8)
    args = parser.parse_args()
    db = sqlite3.connect((args.data_dir / "along.sqlite3").as_uri() + "?mode=ro", uri=True)
    rows = db.execute(
        "SELECT plan_revision,step_id,path,captured FROM media WHERE task_id=? AND kind='frame' "
        "AND expired=0 ORDER BY captured,seq",
        (args.task_id,),
    ).fetchall()
    groups = {}
    for revision, step, path, captured in rows:
        groups.setdefault((revision, step), []).append((args.data_dir / path, captured))
    for (revision, step), frames in groups.items():
        frames = [f for f in frames if f[0].is_file()]
        if not frames:
            continue
        target = args.out / f"r{revision}-{step}.jpg"
        contact_sheet([p for p, _ in frames], [t for _, t in frames], target, args.columns, 240)
        print(f"{target} frames={len(frames)}", flush=True)


if __name__ == "__main__":
    main()
