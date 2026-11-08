"""One-time original five-minute Spanish pilot for an owner-connected channel.

Uses at most TWO explicit Gemini API requests and never enables daily scripts,
auto-uploads, or paid render. Stores the script and links it to an unassigned
channel idea/thumbnail. The existing factory capacity estimator is respected.
One persistent status row makes startup idempotent across deploys.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path

RUN_ID="codigo-fudoshin-5m-review-v1"
VOICE="es-ES-AlvaroNeural"
EXPECTED_LANGUAGE="es"
THREAD=threading.Lock()

def schema(studio):
    with studio.db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS network_five_min_pilots(
            id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '',
            idea_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            words INTEGER NOT NULL DEFAULT 0,
            scenes INTEGER NOT NULL DEFAULT 0,
            estimated_free_mb INTEGER NOT NULL DEFAULT 0,
            estimated_working_mb INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT ''
        )""")

def status(studio):
    with studio.db() as db:
        row=db.execute("""SELECT channel_id,status,started_at,completed_at,
                  project_id,idea_id,title,words,scenes,
                  estimated_free_mb,estimated_working_mb,error
                  FROM network_five_min_pilots WHERE id=?""",(RUN_ID,)).fetchone()
    return dict(row) if row else None

def find_idea(studio):
    import studio_channel_network as network
    # Pilot is bound to exactly the owner-configured connected-channel identity.
    youtube_id=os.getenv("STUDIO_SEED_CHANNEL_YOUTUBE_ID","").strip()
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}",youtube_id):
        return None
    with studio.db() as db:
        row=db.execute("""SELECT i.id AS idea_id,i.channel_id,i.topic,i.title,i.hook
         FROM channel_network_ideas i
         JOIN channel_network_channels c ON c.id=i.channel_id
         WHERE c.yt_channel_id=? AND c.language='es'
           AND c.enabled=0
           AND i.project_id='' AND i.scheduled_at=''
           AND i.status IN ('draft','script_backend_missing')
         ORDER BY i.created_at DESC LIMIT 1""",(youtube_id,)).fetchone()
    return dict(row) if row else None

def run_once(studio,idea):
    import studio_channel_network as network
    import studio_factory as factory
    try:
        subject=("poner límites sin perder la calma, sin confundir "
                 "serenidad con pasividad")
        desired_title="Poner límites con calma: lo que la serenidad NO significa"
        chapters=(
          ("ACTO 1 - La falsa paz",
           "Define el dilema con un gancho directo; distingue calma de silencio "
           "ante el abuso, una situación cotidiana hipotética y una pregunta potente."),
          ("ACTO 2 - La decisión consciente",
           "Describe una manera concreta de poner límites: reconocer el hecho, "
           "expresar la necesidad, actuar con firmeza y elegir consecuencias realistas. "
           "Incluye dos ejemplos cotidianos imaginarios y cierre reflexivo.")
        )
        entries=[]
        for i,(act,description) in enumerate(chapters,1):
            prompt=(
              "Escribe un guion ORIGINAL para un vídeo documental de aproximadamente "
              "cinco minutos del canal de filosofía práctica Código Fudoshin, "
              "en español natural. Tema: "+subject+". "
              "Esta es la parte "+str(i)+" de 2, "+act+". "
              +description+
              " Produce EXACTAMENTE 16 escenas consecutivas de narración, "
              "cada una con 23 a 28 palabras en español. "
              "NO escribas títulos repetitivos, instrucciones de cámara ni "
              "números de escena dentro de la narración hablada. "
              "Para cada escena añade busca_visual con 2 a 6 palabras EN INGLÉS, "
              "describiendo objetos o acciones concretas que un banco de vídeo "
              "puede encontrar. Sin metraje ajeno no autorizado. "
              "No copies títulos, miniaturas, historias ni la estructura literal "
              "de los vídeos de referencia; tampoco atribuyas citas inventadas "
              "a filósofos, ni presentes ejemplos ficticios como hechos reales. "
              "Evita promesas médicas o psicológicas, y escribe sin clickbait engañoso. "
              "Mantén un ritmo narrativo cinematográfico y frases claras para TTS. "
              "Devuelve SOLO JSON: {\"cenas\":[{\"texto\":\"...\","
              "\"busca_visual\":\"...\"}]}."
            )
            result=factory.gemini_json(prompt)
            block=result.get("cenas") or []
            if not isinstance(block,list) or not 13<=len(block)<=18:
                raise RuntimeError("Gemini returned an invalid number of scenes")
            for item in block:
                if not isinstance(item,dict):
                    raise RuntimeError("Invalid scene structure")
                body=str(item.get("texto") or "").strip()
                query=str(item.get("busca_visual") or "").strip()
                count=len(body.split())
                if not 16<=count<=38 or len(body)<80 or not 4<=len(query)<=180:
                    raise RuntimeError("One or more scenes are too short or malformed")
                entries.append({"text":body[:8000],"query":query[:180]})
        words=sum(len(x["text"].split()) for x in entries)
        if not (650<=words<=1000 and 26<=len(entries)<=36):
            raise RuntimeError(f"Invalid estimated runtime: words={words}, scenes={len(entries)}")
        project=studio.create_project(studio.NewProject(
            title=desired_title,topic=subject[:240],duration_minutes=5,
            ratio="16:9",voice=VOICE))
        pid=project["id"]
        studio.set_script(pid,"\n\n".join(x["text"] for x in entries),
                          structured=entries)
        current=studio.get_project(pid)
        if current.get("voice")!=VOICE:
            with studio.db() as db:
                db.execute("UPDATE projects SET voice=? WHERE id=?",(VOICE,pid))
            current=studio.get_project(pid)
        if current.get("voice")!=VOICE or len(current.get("scenes") or [])!=len(entries):
            raise RuntimeError("Spanish project did not retain voice or scenes")
        estimate=factory.estimate(studio,current)
        # Reserve no production resources if the existing worker lacks capacity.
        # The standard channel-pipeline worker only queues it after capacity rises.
        final_state="render_queued" if estimate["can_render_here"] else "render_waiting_storage"
        with studio.db() as db:
            db.execute("""UPDATE channel_network_ideas SET
                project_id=?,status=?,duration_minutes=5 WHERE id=? AND project_id=''""",
                (pid,final_state,idea["idea_id"]))
            db.execute("""UPDATE network_five_min_pilots
                SET status=?,completed_at=?,project_id=?,idea_id=?,title=?,words=?,
                    scenes=?,estimated_free_mb=?,estimated_working_mb=?
                WHERE id=?""",
                (final_state,network.iso(network.now()),pid,idea["idea_id"],
                 desired_title,words,len(entries),
                 int(estimate["available_disk_mb"]),int(estimate["estimated_peak_working_mb"]),
                 RUN_ID))
        if estimate["can_render_here"]:
            factory.queue(studio,"autopilot",project_id=pid,
                          title=desired_title,duration=5,ratio="16:9")
        print("[network-5min] SUCCESS Spanish script; scenes=%d words=%d status=%s "
              "free=%sMB need=%sMB; no upload; no daily automation" %
              (len(entries),words,final_state,estimate["available_disk_mb"],
               estimate["estimated_peak_working_mb"]),flush=True)
    except Exception as exc:
        error=type(exc).__name__
        try:
            import httpx
            if isinstance(exc,httpx.HTTPStatusError):
                error+=" HTTP "+str(exc.response.status_code)
        except ImportError:
            pass
        with studio.db() as db:
            db.execute("""UPDATE network_five_min_pilots SET status='failed',
                completed_at=?,error=? WHERE id=?""",
                (network.iso(network.now()),error,RUN_ID))
        print("[network-5min] FAILED "+error+
              "; no retries, video uploads or further paid requests",flush=True)

def install(studio):
    import studio_channel_network as network
    schema(studio)

    @studio.app.get("/api/network/five-minute-pilot")
    def pilot_status():
        item=status(studio)
        return {"pilot":item,"gemini_configured":bool(os.getenv("GEMINI_API_KEY")),
                "youtube_publish_enabled":False,
                "note":"Roteiro em espanhol; render com verificação de espaço. "
                       "Publicação do canal permanece desligada."}

    @studio.app.on_event("startup")
    def startup():
        if not os.getenv("GEMINI_API_KEY"):
            return
        with THREAD:
            idea=find_idea(studio)
            if not idea:
                return
            with studio.db() as db:
                cur=db.execute("""INSERT OR IGNORE INTO network_five_min_pilots
                  (id,channel_id,status,started_at)
                  VALUES(?,?,?,?)""",
                  (RUN_ID,idea["channel_id"],"running",network.iso(network.now())))
                if cur.rowcount != 1:
                    return
            threading.Thread(target=run_once,args=(studio,idea),
                             name="owner-five-min-spanish-script",
                             daemon=True).start()

if __name__=="__main__":
    app=Path("/app/app.py")
    if not app.exists():
        raise SystemExit("Missing app.py")
    marker="\nimport studio_five_min_pilot as _five_min_pilot\n"
    data=app.read_text(encoding="utf-8")
    if marker not in data:
        app.write_text(data+marker+
                       '_five_min_pilot.install(__import__("sys").modules[__name__])\n',
                       encoding="utf-8")
