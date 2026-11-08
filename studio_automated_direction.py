"""Cost-aware editorial source decisions and channel-adaptive FFmpeg cut editor.

This is a non-destructive layer on top of the semantic storyboard:
- Authentic source only for a named, scene-relevant event, subject to verified
  Commons licence or documented permission. Never download YouTube streams.
- Licensed stock for real-world actions, original animation for abstractions.
- Cut pacing and captions adapt to the channel's actual domain.
- Rendered exports remain untouched; additional edit jobs have a hard disk
  reserve and use only local FFmpeg.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import studio_semantic_media as semantic
import studio_youtube_policy as rights

VERSION="editorial-director-1.0"
MIN_FREE_MB=230
MAX_EDITED_SCENES=24

PROFILES={
 "psychology":{"name":"contemplativo","cuts":2,"contrast":1.04,"saturation":0.94,"accent":"&H009AC7EF&"},
 "history":{"name":"documental","cuts":2,"contrast":1.07,"saturation":0.88,"accent":"&H0096CCEA&"},
 "infrastructure":{"name":"engenharia","cuts":2,"contrast":1.05,"saturation":0.99,"accent":"&H00BFD5E5&"},
 "science_space":{"name":"cosmos","cuts":3,"contrast":1.08,"saturation":1.12,"accent":"&H00F0D6AF&"},
 "technology":{"name":"tecnologia","cuts":3,"contrast":1.06,"saturation":1.10,"accent":"&H00EAD7A8&"},
 "economy":{"name":"explicativo","cuts":2,"contrast":1.05,"saturation":1.02,"accent":"&H00A9DDBD&"},
 "wildlife":{"name":"natural","cuts":1,"contrast":1.03,"saturation":1.02,"accent":"&H009FC8B9&"},
 "food":{"name":"gastronomia","cuts":2,"contrast":1.03,"saturation":1.04,"accent":"&H00A6D1F2&"},
 "general":{"name":"editorial","cuts":2,"contrast":1.04,"saturation":1.00,"accent":"&H00D5D5E1&"},
}

def profile_for(project,channel):
    domain=semantic.domain_of(project.get("title") or "",project.get("topic") or "",
                              channel.get("niche") or "")
    return domain,PROFILES.get(domain,PROFILES["general"])

def _subject_specific(scene,project):
    """Check a named event/subject, not generic history narration."""
    title=semantic.tokens(project.get("title") or "")
    spoken=set(semantic.tokens(scene.get("text") or ""))
    caption=set(semantic.tokens(scene.get("query") or ""))
    meaningful={w for w in title if len(w)>=5 and w not in
                {"history","historia","documentary","documentario",
                 "video","about","sobre","partes","minutos","pilot"}}
    if "ponte" in semantic.normalized(project.get("title") or "") and (
       "amizade" in semantic.normalized(project.get("title") or "")):
        text=semantic.normalized(scene.get("text") or "")
        return ("ponte" in text or "amizade" in text or
                "foz do iguacu" in text or "ciudad del este" in text)
    return len(meaningful & (spoken | caption))>=2

def _is_explainer(scene):
    text=semantic.normalized(scene.get("text") or "")
    return bool(
        re.search(r"\b(?:porcentagem|percentual|por ciento|percent|percentage|"
                  r"grafico|chart|diagram|tabla|tabela|"
                  r"primero|segundo|tercero|paso|passo|step|"
                  r"difference|diferenca|diferencia)\b",text)
        or re.search(r"\d+(?:[.,]\d+)?\s*%",text)
    )

def classify_scene(project,channel,scene,shot):
    domain,style=profile_for(project,channel)
    provider=scene.get("media_provider") or ""
    source=scene.get("source_page") or ""
    # Archives can be *considered*, not automatically accepted. Attribution
    # and source link must be kept, and permission must have been reviewed by
    # the existing rights gate.
    eligible=(domain in ("history","infrastructure","science_space")
              and _subject_specific(scene,project))
    genuine_archive=bool(
        eligible and scene.get("media_kind")=="video" and
        rights.recognized_archival_scene(scene) and
        0<float(scene.get("duration") or 8)<=rights.MAX_YOUTUBE_CLIP_SECONDS and
        ((provider=="wikimedia" and source.startswith("https://commons.wikimedia.org/wiki/File:"))
          or (provider=="authorized_youtube" and source.startswith("https://"))))
    if genuine_archive:
        decision="arquivo_licenciado"
        why="Acontecimento específico; arquivo autenticado, atribuição e licença/autorização verificados."
    elif _is_explainer(scene):
        decision="grafico_original"
        why="Informação abstrata ou numérica: é melhor explicar visualmente do que usar imagens genéricas."
    elif eligible:
        decision="arquivo_ou_stock"
        why="Arquivo autêntico seria preferível se houver licença verificável; caso contrário, usar stock ilustrativo."
    elif domain in ("psychology","food","wildlife","economy","technology",
                    "science_space","history","infrastructure","general"):
        decision="stock_licenciado"
        why="Ação ou ambiente ilustrativo compatível com a narração; não necessita de material de terceiros do YouTube."
    else:
        decision="grafico_original"
        why="Sem correspondência demonstrável: não inserir B-roll enganoso."
    # Even when the writer wanted archive, never bypass rights verification.
    if provider=="authorized_youtube" and not genuine_archive:
        decision="revisar_direitos"
        why="Origem YouTube indicada, mas ainda sem verificação contextual suficiente para uso automático."
    return {"preferred_source":decision,"reason":why,
            "domain":domain,"visual_intent":shot.get("visual_intent") or "context",
            "cut_style":style["name"],"cut_target":style["cuts"] if not genuine_archive else 1,
            "source_used":provider or "not_selected",
            "archive_auto_approved":genuine_archive}

def _free_mb(studio):
    return shutil.disk_usage(studio.DATA).free/1048576

def _ass(t):
    t=max(0.,float(t))
    h=int(t//3600);m=int((t%3600)//60);s=int(t%60);c=int(round(t%1*100))
    if c>=100:s+=1;c=0
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"

def _safe(s,max_chars=60):
    return re.sub(r"[{}\\\n\r]"," ",str(s or "")).strip()[:max_chars]

def typography(project,channel,domain,style):
    scenes=project.get("scenes") or []
    if not scenes:return ""
    total=sum(max(1.,float(s.get("duration") or 8)) for s in scenes)
    name=_safe(channel.get("name") or "Estúdio IA",30)
    title=_safe(project.get("title") or "",46)
    color=style["accent"]
    events=[]
    def title_event(start,end,words,large=False):
        if end<=start or not words:return
        fontsize=35 if large else 23
        # Top safe area: caption track occupies bottom of frame.
        tag=(r"{\an7\move(47,74,62,74,0,600)\fs"+str(fontsize)+
             r"\b1\bord2\shad1\c"+color+r"\fad(300,460)}")
        events.append("Dialogue: 7,"+_ass(start)+","+_ass(end)+
                      ",Default,,0,0,0,,"+tag+_safe(words,54))
    title_event(.32,min(4.1,total),name,True)
    title_event(1.2,min(6.7,total),title,False)
    if len(scenes)>=10:
        elapsed=0.
        for i,scene in enumerate(scenes):
            if 0<i<len(scenes)-2 and i%8==0:
                intro=_safe(scene.get("text") or "",50)
                cut=intro.rfind(" ",0,45)
                if cut>20:intro=intro[:cut]+"…"
                title_event(elapsed+.28,min(elapsed+4.1,total-1),intro)
            elapsed+=max(1.,float(scene.get("duration") or 8))
    title_event(max(0,total-5),max(0,total-1.1),name)
    return "\n".join(events)+"\n"

def _clip_params(duration,cuts):
    duration=min(15.,max(1.2,float(duration)))
    if cuts<=1 or duration<5.0:
        return [(0.,duration)]
    parts=[]
    share=duration/cuts
    for i in range(cuts):
        start=round(i*share,3)
        end=round(duration if i==cuts-1 else (i+1)*share,3)
        parts.append((start,end))
    return parts

def build_cuts(studio,source,pid,scene,domain,style):
    """Create same-source editorial jump-cuts without unlicensed footage.

    The upstream FFmpeg renderer continues to control final caption timing,
    soundtrack, container and target format. Clips are used only as a local
    picture source, and never download any YouTube streams.
    """
    if not os.getenv("STUDIO_MODERN_EDIT_ENABLED","1")=="1":
        return source,0
    if domain not in PROFILES:
        return source,0
    if _free_mb(studio)<MIN_FREE_MB:
        return source,0
    duration=float(scene.get("duration") or 8)
    if duration<5 or duration>16:
        return source,0
    ratio=studio.get_project(pid).get("ratio") or "16:9"
    if ratio!="16:9":
        return source,0
    parts=_clip_params(duration,style["cuts"])
    if len(parts)<2:
        return source,0
    source=Path(source)
    if not source.exists() or source.suffix.lower() not in (".mp4",".webm",".mov"):
        return source,0
    destdir=studio.project_dir(pid)/"uploads"
    destdir.mkdir(parents=True,exist_ok=True)
    fingerprint=hashlib.sha256(
        (str(scene.get("id"))+str(scene.get("media_url"))+
         str(source.stat().st_size)+str(duration)+domain+VERSION).encode()
    ).hexdigest()[:16]
    dest=destdir/("cut_"+fingerprint+".mp4")
    if dest.is_file() and dest.stat().st_size>3000:
        return dest,len(parts)
    scratch=destdir/("cut_"+fingerprint+".part.mp4")
    count=len(parts)
    left=[]
    graph=f"[0:v]fps=24,scale=1120:630:force_original_aspect_ratio=increase,split={count}"+ "".join(f"[v{i}]" for i in range(count))+";"
    for i,(start,stop) in enumerate(parts):
        # Different crops read as an alternating wide/medium/close-up shot.
        # Colors remain natural; no fake historical colorization.
        x=(25,90,55)[i%3]
        y=(45,50,40)[i%3]
        graph+=(f"[v{i}]trim=start={start}:duration={stop-start:.3f},"
                f"setpts=PTS-STARTPTS,crop=960:540:{x}:{y},"
                f"format=yuv420p[s{i}];")
        left.append(f"[s{i}]")
    graph+="".join(left)+f"concat=n={count}:v=1:a=0,"
    graph+=(f"eq=contrast={style['contrast']}:saturation={style['saturation']},"
            f"format=yuv420p[out]")
    cmd=[studio.FFMPEG,"-hide_banner","-loglevel","error","-nostdin",
         "-y","-stream_loop","-1","-i",str(source),
         "-filter_complex",graph,"-map","[out]","-an",
         "-c:v","libx264","-preset","ultrafast","-crf","32",
         "-pix_fmt","yuv420p","-t",f"{duration:.3f}",
         "-movflags","+faststart",str(scratch)]
    try:
        result=subprocess.run(cmd,capture_output=True,timeout=140,check=False)
        if result.returncode or not scratch.is_file() or scratch.stat().st_size<4000:
            raise RuntimeError("FFmpeg cut montage unavailable")
        if _free_mb(studio)<105:
            raise RuntimeError("Disk reserve reached")
        scratch.replace(dest)
        return dest,count
    except (OSError,subprocess.TimeoutExpired,RuntimeError):
        return source,0
    finally:
        scratch.unlink(missing_ok=True)

def schema(studio):
    with studio.db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS studio_editorial_source_audit(
         project_id TEXT NOT NULL,scene_id TEXT NOT NULL,
         source_decision TEXT NOT NULL,source_used TEXT NOT NULL,
         explanation TEXT NOT NULL,style TEXT NOT NULL,
         cuts INTEGER NOT NULL DEFAULT 0,
         PRIMARY KEY(project_id,scene_id))""")

def inspect_project(studio,pid):
    project=studio.get_project(pid)
    channel=semantic.channel_context(studio,project)
    domain,style=profile_for(project,channel)
    board,_=semantic.load_plan(studio,project,allow_ai=False,write=False)
    result=[]
    for scene,shot in zip(project.get("scenes") or [],board["scenes"]):
        recommended=classify_scene(project,channel,scene,shot)
        result.append({"scene":len(result)+1,
                       "spoken_text":str(scene.get("text") or "")[:160],
                       **recommended})
    with studio.db() as db:
        edits=[dict(row) for row in db.execute(
            "SELECT scene_id,source_decision,source_used,style,cuts FROM studio_editorial_source_audit WHERE project_id=?",
            (pid,)).fetchall()]
    return {"project_id":pid,"title":project.get("title"),
            "channel":channel["name"],"domain":domain,
            "edit_style":style["name"],"proposed":result,
            "realized_cuts":edits,
            "status":project.get("status"),
            "modern_edit_enabled":os.getenv("STUDIO_MODERN_EDIT_ENABLED","1")=="1",
            "disk_free_mb":round(_free_mb(studio)),
            "extra_cut_min_free_mb":MIN_FREE_MB,
            "archival_policy":"Archive only with a documented licence/permission and specific subject; no YouTube stream downloads",
            "note":"A decisão é proposta editorial, não prova de que cada quadro contém o objeto exato."}

def self_test():
    def demo(title,topic,text,provider=""):
        p={"title":title,"topic":topic,"ratio":"16:9"}
        c={"name":"Test channel","niche":""}
        shot={"visual_intent":"illustrative"}
        s={"text":text,"query":text[:50],"media_provider":provider}
        return classify_scene(p,c,s,shot)
    assert demo("Estoicismo e serenidade","psicologia","Um homem reflete no silêncio")["preferred_source"]=="stock_licenciado"
    assert demo("Ponte da Amizade","engenharia","A Ponte da Amizade liga Brasil e Paraguai")["preferred_source"]=="arquivo_ou_stock"
    assert demo("Explorando o Universo","astronomia","Uma estrela ilumina o céu")["preferred_source"]=="stock_licenciado"
    assert demo("A inflação","economia","O preço subiu 17%")["preferred_source"]=="grafico_original"
    assert len(_clip_params(9.1,3))==3
    assert len(_clip_params(4,3))==1
    dummy={"scenes":[{"text":"Uma conversa difícil","duration":8}]*12,
           "title":"Serenidad y límites"}
    assert "Dialogue: 7," in typography(dummy,{"name":"Código Fudoshin"},"psychology",PROFILES["psychology"])
    assert sum(x[1]-x[0] for x in _clip_params(9,3))==9
    print("[editorial-director] SELF-TEST PASS source decisions, copyright gating, typography and multi-cut pacing",flush=True)

def install(studio):
    schema(studio)
    old_subtitle=studio.subtitles_for
    old_fetch=studio.fetch_clip
    old_search=studio._auto_find_media

    def directed_search(pid,limit):
        result=old_search(pid,limit)
        project=studio.get_project(pid)
        if project.get("status")=="concluído":return result
        channel=semantic.channel_context(studio,project)
        plan,_=semantic.load_plan(studio,project,allow_ai=False,write=False)
        changed=0
        for index,(scene,shot) in enumerate(zip(project.get("scenes") or [],plan["scenes"])):
            choice=classify_scene(project,channel,scene,shot)
            if scene.get("media_provider") not in ("authorized_youtube","wikimedia"):
                continue
            if choice["archive_auto_approved"]:
                continue
            # Fail closed on irrelevant or unverifiable archive. Provide an
            # original editorial replacement rather than leave a blank shot.
            shot["channel_name"]=channel.get("name") or "Estúdio IA"
            try:
                replacement=semantic.animated_fallback(studio,project,pid,index,shot)
            except (OSError,ValueError,RuntimeError):
                replacement=semantic.fallback_graphic(studio,project,pid,index,shot)
            relative=str(replacement.relative_to(studio.MEDIA))
            kind="video" if replacement.suffix==".mp4" else "image"
            with studio.db() as db:
                db.execute("""UPDATE scenes SET media_url='',media_local=?,
                     media_kind=?,media_provider='original',source_page='',author=?,
                     thumb='' WHERE id=? AND project_id=?""",
                     (relative,kind,"Composição editorial original: arquivo não verificado",
                      scene["id"],pid))
            changed+=1
        if changed:print("[editorial-director] replaced %d unverified or mismatched archive shots with original visuals" % changed,flush=True)
        return result

    def cut_on_fetch(scene,pid,*args,**kwargs):
        source=old_fetch(scene,pid,*args,**kwargs)
        if not source or scene.get("media_kind")!="video":
            return source
        project=studio.get_project(pid)
        if project.get("status")=="concluído":
            return source
        channel=semantic.channel_context(studio,project)
        domain,style=profile_for(project,channel)
        if scene.get("media_provider") in ("wikimedia","authorized_youtube"):
            return source  # preserve provenance and original archival framing
        if scene.get("media_provider") not in ("pixabay","pexels"):
            return source
        if _free_mb(studio)<MIN_FREE_MB:
            return source
        with studio.db() as db:
            count=db.execute("""SELECT COUNT(*) FROM studio_editorial_source_audit
                   WHERE project_id=? AND cuts>1""",(pid,)).fetchone()[0]
        if count>=MAX_EDITED_SCENES:
            return source
        updated,cuts=build_cuts(studio,source,pid,scene,domain,style)
        if cuts>1:
            with studio.db() as db:
                db.execute("""INSERT OR REPLACE INTO studio_editorial_source_audit
                     (project_id,scene_id,source_decision,source_used,explanation,style,cuts)
                     VALUES(?,?,?,?,?,?,?)""",
                     (pid,scene["id"],"stock_licenciado",
                      scene.get("media_provider") or "pixabay",
                      "Recorte de duas ou mais tomadas do mesmo B-roll licenciado.",
                      style["name"],cuts))
        return updated

    def typography_for_channel(project,out,width,height):
        ass,srt=old_subtitle(project,out,width,height)
        if width<height:return ass,srt
        channel=semantic.channel_context(studio,project)
        domain,style=profile_for(project,channel)
        additions=typography(project,channel,domain,style)
        if additions:
            with Path(ass).open("a",encoding="utf-8") as handle:
                handle.write(additions)
        return ass,srt

    studio._auto_find_media=directed_search
    studio.fetch_clip=cut_on_fetch
    studio.subtitles_for=typography_for_channel

    @studio.app.get("/api/editorial/strategy/projects/{pid}")
    def preview_strategy(pid:str):
        return inspect_project(studio,pid)

    @studio.app.get("/api/editorial/strategy")
    def strategy_status():
        return {"version":VERSION,"profiles":list(PROFILES),
                "stock_source":"Pixabay/Pexels licensed",
                "archive_source":"Wikimedia Commons CC/public domain or own authorized archive",
                "youtube_stream_downloads":False,
                "pay_per_edit_ai_required":False,
                "render_quality":"FFmpeg only; scene-specific title and cut pacing",
                "disk_cut_reserve_mb":MIN_FREE_MB,
                "upload_enabled_by_this_module":False}

if __name__=="__main__":
    self_test()
    root=Path("/app/app.py")
    if not root.is_file():
        raise SystemExit("Missing studio app.py")
    marker="\nimport studio_automated_direction as _automated_direction\n"
    source=root.read_text(encoding="utf-8")
    if marker not in source:
        root.write_text(source+marker+
                        '_automated_direction.install(__import__("sys").modules[__name__])\n',
                        encoding="utf-8")
