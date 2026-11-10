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
from fastapi import HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field

import studio_youtube_policy as policy

VERSION="youtube70-semantic-search-v3"
TARGET=70
MAX_SECONDS=10
MAX_SEARCHES=16
MAX_SOURCE_USES=4
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
    source_seconds=Counter()
    for scene in scenes:
        if is_youtube(scene):
            origin=str(scene.get("source_page") or scene.get("media_url") or
                       scene.get("media_local") or "")
            source_seconds[origin]+=video_seconds(scene)
    unique_sources=len(source_seconds)
    eligible_seconds=sum(min(seconds,MAX_SECONDS*MAX_SOURCE_USES)
                         for seconds in source_seconds.values())
    required_sources=max(1,__import__("math").ceil(total*.70/(MAX_SECONDS*MAX_SOURCE_USES))) if total else 0
    return {"project_id":p["id"],"target_youtube_percent":TARGET,
            "target_stock_percent":30,
            "actual_youtube_percent":round(100*y/total,1) if total else 0,
            "actual_stock_percent":round(100*stock/total,1) if total else 0,
            "actual_other_percent":round(100*(total-y-stock)/total,1) if total else 0,
            "youtube_seconds":round(y,1),"total_seconds":round(total,1),
            "missing_youtube_seconds":round(max(0,total*.7-eligible_seconds),1),
            "unique_youtube_sources":unique_sources,
            "required_distinct_sources":required_sources,
            "effective_youtube_seconds":round(eligible_seconds,1),
            "effective_youtube_percent":round(100*eligible_seconds/total,1) if total else 0,
            "source_diversity_ok":bool(unique_sources>=required_sources),
            "ready_for_70_30":bool(total and y/total>=.695 and
                    stock/total>=.285 and (total-y-stock)/total<=.03 and
                    eligible_seconds/total>=.695 and
                    unique_sources>=required_sources),
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

# Visual coherence is local to the phrase. A generic illustrative clip can be
# useful without claiming to depict the actual event or named person.
VISUAL_INTENTS=(
 ("fronteira",("fronteira","ponte","foz do","ciudad del este","border","puente","aduana","travessia"),"Ciudad del Este Foz do Iguacu border crossing bridge traffic"),
 ("comercio",("comercio","loja","shopping","mercado","supermercado","comerciante","vendas","precos","preco","compras"),"Paraguay Ciudad del Este market supermarkets businesses streets"),
 ("financas",("banco","bank","dinheiro","cartao","pagamento","conta","investimento","financeiro","economia","custo de vida","impostos"),"Paraguay business finance bank money economy people"),
 ("mudanca",("mudanca","morar","residencia","imigracao","moradores","migracao","retorno","voltando","familia","packing"),"Brazilian people moving Paraguay family residence immigration"),
 ("politica",("bolsonaro","lula","presidente","governo","congresso","eleicao","politica","politico"),"Paraguay Brazil government politics news press conference"),
 ("trabalho",("trabalho","emprego","funcionario","empresa","escritorio","empreendedor","work"),"Paraguay people working business offices employees"),
 ("cidade",("assuncao","asuncion","ruas","cidade","bairro","trafego","transito","estrada"),"Paraguay Asuncion Ciudad del Este city streets daily life"),
 ("checagem",("viral","video","gravacao","redes sociais","noticia","boato","prova","desinformacao"),"fact checking news video social media journalism verification"),
)

def _visual_intent(scene):
    speech=policy.normalized(str(scene.get("text") or "")+" "+str(scene.get("query") or ""))
    scored=[(sum(1 for tag in tags if tag in speech),name,query)
            for name,tags,query in VISUAL_INTENTS]
    best=max(scored,key=lambda x:x[0])
    return (best[1],best[2]) if best[0]>0 else ("broll",str(scene.get("query") or "").strip())

def _clean_search(s):
    q=str(s or "")
    q=re.sub(r"(?i)\b(teste\s*\d+/\d+|document[aá]rio|cenas?\s*\d+|vídeo\s*de\s*\d+\s*minutos?)\b"," ",q)
    q=re.sub(r"\s+"," ",q).strip(" ,;-")
    return q[:90]

def visual_search_plan(project):
    """Bounded searches driven by the narration across the full timeline.

    One generic channel/title search is insufficient for a documentary:
    cover first, middle and last speaking scenes before adding broad B-roll
    topics. 16 searches cost roughly 1,600 YouTube Data API quota units on a
    cold project; successful results are cached and reused on later calls.
    """
    scenes=project.get("scenes") or []
    options=[]
    seen=set()
    def add(name,query):
        q=_clean_search(query)
        if not q:return
        signature=q.casefold()
        if signature in seen:return
        seen.add(signature)
        options.append({"intent":name,"query":q})
    title=_clean_search(project.get("title") or "")
    if title:add("assunto",title)

    # Breadth-first timeline sampling prevents long scripts from searching
    # only the introduction and forgetting the end of the video.
    pending=[(0,len(scenes)-1)] if scenes else []
    order=[]
    while pending:
        lo,hi=pending.pop(0)
        if lo>hi:continue
        mid=(lo+hi)//2
        order.append(mid)
        pending.append((lo,mid-1))
        pending.append((mid+1,hi))
    for index in order:
        query=_clean_search(scenes[index].get("query") or "")
        if len(query.split())>=2:
            add("cena "+str(index+1),query)
        if len(options)>=MAX_SEARCHES:break

    # Retain descriptive, illustrative searches when scene phrases are vague.
    used_intents=set()
    for scene in scenes:
        name,query=_visual_intent(scene)
        if name!="broll" and name not in used_intents:
            used_intents.add(name)
            add(name,query)
        if len(options)>=MAX_SEARCHES:break
    return options[:MAX_SEARCHES]

SYNONYMS=(
 {"paraguai","paraguay","paraguayan"},
 {"brasil","brazil","brazilian","brasileiro","brasileiros","brasileno"},
 {"fronteira","border","frontera","ponte","puente","bridge","crossing"},
 {"preco","precos","price","prices","cost","custo","custos"},
 {"mercado","market","supermarket","supermercado","compras","shopping"},
 {"dinheiro","money","finance","bank","banco","financial"},
 {"politica","politics","government","governo","presidente","president"},
 {"mudanca","moving","migration","migracao","immigration","imigracao"},
 {"trabalho","work","working","job","empleo","empleados"},
 {"cidade","city","ciudad","street","rua","calle"},
)
def _semantic_words(value):
    result=set(re.findall(r"[a-z0-9]{3,}",policy.normalized(value or "")))
    result-=STOP
    for group in SYNONYMS:
        if result & group:result|=group
    return result

def _video_match(scene,video):
    spoken=str(scene.get("text") or "")
    visual=str(scene.get("query") or "")
    title=str(video.get("title") or "")
    matched=_semantic_words(visual) & _semantic_words(title)
    more=_semantic_words(spoken) & _semantic_words(title)
    intent,focused=_visual_intent(scene)
    concept=_semantic_words(focused) & _semantic_words(title)
    # Query tokens usually contain concrete objects/places; give them priority.
    score=5*len(matched)+2*len(more)+len(concept)
    return max(0,score)

def scene_matches(project, candidates, per_scene=3):
    result=[]
    used=Counter()
    source_list=list(candidates)
    for i,scene in enumerate(project.get("scenes") or []):
        ranked=sorted(source_list,key=lambda x:(
            -_video_match(scene,x),used[x["video_id"]],x["video_id"]))
        picks=[]
        for video in ranked:
            score=_video_match(scene,video)
            if score<3:continue
            if used[video["video_id"]]>=2:continue
            picks.append({"video_id":video["video_id"],
                          "youtube_url":video.get("youtube_url") or
                              ("https://www.youtube.com/watch?v="+video["video_id"]),
                          "title":video["title"],
                          "license":video.get("license_filter") or "unknown",
                          "match_score":score,
                          "reference_only":True})
            used[video["video_id"]]+=1
            if len(picks)>=per_scene:break
        intent,_=_visual_intent(scene)
        result.append({"scene_number":i+1,"visual_intent":intent,
                       "narration_excerpt":str(scene.get("text") or "")[:180],
                       "search_hint":str(scene.get("query") or "")[:120],
                       "suggested_videos":picks})
    return result

def _cached_candidates(studio,pid):
    with studio.db() as db:
        rows=[dict(x) for x in db.execute(
            "SELECT * FROM documentary_youtube_research WHERE project_id=? "
            "ORDER BY created_at DESC LIMIT 240",(pid,)).fetchall()]
    for row in rows:
        row["youtube_url"]="https://www.youtube.com/watch?v="+row["video_id"]
        row["usable_for_render"]=False
    return rows

def discover(studio,pid,refresh=False):
    project=studio.get_project(pid)
    key=os.getenv("YOUTUBE_DATA_API_KEY","").strip()
    if not key:raise HTTPException(409,"Falta configurar YOUTUBE_DATA_API_KEY.")
    cached=_cached_candidates(studio,pid)
    upgraded=any(r.get("query","").startswith("semantic3:") for r in cached)
    plan=visual_search_plan(project)
    if cached and upgraded and not refresh:
        matches=scene_matches(project,cached)
        return {"project_id":pid,"cached":True,"search_plan":plan,
                "candidate_count":len(cached),"candidates":cached,
                "scene_matches":matches,
                "scenes_with_matches":sum(bool(x["suggested_videos"]) for x in matches),
                "api_queries":0,
                "note":"Vídeos selecionados pelo conteúdo falado, inclusive imagens ilustrativas. "
                       "Resultados são referências, não arquivos para montagem."}
    results={}
    errors=[]
    with httpx.Client(timeout=18,follow_redirects=False) as client:
        for plan_entry in plan:
            q=plan_entry["query"]
            try:
                response=client.get("https://www.googleapis.com/youtube/v3/search",
                    params={"part":"snippet","type":"video","maxResults":25,
                            "q":q,"key":key,"safeSearch":"moderate",
                            "order":"relevance","videoEmbeddable":"true"})
                response.raise_for_status()
                for v in response.json().get("items") or []:
                    vid=str((v.get("id") or {}).get("videoId") or "")
                    if not re.fullmatch(r"[A-Za-z0-9_-]{11}",vid):continue
                    sn=v.get("snippet") or {}
                    if vid not in results:
                        results[vid]={"project_id":pid,"video_id":vid,
                           "title":str(sn.get("title") or "")[:200],
                           "channel":str(sn.get("channelTitle") or "")[:100],
                           "description":str(sn.get("description") or "")[:300],
                           "license_filter":"unverified",
                           "query":"semantic3:"+q,
                           "created_at":datetime.now(timezone.utc).isoformat(),
                           "youtube_url":"https://www.youtube.com/watch?v="+vid,
                           "usable_for_render":False}
            except (httpx.HTTPStatusError,httpx.RequestError,ValueError) as exc:
                errors.append({"query":q,"error":type(exc).__name__})
    # Read the uploader's declared license, not a promise of downloading rights.
    if results:
        with httpx.Client(timeout=15,follow_redirects=False) as client:
            ids=list(results)
            for start in range(0,len(ids),50):
                try:
                    response=client.get("https://www.googleapis.com/youtube/v3/videos",
                        params={"part":"status","id":",".join(ids[start:start+50]),"key":key})
                    response.raise_for_status()
                    for entry in response.json().get("items") or []:
                        vid=entry.get("id")
                        if vid in results:
                            results[vid]["license_filter"]=str(
                               (entry.get("status") or {}).get("license") or "unknown")
                except (httpx.HTTPError,ValueError):
                    continue
    # Remove the old, generic results only when new results are available.
    if results:
        with studio.db() as db:
            db.execute("DELETE FROM documentary_youtube_research WHERE project_id=?",(pid,))
            for row in results.values():
                db.execute("""INSERT OR REPLACE INTO documentary_youtube_research
                   (project_id,video_id,title,channel,license_filter,query,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                   (row["project_id"],row["video_id"],row["title"],row["channel"],
                    row["license_filter"],row["query"],row["created_at"]))
    refs=list(results.values()) if results else cached
    matches=scene_matches(project,refs)
    return {"project_id":pid,"cached":False,
            "search_plan":plan,
            "candidate_count":len(refs),"candidates":refs,
            "scene_matches":matches,
            "scenes_with_matches":sum(bool(x["suggested_videos"]) for x in matches),
            "api_queries":len(plan),
            "errors":errors,
            "note":"Busca por assunto e por fala, incluindo imagens de apoio ilustrativas. "
                   "YouTube API não fornece arquivo para FFmpeg; cada referência precisa de "
                   "fonte de arquivo utilizável antes de entrar no vídeo."}

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

def _download_commons(studio,pid,source):
    """Cache a verified Commons copy (never a YouTube stream).

    Re-using different non-overlapping sections is legal only when the
    independently hosted source carries a reusable licence. The Commons
    candidate list verifies that before this function is reached.
    """
    url=str(source.get("url") or "")
    if not url or not studio._approved_url(url,"wikimedia"):
        return None
    ext=Path(urlsplit(url).path).suffix.lower()
    if ext not in (".mp4",".webm",".ogv",".ogg"):
        return None
    folder=studio.project_dir(pid)/"uploads"/"commons_verified"
    folder.mkdir(parents=True,exist_ok=True)
    dest=folder/(hashlib.sha256(url.encode()).hexdigest()[:24]+ext)
    if dest.is_file() and _media_duration(studio,dest)>=4:
        return str(dest.resolve().relative_to(Path(studio.MEDIA).resolve()))
    if shutil.disk_usage(studio.DATA).free < 300*1048576:
        return None
    temp=dest.with_name(dest.stem+".partial"+ext)
    temp.unlink(missing_ok=True)
    try:
        limit=35*1024*1024
        total=0
        with httpx.Client(timeout=35,follow_redirects=False) as client:
            with client.stream("GET",url,headers={
                    "User-Agent":"MeuEstudioIA/1.0 (https://github.com/amendokim/estudio; licensed video)"}) as response:
                if response.status_code!=200:
                    return None
                length=int(response.headers.get("content-length") or 0)
                if length>limit:return None
                kind=str(response.headers.get("content-type") or "").split(";")[0]
                if kind and kind not in ("video/webm","video/mp4","video/ogg",
                                          "application/octet-stream"):
                    return None
                with temp.open("wb") as handle:
                    for chunk in response.iter_bytes(chunk_size=1024*1024):
                        total+=len(chunk)
                        if total>limit: return None
                        handle.write(chunk)
        if total<5000 or _media_duration(studio,temp)<4:
            return None
        temp.replace(dest)
        print("[youtube70] cached separately hosted Commons file: "+
              str(total)+" bytes",flush=True)
        return str(dest.resolve().relative_to(Path(studio.MEDIA).resolve()))
    except (httpx.HTTPError,OSError,ValueError,subprocess.TimeoutExpired):
        return None
    finally:
        temp.unlink(missing_ok=True)


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
    counts=Counter(
        str(s.get("source_page") or s.get("media_url") or s.get("media_local") or "")
        for s in scenes if is_youtube(s))
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
                key=str(source.get("source_page") or source.get("local") or source.get("url"))
                if counts[key]>=MAX_SOURCE_USES:continue
                if source["category"]=="authorized":
                    local=_cut(studio,pid,source,scene,counts[key])
                    if not local:continue
                    provider="authorized_youtube"
                    author=(source["author"]+" | autorização documentada")[:200]
                    url=""
                else:
                    # Fetch only the independently licensed Commons copy and
                    # cut distinct windows from it. Never fetch YouTube streams.
                    cached=_download_commons(studio,pid,source)
                    provider="wikimedia"
                    author=(source["author"]+" | vídeo originalmente publicado no YouTube | licença CC")[:200]
                    if cached:
                        local=_cut(studio,pid,{**source,"local":cached},scene,counts[key])
                        if not local:continue
                        url=""
                    else:
                        if counts[key]>=1:continue
                        local=""
                        url=source["url"]
                        if not studio._approved_url(url,"wikimedia"):continue
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


def montage_mix(montage):
    """Audit final shots, including distinct-origin limits to avoid fake 70%."""
    total=youtube=stock=0.0
    shots=0
    maxclip=0.0
    sources=Counter()
    for scene in montage.get("timeline") or []:
        for shot in scene.get("shots") or []:
            seconds=float(shot.get("seconds") or 0)
            asset=shot.get("asset") or {}
            total+=seconds
            shots+=1
            if is_youtube({**asset,"duration":seconds}):
                youtube+=seconds
                maxclip=max(maxclip,seconds)
                origin=str(asset.get("source_page") or asset.get("media_url") or
                           asset.get("media_local") or "")
                sources[origin]+=seconds
            elif asset.get("media_provider") in ("pixabay","pexels"):
                stock+=seconds
    eligible=sum(min(seconds,MAX_SECONDS*MAX_SOURCE_USES) for seconds in sources.values())
    required=max(1,__import__("math").ceil(total*.70/(MAX_SECONDS*MAX_SOURCE_USES))) if total else 0
    return {"total_seconds":round(total,2),
            "youtube_seconds":round(youtube,2),
            "stock_seconds":round(stock,2),
            "youtube_percent":round(youtube*100/total,1) if total else 0,
            "stock_percent":round(stock*100/total,1) if total else 0,
            "other_percent":round(max(0,total-youtube-stock)*100/total,1) if total else 0,
            "effective_youtube_percent":round(eligible*100/total,1) if total else 0,
            "unique_youtube_sources":len(sources),
            "required_distinct_sources":required,
            "source_diversity_ok":bool(len(sources)>=required),
            "shot_count":shots,
            "max_youtube_seconds":maxclip}

def enrich_montage(studio,project,montage):
    """Use authorized Youtube-origin media for up to 70% of long-form microshots.

    No direct download from YouTube; uses registered files from the media library
    or separately hosted, license-verified Commons copies. Fails closed when
    actual final shot durations do not reach the requested 70/30 ratio.
    """
    if not is_documentary(project) or os.getenv("STUDIO_YOUTUBE70_STRICT","1")!="1":
        return montage
    import copy
    timeline=copy.deepcopy(montage)
    current=montage_mix(timeline)
    target_seconds=current["total_seconds"]*.70
    candidates=available_sources(studio,project)
    counts=Counter(
        str((shot.get("asset") or {}).get("source_page") or
            (shot.get("asset") or {}).get("media_url") or
            (shot.get("asset") or {}).get("media_local") or "")
        for scene in timeline.get("timeline") or []
        for shot in scene.get("shots") or []
        if is_youtube({**(shot.get("asset") or {}),
                       "duration":float(shot.get("seconds") or 0)}))
    indexed=[]
    for i,entry in enumerate(timeline.get("timeline") or []):
        scene=project["scenes"][i]
        for j,shot in enumerate(entry.get("shots") or []):
            dur=float(shot.get("seconds") or 0)
            if dur<1 or dur>MAX_SECONDS:continue
            if is_youtube({**(shot.get("asset") or {}),"duration":dur}):continue
            options=[(relevance(scene,x),x) for x in candidates]
            options=[(score,x) for score,x in options if score>0]
            if not options:continue
            # Seven roughly spaced opportunities per ten shots keeps the
            # rhythm documentary-like throughout the whole video.
            n=len(indexed)
            priority=0 if (n % 10) in (0,1,3,4,6,7,9) else 1
            indexed.append((priority,-max(score for score,_ in options),i,j,dur,options))
    indexed.sort(key=lambda x:(x[0],x[1],x[2],x[3]))
    replaced=0
    for priority,rank,i,j,dur,options in indexed:
        if current["youtube_seconds"]>=target_seconds:break
        options.sort(key=lambda item:(-item[0],counts[str(item[1].get("source_page") or item[1].get("local") or item[1].get("url"))]))
        for score,source in options:
            key=str(source.get("source_page") or source.get("local") or source.get("url"))
            if counts[key]>=MAX_SOURCE_USES:continue
            if source["category"]=="authorized":
                clip_scene={"duration":dur}
                local=_cut(studio,project["id"],source,clip_scene,counts[key])
                if not local:continue
                asset={"media_local":local,"media_url":"",
                    "media_provider":"authorized_youtube","media_kind":"video",
                    "source_page":source["source_page"],
                    "author":(source["author"]+" | autorização documentada")[:200]}
            else:
                cached=_download_commons(studio,project["id"],source)
                if cached:
                    clip=_cut(studio,project["id"],{**source,"local":cached},
                              {"duration":dur},counts[key])
                    if not clip:continue
                    asset={"media_local":clip,"media_url":"",
                           "media_provider":"wikimedia","media_kind":"video",
                           "source_page":source["source_page"],
                           "author":(source["author"]+
                              " | vídeo originalmente publicado no YouTube")[:200]}
                else:
                    if counts[key]>=1:continue
                    if not studio._approved_url(source["url"],"wikimedia"):continue
                    asset={"media_local":"","media_url":source["url"],
                        "media_provider":"wikimedia","media_kind":"video",
                        "source_page":source["source_page"],
                        "author":(source["author"]+
                           " | vídeo originalmente publicado no YouTube")[:200]}
            timeline["timeline"][i]["shots"][j]["asset"]=asset
            counts[key]+=1
            current["youtube_seconds"]+=dur
            replaced+=1
            break
    metric=montage_mix(timeline)
    metric["new_youtube_shots"]=replaced
    metric["documentary_target_youtube_percent"]=TARGET
    metric["documentary_target_stock_percent"]=30
    metric["sources_available"]=len(candidates)
    out=studio.project_dir(project["id"])/"export"
    out.mkdir(parents=True,exist_ok=True)
    (out/"montage_youtube70_source_audit.json").write_text(
        json.dumps(metric,ensure_ascii=False,indent=2),encoding="utf-8")
    print("[youtube70] final montage source mix: "+json.dumps(metric),flush=True)
    # 1-2% tolerance for indivisible 5-8-second cuts; never falsify metrics.
    if (metric["youtube_percent"]<69.5 or metric["stock_percent"]<28.5
        or metric["other_percent"]>3 or metric["max_youtube_seconds"]>MAX_SECONDS
        or not metric["source_diversity_ok"] or metric["effective_youtube_percent"]<69.5):
        raise RuntimeError(
            "Plano 70% YouTube / 30% Pexels-Pixabay não atendido: "+
            str(metric["youtube_percent"])+"% de YouTube e "+
            str(metric["stock_percent"])+"% de bancos. "+
            "Faltam fontes documentais utilizáveis; não renderizar com percentuais fictícios.")
    return timeline


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

async def upload_source(studio,pid,video,youtube_url,title,author,evidence_url,rights_confirmed):
    """Store a user-provided source file; never extract video from YouTube.
    File and rights metadata are validated before the source is usable.
    """
    studio.get_project(pid)
    if not rights_confirmed:
        raise HTTPException(422,"Informe que você possui direitos ou autorização de reutilização.")
    try:policy.safe_video_reference(youtube_url)
    except ValueError as exc:raise HTTPException(422,str(exc))
    extension=Path(video.filename or "").suffix.lower()
    if extension not in (".mp4",".mov",".webm",".mkv"):
        raise HTTPException(422,"Formato não suportado: use MP4, MOV, WEBM ou MKV.")
    dest_dir=studio.project_dir(pid)/"uploads"/"youtube_sources"
    dest_dir.mkdir(parents=True,exist_ok=True)
    dest=dest_dir/(hashlib.sha256(os.urandom(32)).hexdigest()[:24]+extension)
    limit=150*1024*1024
    received=0
    try:
        with dest.open("wb") as target:
            while True:
                chunk=await video.read(1024*1024)
                if not chunk:break
                received+=len(chunk)
                if received>limit:raise HTTPException(413,"Arquivo excede 150 MB.")
                target.write(chunk)
        if received<5000 or _media_duration(studio,dest)<4:
            raise HTTPException(422,"Não foi possível confirmar um arquivo de vídeo válido.")
        rel=str(dest.resolve().relative_to(Path(studio.MEDIA).resolve()))
        saved=register_source(studio,pid,RegisterSource(
            youtube_url=youtube_url,media_local=rel,
            evidence_url=evidence_url,title=title,author=author,
            description="Arquivo enviado pelo usuário; origem YouTube documentada",
            rights_confirmed=True))
        return {**saved,"bytes":received,
                "note":"Arquivo disponível para cortes de até 10s; busca oficial não baixa vídeos."}
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    finally:
        await video.close()

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
            and os.getenv("STUDIO_YOUTUBE70_STRICT","1")=="1"
            and (int(project.get("duration_minutes") or 0)<6 or
                 os.getenv("STUDIO_LONGFORM_DYNAMIC_EDIT","1")!="1")):
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

    def licensed_preview(pid):
        """Explicit alternative when the requested 70/30 cannot be sourced.

        This is NOT a 70/30 documentary: actual YouTube and Commons percentages
        remain distinguishable in the source audit. Executed by the existing
        factory queue only after user confirmation.
        """
        project=studio.get_project(pid)
        if project.get("status")=="concluído":
            raise RuntimeError("Projeto já concluído")
        if int(project.get("duration_minutes") or 0)>5:
            raise RuntimeError("Piloto alternativo limitado a 5 minutos neste trabalhador")
        import studio_licensed_archive as archive
        archive.fill_short_preview(studio,pid)
        return previous_render(pid)

    studio.render_licensed_preview=licensed_preview

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

    @studio.app.post("/api/youtube70/projects/{pid}/upload")
    async def upload(pid:str,
            video:UploadFile=File(...),
            youtube_url:str=Form(...),
            title:str=Form(...),
            author:str=Form(""),
            evidence_url:str=Form(...),
            rights_confirmed:bool=Form(False)):
        return await upload_source(studio,pid,video,youtube_url,title,author,
                                   evidence_url,rights_confirmed)

    @studio.app.get("/api/youtube70/projects/{pid}/licensed-broll")
    def licensed_broll(pid:str):
        import studio_licensed_archive as archive
        p=studio.get_project(pid)
        matches=archive.discover(p)
        return {"project_id":pid,"count":len(matches),
                "files":[{"title":x["archive_title"],"page":x["source_page"],
                          "license":x["archive_license"]}
                         for x in matches[:30]],
                "note":"Acervo Wikimedia reutilizável. Estes arquivos não são contabilizados como origem YouTube."}

    @studio.app.get("/api/youtube70/projects/{pid}/sources")
    def sources(pid:str):
        p=studio.get_project(pid)
        pool=available_sources(studio,p)
        return {"project_id":pid,"available_source_count":len(pool),
                "sources":[{"category":x["category"],
                            "title":x["title"],"source_page":x["source_page"],
                            "rights_evidence":x["evidence"]}
                           for x in pool[:60]],
                "mix":count(p),
                "note":"Arquivos do YouTube não são transferidos pela API oficial."}

def self_test():
    src={"title":"Paraguai Ciudad del Este bridge traffic","description":"Fronteira"}
    scene={"text":"O trânsito na ponte ligando Brasil ao Paraguai","query":"Ciudad del Este bridge"}
    assert relevance(scene,src)>0
    assert video_seconds({"duration":8})==8
    assert not is_youtube({"media_provider":"pixabay","media_kind":"video","duration":8,
                           "media_url":"https://pixabay.com/clip.mp4"})
    mock={"timeline":[{"shots":[
       {"seconds":7,"asset":{"media_kind":"video","media_provider":"pixabay",
                              "media_url":"https://pixabay.com/test.mp4"}},
       {"seconds":7,"asset":{"media_kind":"video","media_provider":"pixabay",
                              "media_url":"https://pixabay.com/test2.mp4"}}
    ]}]}
    report=montage_mix(mock)
    assert report["stock_percent"]==100.0 and report["youtube_percent"]==0.0
    p={"id":"smoke","scenes":[{"media_kind":"video","media_provider":"wikimedia",
        "media_url":"https://upload.wikimedia.org/test.webm",
        "source_page":"https://commons.wikimedia.org/wiki/File:Test",
        "author":"originalmente publicado no YouTube","duration":7} for _ in range(24)]}
    audited=count(p)
    assert audited["actual_youtube_percent"]==100
    assert audited["effective_youtube_percent"]<40
    assert not audited["source_diversity_ok"]
    visual=visual_search_plan({"title":"Teste 70/30 — Mudança para Paraguai","scenes":[
        {"text":"Custo de vida no mercado","query":"Paraguay supermarket prices"},
        {"text":"Famílias se mudando de país","query":"moving family boxes"},
        {"text":"Política e decisões de governo","query":"government press conference"},
        {"text":"Trânsito na Ponte da Amizade","query":"Ciudad del Este bridge cars"}]})
    assert 4<=len(visual)<=MAX_SEARCHES,visual
    example={"scenes":[{"text":"Compre comida no supermercado","query":"supermarket prices"}]}
    match=scene_matches(example,[{"video_id":"dQw4w9WgXcQ",
        "title":"Supermarket prices and grocery shopping Paraguay",
        "license_filter":"youtube"}])
    assert len(match)==1 and len(match[0]["suggested_videos"])==1,match
    print("[youtube70] SELF-TEST PASS search/rights/70-30/caps and long-form montage",flush=True)

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
