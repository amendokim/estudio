"""One-time, free, ~45-second editorial comparison proof for Código Fudoshin.

Reuses the first six already-approved Spanish narration scenes. No Gemini calls,
no uploads, no scheduled publication, and the completed 5-minute MP4 remains
untouched. Stop before the 500 MB Railway volume is exhausted.
"""
from __future__ import annotations
import os
import shutil
import sqlite3
import threading
from pathlib import Path
from datetime import datetime,timezone
from fastapi import HTTPException

ORIGINAL="8c855a6d-c95a-4642-bf9c-a46074404935"
RUN_ID="codigo-fudoshin-visual-broll-preview-v1"
RESERVE_MB=85
START_MB=150
_LOCAL=threading.local()

def free(studio):
    return shutil.disk_usage(studio.DATA).free/1048576

def schema(studio):
    with studio.db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS studio_editorial_previews(
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL DEFAULT '',
            source_project_id TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL)""")

def lookup(studio):
    with studio.db() as db:
        row=db.execute("SELECT * FROM studio_editorial_previews WHERE id=?",(RUN_ID,)).fetchone()
    return dict(row) if row else None

def _ensure_disk(studio, threshold=RESERVE_MB):
    n=free(studio)
    if n < threshold:
        raise RuntimeError("Espaço de disco baixo; prévia cancelada com "
                           f"{n:.0f} MB livres, preservando o MP4 anterior.")

def job(studio):
    pid=""
    try:
        _ensure_disk(studio,START_MB)
        parent=studio.get_project(ORIGINAL)
        if parent["status"]!="concluído":
            raise RuntimeError("Documentário original não está concluído")
        src=(parent.get("scenes") or [])[:6]
        if len(src)!=6:
            raise RuntimeError("A narrativa original não contém seis cenas")
        scenes=[{"text":str(x.get("text") or "").strip(),
                 "query":str(x.get("query") or "person thinking").strip()}
                for x in src]
        if any(len(s["text"].split())<12 for s in scenes):
            raise RuntimeError("Cenas incompletas; prévia não criada")
        draft=studio.create_project(studio.NewProject(
            title="Código Fudoshin — muestra de edición y B-roll",
            topic="Cómo poner límites con calma: prueba de imágenes y edición",
            duration_minutes=1,ratio="16:9",voice="es-ES-AlvaroNeural"))
        pid=draft["id"]
        studio.set_script(pid,"\n\n".join(x["text"] for x in scenes),
                          structured=scenes)
        with studio.db() as db:
            db.execute("UPDATE studio_editorial_previews SET project_id=?,status='rendering' WHERE id=?",
                       (pid,RUN_ID))
        _LOCAL.active=True
        print("[editorial-preview] selected 6 existing Spanish script scenes; "
              "no Gemini requests; no uploads; free=%.1fMB" % free(studio),flush=True)
        studio.autopilot_task(pid)
        done=studio.get_project(pid)
        if done["status"]!="concluído":
            raise RuntimeError("Render não concluído: "+str(done.get("error") or done["status"]))
        with studio.db() as db:
            db.execute("UPDATE studio_editorial_previews SET status='completed' WHERE id=?",
                       (RUN_ID,))
        print("[editorial-preview] DONE project=%s free=%.1fMB" %
              (pid,free(studio)),flush=True)
    except Exception as exc:
        with studio.db() as db:
            db.execute("UPDATE studio_editorial_previews SET status='failed',error=? WHERE id=?",
                       (type(exc).__name__+": "+str(exc)[:300],RUN_ID))
        print("[editorial-preview] FAILED "+type(exc).__name__+
              " free=%.1fMB; original video preserved" % free(studio),flush=True)
    finally:
        _LOCAL.active=False

def install(studio):
    schema(studio)
    old_ffmpeg=studio.run_ffmpeg
    old_fetch=studio.fetch_clip
    def ffmpeg(*args,**kwargs):
        if getattr(_LOCAL,"active",False):_ensure_disk(studio)
        return old_ffmpeg(*args,**kwargs)
    def fetch(scene,pid,*args,**kwargs):
        if getattr(_LOCAL,"active",False):_ensure_disk(studio)
        item=old_fetch(scene,pid,*args,**kwargs)
        if getattr(_LOCAL,"active",False):_ensure_disk(studio)
        return item
    studio.run_ffmpeg=ffmpeg
    studio.fetch_clip=fetch

    @studio.app.get("/api/editorial/preview")
    def preview():
        item=lookup(studio)
        return {"item":item,"disk_free_mb":round(free(studio),1),
                "source_preserved":True,
                "note":"Prévia de 6 cenas sem Gemini. O MP4 de 5 minutos permanece intacto."}

    @studio.app.on_event("startup")
    def startup():
        # Do not create a second project when storage is already too low.
        if free(studio)<START_MB:return
        try:
            parent=studio.get_project(ORIGINAL)
            if parent.get("status")!="concluído":return
        except Exception:
            return
        with studio.db() as db:
            cur=db.execute("""INSERT OR IGNORE INTO studio_editorial_previews
                       (id,source_project_id,status,created_at)
                       VALUES(?,?,?,?)""",
                       (RUN_ID,ORIGINAL,"preparing",
                        datetime.now(timezone.utc).isoformat()))
            if cur.rowcount!=1:return
        threading.Thread(target=job,args=(studio,),name="editorial-preview-v1",
                         daemon=True).start()

if __name__=="__main__":
    assert RESERVE_MB>50 and START_MB>RESERVE_MB
    root=Path("/app/app.py")
    if not root.exists():raise SystemExit("Missing app.py")
    mark="\nimport studio_editorial_preview as _editorial_preview\n"
    existing=root.read_text(encoding="utf-8")
    if mark not in existing:
        root.write_text(existing+mark+
                        '_editorial_preview.install(__import__("sys").modules[__name__])\n',
                        encoding="utf-8")
