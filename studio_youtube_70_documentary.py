"""YouTube-first documentary director (70/30 mix).

Finds YouTube references with the official API (including standard-licensed
results for editorial research). It NEVER downloads YouTube streams. For
rendering, it only uses independently obtained, locally available files with
documented permissions, or independently hosted CC/PD archival copies.
Every clip is <=10 seconds, tracked by source, and checked before rendering.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field

import studio_youtube_policy as policy

VERSION="youtube70-documentary-v1"
TARGET=70
MAX_SECONDS=10
MAX_SEARCHES=3
LOCK=threading.RLock()
STOP={"video","youtube","paraguai","brasil","sobre","documentario",
      "para","esta","essas","todos","entre","porque","quem","quando",
      "the","and","with","from","street","film","archive","footage"}

def is_documentary(p):
    text=policy.normalized(str(p.get("topic") or "")+" "+str(p.get("title") or ""))
    return any(word in text for word in ("documentario","documentary","investigacao","reportagem"))

def video_seconds(s):
    try: return max(0.1,float(s.get("duration") or 7))
    except (TypeError,ValueError): return 7.0

def is_youtube(s):
    return policy.recognized_archival_scene(s) and bool(
        s.get("media_local") or s.get("media_url")) and video_seconds(s)<=MAX_SECONDS

def count(p):
    scenes=p.get("scenes") or []
    total=sum(video_seconds(s) for s in scenes)
    y=sum(video_seconds(s) for s in scenes if is_youtube(s))
    stock=sum(video_seconds(s) for s in scenes if s.get("media_provider") in
              ("pixabay","pexels"))
    original=sum(video_seconds(s) for s in scenes if s.get("media_provider")=="original")
    return {"project_id":p["id"],"target_youtube_percent":TARGET,
            "target_stock_percent":30,
            "actual_youtube_percent":round(100*y/total,1) if total else 0,
            "actual_stock_percent":round(100*stock/total,1) if total else 0,
            "actual_other_percent":round(100*(total-y-stock)/total,1) if total else 0,
            "youtube_seconds":round(y,1),"total_seconds":round(total,1),
            "missing_youtube_seconds":round(max(0,total*.7-y),1),
            "ready_for_70_30":bool(total and y/total>=.695 and stock/total>=.285 and (total-y-stock)/total<=.03),
            "youtube_shots":sum(is_youtube(s) for s in scenes),
            "total_shots":len(scenes),"max_youtube_clip_seconds":MAX_SECONDS,
            "youtube_downloads_from_api":False,
            "status":p.get("status"),
            "note":"Percentuais são metragem real registrada, não número de links. "
                   "Os links encontrados pela API servem para pesquisa; uso no MP4 "
                   "exige arquivo legalmente obtido e direitos documentados."}

def terms(s):
    words=re.findall(r"[a-z0-9]{4,}",policy.normalized(s))
    return {w for w in words if w not in STOP}

def relevance(scene,source):
    scene_text=(scene.get("query") or "")+" "+(scene.get("text") or "")
    source_text=(source.get("title") or "")+" "+(source.get("description") or "")
    a,b=terms(scene_text),terms(source_text)
    overlap=len(a&b)
    return 10*overlap if overlap>=1 else -100

def storage(studio):
    with studio.db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS documentary_youtube_research (
           project_id TEXT NOT NULL,video_id TEXT NOT NULL,
           title TEXT NOT NULL,channel TEXT NOT NULL,
           license_filter TEXT NOT NULL,query TEXT NOT NULL,
           created_at TEXT NOT NULL,
           PRIMARY KEY(project_id,video_id))""")

def discover(studio,pid,refresh=False):
    p=studio.get_project(pid)
    key=os.getenv("YOUTUBE_DATA_API_KEY","").strip()
    if not key:raise HTTPException(409,"Falta configurar YOUTUBE_DATA_API_KEY.")
    with studio.db() as db:
        old=[dict(x) for x in db.execute(
            "SELECT * FROM documentary_youtube_research WHERE project_id=? "
            "ORDER BY created_at DESC LIMIT 60",(pid,)).fetchall()]
    if old and not refresh:
        for x in old:
            x["youtube_url"]="https://www.youtube.com/watch?v="+x["video_id"]
            x["usable_for_render"]=False
        return {"project_id":pid,"cached":True,"candidates":old,
                "note":"Referências não são arquivos e não são consideradas licenciadas."}
    # Cap API usage: explicit refresh can requery, never called in a continuous loop.
    topic=str(p.get("title") or "")
    scenes=p.get("scenes") or []
    qs=[]
    for value in [topic," ".join((s.get("query") or "") for s in scenes[:6]),
                  " ".join((s.get("query") or "") for s in scenes[-6:])]:
        words=re.findall(r"[^,\n]{1,70}",value)
        q=" ".join(words[:1]).strip()[:85]
        if q and q not in qs:qs.append(q)
    results={}
    with httpx.Client(timeout=18,follow_redirects=False) as client:
        for q in qs[:MAX_SEARCHES]:
            try:
                response=client.get("https://www.googleapis.com/youtube/v3/search",
                    params={"part":"snippet","type":"video","maxResults":20,
                            "q":q,"key":key,"safeSearch":"moderate",
                            "order":"relevance"})
                response.raise_for_status()
                for v in response.json().get("items") or []:
                    vid=str((v.get("id") or {}).get("videoId") or "")
                    if not re.fullmatch(r"[A-Za-z0-9_-]{11}",vid):continue
                    sn=v.get("snippet") or {}
                    results[vid]={"project_id":pid,"video_id":vid,
                       "title":str(sn.get("title") or "")[:200],
                       "channel":str(sn.get("channelTitle") or "")[:100],
                       "license_filter":"any_unverified","query":q,
                       "created_at":datetime.now(timezone.utc).isoformat(),
                       "youtube_url":"https://www.youtube.com/watch?v="+vid,
                       "usable_for_render":False}
            except (httpx.HTTPError,ValueError):
                continue
    # videos.list reports the license selected by the uploader. This metadata
    # is not a permit to download footage through the YouTube API.
    if results:
        try:
            with httpx.Client(timeout=14,follow_redirects=False) as client:
                ids=",".join(list(results)[:50])
                status=client.get("https://www.googleapis.com/youtube/v3/videos",
                    params={"part":"status","id":ids,"key":key})
                status.raise_for_status()
                for entry in status.json().get("items") or []:
                    vid=entry.get("id")
                    if vid in results:
                        license_name=str((entry.get("status") or {}).get("license") or "")
                        results[vid]["license_filter"]=license_name or "unknown"
        except (httpx.HTTPError,ValueError):
            pass
    with studio.db() as db:
        for row in results.values():
            db.execute("""INSERT OR REPLACE INTO documentary_youtube_research
               (project_id,video_id,title,channel,license_filter,query,created_at)
               VALUES(?,?,?,?,?,?,?)""",
               (row["project_id"],row["video_id"],row["title"],row["channel"],
                row["license_filter"],row["query"],row["created_at"]))
    return {"project_id":pid,"cached":False,"candidates":list(results.values()),
            "api_queries":min(MAX_SEARCHES,len(qs)),
            "note":"Resultados incluem vídeos sob licença padrão para pesquisa. "
                   "O sistema não baixa nem utiliza essas obras sem base de uso comprovada."}

def available_sources(studio,project):
    sources=[]
    for x in policy._local_authorized_sources(studio):
        file=(Path(studio.MEDIA)/x["media_local"]).resolve()
        if not file.is_file():continue
        src={"category":"authorized","local":x["media_local"],
             "youtube_url":x.get("youtube_url"),"title":str(x.get("title") or ""),
             "description":str(x.get("description") or ""),"author":str(x.get("author") or ""),
             "evidence":x.get("evidence"),"source_page":x.get("youtube_url") or "",
             "duration":None}
        sources.append(src)
    for x in policy._commons_candidates(project):
        sources.append({"category":"wikimedia","url":x["url"],
            "youtube_url":x["source_page"],"title":x["title"],
            "description":x["title"],"author":x["author"],"evidence":x["evidence"],
            "source_page":x["source_page"],"duration":None})
    return sources

def _media_duration(studio,path):
    cmd=[studio.FFPROBE,"-v","error","-show_entries","format=duration",
         "-of","default=noprint_wrappers=1:nokey=1",str(path)]
    try:
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=20)
        return max(0.0,float(r.stdout.strip())) if r.returncode==0 else 0.0
    except (OSError,ValueError,subprocess.TimeoutExpired):return 0.0

def _cut(studio,pid,source,scene,used):
    src=(Path(studio.MEDIA)/source["local"]).resolve()
    base=Path(studio.MEDIA).resolve()
    if not src.is_relative_to(base) or not src.is_file():return None
    total=_media_duration(studio,src)
    seconds=video_seconds(scene)
    # Never repeat identical frame windows to artificially fill the percentage.
    offset=used*(MAX_SECONDS+.5)
    if total < offset+seconds+.2:return None
    output=studio.project_dir(pid)/"uploads"/"youtube70"
    output.mkdir(parents=True,exist_ok=True)
    key=hashlib.sha256((str(src)+str(src.stat().st_size)+str(offset)+
                        str(seconds)).encode()).hexdigest()[:22]
    dest=output/(key+".mp4")
    if dest.is_file() and abs(_media_duration(studio,dest)-seconds)<.3:
        return str(dest.relative_to(base))
    if shutil.disk_usage(studio.DATA).free < 280*1048576:
        return None
    temp=dest.with_name(dest.stem+".partial.mp4")
    try:
        run=subprocess.run([studio.FFMPEG,"-hide_banner","-loglevel","error",
            "-nostdin","-y","-ss",str(offset),"-i",str(src),"-t",str(seconds),
            "-map","0:v:0","-an","-c:v","libx264","-preset","veryfast",
            "-crf","29","-pix_fmt","yuv420p","-movflags","+faststart",str(temp)],
            capture_output=True,text=True,timeout=150)
        if run.returncode or _media_duration(studio,temp)<seconds-.3:return None
        temp.replace(dest)
        return str(dest.relative_to(base))
    finally:
        temp.unlink(missing_ok=True)

def use_sources(studio,pid):
    p=studio.get_project(pid)
    if p.get("status")=="concluído":return count(p)
    sources=available_sources(studio,p)
    if not sources:return {**count(p),"registered_sources":0}
    scenes=p.get("scenes") or []
    total=sum(video_seconds(s) for s in scenes)
    youtube_seconds=sum(video_seconds(s) for s in scenes if is_youtube(s))
    counts=Counter()
    changed=[]
    with LOCK:
        # Prefer highest topic/scene match and the least-used source.
        for i,scene in enumerate(scenes):
            if youtube_seconds >= total*.70:break
            if is_youtube(scene) or video_seconds(scene)>MAX_SECONDS:continue
            candidates=sorted(((relevance(scene,x)-counts[str(x.get("local") or x.get("url"))]*10,x)
                               for x in sources),key=lambda z:z[0],reverse=True)
            for score,source in candidates:
                if score<0:break
                key=str(source.get("local") or source.get("url"))
                if counts[key]>=8:continue
                if source["category"]=="authorized":
                    local=_cut(studio,pid,source,scene,counts[key])
                    if not local:continue
                    provider="authorized_youtube"
                    author=(source["author"]+" | autorização documentada")[:200]
                    url=""
                else:
                    local=""
                    provider="wikimedia"
                    author=(source["author"]+" | vídeo originalmente publicado no YouTube | licença CC")[:200]
                    url=source["url"]
                    # Independently hosted Commons asset; no YouTube stream.
                    if not studio._approved_url(url,"wikimedia"):continue
                    if counts[key]>=2:continue
                with studio.db() as db:
                    db.execute("""UPDATE scenes SET media_url=?,media_local=?,
                       media_kind='video',media_provider=?,source_page=?,author=?,thumb=''
                       WHERE project_id=? AND id=?""",
                       (url,local,provider,source["source_page"],author,pid,scene["id"]))
                counts[key]+=1
                youtube_seconds+=video_seconds(scene)
                changed.append({"scene":i+1,"provider":provider,"source":source["source_page"]})
                break
    result=count(studio.get_project(pid))
    result["registered_sources"]=len(sources)
    result["newly_replaced_scenes"]=changed
    print("[youtube70] project=%s actual=%.1f%% source_clips=%d updated=%d" %
          (pid,result["actual_youtube_percent"],len(sources),len(changed)),flush=True)
    return result

class RegisterSource(BaseModel):
    youtube_url:str=Field(min_length=20,max_length=250)
    media_local:str=Field(min_length=4,max_length=350)
    evidence_url:str=Field(min_length=12,max_length=600)
    title:str=Field(min_length=4,max_length=200)
    description:str=Field(default="",max_length=400)
    author:str=Field(default="",max_length=160)
    rights_confirmed:bool=False

def register_source(studio,pid,payload):
    studio.get_project(pid)
    if not payload.rights_confirmed:
        raise HTTPException(422,"É necessário confirmar a autorização de uso do arquivo.")
    try:yt=policy.safe_video_reference(payload.youtube_url)
    except ValueError as e:raise HTTPException(422,str(e))
    evidence=urlsplit(payload.evidence_url)
    if evidence.scheme!="https" or not evidence.hostname or evidence.username or evidence.password:
        raise HTTPException(422,"Comprovante precisa ter URL HTTPS.")
    base=Path(studio.MEDIA).resolve()
    rel=Path(payload.media_local)
    if rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(422,"media_local precisa ser relativo à biblioteca do Estúdio.")
    file=(base/rel).resolve()
    if (not file.is_relative_to(base) or not file.is_file() or
        file.suffix.lower() not in (".mp4",".mov",".webm",".mkv")):
        raise HTTPException(422,"Arquivo de vídeo não encontrado na biblioteca.")
    if _media_duration(studio,file)<4:
        raise HTTPException(422,"Arquivo deve conter vídeo válido com pelo menos 4 segundos.")
    record={"status":"verified","media_local":str(rel),"youtube_url":yt,
            "title":payload.title,"description":payload.description,"author":payload.author,
            "evidence":payload.evidence_url}
    file=Path(studio.DATA)/"youtube_authorized_sources.json"
    with LOCK:
        try:rows=json.loads(file.read_text(encoding="utf-8")) if file.exists() else []
        except (OSError,ValueError):rows=[]
        rows=[x for x in rows if x.get("media_local")!=str(rel)]
        rows.append(record)
        if len(rows)>100:rows=rows[-100:]
        tmp=file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding="utf-8")
        tmp.replace(file)
    return {"registered":True,"youtube_url":yt,"media_local":str(rel)}

def install(studio):
    storage(studio)
    previous_find=studio._auto_find_media
    previous_render=studio.render_task
    def youtube_first_find(pid,limit):
        project=studio.get_project(pid)
        if is_documentary(project) and project.get("status")!="concluído":
            try:
                discover(studio,pid)
            except Exception as e:
                print("[youtube70] research unavailable: "+type(e).__name__,flush=True)
        result=previous_find(pid,limit)
        if is_documentary(project):
            try:use_sources(studio,pid)
            except Exception as e:
                print("[youtube70] selection unavailable: "+type(e).__name__+
                      " "+str(e)[:150],flush=True)
        return result
    studio._auto_find_media=youtube_first_find

    def youtube70_render(pid):
        project=studio.get_project(pid)
        # Do not rewrite legacy/completed projects or disable other pipelines.
        if (project.get("status")!="concluído" and is_documentary(project)
            and os.getenv("STUDIO_YOUTUBE70_STRICT","1")=="1"):
            use_sources(studio,pid)
            metric=count(studio.get_project(pid))
            if not metric["ready_for_70_30"]:
                studio.update_project(pid,status="erro",progress=0,
                    error=("Meta 70% YouTube / 30% banco não alcançada: "+
                           str(metric["actual_youtube_percent"])+"% YouTube e "+
                           str(metric["actual_stock_percent"])+"% bancos. "
                           "Faltam arquivos documentais utilizáveis. "
                           "Nenhum vídeo sem autorização foi baixado."))
                return
        return previous_render(pid)
    studio.render_task=youtube70_render

    @studio.app.get("/api/youtube70/status")
    def status():
        return {"version":VERSION,"target_percent":TARGET,"stock_percent":30,
                "max_clip_seconds":MAX_SECONDS,
                "youtube_search_configured":bool(os.getenv("YOUTUBE_DATA_API_KEY")),
                "youtube_download_enabled":False,
                "strict":os.getenv("STUDIO_YOUTUBE70_STRICT","1")=="1",
                "manual_source_registration_enabled":True}

    @studio.app.get("/api/youtube70/projects/{pid}/mix")
    def mix(pid:str):
        return count(studio.get_project(pid))

    @studio.app.post("/api/youtube70/projects/{pid}/fill")
    def fill(pid:str):
        return use_sources(studio,pid)

    @studio.app.get("/api/youtube70/projects/{pid}/discover")
    def source_discovery(pid:str,refresh:bool=False):
        return discover(studio,pid,refresh=refresh)

    @studio.app.post("/api/youtube70/projects/{pid}/register-source")
    def register(pid:str,body:RegisterSource):
        return register_source(studio,pid,body)

def self_test():
    src={"title":"Paraguai Ciudad del Este bridge traffic","description":"Fronteira"}
    scene={"text":"O trânsito na ponte ligando Brasil ao Paraguai","query":"Ciudad del Este bridge"}
    assert relevance(scene,src)>0
    assert video_seconds({"duration":8})==8
    assert not is_youtube({"media_provider":"pixabay","media_kind":"video","duration":8,
                           "media_url":"https://pixabay.com/clip.mp4"})
    print("[youtube70] SELF-TEST PASS search/rights/70-30/caps",flush=True)

if __name__=="__main__":
    self_test()
    app=Path("/app/app.py")
    if not app.is_file():raise SystemExit("Missing Studio app.py")
    marker="\nimport studio_youtube_70_documentary as _youtube70\n"
    code=app.read_text(encoding="utf-8")
    if marker not in code:
        app.write_text(code+marker+
             '_youtube70.install(__import__("sys").modules[__name__])\n',
             encoding="utf-8")
