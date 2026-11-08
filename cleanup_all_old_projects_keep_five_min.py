"""Owner-authorized, ONE-TIME removal of all old studio test projects.

The 5-minute Código Fudoshin film is expressly preserved. Delete only the
five pre-identified, completed project records and their media directories.
Never touch shared DB data for channel accounts, OAuth tokens or new pilot.
Runs before uvicorn (no ongoing renders), prints size reclaimed.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DATA=Path("/app/data")
DB=DATA/"studio.sqlite3"
PROJECTS=DATA/"projects"
KEEP="8c855a6d-c95a-4642-bf9c-a46074404935"
DELETE=(
  "21c5810e-1aa6-49df-acff-60556f558a9d",
  "a161716b-3f1d-4d0c-9f99-f217472d92b4",
  "4f79c143-e8fe-4e01-8953-45e6bc4f80e1",
  "2b7fd986-2634-4e48-8c7f-aff527dff651",
  "aad807ab-18c8-44a5-b287-8ac97ce1a4c2",
)
STAMP=DATA/".owner_cleanup_for_five_minute_2026_11_08.json"
UUID=re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")

def disk_dir_bytes(path):
    total=0
    for current, directories, files in os.walk(path,followlinks=False):
        directories[:]=[d for d in directories if not (Path(current)/d).is_symlink()]
        for name in files:
            candidate=Path(current)/name
            if candidate.is_symlink():continue
            try:total+=candidate.stat().st_size
            except FileNotFoundError:pass
    return total

def main():
    if STAMP.is_file():
        print("[owner-cleanup] Already ran once; no deletion",flush=True)
        return
    if not DB.is_file() or not PROJECTS.is_dir() or PROJECTS.is_symlink():
        raise SystemExit("[owner-cleanup] Studio DB or projects directory missing; aborting")
    if KEEP in DELETE or not all(UUID.fullmatch(x) for x in (KEEP,*DELETE)):
        raise SystemExit("[owner-cleanup] Bad project ID allowlist")
    with sqlite3.connect(f"file:{DB}?mode=rw",uri=True,timeout=35) as conn:
        conn.row_factory=sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        current={row["id"]:row["status"] for row in conn.execute("SELECT id,status FROM projects")}
        if KEEP not in current:
            raise SystemExit("[owner-cleanup] Five-minute project missing: refusing all deletion")
        targets=[pid for pid in DELETE if pid in current]
        unsafe=[pid for pid in targets if current[pid] not in ("concluído","erro","roteiro pronto","pronto")]
        if unsafe:
            raise SystemExit("[owner-cleanup] Active projects detected; refusing deletion: "+str(unsafe))
        # Abort if anything beyond known historical tests and the protected film
        # exists. Never delete projects produced concurrently or by another user.
        unexpected=set(current)-(set(DELETE)|{KEEP})
        if unexpected:
            raise SystemExit("[owner-cleanup] Additional projects detected; manual review: "+str(sorted(unexpected)))
        estimate=sum(disk_dir_bytes(PROJECTS/pid) for pid in DELETE if (PROJECTS/pid).is_dir())
        with conn:
            # Keep channel ideas/thumbnails. Detach only deleted film IDs.
            tables={r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "channel_network_ideas" in tables:
                for pid in DELETE:
                    conn.execute("""UPDATE channel_network_ideas SET project_id='',
                       status=CASE WHEN status IN
                         ('script_ready','rendered_unplanned','render_failed','render_queued')
                         THEN 'draft' ELSE status END
                       WHERE project_id=?""",(pid,))
            if "channel_network_gemini_pilots" in tables:
                for pid in DELETE:
                    conn.execute("""UPDATE channel_network_gemini_pilots
                       SET project_id='',status='archived' WHERE project_id=?""",(pid,))
            if "factory_jobs" in tables:
                for pid in DELETE:
                    conn.execute("DELETE FROM factory_jobs WHERE project_id=?",(pid,))
            # Other long-lived metadata is preserved. Scenes should cascade.
            for pid in targets:
                conn.execute("DELETE FROM projects WHERE id=?",(pid,))
    # Database commit succeeded. Remove only known test project media folders.
    removed=0
    for pid in DELETE:
        target=PROJECTS/pid
        if target.is_symlink():
            raise SystemExit("[owner-cleanup] Symbolic project directory: abort")
        if target.is_dir():
            if target.resolve() != PROJECTS.resolve()/pid:
                raise SystemExit("[owner-cleanup] Unexpected project directory: abort")
            shutil.rmtree(target)
            removed+=1
    summary={"timestamp":datetime.now(timezone.utc).isoformat(),
             "preserved_project":KEEP,
             "deleted_project_records":targets,
             "deleted_project_directories":removed,
             "estimated_bytes_reclaimed":estimate,
             "disk_free_mb_after":round(shutil.disk_usage(DATA).free/1048576,1)}
    tmp=STAMP.with_suffix(".json.partial")
    tmp.write_text(json.dumps(summary,indent=2),encoding="utf-8")
    tmp.replace(STAMP)
    print("[owner-cleanup] COMPLETE deleted_records=%d deleted_dirs=%d reclaimed=%.1fMB "
          "free_after=%.1fMB retained_5min=%s" %
          (len(targets),removed,estimate/1048576,summary["disk_free_mb_after"],KEEP),
          flush=True)

if __name__=="__main__":
    main()
