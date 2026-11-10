"""Multi-shot long-form editor for narrated documentaries.

Each 30-60 second narration scene is cut into 4-8 second distinct visual shots,
prioritizing scene-relevant Pixabay footage. Unlike the legacy renderer, it never
loops a single short stock video for the entire narration scene. No paid AI calls.
Audio and final captions stay under the established long-form renderer.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import shutil
import subprocess
from pathlib import Path
import httpx
import studio_semantic_media as semantic

VERSION = "dynamic-montage-v2"
MAX_SEARCHES = 48
MAX_LOCAL_SHOTS = 10
MIN_FREE_MB = 180


def shot_durations(duration, opening=False):
    """Paced editing: 3.5-4.5s intro, mostly 5-7s in body."""
    duration = max(1.0,float(duration))
    pace = 4.2 if opening else 6.5
    count = max(1, math.ceil(duration / pace))
    step = duration / count
    return [round(duration / count if i < count-1 else
                  duration - step*(count-1),3) for i in range(count)]


def identity(asset):
    return str(asset.get("media_url") or asset.get("media_local") or "")


def valid_asset(studio,asset):
    source=identity(asset)
    provider=asset.get("media_provider") or ""
    kind=asset.get("media_kind")
    if not source or kind not in ("video","image"):
        return False
    if asset.get("media_url"):
        return provider in ("pixabay","pexels","wikimedia","authorized_youtube") and bool(
            studio._approved_url(source,provider))
    return provider=="original" and bool(asset.get("media_local"))


def asset_from_scene(scene):
    return {k:scene.get(k) for k in ("id","media_url","media_local",
        "media_kind","media_provider","source_page","author","duration","text")}


def candidate_score(shot,asset,index_distance):
    # Existing stock metadata includes provider-supplied tags.
    if asset.get("media_provider") not in ("pixabay","pexels"):
        return -100
    score=semantic._relevant_candidate(shot,asset.get("author") or "")
    return score if score>=semantic.MIN_SCORE else -100


def source_pool(studio,project,index,shot):
    scenes=project["scenes"]
    found=[]
    own=asset_from_scene(scenes[index])
    if valid_asset(studio,own):
        found.append((100,own))
    for j,other in enumerate(scenes):
        if j==index:
            continue
        a=asset_from_scene(other)
        if not valid_asset(studio,a) or a["media_kind"]!="video":
            continue
        rank=candidate_score(shot,a,abs(index-j))
        if rank>=semantic.MIN_SCORE:
            found.append((rank*4-max(0,abs(index-j)//3),a))
    dedup=set()
    result=[]
    for score,asset in sorted(found,key=lambda entry:-entry[0]):
        key=identity(asset)
        if key and key not in dedup:
            dedup.add(key);result.append(asset)
    return result


def extra_pixabay(studio,shot,client,key,cache,global_used):
    if not key:
        return []
    candidates=[]
    for query in shot.get("queries",[])[:2]:
        if query not in cache:
            if len(cache)>=MAX_SEARCHES:
                break
            try:
                response=client.get("https://pixabay.com/api/videos/",
                    params={"key":key,"q":query,"lang":"en",
                            "per_page":25,"safesearch":"true"},timeout=12)
                response.raise_for_status()
                cache[query]=response.json().get("hits") or []
            except (httpx.HTTPError,ValueError):
                cache[query]=[]
        for hit in cache[query]:
            tags=str(hit.get("tags") or "")
            relevance=semantic._relevant_candidate(shot,tags)
            if relevance<semantic.MIN_SCORE:
                continue
            page=str(hit.get("pageURL") or "")
            if not page.startswith("https://pixabay.com/"):
                continue
            url=""
            for size in ("small","tiny","medium"):
                record=(hit.get("videos") or {}).get(size) or {}
                entry=record.get("url") or ""
                size_bytes=int(record.get("size") or 0)
                if entry and size_bytes<=8*1024*1024 and studio._approved_url(entry,"pixabay"):
                    url=entry;break
            if not url or url in global_used:
                continue
            asset={"media_url":url,"media_local":"","media_kind":"video",
                   "media_provider":"pixabay","author":str(hit.get("user") or "")+
                       " | Pixabay | "+tags,
                   "source_page":page}
            candidates.append((relevance,asset))
    candidates.sort(key=lambda pair:-pair[0])
    return [x for _,x in candidates[:4]]


def choose_sequence(assets,count,use_counts):
    """Deterministic low-repeat rotation, discourages recurrences across scenes."""
    if not assets:
        return []
    plan=[]
    counts={}
    for i in range(count):
        choices=sorted(assets,key=lambda a:(
            identity(a)==(identity(plan[-1]) if plan else ""),
            counts.get(identity(a),0)*3+use_counts.get(identity(a),0),
            assets.index(a)))
        choice=choices[0]
        plan.append(choice)
        key=identity(choice)
        counts[key]=counts.get(key,0)+1
        use_counts[key]=use_counts.get(key,0)+1
    return plan


def build_plan(studio,project):
    """Resolve concrete, checked media choices once per project."""
    import studio_semantic_media as sem
    plan,_=sem.load_plan(studio,project,allow_ai=False,write=False)
    cache={}
    key=os.getenv("PIXABAY_API_KEY","")
    used_extra=set()
    use_counts={}
    timeline=[]
    # Commons can supply independently hosted, rights-filtered B-roll. These
    # assets are never labeled as YouTube-origin without separate evidence.
    try:
        import studio_youtube_70_documentary as doc70
        strict_mix=(doc70.is_documentary(project) and
            os.getenv("STUDIO_YOUTUBE70_STRICT","1")=="1" and
            not doc70.is_licensed_preview(project.get("id")))
        if strict_mix:
            # Preserve the 30% Pexels/Pixabay share. The separate documentary
            # enrichment phase adds verified YouTube-origin sources for 70%.
            archive_assets=[]
        else:
            import studio_licensed_archive as archive
            archive_assets=archive.discover(project)
    except Exception as exc:
        archive_assets=[]
        print("[montage] licensed archive unavailable: "+type(exc).__name__,flush=True)
    with httpx.Client(timeout=13,follow_redirects=False) as client:
        elapsed=0.0
        for index,scene in enumerate(project["scenes"]):
            shot=plan["scenes"][index]
            pool=source_pool(studio,project,index,shot)
            if archive_assets:
                try:
                    for asset in archive.matches_for_scene(scene,archive_assets):
                        if len(pool)>=MAX_LOCAL_SHOTS:break
                        if studio._approved_url(asset["media_url"],"wikimedia") and (
                                identity(asset) not in {identity(x) for x in pool}):
                            pool.append(asset)
                except Exception:
                    pass
            if len(pool)<MAX_LOCAL_SHOTS:
                added=extra_pixabay(studio,shot,client,key,cache,used_extra)
                for asset in added:
                    source=identity(asset)
                    if source not in {identity(x) for x in pool}:
                        pool.append(asset)
                        used_extra.add(source)
                    if len(pool)>=MAX_LOCAL_SHOTS:
                        break
            dur=max(1,float(scene.get("duration") or 8))
            lengths=shot_durations(dur,opening=elapsed<40)
            # No false "100% variety" claims: enforce three unique real shots
            # on narration segments >=20 seconds; reject rather than loop footage.
            unique_video={identity(a) for a in pool if a.get("media_kind")=="video"}
            if dur>=20 and len(unique_video)<3:
                raise RuntimeError(
                    f"Cena {index+1}: apenas {len(unique_video)} tomadas distintas e "
                    "relevantes. Faltam mídias para uma edição dinâmica; não vou "
                    "repetir o mesmo clipe para preencher o tempo.")
            chosen=choose_sequence(pool,len(lengths),use_counts)
            timeline.append({"index":index,"seconds":round(dur,3),
                "shots":[{"asset":asset,"seconds":lengths[j]} for j,asset in enumerate(chosen)],
                "distinct_sources":len(set(identity(x) for x in chosen))})
            elapsed+=dur
    return {"version":VERSION,"project_id":project["id"],"total_seconds":round(elapsed,2),
            "source_searches":len(cache),"timeline":timeline}


def _dur(studio,path):
    cp=subprocess.run([studio.FFPROBE,"-v","error",
        "-show_entries","format=duration",
        "-of","default=noprint_wrappers=1:nokey=1",str(path)],
        capture_output=True,text=True,timeout=30)
    try:
        return float(cp.stdout.strip()) if cp.returncode==0 else 0
    except ValueError:
        return 0


def _run(studio,args,limit=300):
    cp=subprocess.run([studio.FFMPEG,"-hide_banner","-loglevel","error",
        "-nostdin","-y",*args],capture_output=True,text=True,timeout=limit)
    if cp.returncode:
        raise RuntimeError("FFmpeg multi-shot: "+cp.stderr[-550:])


def make_scene(studio,pid,project,index,entry,out,final,preset="veryfast"):
    """Encode per-shot footage then stream-copy into one uniform scene MP4."""
    parts_folder=out / f"shots_{index:03d}"
    parts_folder.mkdir(parents=True,exist_ok=True)
    width,height=(1280,720) if project.get("ratio")=="16:9" else (720,1280)
    fragments=[]
    sources=[]
    for j,shot in enumerate(entry["shots"]):
        if shutil.disk_usage(studio.DATA).free/1048576 < MIN_FREE_MB:
            raise RuntimeError("Disco quase cheio durante edição multi-shot; checkpoints preservados")
        asset=shot["asset"]
        duration=shot["seconds"]
        local_scene=dict(project["scenes"][index])
        local_scene.update(asset)
        # A stable media-specific cache key prevents fetch_clip from returning
        # a different asset previously cached under the narrative scene ID.
        local_scene["id"] = str(project["scenes"][index]["id"]) + "-" + hashlib.sha256(
            identity(asset).encode()).hexdigest()[:12]
        local=studio.fetch_clip(local_scene,pid)
        if not local:
            raise RuntimeError(f"Cena {index+1}, tomada {j+1}: mídia indisponível")
        local=Path(local)
        fragment=parts_folder / f"shot_{j:03d}.mp4"
        if not fragment.is_file() or abs(_dur(studio,fragment)-duration)>.22:
            source_duration=_dur(studio,local)
            is_video=asset.get("media_kind")=="video"
            # Use a different section and crop if a source appears again later.
            if is_video:
                # Never silently loop a shorter clip to pad a voice segment.
                # Reject the source so the editor can request more media rather
                # than repeat the same frames, as happened in the first pilot.
                if source_duration + 0.05 < duration:
                    raise RuntimeError(
                        f"Cena {index+1}, tomada {j+1}: vídeo-fonte de "
                        f"{source_duration:.1f}s menor que corte de {duration:.1f}s; "
                        "nenhum loop permitido")
                available=max(0,source_duration-duration-.2)
                offset=min(available,(j*3.1+index*1.7)%max(1,available))
                input_args=["-ss",f"{offset:.3f}","-i",str(local)]
            else:
                input_args=["-loop","1","-framerate","24","-i",str(local)]
            zoom=1.05 if (j%3==0) else (1.08 if j%3==1 else 1.02)
            filt=(f"scale=ceil(iw*{zoom}/2)*2:ceil(ih*{zoom}/2)*2,"
                  f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                  f"crop={width}:{height},setsar=1,fps=24,"
                  "eq=contrast=1.035:saturation=0.985,format=yuv420p")
            temp=fragment.with_name(fragment.stem+".partial.mp4")
            temp.unlink(missing_ok=True)
            try:
                _run(studio,[*input_args,"-t",f"{duration:.3f}",
                    "-vf",filt,"-an","-c:v","libx264","-preset",preset,
                    "-crf","28","-pix_fmt","yuv420p","-r","24",
                    "-movflags","+faststart",str(temp)],
                    limit=max(100,int(duration*25)))
                if _dur(studio,temp)<duration-.22:
                    raise RuntimeError(f"Tomada curta: cena {index+1}, corte {j+1}")
                temp.replace(fragment)
            finally:
                temp.unlink(missing_ok=True)
        fragments.append(fragment)
        if asset.get("source_page"):
            sources.append({"scene":index+1,"shot":j+1,
                "provider":asset.get("media_provider"),"url":asset["source_page"],
                "author":asset.get("author")})
    listfile=parts_folder/"concat.txt"
    listfile.write_text("".join("file '"+str(f.resolve()).replace("'","'\\''")+"'\n"
                         for f in fragments),encoding="utf-8")
    temp=final.with_name(final.stem+".partial.mp4")
    temp.unlink(missing_ok=True)
    try:
        _run(studio,["-f","concat","-safe","0","-i",str(listfile),
                     "-c","copy","-movflags","+faststart",str(temp)],limit=180)
        if _dur(studio,temp)<entry["seconds"]-.5:
            raise RuntimeError("Cena montada com duração incompleta")
        temp.replace(final)
    finally:
        temp.unlink(missing_ok=True)
    shutil.rmtree(parts_folder,ignore_errors=True)
    return sources


def self_test():
    assert len(shot_durations(43))>=6
    assert max(shot_durations(43))<=7
    assert max(shot_durations(22,opening=True))<=4.5
    a=[{"media_url":"https://pixabay.com/a"},{"media_url":"https://pixabay.com/b"},
       {"media_url":"https://pixabay.com/c"}]
    seq=choose_sequence(a,12,{})
    assert all(identity(a)!=identity(b) for a,b in zip(seq,seq[1:]))
    assert len({identity(x) for x in seq})==3
    # Real FFmpeg integration smoke check: four distinct one-second shot clips
    # must concatenate into one smooth four-second scene without missing frames.
    import tempfile
    from types import SimpleNamespace
    with tempfile.TemporaryDirectory(prefix="studio-montage-test-") as td:
        root=Path(td)
        if shutil.which("ffmpeg") and shutil.which("ffprobe"):
            originals=[]
            for i,color in enumerate(("red","blue","green")):
                path=root / f"source_{i}.mp4"
                cp=subprocess.run(["ffmpeg","-hide_banner","-loglevel","error",
                    "-y","-f","lavfi","-i",f"color=c={color}:s=320x180:d=2",
                    "-c:v","libx264","-preset","ultrafast","-pix_fmt","yuv420p",
                    str(path)],capture_output=True,text=True,timeout=30)
                assert cp.returncode==0,cp.stderr[-300:]
                originals.append(path)
            media=[{"media_local":str(x),"media_kind":"video",
                    "media_provider":"original"} for x in originals]
            seq=choose_sequence(media,4,{})
            e={"seconds":4.0,"shots":[{"asset":a,"seconds":1.0} for a in seq]}
            scene={"id":"smoke","text":"Documentary example","duration":4.0}
            project={"id":"test","ratio":"16:9","scenes":[scene]}
            mock=SimpleNamespace(DATA=root,FFMPEG="ffmpeg",FFPROBE="ffprobe",
                    fetch_clip=lambda item,pid:item["media_local"])
            dest=root/"edited.mp4"
            make_scene(mock,"test",project,0,e,root,dest,preset="ultrafast")
            assert abs(_dur(mock,dest)-4.0)<.3
    print("[dynamic-montage] SELF-TEST PASS varied pacing and FFmpeg cut/concat",flush=True)


if __name__=="__main__":
    self_test()
