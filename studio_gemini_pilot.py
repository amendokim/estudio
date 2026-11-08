"""One-time, one-request Gemini script pilot for the owner's Spanish channel.

The user specifically authorized a first test after configuring GEMINI_API_KEY.
This module never loops, auto-renders, schedules publication, or enables paid
daily automation. A persistent audit row prevents repeat calls after deploys.
"""
from __future__ import annotations
import os
import re
import threading
import time
from pathlib import Path

from fastapi import HTTPException

RUN_ID = "first-spanish-gemini-script-v2"
TARGET_VOICE = "es-ES-AlvaroNeural"


def schema(studio):
    with studio.db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS channel_network_gemini_pilots(
            id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            words INTEGER NOT NULL DEFAULT 0,
            scenes INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT ''
        )""")


def get_status(studio):
    with studio.db() as db:
        row = db.execute("""SELECT channel_id,status,started_at,completed_at,
                                    project_id,title,words,scenes,error
                            FROM channel_network_gemini_pilots WHERE id=?""",
                         (RUN_ID,)).fetchone()
    return dict(row) if row else None


def first_idea(studio):
    cid = os.getenv("STUDIO_SEED_CHANNEL_YOUTUBE_ID", "").strip()
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", cid):
        return None
    with studio.db() as db:
        row = db.execute("""SELECT i.id AS idea_id,i.channel_id,i.topic,i.title,i.hook
             FROM channel_network_ideas i
             JOIN channel_network_channels c ON c.id=i.channel_id
             WHERE c.yt_channel_id=? AND c.language='es'
               AND i.project_id='' AND i.scheduled_at=''
               AND i.status IN ('draft','script_backend_missing')
             ORDER BY i.created_at ASC LIMIT 1""", (cid,)).fetchone()
    return dict(row) if row else None


def generate_once(studio):
    import studio_channel_network as network
    schema(studio)
    if not os.getenv("GEMINI_API_KEY", "").strip():
        return {"status": "missing_key"}
    item = first_idea(studio)
    if not item:
        return {"status": "no_unlinked_spanish_idea"}
    with studio.db() as db:
        db.execute("INSERT OR IGNORE INTO channel_network_gemini_pilots "
                   "(id,channel_id,status,started_at) VALUES(?,?,?,?)",
                   (RUN_ID,item["channel_id"],"running",network.iso(network.now())))
        row = db.execute("SELECT status FROM channel_network_gemini_pilots WHERE id=?",
                         (RUN_ID,)).fetchone()
        if not row or row["status"]!="running":
            return get_status(studio)
        # Give only the first created worker the right to call the model.
        # If the process crashed after INSERT, status remains 'running' and
        # will not be retried until explicitly diagnosed by the owner.
        started = db.execute("SELECT started_at FROM channel_network_gemini_pilots WHERE id=?",
                             (RUN_ID,)).fetchone()["started_at"]
        if started != network.iso(network.now()):
            # insert_or_ignore alone is not sufficient for re-entrant calls,
            # so external startup calls are guarded by the single-thread model
            # plus persistent status.
            pass
    try:
        import studio_factory as factory
        prompt = (
            "Escribe un guion documental TOTALMENTE ORIGINAL en español natural "
            "para el canal Código Fudoshin, con una duración estimada de "
            "aproximadamente 2 minutos (de 210 a 290 palabras). "
            f"Tema: {item['topic']}. Enfoque: {item['hook']}. "
            "Analiza la diferencia entre serenidad y pasividad, cómo poner "
            "límites sin perder el autocontrol, y una aplicación práctica. "
            "No copies historias ni títulos de otros vídeos. No inventes "
            "citas de filósofos ni atribuciones históricas. "
            "Divide el texto en 8 o 9 escenas de narración, cada una con "
            "24 a 37 palabras y una búsqueda visual breve en inglés "
            "sobre acciones, personas y situaciones pertinentes. "
            "No añadas indicaciones escénicas al texto que se leerá. "
            'Devuelve JSON: {"titulo":"...","cenas":[{"texto":"...","busca_visual":"..."}]}.'
        )
        result = factory.gemini_json(prompt)
        scenes = result.get("cenas") or []
        if not isinstance(scenes,list) or not 7<=len(scenes)<=10:
            raise RuntimeError("Gemini não retornou 7 a 10 cenas válidas")
        entries=[]
        for scene in scenes:
            if not isinstance(scene,dict):
                raise RuntimeError("Cena fora do formato JSON esperado")
            text=str(scene.get("texto") or "").strip()
            query=str(scene.get("busca_visual") or "").strip()
            if len(text)<55 or len(query)<4:
                raise RuntimeError("Gemini gerou cena incompleta")
            entries.append({"text":text[:4000],"query":query[:180]})
        count=sum(len(s["text"].split()) for s in entries)
        if not 145<=count<=410:
            raise RuntimeError("Roteiro curto ou longo demais para o teste")
        project=studio.create_project(studio.NewProject(
            title=item["title"][:155],
            topic=item["topic"][:250],
            duration_minutes=2,
            ratio="16:9",
            voice=TARGET_VOICE))
        pid=project["id"]
        studio.set_script(pid,"\n\n".join(x["text"] for x in entries),structured=entries)
        current=studio.get_project(pid)
        if current.get("voice") != TARGET_VOICE:
            # Support older studio builds that ignore the voice on NewProject.
            with studio.db() as db:
                db.execute("UPDATE projects SET voice=? WHERE id=?",
                           (TARGET_VOICE,pid))
            current=studio.get_project(pid)
        if current.get("voice")!=TARGET_VOICE or len(current.get("scenes") or [])<7:
            raise RuntimeError("Projeto não preservou idioma ou cenas")
        with studio.db() as db:
            db.execute("""UPDATE channel_network_ideas SET project_id=?,status='script_ready'
                          WHERE id=? AND project_id=''""",(pid,item["idea_id"]))
            db.execute("""UPDATE channel_network_gemini_pilots SET
                status='success',completed_at=?,project_id=?,title=?,words=?,scenes=?
                WHERE id=?""",
                (network.iso(network.now()),pid,current["title"],count,len(entries),RUN_ID))
        print("[network-gemini] PASS: original Spanish script; scenes=%d words=%d; "
              "no render, no upload, no daily automation" % (len(entries),count),flush=True)
    except Exception as exc:
        # Don't surface provider payloads or any configuration secrets.
        label=type(exc).__name__
        try:
            import httpx
            if isinstance(exc,httpx.HTTPStatusError):
                label += ": HTTP " + str(exc.response.status_code)
        except ImportError:
            pass
        with studio.db() as db:
            db.execute("""UPDATE channel_network_gemini_pilots
                SET status='failed',completed_at=?,error=? WHERE id=?""",
                (network.iso(network.now()),label,RUN_ID))
        print("[network-gemini] FAIL: "+label+"; no retry or publication",flush=True)
    return get_status(studio)


def install(studio):
    schema(studio)
    @studio.app.get("/api/network/gemini-pilot")
    def status():
        return {"gemini_configured":bool(os.getenv("GEMINI_API_KEY")),
                "pilot":get_status(studio),
                "render_started":False,"youtube_upload_started":False,
                "automatic_daily_generation_enabled":
                    os.getenv("STUDIO_NETWORK_AUTOMATION_ENABLED")=="1"}

    @studio.app.on_event("startup")
    def one_time_script():
        if os.getenv("GEMINI_API_KEY") and first_idea(studio):
            # The database row is created synchronously before the thread starts.
            # Protect against duplicate app startups: only one fresh insert can
            # trigger the request, regardless of replica count.
            import studio_channel_network as network
            with studio.db() as db:
                cur=db.execute("""INSERT OR IGNORE INTO channel_network_gemini_pilots
                    (id,channel_id,status,started_at)
                    VALUES(?,?,?,?)""",
                    (RUN_ID,first_idea(studio)["channel_id"],"running",
                     network.iso(network.now())))
                fresh=cur.rowcount == 1
            if fresh:
                threading.Thread(target=generate_once,args=(studio,),
                                 name="one-gemini-spanish-script",daemon=True).start()


if __name__=="__main__":
    app=Path("/app/app.py")
    if not app.exists():
        raise SystemExit("Missing studio app.py")
    marker="\nimport studio_gemini_pilot as _gemini_pilot\n"
    text=app.read_text(encoding="utf-8")
    if marker not in text:
        app.write_text(text+marker+
            '_gemini_pilot.install(__import__("sys").modules[__name__])\n',
            encoding="utf-8")
