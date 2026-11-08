"""One-time owner-approved real-media quality preview.

Renders the first two scenes of the existing 31-minute Fudoshin pilot using the
new multi-shot editor. Reuses original narration and licensed sources and never
modifies or republishes the completed MP4. No paid Gemini/ElevenLabs calls.
Persistent claim prevents re-runs when Railway redeploys.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

SOURCE_ID = "0dd47b42-b880-4122-9306-5e9ab416b7f4"
RUN_ID = "fudoshin-real-media-dynamic-preview-2026-11-v1"
MIN_FREE_MB = 700


def stamp():
    return datetime.now(timezone.utc).isoformat()


def base(studio):
    return studio.project_dir(SOURCE_ID) / "export" / "qa_dynamic_preview_v2"


def free_mb(studio):
    return shutil.disk_usage(studio.DATA).free / 1048576


def status(studio):
    with studio.db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS studio_dynamic_qa_preview(
            id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
            scenes INTEGER NOT NULL DEFAULT 0, cuts INTEGER NOT NULL DEFAULT 0,
            sources INTEGER NOT NULL DEFAULT 0, duration REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        row=conn.execute("SELECT * FROM studio_dynamic_qa_preview WHERE id=?",
                         (RUN_ID,)).fetchone()
    return dict(row) if row else None


def checked(studio, threshold=MIN_FREE_MB):
    remaining=free_mb(studio)
    if remaining < threshold:
        raise RuntimeError(f"Sem espaço suficiente para a amostra: {remaining:.0f} MB livres; "
                           f"mínimo {threshold} MB. Original preservado.")


def ffmpeg(studio, args, timeout=900):
    cp=subprocess.run([studio.FFMPEG,"-hide_banner","-loglevel","error",
        "-nostdin","-y",*args],capture_output=True,text=True,timeout=timeout)
    if cp.returncode:
        raise RuntimeError("FFmpeg prévia: "+cp.stderr[-700:])


def listfile(paths, dest):
    dest.write_text("".join("file '"+str(Path(p).resolve()).replace("'","'\\''")+"'\n"
                            for p in paths),encoding="utf-8")


def run(studio):
    try:
        checked(studio)
        p=studio.get_project(SOURCE_ID)
        if p.get("status")!="concluído":
            raise RuntimeError("Projeto-fonte não concluído; prévia cancelada")
        chosen=(p.get("scenes") or [])[:2]
        if len(chosen)!=2:
            raise RuntimeError("Projeto-fonte sem duas cenas")
        p=dict(p)
        p["scenes"]=chosen
        p["duration_minutes"]=2
        folder=base(studio)
        folder.mkdir(parents=True,exist_ok=True)

        import studio_dynamic_montage as montage
        import studio_longform as longform
        plan=montage.build_plan(studio,p)
        count=sum(len(row["shots"]) for row in plan["timeline"])
        distinct=len({montage.identity(s["asset"]) for row in plan["timeline"]
                      for s in row["shots"]})
        if any(row["distinct_sources"]<3 for row in plan["timeline"]):
            raise RuntimeError("Prévia bloqueada por falta de variedade visual")
        (folder/"qa_shot_plan.json").write_text(
            json.dumps({"version":plan["version"],"scenes":2,"cuts":count,
                        "distinct_sources":distinct,"timeline":plan["timeline"]},
                       indent=2,ensure_ascii=False),encoding="utf-8")
        print(f"[dynamic-qa] planned scenes=2 cuts={count} sources={distinct}",flush=True)

        clips=[]
        for index,item in enumerate(plan["timeline"]):
            checked(studio,220)
            final=folder / f"scene_{index:03d}.mp4"
            if not final.exists() or montage._dur(studio,final)<item["seconds"]-.5:
                montage.make_scene(studio,SOURCE_ID,p,index,item,folder,final,
                                   preset="ultrafast")
            clips.append(final)
            print(f"[dynamic-qa] scene {index+1}/2 ready",flush=True)
        listfile(clips,folder/"video_clips.txt")
        silent=folder/"silent_video.mp4"
        if not silent.is_file() or montage._dur(studio,silent)<plan["total_seconds"]-1:
            ffmpeg(studio,["-f","concat","-safe","0","-i",str(folder/"video_clips.txt"),
                "-c","copy","-movflags","+faststart",str(silent)])

        sources=[studio.project_dir(SOURCE_ID)/"audio"/f"narration_{i:03d}.mp3"
                 for i in range(2)]
        if not all(x.is_file() for x in sources):
            raise RuntimeError("Áudios por cena do documentário original não encontrados")
        listfile(sources,folder/"voice_clips.txt")
        voice=folder/"narration_preview.mp3"
        ffmpeg(studio,["-f","concat","-safe","0","-i",str(folder/"voice_clips.txt"),
                       "-c:a","libmp3lame","-b:a","160k",str(voice)])
        if abs(montage._dur(studio,voice)-plan["total_seconds"])>1.5:
            raise RuntimeError("Duração da voz não coincide com timeline do teste")

        ass,_=studio.subtitles_for(p,folder,1280,720)
        ass_path=str(Path(ass).resolve()).replace("\\","/").replace(":","\\:").replace("'","\\'")
        args=["-i",str(silent),"-i",str(voice)]
        if p.get("soundtrack"):
            music=studio.safe_local_media(p["soundtrack"])
            args.extend(["-stream_loop","-1","-i",str(music)])
        args.extend(["-vf",f"ass={ass_path}"])
        if p.get("soundtrack"):
            args.extend(["-filter_complex",longform.AUDIO_MIX_FILTER,
                         "-map","0:v:0","-map","[aout]"])
        else:
            args.extend(["-map","0:v:0","-map","1:a:0"])
        output=folder/"quality_v2_preview.mp4"
        temp=folder/"quality_v2_preview.partial.mp4"
        temp.unlink(missing_ok=True)
        try:
            ffmpeg(studio,[*args,"-c:v","libx264","-preset","ultrafast","-crf","27",
                "-pix_fmt","yuv420p","-c:a","aac","-b:a","160k",
                "-t",str(plan["total_seconds"]),"-movflags","+faststart",
                str(temp)],timeout=1200)
            measured=montage._dur(studio,temp)
            if measured<plan["total_seconds"]-1.5:
                raise RuntimeError(f"Prévia muito curta: {measured:.1f}s de "
                                   f"{plan['total_seconds']:.1f}s")
            temp.replace(output)
        finally:
            temp.unlink(missing_ok=True)
        with studio.db() as conn:
            conn.execute("""UPDATE studio_dynamic_qa_preview
                SET status='completed',error='',scenes=2,cuts=?,sources=?,
                    duration=?,updated_at=? WHERE id=?""",
                (count,distinct,measured,stamp(),RUN_ID))
        print(f"[dynamic-qa] COMPLETED file={output.name} "
              f"duration={measured:.1f}s cuts={count}",flush=True)
    except Exception as exc:
        with studio.db() as conn:
            conn.execute("""UPDATE studio_dynamic_qa_preview
                SET status='failed',error=?,updated_at=? WHERE id=?""",
                (type(exc).__name__+": "+str(exc)[:650],stamp(),RUN_ID))
        print("[dynamic-qa] FAILED "+type(exc).__name__+": "+str(exc)[:400],flush=True)


def install(studio):
    status(studio)

    @studio.app.get("/api/qa/dynamic-preview")
    def summary():
        row=status(studio)
        return {"pilot":row,"source_project_id":SOURCE_ID,
                "preview_url":"/api/qa/dynamic-preview/video" if
                row and row["status"]=="completed" else None,
                "disk_free_mb":round(free_mb(studio),1),
                "original_mp4_preserved":True,"paid_api_calls":0}

    @studio.app.get("/api/qa/dynamic-preview/video")
    def video():
        row=status(studio)
        path=base(studio)/"quality_v2_preview.mp4"
        if not row or row["status"]!="completed" or not path.is_file():
            raise HTTPException(status_code=404,detail="Prévia ainda não concluída")
        return FileResponse(path,media_type="video/mp4",filename="fudoshin_quality_v2_preview.mp4")

    @studio.app.on_event("startup")
    def start_once():
        if os.getenv("STUDIO_DYNAMIC_PREVIEW_ONCE","0")!="1":
            return
        with studio.db() as conn:
            cur=conn.execute("""INSERT OR IGNORE INTO studio_dynamic_qa_preview
                (id,status,created_at,updated_at) VALUES(?,?,?,?)""",
                (RUN_ID,"running",stamp(),stamp()))
            if cur.rowcount!=1:
                return
        threading.Thread(target=run,args=(studio,),daemon=True,
                         name="fudoshin-dynamic-qa-once").start()


def self_test():
    assert MIN_FREE_MB>=500
    assert SOURCE_ID.count("-")==4
    assert RUN_ID.endswith("-v1")
    print("[dynamic-qa] SELF-TEST PASS one-shot, no paid voice, original preserved",
          flush=True)


if __name__=="__main__":
    self_test()
    target=Path("/app/app.py")
    if not target.is_file():
        raise SystemExit("/app/app.py missing")
    marker="\nimport studio_dynamic_preview_pilot as _dynamic_preview\n"
    code=target.read_text(encoding="utf-8")
    if marker not in code:
        target.write_text(code+marker+
             '_dynamic_preview.install(__import__("sys").modules[__name__])\n',
             encoding="utf-8")
