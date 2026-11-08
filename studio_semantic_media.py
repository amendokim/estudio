"""Universal scene-aware, channel-aware licensed stock video director.

A storyboard is derived from (channel, title, topic, individual narration scenes)
before any asset is accepted. Gemini, when explicitly enabled (default: off),
may propose ONE bounded storyboard per project; all requests and results are
validated against the actual script and passed through a source metadata gate.
The deterministic fallback works for multiple niches in Spanish, Portuguese and
English. If evidence of relevance is weak, use original, clearly illustrative
typography rather than unrelated stock footage.

No YouTube scraping, third-party copyrighted footage or paid per-video AI
is necessary to run the default mode. Never edits an exported MP4.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

VERSION = "universal-storyboard-v1"
SEARCH_LIMIT = 24
SCENE_LIMIT = 120
MIN_SCORE = 5
MAX_VIDEO_BYTES = 8 * 1024 * 1024
STOP = frozenset("""
a an and are as at be by can do does for from how i in into is it of on or
the their them this to was were what when where which who will with without you
de la el los las un una y o en con por para del al que como mas muy lo su sus
es se ser pero cuando porque donde
do da dos das o os a um uma e em com para por no na nos nas que como muito
this video footage stock background cinematic beautiful amazing
""".split())
THEMES = {
  "psychology": {
    "markers": ("estoic","seren","autocontrol","calma","mentalidad","psicolog","psycholog",
                "mente","emotion","resilien","disciplina","discipl","respeto",
                "limite","boundary","boundaries","motivac","motivatio","passiv","pasiv"),
    "default": ("person thinking", "people conversation"),
    "concepts": (
      ("conversation",("convers","hablar","habland","discut","conflict","comunic",
                        "dialog","argumen","respon","escuch","respect","respecto",
                        "habla","say","speak","talk","conversation"),
       ("people talking", "serious conversation"),("people","talking","conversation","meeting","discussion")),
      ("boundaries",("limite","limites","boundary","boundaries","rechaz","firme",
                     "respeto","respect","negativa","say no","defend","mano"),
       ("person saying no", "confident person gesture"),("person","gesture","hand","standing","confident")),
      ("work",("trabaj","oficina","empleo","jefe","laboral","colega","workplace","office","boss"),
       ("business colleagues discussion", "office meeting"),("office","business","people","meeting","colleagues")),
      ("reflection",("reflex","mente","calma","pensar","piens","tranquil","silencio","respir",
                     "medit","thought","inner","reflect","stoic","estoic"),
       ("person thinking", "mindfulness meditation"),("person","thinking","meditation","portrait","woman","man")),
      ("choice",("decid","decis","elegir","choice","decision","actuar","consecuen"),
       ("person making decision", "person thoughtful"),("person","thoughtful","decision","thinking")),
      ("journal",("escrib","cuaderno","apunt","diario","writing","journal","notebook"),
       ("writing notebook", "person journaling"),("hand","notebook","writing","journal")),
    ),
    "incompatible": ("bridge","bridges","puente","ponte","river","traffic","highway",
                     "truck","glacier","mountain","fjord","airplane","train","spaceship",
                     "galaxy","ocean"),
  },
  "infrastructure": {
    "markers": ("ponte","puente","bridge","ingenier","engenhar","engineering",
                "construction","construc","arquitet","architecture","tunel","tunnel",
                "rodovia","highway","ferrovia","railway"),
    "default": ("bridge construction", "city infrastructure"),
    "concepts": (
      ("building",("construc","constru","engineering","estrutura","estructura",
                   "obra","build","steel","concrete","concreto"),
       ("bridge construction", "engineer construction"),("bridge","construction","engineering","building","concrete")),
      ("transport",("transit","trafic","tráfic","trafego","camion","caminh","transport",
                    "vehicle","carro","truck","road"),
       ("bridge traffic", "road transport"),("bridge","traffic","road","truck","vehicles")),
      ("urban",("ciudad","cidade","urban","fronter","border","crossing","pedestre"),
       ("urban bridge", "city streets"),("city","bridge","road","urban","pedestrian")),
    ),
    "incompatible": ("galaxy","spaceship","microbiology","cosmos"),
  },
  "science_space": {
    "markers": ("space","espaco","espacio","nasa","cosmos","planet","planeta","univers",
                "galax","estrel","star","satellite","satelit","astronaut","marte","mars",
                "moon","luna","lua","rocket","foguete"),
    "default": ("planet space", "telescope stars"),
    "concepts": (
      ("launch",("lanc","lanza","launch","rocket","foguete","cohete"),
       ("rocket launch", "space rocket"),("rocket","launch","space","astronaut")),
      ("planet",("planet","earth","terra","tierra","marte","mars","moon","luna","lua"),
       ("planet space", "earth from space"),("planet","earth","space","moon","solar")),
      ("stars",("galax","estrela","stars","constel","cosmos","univers"),
       ("galaxy stars", "night sky stars"),("galaxy","stars","space","universe","cosmos")),
      ("research",("scient","cient","telesco","observ","investig","research","scientist"),
       ("astronomer telescope", "space observatory"),("telescope","astronomy","scientist","observatory")),
    ),
    "incompatible": ("bridge","highway","truck","office meeting","traffic","cooking"),
  },
  "history": {
    "markers": ("historia","história","history","historical","antig","ancient",
                "guerra","war","imperio","empire","civiliz","arqueolog","revolu",
                "medieval","roma","roman","archiv","documentary history"),
    "default": ("historical archive", "ancient ruins"),
    "concepts": (
      ("war",("guerra","battle","batalha","soldad","militar","war","warfare"),
       ("historical battlefield", "military archive"),("soldier","army","military","battle","history")),
      ("architecture",("templo","ruin","monument","castle","palacio","arquitet","architecture"),
       ("ancient ruins", "historic architecture"),("ruins","ancient","architecture","castle","monument")),
      ("documents",("document","mapa","map","registro","archivo","archive","letter"),
       ("historical documents", "old manuscripts"),("documents","archive","manuscript","history","paper")),
    ),
    "incompatible": ("spaceship","ai robot","modern traffic"),
  },
  "economy": {
    "markers": ("financ","finance","econom","economia","dinheiro","dinero","money",
                "inflac","bank","banco","mercado financeiro","investment","invest",
                "credito","crédito","stocks","stock market"),
    "default": ("business finance", "financial documents"),
    "concepts": (
      ("market",("mercado","market","stock","acoes","acciones","bolsa","invest"),
       ("stock market", "financial trading"),("market","business","finance","trade","stock")),
      ("currency",("dinheiro","dinero","money","dolar","dollar","currency","moeda","banco","bank"),
       ("counting money", "currency bank"),("money","currency","cash","bank","dollar")),
      ("cost",("inflac","preco","precio","cost","salary","salario","budget"),
       ("shopping supermarket", "household budget"),("shopping","money","price","supermarket","family")),
    ),
    "incompatible": ("bridge","galaxy","ocean","rocket","wildlife"),
  },
  "wildlife": {
    "markers": ("natureza","naturaleza","nature","animal","wildlife","selva","jungle",
                "floresta","forest","biodivers","especies","species","oceano","ocean",
                "wild animals","conserva"),
    "default": ("wild animals nature", "natural habitat"),
    "concepts": (
      ("animals",("animal","predador","predator","species","especie","pássaro","ave","bird"),
       ("wild animals", "birds wildlife"),("wildlife","animals","nature","species","bird")),
      ("ecosystem",("floresta","forest","selva","habitat","biodivers","ecosystem"),
       ("forest animals", "nature ecosystem"),("forest","wildlife","nature","environment")),
      ("ocean",("oceano","ocean","marine","marino","marinha","whale","ballena","baleia"),
       ("ocean wildlife", "marine animals"),("ocean","sea","marine","wildlife","fish")),
    ),
    "incompatible": ("office","bank","stocks","highway","traffic","rocket"),
  },
  "technology": {
    "markers": ("technology","tecnolog","inteligencia artificial","artificial intelligence",
                "comput","software","programac","programming","cyber","ciber","robot",
                "digital","internet","algoritm","algorithm","chip","microprocessor"),
    "default": ("computer technology", "software developer"),
    "concepts": (
      ("coding",("codigo","coding","programa","software","app","desenvol","develop"),
       ("programmer computer", "software developer"),("computer","programmer","coding","technology","software")),
      ("security",("hack","segur","security","ciber","cyber","privac","data"),
       ("computer security", "data center"),("computer","security","data","technology")),
      ("hardware",("robot","chip","device","hardware","process","maquina","machine"),
       ("computer chip", "electronics technology"),("technology","electronics","computer","robot")),
    ),
    "incompatible": ("bridge","river","forest","wildlife","ocean"),
  },
  "food": {
    "markers": ("comida","food","culinar","cozinh","cocina","recipe","receita",
                "receta","chef","restaurante","restaurant","baking","bakery"),
    "default": ("food cooking", "chef kitchen"),
    "concepts": (
      ("kitchen",("cocin","cozinh","chef","kitchen","cook","cocina"),
       ("chef cooking", "kitchen preparation"),("chef","food","cooking","kitchen")),
      ("ingredients",("ingred","veget","verdura","fruta","fruit","spice","tempero"),
       ("fresh ingredients", "vegetables kitchen"),("food","vegetables","ingredients","cooking")),
      ("serving",("prato","plato","dish","serve","restaurant","restaurante"),
       ("serving food", "restaurant plate"),("food","restaurant","dish","meal")),
    ),
    "incompatible": ("galaxy","rocket","bridge","traffic","military"),
  },
  "general": {
    "markers": (),
    "default": ("people real life", "documentary background"),
    "concepts": (),
    "incompatible": (),
  },
}
TRANSLATE = {
 "limite":"boundaries","limites":"boundaries","serenidad":"calm","calma":"calm",
 "pensar":"thinking","pensando":"thinking","reflexao":"reflection",
 "conversa":"conversation","conversacion":"conversation","hablando":"talking",
 "trabajo":"office","trabalho":"office","ciudad":"city","cidade":"city",
 "puente":"bridge","ponte":"bridge","rio":"river","caminhao":"truck",
 "historia":"history","historia":"history","guerra":"war",
 "planeta":"planet","espacio":"space","espaco":"space","estrela":"stars",
 "mercado":"market","dinero":"money","dinheiro":"money","banco":"bank",
 "floresta":"forest","selva":"jungle","animal":"animal","oceano":"ocean",
 "cozinha":"kitchen","cocina":"kitchen","comida":"food",
 "computador":"computer","ordenador":"computer","codigo":"coding",
}
REJECTED = frozenset({"footage","video","stock","background","cinematic","4k",
                       "abstract","beautiful","amazing","best","youtube"})
_thread = threading.local()

def normalized(text):
    s=unicodedata.normalize("NFKD",str(text or "").casefold())
    return "".join(c for c in s if not unicodedata.combining(c))

def tokens(text):
    return [x for x in re.findall(r"[a-z0-9]+",normalized(text))
            if len(x)>2 and x not in STOP and x not in REJECTED]

def domain_of(title,topic,niche=""):
    text=normalized(title+" "+topic)
    secondary=normalized(niche)
    ranks=[]
    for domain,info in THEMES.items():
        if domain=="general":continue
        score=sum(3 for mark in info["markers"] if mark in text)
        score+=sum(1 for mark in info["markers"] if mark in secondary)
        ranks.append((score,domain))
    ranks.sort(reverse=True)
    return ranks[0][1] if ranks and ranks[0][0]>0 else "general"

def channel_context(studio,project):
    result={"name":"Estúdio IA","niche":"","language":"es" if str(project.get("voice") or "").startswith("es") else
            "en" if str(project.get("voice") or "").startswith("en") else "pt"}
    try:
        with studio.db() as db:
            row=db.execute("""SELECT c.name,c.niche,c.language
                FROM channel_network_ideas i JOIN channel_network_channels c
                    ON c.id=i.channel_id
                WHERE i.project_id=? ORDER BY i.created_at DESC LIMIT 1""",
                (project["id"],)).fetchone()
        if row:
            result.update({k:str(row[k]) for k in ("name","niche","language")})
    except Exception:
        pass
    return result

def _semantic_conflict(query,domain,scene_text="",extra_reject=()):
    lower=normalized(query)
    forbidden=THEMES[domain]["incompatible"]
    relevant=normalized(scene_text)
    for phrase in (*forbidden,*extra_reject):
        phrase=normalized(phrase)
        if not phrase:continue
        if phrase in lower and phrase not in relevant:
            return True
    return False

def _clean_query(q):
    parts=tokens(q)
    if not 2<=len(parts)<=7 or len(str(q))>115:
        return ""
    return " ".join(parts[:6])[:75]

def _hint_relevant(hint,domain,scene_text):
    hint=_clean_query(hint)
    if not hint or _semantic_conflict(hint,domain,scene_text):
        return False
    if domain=="general":
        return True
    # Queries from a script may be concrete even when it is a niche topic.
    shared=set(tokens(hint)) & set(tokens(scene_text))
    concepts={x for _,_,queries,tags in THEMES[domain]["concepts"]
              for q in queries for x in tokens(q)}|set(tokens(" ".join(THEMES[domain]["default"])))
    concepts |= {x for _,_,_,tags in THEMES[domain]["concepts"] for t in tags for x in tokens(t)}
    return bool(shared or set(tokens(hint))&concepts)

def scene_plan(scene,context,domain,index):
    speech=normalized(scene.get("text") or "")
    original=str(scene.get("query") or "")
    concepts=[]
    for name,signals,queries,tags in THEMES[domain]["concepts"]:
        relevance=sum(2 for s in signals if s in speech)
        if relevance:
            concepts.append((relevance,name,queries,tags))
    concepts.sort(reverse=True)
    focused=concepts[0] if concepts else None
    queries=[]
    if _hint_relevant(original,domain,speech):
        queries.append(_clean_query(original))
    if focused:
        queries.extend(focused[2])
    queries.extend(THEMES[domain]["default"])
    # Unknown niche: use the author's actual visual search phrase before the
    # broad fallback, not a random channel-wide phrase.
    if domain=="general":
        concrete=[TRANSLATE.get(w,w) for w in tokens(original)]
        if len(concrete)>=2:
            queries.insert(0," ".join(concrete[:5]))
    valid=[]
    for q in queries:
        cleaned=_clean_query(q)
        if cleaned and cleaned not in valid and not _semantic_conflict(cleaned,domain,speech):
            valid.append(cleaned)
    specificity=(set(tokens(original))&set(tokens(speech)))
    return {"index":index,"domain":domain,
            "scene_excerpt":str(scene.get("text") or "")[:140],
            "visual_intent":focused[1] if focused else "context",
            "queries":valid[:4],
            "must_tags":list(focused[3]) if focused else [],
            "reject_tags":list(THEMES[domain]["incompatible"]),
            "mode":"literal" if specificity else "illustrative",
            "evidence":"scene_and_topic" if focused else "topic_fallback",
            "editorial_label":str(scene.get("text") or "").strip().split(".")[0][:100]}

def storyboard(project,channel):
    scenes=(project.get("scenes") or [])[:SCENE_LIMIT]
    domain=domain_of(project.get("title") or "",project.get("topic") or "",
                     channel.get("niche") or "")
    return {"version":VERSION,"project_id":project["id"],
            "channel":channel,
            "title":project.get("title") or "","topic":project.get("topic") or "",
            "domain":domain,
            "scenes":[scene_plan(s,channel,domain,i+1) for i,s in enumerate(scenes)]}

def _relevant_candidate(plan,metadata):
    """Rank by real stock-provider tags, never by opaque media IDs."""
    descriptor=normalized(metadata or "")
    words=set(tokens(descriptor))
    if not words:
        return -100
    phrase=" ".join(words)
    domain=plan["domain"]
    for bad in plan["reject_tags"]:
        b=normalized(bad)
        if b and (b in words or (" " in b and b in descriptor)):
            if b not in normalized(plan["scene_excerpt"]):
                return -100
    query_words=set()
    for query in plan["queries"][:2]:
        query_words |= set(tokens(query))
    query_words -= STOP
    must=set(tokens(" ".join(plan["must_tags"])))
    qmatches=len(words&query_words)
    cmatches=len(words&must)
    # Generic 'people' cannot rescue a video of a different niche;
    # require >= one concrete tag overlap.
    score=qmatches*3+cmatches*2
    if qmatches==0 and cmatches==0:
        return -100
    return score

def _max_calls(studio):
    with studio.db() as db:
        row=db.execute("""SELECT COUNT(*) FROM studio_semantic_audit
            WHERE date(created_at) = date('now') AND mode='gemini'""").fetchone()
    return int(row[0] or 0)

def schema(studio):
    with studio.db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS studio_semantic_plans(
          project_id TEXT PRIMARY KEY,
          signature TEXT NOT NULL,
          channel_json TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          source TEXT NOT NULL,
          created_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS studio_semantic_audit(
          project_id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          mode TEXT NOT NULL,
          state TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS studio_semantic_asset_audit(
          project_id TEXT NOT NULL,
          scene_number INTEGER NOT NULL,
          source TEXT NOT NULL,
          keyword_score INTEGER NOT NULL,
          search_query TEXT NOT NULL,
          decision TEXT NOT NULL,
          PRIMARY KEY(project_id,scene_number))""")

def _signature(project,channel):
    material={"v":VERSION,"title":project.get("title"),
              "topic":project.get("topic"),"channel":channel,
              "scenes":[[x.get("text"),x.get("query")] for x in project.get("scenes") or []]}
    return hashlib.sha256(json.dumps(material,sort_keys=True,
                        ensure_ascii=False).encode("utf8")).hexdigest()

def _gemini_enabled():
    return bool(os.getenv("GEMINI_API_KEY")) and os.getenv(
        "STUDIO_SEMANTIC_GEMINI_ENABLED","0")=="1"

def _ai_rewrite(studio,project,shotlist):
    if not _gemini_enabled():
        return shotlist,"rules"
    with studio.db() as db:
        db.execute("BEGIN IMMEDIATE")
        if _max_calls(studio)>=min(20,max(1,int(os.getenv(
                 "STUDIO_SEMANTIC_GEMINI_MAX_DAILY","6")))):
            return shotlist,"rules_daily_cap"
        cursor=db.execute("""INSERT OR IGNORE INTO studio_semantic_audit
          (project_id,created_at,mode,state) VALUES(?,?,?,?)""",
          (project["id"],datetime.now(timezone.utc).isoformat(),"gemini","started"))
        if cursor.rowcount!=1:
            return shotlist,"rules_existing_attempt"
    import studio_factory
    context=shotlist["channel"]
    slices=shotlist["scenes"][:30]
    content=[{"index":e["index"],"text":e["scene_excerpt"][:125],
              "writer_visual_query":str((project.get("scenes") or [])[e["index"]-1].get("query") or "")[:75]}
             for e in slices]
    prompt=(
      "Eres un director de B-roll de documentales que trabaja con películas "
      "de stock de Pixabay. El resultado debe ser MATERIALMENTE FIEL a cada "
      "frase del guion, no simplemente del mismo tema general. "
      "Elige en INGLES una acción o objeto visual concreto que un proveedor "
      "pueda buscar (2 a 5 palabras). No inventes lugares ni escenas históricas. "
      "NO uses imágenes genéricas de puentes, ríos, tráfico, oficinas o paisajes "
      "si no aparecen en la narración, aunque existan en otros proyectos. "
      "No marques reconstrucciones o metraje ilustrativo como documental real. "
      "Cada respuesta debe corresponder a su índice. Contexto: "+
      json.dumps({"channel":context,"title":shotlist["title"],
                  "topic":shotlist["topic"],"domain":shotlist["domain"],
                  "scenes":content},ensure_ascii=False)+
      '. Devuelve SOLO JSON {"shots":[{"index":1,"query":"person thinking",'
      '"alternative":"woman reflecting","visual_intent":"reflective pause"}]}. '
      "Cobertura: una fila por escena. Originalidad y coherencia sobre cantidad."
    )
    try:
        answer=studio_factory.gemini_json(prompt)
        shots=answer.get("shots") or []
        modified=0
        if not isinstance(shots,list): raise ValueError("Bad AI storyboard")
        for item in shots:
            if not isinstance(item,dict):continue
            n=item.get("index")
            if not isinstance(n,int) or not 1<=n<=len(slices):continue
            plan=shotlist["scenes"][n-1]
            speech=str((project.get("scenes") or [])[n-1].get("text") or "")
            for field in ("query","alternative"):
                q=_clean_query(item.get(field,""))
                if not q or _semantic_conflict(q,plan["domain"],speech):
                    continue
                # AI suggestions are search hypotheses, never proof of footage.
                plan["queries"]=[q]+[z for z in plan["queries"] if z!=q]
                plan["queries"]=plan["queries"][:4]
                plan["evidence"]="gemini_query_with_topic_guard"
                modified+=1
        with studio.db() as db:
            db.execute("UPDATE studio_semantic_audit SET state=? WHERE project_id=?",
                       ("validated",project["id"]))
        return shotlist,"gemini" if modified else "rules_ai_no_valid_queries"
    except Exception as exc:
        with studio.db() as db:
            db.execute("UPDATE studio_semantic_audit SET state=? WHERE project_id=?",
                       ("fallback_"+type(exc).__name__,project["id"]))
        return shotlist,"rules_ai_fallback"

def load_plan(studio,project,allow_ai=False,write=False):
    ctx=channel_context(studio,project)
    sig=_signature(project,ctx)
    with studio.db() as db:
        row=db.execute("SELECT signature,payload_json,source FROM studio_semantic_plans WHERE project_id=?",
                       (project["id"],)).fetchone()
    if row and row["signature"]==sig:
        try:return json.loads(row["payload_json"]),row["source"]
        except (ValueError,TypeError,KeyError):pass
    result=storyboard(project,ctx)
    source="rules"
    if allow_ai:
        result,source=_ai_rewrite(studio,project,result)
    if write:
        with studio.db() as db:
            db.execute("""INSERT OR REPLACE INTO studio_semantic_plans
                (project_id,signature,channel_json,payload_json,source,created_at)
                VALUES(?,?,?,?,?,?)""",
                (project["id"],sig,json.dumps(ctx,ensure_ascii=False),
                 json.dumps(result,ensure_ascii=False),source,
                 datetime.now(timezone.utc).isoformat()))
    return result,source

def animated_fallback(studio,project,pid,index,shot):
    """Make an original scene-specific card count as moving visual coverage.

    Reuses an existing animation, targets 540p and never claims to show
    historical eyewitness footage. Protects disk on the small preview worker.
    """
    import shutil
    import subprocess
    still=fallback_graphic(studio,project,pid,index,shot)
    dest=still.with_suffix(".mp4")
    if dest.is_file() and dest.stat().st_size>4000:
        return dest
    # If the worker has little disk, fail closed; don't grow temporary files.
    if shutil.disk_usage(studio.DATA).free < 105*1024*1024:
        raise RuntimeError("Insufficient disk space for original moving card")
    temp=dest.with_name(dest.stem+".part.mp4")
    # The rendering pipeline may loop this 9-second clip to scene duration.
    args=[studio.FFMPEG,"-hide_banner","-loglevel","error","-nostdin","-y",
          "-loop","1","-framerate","24","-i",str(still),"-t","12",
          "-vf","zoompan=z='min(zoom+0.00035,1.08)':x='iw/2-(iw/zoom/2)':"
                 "y='ih/2-(ih/zoom/2)':d=1:s=960x540:fps=24,format=yuv420p",
          "-an","-c:v","libx264","-preset","ultrafast","-crf","33",
          "-pix_fmt","yuv420p","-movflags","+faststart",str(temp)]
    try:
        complete=subprocess.run(args,timeout=90,capture_output=True,check=False)
        if complete.returncode or not temp.is_file() or temp.stat().st_size<4000:
            raise RuntimeError("Original card motion rendering failed")
        temp.replace(dest)
        return dest
    finally:
        temp.unlink(missing_ok=True)

def pick_media(studio,scene,shot,client,key,cache,used):
    checks=0
    for query in shot["queries"][:3]:
        if query not in cache:
            if len(cache)>=SEARCH_LIMIT:break
            try:
                response=client.get("https://pixabay.com/api/videos/",
                     params={"key":key,"q":query,"lang":"en",
                             "per_page":25,"safesearch":"true"},timeout=13)
                response.raise_for_status()
                cache[query]=response.json().get("hits") or []
            except (httpx.HTTPError,ValueError):
                cache[query]=[]
        results=sorted(cache[query],key=lambda hit:
            _relevant_candidate(shot,hit.get("tags") or ""),reverse=True)
        for hit in results:
            score=_relevant_candidate(shot,hit.get("tags") or "")
            checks+=1
            if score<MIN_SCORE:continue
            page=str(hit.get("pageURL") or "")
            if not page.startswith("https://pixabay.com/"):continue
            for size in ("small","tiny","medium"):
                media=(hit.get("videos") or {}).get(size) or {}
                url=media.get("url") or ""
                size_bytes=int(media.get("size") or 0)
                if (not url or url in used or not studio._approved_url(url,"pixabay")
                     or (size_bytes and size_bytes>MAX_VIDEO_BYTES)):
                    continue
                return {"url":url,"page":page,"tags":str(hit.get("tags") or ""),
                        "author":str(hit.get("user") or "Pixabay"),
                        "score":score,"query":query}
    return None

def fallback_graphic(studio,project,pid,idx,shot):
    """A scene-specific diagram/card; no invented stock relevance."""
    from PIL import Image,ImageDraw,ImageFont
    root=studio.project_dir(pid)/"uploads"
    root.mkdir(parents=True,exist_ok=True)
    dest=root/f"universal_storyboard_{idx:03d}.png"
    if dest.is_file() and dest.stat().st_size>1000:return dest
    w,h=960,540
    image=Image.new("RGB",(w,h),"#091522")
    draw=ImageDraw.Draw(image)
    draw.rectangle((0,0,w,12),fill="#c9a66d")
    draw.line((50,370,910,370),fill="#3c5769",width=2)
    try:
        font_title=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",30)
        font_body=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",20)
    except OSError:
        font_title=font_body=ImageFont.load_default()
    channel=shot.get("channel_name") or "ESTÚDIO IA"
    raw_title=re.sub(r"\s+"," ",str(project.get("title") or ""))[:65]
    raw_scene=re.sub(r"\s+"," ",shot.get("editorial_label") or "")[:120]
    def wrap(value,maxchars=38):
        lines=[];words=value.split();row=""
        for word in words:
            if len(row+" "+word)>maxchars:
                if row:lines.append(row)
                row=word
            else:row=(row+" "+word).strip()
        if row:lines.append(row)
        return lines[:3]
    draw.text((50,55),channel[:45],font=font_body,fill="#b8b5a5")
    for i,line in enumerate(wrap(raw_title,36)):
        draw.text((50,135+i*46),line,font=font_title,fill="#f4f0e9")
    for i,line in enumerate(wrap(raw_scene,65)):
        draw.text((50,398+i*30),line,font=font_body,fill="#c9a66d")
    image.save(dest,optimize=True)
    return dest

def select_for_project(studio,pid,limit):
    project=studio.get_project(pid)
    if project.get("status")=="concluído":
        return {"status":"preserved_completed"}
    plan,source=load_plan(studio,project,allow_ai=True,write=True)
    scenes=project.get("scenes") or []
    selected=0;retained=0;graphics=0;rejected=0
    used={s.get("media_url") for s in scenes if s.get("media_url")}
    cache={}
    key=os.getenv("PIXABAY_API_KEY")
    with httpx.Client(timeout=15,follow_redirects=False) as client:
        for idx,scene in enumerate(scenes[:min(max(limit,24),SCENE_LIMIT)]):
            shot=plan["scenes"][idx]
            previous=scene.get("media_url") or ""
            # Verify existing archival/local authorized media separately; never
            # overwrite a verified asset merely to reach a stock percentage.
            if scene.get("media_provider") in ("authorized_youtube","wikimedia"):
                # Permission alone is not relevance: a known bridge archive
                # must not survive on a space or psychology story.
                archive_description=(scene.get("author") or "")+" "+(scene.get("source_page") or "")
                if not _semantic_conflict(archive_description,plan["domain"],scene.get("text") or ""):
                    retained+=1
                    continue
            # Existing Pixabay clips are not automatically relevant:
            # original selectors may have used a stale fallback.
            source_tags=scene.get("author") or ""
            current_score=_relevant_candidate(shot,source_tags)
            if (scene.get("media_provider") in ("pixabay","pexels")
                and previous and current_score>=MIN_SCORE
                and previous not in (x.get("media_url") for x in scenes[:idx])):
                retained+=1
                continue
            chosen=None
            if key:
                chosen=pick_media(studio,scene,shot,client,key,cache,used)
            with studio.db() as db:
                if chosen:
                    db.execute("""UPDATE scenes SET media_url=?,media_local='',
                        media_kind='video',media_provider='pixabay',
                        source_page=?,author=?,thumb=''
                        WHERE id=? AND project_id=?""",
                        (chosen["url"],chosen["page"],
                         (chosen["author"][:60]+" | Pixabay | "+chosen["tags"][:130]),
                         scene["id"],pid))
                    used.add(chosen["url"]);selected+=1
                    audit=("pixabay",chosen["score"],chosen["query"],"matched")
                else:
                    shot["channel_name"]=plan["channel"].get("name") or "ESTÚDIO IA"
                    try:
                        art=animated_fallback(studio,project,pid,idx,shot)
                    except (OSError,ValueError,RuntimeError):
                        art=fallback_graphic(studio,project,pid,idx,shot)
                    relative=str(art.relative_to(studio.MEDIA))
                    media_kind="video" if art.suffix==".mp4" else "image"
                    db.execute("""UPDATE scenes SET media_url='',media_local=?,
                        media_kind=?,media_provider='original',
                        source_page='',author='Composição original ilustrativa',
                        thumb='' WHERE id=? AND project_id=?""",
                        (relative,media_kind,scene["id"],pid))
                    graphics+=1
                    audit=("original",-1,"","no_relevant_licensed_clip")
                db.execute("""INSERT OR REPLACE INTO studio_semantic_asset_audit
                      (project_id,scene_number,source,keyword_score,search_query,decision)
                      VALUES(?,?,?,?,?,?)""",
                      (pid,idx+1,*audit))
                if previous and not chosen:rejected+=1
    result={"project_id":pid,"scenes_evaluated":min(max(limit,24),len(scenes)),
            "matched_broll":selected,"retained_relevant":retained,
            "original_graphics":graphics,"rejected_stale":rejected,
            "stock_requests":len(cache),"storyboard_source":source}
    print("[semantic-media] "+json.dumps(result,ensure_ascii=False),flush=True)
    return result

def run_tests():
    samples=[
      ({"title":"Poner límites con calma","topic":"serenidad y autocontrol",
        "scenes":[{"text":"Cuando tu jefe alza la voz en una reunión, respira y responde con respeto.",
                   "query":"office colleagues discussion"}]}, "psychology","office"),
      ({"title":"Ponte da Amizade: sua construção","topic":"engenharia da ponte",
        "scenes":[{"text":"Os caminhões cruzam a ponte entre Brasil e Paraguai.",
                   "query":"bridge traffic"}]}, "infrastructure","bridge"),
      ({"title":"Explorando Marte","topic":"astronomia e planetas",
        "scenes":[{"text":"El cohete despega hacia el planeta rojo.",
                   "query":"rocket launch"}]}, "science_space","rocket"),
      ({"title":"A inflação no mercado","topic":"economia e custo de vida",
        "scenes":[{"text":"O preço dos alimentos sobe no supermercado.",
                   "query":"shopping supermarket"}]}, "economy","shopping"),
      ({"title":"Os animais da floresta","topic":"biodiversidade e natureza",
        "scenes":[{"text":"A floresta abriga milhares de espécies.",
                   "query":"forest animals"}]}, "wildlife","forest"),
      ({"title":"O nascimento da inteligência artificial","topic":"tecnologia e algoritmos",
        "scenes":[{"text":"Os programadores criam o primeiro protótipo.",
                   "query":"programmer computer"}]}, "technology","programmer"),
      ({"title":"Cozinhar massas na Itália","topic":"culinária italiana",
        "scenes":[{"text":"O chef prepara os ingredientes na cozinha.",
                   "query":"chef cooking"}]}, "food","chef"),
      ({"title":"História do Império Romano","topic":"civilizações antigas",
        "scenes":[{"text":"O imperador governava uma cidade monumental.",
                   "query":"ancient ruins"}]}, "history","ruins"),
    ]
    for i,(project,expected,needle) in enumerate(samples,1):
        project["id"]=str(i)
        board=storyboard(project,{"name":"Example channel","niche":"","language":"pt"})
        assert board["domain"]==expected,(i,board["domain"],expected)
        assert any(needle in q for q in board["scenes"][0]["queries"]),board
        assert _relevant_candidate(board["scenes"][0],"bridge road traffic")<MIN_SCORE if expected=="psychology" else True
    bridge=storyboard(samples[1][0],{"name":"Engineering","niche":"","language":"pt"})
    assert _relevant_candidate(bridge["scenes"][0],"bridge construction road traffic")>=MIN_SCORE
    psychology=storyboard(samples[0][0],{"name":"Fudoshin","niche":"estoicismo","language":"es"})
    assert _relevant_candidate(psychology["scenes"][0],"road bridge river")<MIN_SCORE
    assert _relevant_candidate(psychology["scenes"][0],"office business people discussion")>=MIN_SCORE
    print("[semantic-media] SELF-TEST PASS 8 niches, script alignment and cross-topic rejection",flush=True)

def install(studio):
    schema(studio)
    old_find=studio._auto_find_media

    def semantic_find(pid,limit):
        response=old_find(pid,limit)
        try:
            select_for_project(studio,pid,limit)
        except Exception as exc:
            print("[semantic-media] safe fallback: "+type(exc).__name__,flush=True)
        return response
    studio._auto_find_media=semantic_find

    @studio.app.get("/api/editorial/semantic/projects/{pid}")
    def inspect(pid:str):
        project=studio.get_project(pid)
        plan,source=load_plan(studio,project,allow_ai=False,write=False)
        with studio.db() as db:
            assets=[dict(x) for x in db.execute("""SELECT scene_number,source,
              keyword_score,search_query,decision FROM studio_semantic_asset_audit
              WHERE project_id=? ORDER BY scene_number LIMIT 120""",(pid,)).fetchall()]
        return {"project_id":pid,"status":project["status"],
                "preserved_completed":project["status"]=="concluído",
                "domain":plan["domain"],"channel":plan["channel"],
                "title":plan["title"],"storyboard_source":source,
                "scene_count":len(plan["scenes"]),"scenes":plan["scenes"],
                "decisions":assets,"gemini_optional":_gemini_enabled(),
                "note":"Inspeção sem busca paga, sem download e sem modificar MP4s."}

    @studio.app.get("/api/editorial/semantic/health")
    def health():
        return {"version":VERSION,"niches":list(THEMES),
                "semantic_filter":True,
                "gemini_enabled":_gemini_enabled(),
                "gemini_daily_cap":min(20,max(1,int(os.getenv(
                    "STUDIO_SEMANTIC_GEMINI_MAX_DAILY","6")))),
                "max_stock_queries_per_project":SEARCH_LIMIT,
                "licensing_policy":"Pixabay autorizado ou gráfico original; sem scraping do YouTube"}

if __name__=="__main__":
    run_tests()
    app=Path("/app/app.py")
    if not app.exists():raise SystemExit("Missing app.py")
    marker="\nimport studio_semantic_media as _semantic_media\n"
    code=app.read_text(encoding="utf-8")
    if marker not in code:
        app.write_text(code+marker+
                       '_semantic_media.install(__import__("sys").modules[__name__])\n',
                       encoding="utf-8")
