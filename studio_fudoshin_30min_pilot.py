"""Owner-requested one-shot 30-minute Código Fudoshin pilot.

The module is installed with the existing Railway app. It requests exactly
one Spanish original documentary script from the existing Gemini-backed factory
(1 outline + 6 chapter calls) then queues a single FFmpeg autopilot render.
It does NOT enable daily channel automation, external uploads or paid services
beyond the requested Gemini pilot. A durable record prevents retriggering on
redeploy. Projects already finished are never modified.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

PILOT="codigo-fudoshin-30min-2026-11-v1"
TITLE="Antes de reaccionar: seis decisiones que protegen tu paz interior"
TOPIC=(
    "Documental original en español para el canal Código Fudoshin, dirigido "
    "a adultos interesados en el estoicismo práctico y la psicología cotidiana. "
    "Tema central: cómo elegir una respuesta serena ante provocaciones, "
    "conflictos de pareja y familia, presiones sociales, trabajo, "
    "expectativas ajenas, redes sociales y decisiones difíciles. "
    "Estructura seis capítulos progresivos y narrativos de aproximadamente "
    "cinco minutos. Narración natural en español, atractiva, profunda, "
    "con ejemplos concretos, transiciones y una conclusión aplicable. "
    "Evita repetir el documental anterior sobre poner límites con calma. "
    "No inventes afirmaciones científicas, citas de filósofos ni datos "
    "históricos; cuando una afirmación no sea verificable exprésala "
    "como reflexión, no como hecho demostrado. Evita títulos sensacionalistas "
    "y el material visual específico de puentes o carreteras salvo que "
    "hable de ellos. Imágenes documentales ilustrativas, sin suplantar "
    "grabaciones reales de eventos."
)

def _now():
    return datetime.now(timezone.utc).isoformat()

def _schema(studio):
    with studio.db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS studio_owner_30min_pilot(
           id TEXT PRIMARY KEY,
           stage TEXT NOT NULL,
           project_id TEXT NOT NULL DEFAULT '',
           factory_job_id TEXT NOT NULL DEFAULT '',
           note TEXT NOT NULL DEFAULT '',
           created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL)""")

def _state(studio):
    with studio.db() as db:
        row=db.execute("SELECT * FROM studio_owner_30min_pilot WHERE id=?",
                       (PILOT,)).fetchone()
    return dict(row) if row else None

def _update(studio,stage,project_id=None,job_id=None,note=None):
    with studio.db() as db:
        db.execute("""UPDATE studio_owner_30min_pilot SET
            stage=?,project_id=COALESCE(?,project_id),
            factory_job_id=COALESCE(?,factory_job_id),
            note=COALESCE(?,note),updated_at=? WHERE id=?""",
            (stage,project_id,job_id,note,_now(),PILOT))

def _attach_channel(studio,pid):
    """Attach source project to existing Fudoshin channel without new YT research."""
    import studio_channel_network as net
    with studio.db() as db:
        ch=db.execute("""SELECT id FROM channel_network_channels
             WHERE lower(name) LIKE '%fudoshin%' LIMIT 1""").fetchone()
    if not ch:
        print("[fudoshin-pilot] Existing Fudoshin channel not found: "
              "project kept unlinked",flush=True)
        return False
    channel=net.get_channel(studio,ch["id"])
    with studio.db() as db:
        current=db.execute(
            "SELECT id FROM channel_network_ideas WHERE project_id=?",
            (pid,)).fetchone()
        if current:
            return True
    idea_id=str(uuid.uuid4())
    thumb=net.thumbnail(studio,channel,idea_id,
                        "Seis decisiones para recuperar la calma",
                        "Estoicismo práctico")
    refs={"source_urls":[],"seed_mode":"owner_approved_30_minute_pilot",
          "source_notice":"No se modeló material ni se copió ningún vídeo externo."}
    with studio.db() as db:
        db.execute("""INSERT OR IGNORE INTO channel_network_ideas
           (id,channel_id,topic,angle,title,hook,analysis_json,thumbnail_local,
             project_id,status,scheduled_at,duration_minutes,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
           (idea_id,channel["id"],"Estoicismo práctico y elección serena",
            "Seis decisiones de autocontrol sin clichés",TITLE,
            "El espacio entre la provocación y tu respuesta puede cambiarlo todo.",
            json.dumps(refs,ensure_ascii=False),thumb,pid,
            "render_queued","",30,_now()))
    return True

def _run(studio):
    import studio_factory as factory
    # Acquiring a durable claim makes repeated process restarts harmless.
    with studio.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT stage FROM studio_owner_30min_pilot WHERE id=?",
                       (PILOT,)).fetchone()
        if not row or row["stage"]!="queued":
            return
        db.execute("""UPDATE studio_owner_30min_pilot
           SET stage='writing',note='Gemini roteiro em espanhol: até 7 chamadas',
               updated_at=? WHERE id=?""",(_now(),PILOT))
    try:
        if not os.getenv("GEMINI_API_KEY"):
            raise RuntimeError("Gemini API key not configured")
        if factory.limit_int("STUDIO_FACTORY_MAX_RENDER_MINUTES",5,1,60)<30:
            raise RuntimeError("Pilot render limit not set to 30 minutes")
        free=__import__("shutil").disk_usage(studio.DATA).free/1048576
        if free<3200:
            raise RuntimeError("At least 3200 MB of free disk required before 30m pilot")
        pid=factory.generate_script(studio,TOPIC,30,"16:9",
                                    title=TITLE,language="es")
        # Project is now durable: never regenerate the paid script just
        # because storage/render fails later.
        _update(studio,"script_ready",project_id=pid,
                note="Roteiro espanhol concluído, preparando vídeo")
        try:
            _attach_channel(studio,pid)
        except (OSError,ValueError,RuntimeError,KeyError) as exc:
            print("[fudoshin-pilot] Channel metadata optional; continuing without link: "
                  +type(exc).__name__,flush=True)
        project=studio.get_project(pid)
        capacity=factory.estimate(studio,project)
        if not capacity["can_render_here"]:
            _update(studio,"waiting_capacity",
                    note="Roteiro concluído mas renderização bloqueada: "+
                    capacity["reason"][:140])
            return
        queued=factory.queue(studio,"autopilot",project_id=pid,
                             title=TITLE,duration=30,ratio="16:9")
        _update(studio,"render_queued",job_id=queued["id"],
                note="Render automático enfileirado, sem publicar no YouTube")
        print("[fudoshin-pilot] queued original Spanish 30m documentary "
              "project="+pid+" job="+queued["id"],flush=True)
    except Exception as exc:
        _update(studio,"failed",note=type(exc).__name__+": "+str(exc)[:300])
        print("[fudoshin-pilot] Failed safely: "+type(exc).__name__,flush=True)

def install(studio):
    _schema(studio)

    @studio.app.get("/api/pilot/fudoshin-30min")
    def status():
        row=_state(studio)
        if not row:return {"state":"not_requested"}
        info={**row,"requested_minutes":30,"language":"es",
              "automatic_publish":False,"daily_scheduler_enabled":False,
              "title":TITLE}
        if row["project_id"]:
            try:
                p=studio.get_project(row["project_id"])
                info["video_status"]=p.get("status")
                info["progress_percent"]=p.get("progress")
                info["error"]=p.get("error")
                if p.get("status")=="concluído":
                    info["state"]="video_ready"
            except (HTTPException,ValueError,RuntimeError):
                pass
        if row["factory_job_id"]:
            with studio.db() as db:
                job=db.execute(
                    "SELECT state,error FROM factory_jobs WHERE id=?",
                    (row["factory_job_id"],)).fetchone()
            if job:
                info["render_job"]=dict(job)
        return info

    @studio.app.on_event("startup")
    def one_time_start():
        # This is the explicitly requested one-time production, not a cron job.
        if os.getenv("STUDIO_FUDOSHIN_30MIN_PILOT")!="1":
            return
        with studio.db() as db:
            db.execute("""INSERT OR IGNORE INTO studio_owner_30min_pilot
                (id,stage,created_at,updated_at) VALUES(?,?,?,?)""",
                (PILOT,"queued",_now(),_now()))
        previous=_state(studio)
        if previous and previous["stage"] in ("failed","writing","waiting_capacity"):
            # Read-only error diagnostics: no second AI call is initiated.
            print("[fudoshin-pilot] EXISTING state="+previous["stage"]+
                  " detail="+str(previous.get("note") or "")[:320],flush=True)
        threading.Thread(target=_run,args=(studio,),
                daemon=True,name="fudoshin-owner-30min-pilot").start()

def self_test():
    assert len(TITLE)>15
    assert "Código Fudoshin" in TOPIC
    assert "seis capítulos" in TOPIC
    assert PILOT.endswith("-v1")
    print("[fudoshin-pilot] SELF-TEST PASS one-shot, Spanish, render only, no uploads",flush=True)

if __name__=="__main__":
    self_test()
    from pathlib import Path
    app=Path("/app/app.py")
    if not app.is_file():
        raise SystemExit("Missing /app/app.py")
    marker="\nimport studio_fudoshin_30min_pilot as _fudoshin_30min_pilot\n"
    code=app.read_text(encoding="utf-8")
    if marker not in code:
        app.write_text(code+marker+
            '_fudoshin_30min_pilot.install(__import__("sys").modules[__name__])\n',
            encoding="utf-8")
