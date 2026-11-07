"""One-time, ID-allowlisted cleanup explicitly requested by the studio owner.
Preserves the active two-minute documentary and OAuth/session state.
Safe to re-run; never deletes arbitrary project directories.
"""
from pathlib import Path
import shutil
import sqlite3

ROOT = Path("/app/data")
DB = ROOT / "studio.sqlite3"
PROJECTS = ROOT / "projects"
KEEP = "aad807ab-18c8-44a5-b287-8ac97ce1a4c2"
DELETE_IDS = (
    "f12354fc-f1e8-446d-9837-7b748d0e4e2a",
    "69860031-4639-4e84-b375-62fbc35495f9",
    "af06cbfc-3491-455f-b91e-7e7cddc114d9",
    "a43fe655-027f-45e0-8e9b-1e5887b8acef",
)

def main():
    if not DB.is_file():
        print("[cleanup] No persisted DB; skipping", flush=True)
        return
    con = sqlite3.connect(f"file:{DB}?mode=rw", uri=True, timeout=30)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        existing = {r[0] for r in con.execute("SELECT id FROM projects")}
        if KEEP not in existing:
            print("[cleanup] Keep-project missing; refusing deletion", flush=True)
            return
        selected = [pid for pid in DELETE_IDS if pid in existing]
        if not selected:
            print("[cleanup] Already cleaned; preserved active project", flush=True)
            return
        with con:
            for pid in selected:
                con.execute("DELETE FROM projects WHERE id=?", (pid,))
        for pid in selected:
            target = PROJECTS / pid
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
        print(f"[cleanup] Removed {len(selected)} old projects; preserved {KEEP}", flush=True)
    finally:
        con.close()

if __name__ == "__main__":
    main()
