"""Targeted 5-minute documentary V2 rescue: replace some static inserts with
already selected real video B-roll. Never claims clips are from YouTube.
Never disables the 80% video guard. Only affects one owner-requested test.
"""
from __future__ import annotations
import json
import math
import re
import unicodedata
from collections import Counter
from pathlib import Path

TARGET = "4eaa1294-628f-4ea1-83c4-44a29d9fa1c4"
TARGET_RATIO = 0.825
MAX_USES_PER_SOURCE = 3
COMMON = {
    "para", "pelo", "pela", "pessoa", "pessoas", "entre", "sobre",
    "agora", "todas", "todos", "brasileiros", "brasileiro", "brasileira",
    "paraguai", "paraguaio", "alguns", "mais", "menos", "muitos", "uma",
    "isso", "aqui", "essa", "esse", "dessa", "desses", "depois", "anos",
    "pode", "quando", "pais", "muito", "estao", "podem", "video",
}

def _tokens(s):
    plain = unicodedata.normalize("NFKD", str(s or "").casefold())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return {v for v in re.findall(r"[a-z]{4,}", plain) if v not in COMMON}

def _seconds(scene):
    try:
        return max(0.1,float(scene.get("duration") or 7))
    except (ValueError,TypeError):
        return 7.0

def _source(scene):
    return str(scene.get("media_url") or scene.get("media_local") or "")

def _is_video(scene):
    return bool(scene.get("media_kind")=="video" and _source(scene))

def _score(scene,candidate,distance,reuse):
    # The original media's associated narration + search query provide a
    # grounded subject tag, not just an opaque file name.
    a=_tokens((scene.get("query") or "")+" "+(scene.get("text") or ""))
    b=_tokens((candidate.get("query") or "")+" "+
              (candidate.get("text") or "")+" "+(candidate.get("author") or ""))
    overlap=len(a.intersection(b))
    # Keep a chance for adjacent, same-storyline background; avoid blind
    # imports from distant portions of the documentary.
    return overlap*12 - distance*1.6 - reuse*7

def calculate(scenes):
    total=sum(_seconds(s) for s in scenes)
    video=sum(_seconds(s) for s in scenes if _is_video(s))
    return (video/total if total else 0, video, total)

def choose(scenes,target_ratio=TARGET_RATIO):
    scenes=[dict(s) for s in scenes]
    ratio,video,total=calculate(scenes)
    initial=ratio
    counts=Counter(_source(s) for s in scenes if _is_video(s))
    originals=[(i,s) for i,s in enumerate(scenes)
               if _is_video(s) and s.get("media_provider") in
               ("pixabay","pexels","wikimedia","authorized_youtube")]
    choices=[]
    # For each gap, find a reasonably matching already selected B-roll source.
    # Require positive overlap, unless in same immediate story block.
    while ratio < target_ratio and originals:
        best=None
        for i,scene in enumerate(scenes):
            if _is_video(scene):continue
            for j,candidate in originals:
                key=_source(candidate)
                if counts[key]>=MAX_USES_PER_SOURCE:continue
                if i==j:continue
                distance=abs(i-j)
                score=_score(scene,candidate,distance,counts[key]-1)
                # Unrelated footage far away is worse than an honest graphic.
                if not (_tokens((scene.get("query") or "")+" "+(scene.get("text") or "")) &
                        _tokens((candidate.get("query") or "")+" "+(candidate.get("text") or "")+
                                " "+(candidate.get("author") or ""))) and distance>2:
                    continue
                entry=(score+min(10,_seconds(scene)), -distance, -i, i,j)
                if best is None or entry>best:
                    best=entry
        if best is None:break
        _,__,___,i,j=best
        original=originals[[x[0] for x in originals].index(j)][1]
        scene=scenes[i]
        choices.append({"scene_number":i+1,"source_scene_number":j+1,
                        "old_kind":scene.get("media_kind"),"provider":original.get("media_provider"),
                        "source_page":original.get("source_page"),"reuse_source":_source(original)})
        for field in ("media_url","media_local","media_kind","media_provider","source_page","author","thumb"):
            scene[field]=original.get(field) or ""
        counts[_source(original)]+=1
        ratio,video,total=calculate(scenes)
    return scenes,{"initial_video_percent":round(initial*100,1),
                   "video_percent":round(ratio*100,1),
                   "minimum_video_percent":80.0,
                   "changed_shots":len(choices),
                   "choices":choices,"sources":"Licensed stock already found in project, NOT YouTube"}

def repair(studio,pid):
    if pid!=TARGET:return
    project=studio.get_project(pid)
    if project.get("status")=="concluído":return
    scenes=project.get("scenes") or []
    if len(scenes)!=45:return
    after,report=choose(scenes)
    if report["changed_shots"]:
        with studio.db() as db:
            for old,new in zip(scenes,after):
                if _source(old)==_source(new) and old.get("media_kind")==new.get("media_kind"):
                    continue
                db.execute("""UPDATE scenes SET media_url=?,media_local=?,media_kind=?,
                    media_provider=?,source_page=?,author=?,thumb=?
                    WHERE project_id=? AND id=?""",
                    (new.get("media_url") or "",new.get("media_local") or "",
                     new.get("media_kind") or "",new.get("media_provider") or "",
                     new.get("source_page") or "",new.get("author") or "",
                     new.get("thumb") or "",pid,old["id"]))
    folder=studio.project_dir(pid)/"export"
    folder.mkdir(parents=True,exist_ok=True)
    (folder/"v2_media_repair_audit.json").write_text(
        json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print("[paraguai-v2-rescue] "+json.dumps(
        {k:v for k,v in report.items() if k!="choices"},ensure_ascii=False),flush=True)

def install(studio):
    previous=studio.render_task
    def v2_render(pid):
        if pid==TARGET:repair(studio,pid)
        return previous(pid)
    studio.render_task=v2_render
    @studio.app.get("/api/projects/{pid}/v2-media-audit")
    def report(pid:str):
        if pid!=TARGET:return {"applies":False}
        file=studio.project_dir(pid)/"export"/"v2_media_repair_audit.json"
        if not file.is_file():return {"applies":True,"status":"not_run"}
        return json.loads(file.read_text(encoding="utf-8"))

def self_test():
    def item(i,video):
        return dict(id=str(i),duration=7,query="paraguay bank payments" if i<10 else "border cars road",
                    text="Paraguay bank account news" if i<10 else "border highway traffic",
                    media_kind="video" if video else "image",media_url="https://pixabay.com/"+str(i) if video else "",
                    media_local="",media_provider="pixabay" if video else "original",
                    author="bank account" if i<10 else "border traffic",
                    source_page="https://pixabay.com/",thumb="")
    scenes=[item(i,i%2==0) for i in range(20)]
    adjusted,report=choose(scenes)
    assert report["initial_video_percent"]==50
    assert report["video_percent"]>=80,report
    assert report["changed_shots"]>=6,report
    assert all(s["media_kind"]=="video" for s in adjusted if s["media_url"])
    assert not any(s.get("media_provider")=="youtube" for s in adjusted)
    print("[paraguai-v2-rescue] SELF-TEST PASS",flush=True)

if __name__=="__main__":
    self_test()
    app=Path("/app/app.py")
    if not app.is_file():raise SystemExit("Missing app.py")
    marker="\nimport studio_paraguai_v2_rescue as _v2rescue\n"
    current=app.read_text(encoding="utf-8")
    if marker not in current:
        app.write_text(current+marker+
            '_v2rescue.install(__import__("sys").modules[__name__])\n',encoding="utf-8")
