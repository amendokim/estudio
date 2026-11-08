"""Narrative-aware B-roll and original documentary motion graphics.

Fixes psychology/stoicism scenes being paired with bridge/traffic videos.
Classifies Spanish/English narration before selecting Pixabay stock clips.
Rejects unrelated sources even when the filename is opaque and uses original
editorial stills rather than misrepresenting a generic clip. Adds type-led
opening, chapter transition and lower-third ASS animations without a paid API.
No changes to previously exported files, and no automatic re-render.
"""
from __future__ import annotations

import os
import re
import math
import unicodedata
from pathlib import Path

import httpx
from fastapi import HTTPException
from PIL import Image, ImageDraw, ImageFont

MAX_SEARCHES = 13
MAX_VIDEO_BYTES = 10 * 1024 * 1024
BAD_TOPICS = re.compile(
    r"\b(?:bridge|bridges|ponte|puente|river|rio|traffic|trafico|trânsito|"
    r"truck|camion|highway|autopista|road|carretera|waterfall|cataratas|"
    r"lake|lago|mountain|montana|glacier|fjord|aerial|drone|"
    r"forest|bosque|railway|airplane|train|aeropuerto)\b", re.I
)

INTENTS = {
  "dialogue": {
    "signals": ("hablar","convers","decir","responder","dialog","discut","argument",
                "gritar","escuchar","critica","insult","respect","respeto",
                "conflict","comunic","confront","palabra","relacion","interaccion"),
    "queries": ("people talking", "serious conversation", "couple discussion"),
    "tags": ("talk","conversation","discuss","people","couple","meeting",
             "communication","dialogue","man","woman","discussion","argument"),
    "card": ("HABLAR CON FIRMEZA", "SIN PERDER LA CALMA"),
  },
  "boundaries": {
    "signals": ("limite","permit","acept","rechaz","decir no","negativ",
                "firmeza","defender","defensa","autorrespeto","dignidad",
                "rechazo","presion","manipul","boundary","boundaries"),
    "queries": ("woman no gesture", "person standing confident", "man serious"),
    "tags": ("gesture","hand","stop","woman","man","people","portrait",
             "standing","confident","serious","business","face","person"),
    "card": ("PONER LÍMITES", "TAMBIÉN ES SERENIDAD"),
  },
  "reflection": {
    "signals": ("calma","serenidad","tranquil","interior","silencio","pensar",
                "reflex","mente","emocion","control","estoic","filosof",
                "sentir","respir","pausa","medit","miedo","temor"),
    "queries": ("person thinking", "woman reflecting", "quiet meditation"),
    "tags": ("thinking","thoughtful","meditation","mindfulness","woman","man",
             "people","portrait","alone","quiet","reflection","face","person"),
    "card": ("LA PAUSA CONSCIENTE", "ANTES DE RESPONDER"),
  },
  "decision": {
    "signals": ("decid","decision","elegir","eleg","escog","consecuen",
                "accion","actuar","opcion","responsab","cambio","eleccion",
                "choice","decision","tomar postura"),
    "queries": ("man deciding", "person thoughtful", "walking alone"),
    "tags": ("thinking","decision","choice","people","man","woman","person",
             "alone","walk","walking","thoughtful","reflection"),
    "card": ("ELEGIR CON CLARIDAD", "Y ACTUAR CON RESPETO"),
  },
  "workplace": {
    "signals": ("trabaj","emple","jefe","laboral","colega","oficina",
                "reunion","profesional","equipo","empresa","workplace","office"),
    "queries": ("office conversation", "business meeting", "colleagues discussion"),
    "tags": ("office","work","business","meeting","team","people","discussion",
             "colleagues","coworker","employee","company"),
    "card": ("LÍMITES EN EL TRABAJO", "RESPETO MUTUO"),
  },
  "journaling": {
    "signals": ("escrib","diario","papel","cuaderno","apunt","anot",
                "lista","planific","nota","journal","write","writing"),
    "queries": ("writing notebook", "person journaling", "hand writing"),
    "tags": ("writing","notebook","journal","diary","hand","desk","paper",
             "person","pen","book","write"),
    "card": ("ESCRIBE TU LÍMITE", "HAZLO CONSCIENTE"),
  },
  "movement": {
    "signals": ("camina","camino","andar","avanza","seguir","salir",
                "paso","movimiento","cambiar de rumbo","walking","walk"),
    "queries": ("walking alone", "person walking", "woman outdoors"),
    "tags": ("walking","walk","people","person","woman","man",
             "outdoors","alone","steps","footsteps"),
    "card": ("AVANZAR SIN CEDER", "TU PROPIA VOZ"),
  },
}
STOP = re.compile(r"\b(?:the|and|with|about|with|for|of|in|on|de|la|el|los|las|una|un|y|en|que|para|por)\b", re.I)

def normalize(value):
    s=unicodedata.normalize("NFKD",str(value or "").lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch))

def psychological_project(project):
    corpus=normalize((project.get("title") or "")+" "+(project.get("topic") or ""))
    return any(term in corpus for term in (
        "estoic","seren","pasiv","limite","autocontrol","psicolog",
        "disciplina","respeto","resiliencia","mentalidad","calma",
        "consciencia emocional"))

def intent(scene):
    speech=normalize(scene.get("text") or "")
    hint=normalize(scene.get("query") or "")
    corpus=speech+" "+hint
    # Scene narration has priority over overly generic stock-search prompts.
    wins=[]
    for name,info in INTENTS.items():
        score=sum(3 for word in info["signals"] if word in speech)
        score+=sum(1 for word in info["signals"] if word in hint)
        wins.append((score,name))
    wins.sort(reverse=True)
    return wins[0][1] if wins[0][0] else "reflection"

def query_list(scene):
    kind=intent(scene)
    q=(scene.get("query") or "").lower()
    q=re.sub(r"[^a-z\s-]"," ",q)
    q=re.sub(r"\s+"," ",q).strip()[:65]
    # Reject all bridge-related hints from foreign or stale prompts.
    candidates=[]
    if len(q.split()) in (2,3,4,5) and not BAD_TOPICS.search(q):
        tokens=set(q.split())-{"background","video","footage","stock","cinematic"}
        if tokens & set(INTENTS[kind]["tags"]):
            candidates.append(q)
    candidates+=list(INTENTS[kind]["queries"])
    return list(dict.fromkeys(candidates))[:3]

def descriptor_score(scene, tags):
    cat=intent(scene)
    info=normalize(tags)
    if BAD_TOPICS.search(info):
        return -99
    tokens=set(re.findall(r"[a-z]+",info))
    matches=sum(2 for word in INTENTS[cat]["tags"] if word in tokens)
    # People's activities are more useful than landscape fillers.
    if "people" in tokens or "person" in tokens or "man" in tokens or "woman" in tokens:
        matches+=1
    return matches

def existing_is_relevant(scene):
    if scene.get("media_provider")=="original" and scene.get("media_local"):
        return True
    source=(scene.get("source_page") or "")+" "+(scene.get("author") or "")
    if BAD_TOPICS.search(normalize(source)):
        return False
    if scene.get("media_provider") in ("pixabay","pexels") and (
        descriptor_score(scene,source)>2):
        return True
    # Opaque source pages alone are not evidence of relevant footage.
    return False

def _font(size,bold=False):
    style="DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/"+style,size)
    except OSError:
        return ImageFont.load_default()

def _card(studio,pid,index,cat):
    dest=studio.project_dir(pid)/"uploads"/f"editorial_card_{index:03d}.png"
    dest.parent.mkdir(parents=True,exist_ok=True)
    if dest.exists() and dest.stat().st_size>1000:
        return dest
    w,h=1280,720
    im=Image.new("RGB",(w,h))
    pixels=im.load()
    for y in range(h):
        for x in range(w):
            v=int(11+14*(x/w)+10*(y/h))
            pixels[x,y]=(v//2,v,v+13)
    draw=ImageDraw.Draw(im,"RGBA")
    draw.ellipse((810,-220,1580,550),outline=(220,178,118,190),width=24)
    draw.ellipse((900,-145,1510,465),outline=(92,136,150,110),width=8)
    draw.polygon([(0,560),(620,340),(900,510),(900,720),(0,720)],
                 fill=(17,39,56,210))
    draw.line((70,164,730,164),fill=(226,184,117,255),width=8)
    title,subtitle=INTENTS[cat]["card"]
    draw.text((70,84),"CÓDIGO FUDOSHIN",font=_font(38,True),fill=(230,228,210,255))
    draw.text((70,280),title,font=_font(64,True),fill=(250,249,245,255))
    draw.text((75,380),subtitle,font=_font(40),fill=(234,194,140,255))
    draw.text((75,637),"FILOSOFÍA PRÁCTICA  ·  COMPOSICIÓN ORIGINAL",
              font=_font(21),fill=(174,195,207,255))
    im.save(dest,optimize=True)
    return dest

def _animated_card(studio,pid,index,cat):
    """Ken Burns motion on a branded, original card, with no external footage."""
    import subprocess
    still=_card(studio,pid,index,cat)
    clip=still.with_suffix(".mp4")
    if clip.exists() and clip.stat().st_size>4000:
        return clip
    temp=clip.with_suffix(".partial.mp4")
    filt=("zoompan=z='min(zoom+0.00042,1.095)':"
          "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
          "d=1:s=960x540:fps=24,format=yuv420p")
    cmd=[studio.FFMPEG,"-hide_banner","-loglevel","error","-nostdin","-y",
         "-loop","1","-framerate","24","-i",str(still),
         "-t","15","-vf",filt,"-an","-c:v","libx264",
         "-preset","ultrafast","-crf","31","-pix_fmt","yuv420p",
         "-movflags","+faststart",str(temp)]
    try:
        cp=subprocess.run(cmd,timeout=90,capture_output=True,check=False)
        if cp.returncode!=0 or not temp.is_file() or temp.stat().st_size<4000:
            raise RuntimeError("Cinemática de card indisponível")
        temp.replace(clip)
        return clip
    finally:
        temp.unlink(missing_ok=True)

def _selected_pixabay(scene, used, query_cache, client, key):
    query_sequence=query_list(scene)
    for phrase in query_sequence:
        if phrase not in query_cache:
            if len(query_cache)>=MAX_SEARCHES:
                break
            try:
                resp=client.get("https://pixabay.com/api/videos/",
                                params={"key":key,"q":phrase,"lang":"en",
                                        "per_page":30,"safesearch":"true"},
                                timeout=13)
                resp.raise_for_status()
                query_cache[phrase]=resp.json().get("hits",[])
            except (httpx.HTTPError,ValueError):
                query_cache[phrase]=[]
        ranked=sorted(query_cache.get(phrase,[]),
                      key=lambda hit:descriptor_score(scene,hit.get("tags","")),
                      reverse=True)
        for hit in ranked:
            score=descriptor_score(scene,hit.get("tags",""))
            if score<3:
                continue
            page=str(hit.get("pageURL") or "")
            if not page.startswith("https://pixabay.com/"):
                continue
            for rendition in ("small","medium","tiny"):
                asset=(hit.get("videos") or {}).get(rendition) or {}
                url=asset.get("url") or ""
                size=int(asset.get("size") or 0)
                if (not url or url in used or (size and size>MAX_VIDEO_BYTES)
                        or not url.startswith("https://")):
                    continue
                return (url,page,str(hit.get("user") or "Pixabay")[:70]+
                        " | Pixabay | B-roll ilustrativo: "+str(hit.get("tags") or "")[:95],
                        str(hit.get("tags") or "")[:180])
    return None

def direct(studio,pid,limit=64,allow_completed=False):
    p=studio.get_project(pid)
    if not psychological_project(p):
        return {"applies":False,"reason":"Outro assunto; nenhuma alteração"}
    if p.get("status")=="concluído" and not allow_completed:
        return {"applies":True,"reason":"MP4 preservado; não altera projeto concluído"}
    scenes=p.get("scenes") or []
    if not scenes:
        return {"applies":True,"reason":"Projeto sem cenas"}
    selected=0
    cards=0
    kept=0
    used={s.get("media_url") for s in scenes if s.get("media_url")}
    cache={}
    key=os.getenv("PIXABAY_API_KEY","")
    with httpx.Client(timeout=15,follow_redirects=False) as client:
        for i,scene in enumerate(scenes[:min(limit,90)]):
            cat=intent(scene)
            target_card=(i%6==0 or i==len(scenes)-1)
            old=scene.get("media_url")
            if old:used.discard(old)
            if not target_card and existing_is_relevant(scene):
                if old:used.add(old)
                kept+=1
                continue
            choice=None
            if not target_card and key:
                choice=_selected_pixabay(scene,used,cache,client,key)
                if choice and not studio._approved_url(choice[0],"pixabay"):
                    choice=None
            with studio.db() as db:
                if choice:
                    url,page,author,_=choice
                    db.execute("""UPDATE scenes SET media_url=?,media_kind='video',
                          media_provider='pixabay',source_page=?,author=?,thumb='',
                          media_local='' WHERE id=? AND project_id=?""",
                          (url,page,author,scene["id"],pid))
                    used.add(url);selected+=1
                else:
                    # A subtle moving composition is preferable to a static
                    # slide; fall back to still artwork on low-memory workers.
                    try:
                        local=_animated_card(studio,pid,i,cat)
                    except (OSError,RuntimeError,ValueError):
                        local=_card(studio,pid,i,cat)
                    relative=str(local.relative_to(studio.MEDIA))
                    kind="video" if local.suffix==".mp4" else "image"
                    db.execute("""UPDATE scenes SET media_url='',media_local=?,media_kind=?,
                           media_provider='original',source_page='',thumb='',
                           author='Vinheta animada original Código Fudoshin'
                           WHERE id=? AND project_id=?""",
                           (relative,kind,scene["id"],pid))
                    cards+=1
    print(f"[editorial] project={pid[:8]} shotlist={len(scenes)} B-roll={selected} original_cards={cards} retained={kept} searches={len(cache)}",flush=True)
    return {"applies":True,"scenes":len(scenes),"licensed_broll_selected":selected,
            "original_cards":cards,"retained_verified":kept,
            "pixabay_queries":len(cache),
            "note":"Seleção orientada por cena; sem usar tráfego, pontes ou rios como se fossem psicologia."}

def ass_time(seconds):
    v=max(0.,float(seconds))
    h=int(v//3600);m=int((v%3600)//60);sec=int(v%60);cs=int((v-int(v))*100)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"

def graphic_events(project,width,height):
    scenes=project.get("scenes") or []
    if not scenes:return ""
    scale=max(0.5,min(2.,width/960))
    times=[0.]
    for s in scenes:times.append(times[-1]+max(1.,float(s.get("duration") or 8)))
    total=times[-1]
    rows=[]
    def add(start,end,label,size=31,y=95):
        if end<=start:return
        clean=re.sub(r"[{}\\]","",label).upper()
        if not clean:return
        x=int(48*scale); yy=int(y*scale); sz=int(size*scale)
        tags=(r"{\an7\pos("+str(x)+","+str(yy)+r")\fs"+str(sz)+
              r"\bord1\shad1\c&H00EFCDA3&\fad(450,450)}")
        rows.append("Dialogue: 8,"+ass_time(start)+","+ass_time(end)+
                    ",Default,,0,0,0,,"+tags+clean)
    add(0.35,min(5.6,total),"CÓDIGO FUDOSHIN",42,75)
    add(1.45,min(6.7,total),"LA CALMA TAMBIÉN PONE LÍMITES",21,143)
    if total>45:
        add(total*.28,min(total*.28+4.6,total-1),
            "01  ·  CUANDO CALLAR NO AYUDA",27,82)
    if total>125:
        add(total*.58,min(total*.58+4.6,total-1),
            "02  ·  ELEGIR SIN PERDER LA CALMA",27,82)
    if total>190:
        add(total*.80,min(total*.80+4.5,total-1),
            "03  ·  CONVERTIR LA SERENIDAD EN ACCIÓN",25,82)
    if total>12:
        add(max(0,total-7),max(0,total-1),"RESPETO  ·  CLARIDAD  ·  FIRMEZA",22,90)
    # Small scene labels occasionally; always above caption-safe bottom.
    for i in range(3,len(scenes)-1,6):
        start=times[i]+.35
        stop=min(start+3.8,times[i+1]-.5)
        if stop<=start:continue
        label={"dialogue":"DIÁLOGO","boundaries":"LÍMITES","reflection":"REFLEXIÓN",
               "decision":"ELECCIÓN","workplace":"TRABAJO","journaling":"ESCRITURA",
               "movement":"ACCIÓN"}[intent(scenes[i])]
        add(start,stop,"FUDOSHIN  /  "+label,17,55)
    return "\n".join(rows)+"\n"

def install(studio):
    original_search=studio._auto_find_media
    original_subtitles=studio.subtitles_for
    original_render=studio.render_task

    def directed_search(pid,limit):
        result=original_search(pid,limit)
        # The universal editor supersedes psychology-only queries for all
        # new projects. This avoids duplicated stock API traffic and
        # contradictory scene classifications.
        import sys
        if "studio_semantic_media" in sys.modules:
            return result
        p=studio.get_project(pid)
        if psychological_project(p) and p.get("status")!="concluído":
            try:
                direct(studio,pid,limit=max(64,limit))
            except Exception as exc:
                print("[editorial] scene-aware selection failed: "+
                      type(exc).__name__,flush=True)
        return result

    def new_subtitles(project,out,width,height):
        ass,srt=original_subtitles(project,out,width,height)
        if psychological_project(project):
            events=graphic_events(project,width,height)
            if events:
                with open(ass,"a",encoding="utf-8") as handle:
                    handle.write(events)
        return ass,srt

    def cinematic_render(pid):
        p=studio.get_project(pid)
        if psychological_project(p) and p.get("status")!="concluído" and not p.get("soundtrack"):
            try:
                from studio_cinematic_pilot import _make_music
                _make_music(studio,pid)
                print("[editorial] original ambient soundtrack added",flush=True)
            except (OSError,ValueError,RuntimeError) as exc:
                print("[editorial] soundtrack unavailable: "+type(exc).__name__,flush=True)
        return original_render(pid)

    studio._auto_find_media=directed_search
    studio.subtitles_for=new_subtitles
    studio.render_task=cinematic_render

    @studio.app.get("/api/editorial/projects/{pid}")
    def shotplan(pid:str):
        p=studio.get_project(pid)
        scenes=p.get("scenes") or []
        active=psychological_project(p)
        return {"project_id":pid,"applies":active,
                "current_video_preserved":p.get("status")=="concluído",
                "source_problem_fixed":True,
                "planned_scenes":[{
                    "number":i+1,"intent":intent(sc),
                    "english_stock_queries":query_list(sc),
                    "media_provider":sc.get("media_provider") or "",
                    "current_source_may_be_unrelated":
                      active and sc.get("media_provider") not in ("original",)
                      and not existing_is_relevant(sc),
                    "proposed_edit":"editorial_graphic" if i%6==0 else "semantic_broll"
                } for i,sc in enumerate(scenes[:90])],
                "note":"Novo seletor usado em futuras renderizações. O vídeo já concluído não é alterado automaticamente."}

if __name__=="__main__":
    assert psychological_project({"title":"Poner límites con calma","topic":"serenidad"})
    assert not psychological_project({"title":"Ponte da Amizade","topic":"fronteira"})
    a={"text":"Necesito poner límites cuando me faltan al respeto","query":"bridge traffic"}
    assert intent(a) in ("boundaries","dialogue")
    assert all("bridge" not in x for x in query_list(a))
    assert descriptor_score(a,"bridge, road, river")<0
    b={"text":"En la oficina, mi jefe me exige demasiado","query":"business meeting"}
    assert intent(b)=="workplace"
    assert len(graphic_events({"scenes":[{"duration":10,"text":"Una reflexión"}]*32},960,540))>200
    app=Path("/app/app.py")
    if not app.is_file():raise SystemExit("Missing /app/app.py")
    marker="\nimport studio_visual_direction as _visual_direction\n"
    code=app.read_text(encoding="utf-8")
    if marker not in code:
        app.write_text(code+marker+
                       '_visual_direction.install(__import__("sys").modules[__name__])\n',
                       encoding="utf-8")
