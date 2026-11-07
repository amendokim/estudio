"""Multi-channel YouTube research, original packaging and release planner.

Real official YouTube Data API only, searching a rolling 96h worldwide window.
View velocity and engagement are estimates, not assertions of 'viral' status.
No competitor video downloads, thumbnail copying, unauthorized publication,
paid model calls, or automatic posting. Per-channel workflows are separate.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import shutil
import textwrap
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, Response
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field

YT_API = "https://www.googleapis.com/youtube/v3"
QUERY_WINDOW_HOURS = 96
MAX_RESULTS_PER_SEARCH = 25
MAX_QUERIES_PER_RESEARCH = 3
MAX_DAILY_SEARCH_CALLS = 45
DAILY_NETWORK_PLAN_CAP = 12
DEFAULT_TIMES = ["18:00"]
INSPIRATION_MODELS = (
    ("context", "O contexto que faltava para entender {subject}"),
    ("consequences", "Como {subject} mudou a história que conhecemos"),
    ("mechanism", "O que realmente acontece por trás de {subject}"),
    ("perspective", "{subject}: a história vista de outro ângulo"),
    ("questions", "As perguntas mais importantes sobre {subject}"),
)
INSPIRATION_MODELS_ES = (
    ("context", "Lo que casi nadie explica sobre {subject}"),
    ("consequences", "Cómo {subject} cambia lo que creemos saber"),
    ("mechanism", "Qué ocurre realmente detrás de {subject}"),
    ("perspective", "{subject}: una perspectiva que merece atención"),
    ("questions", "Las preguntas que debemos hacernos sobre {subject}"),
)
INSPIRATION_MODELS_EN = (
    ("context", "What Most People Miss About {subject}"),
    ("consequences", "How {subject} Changes What We Think We Know"),
    ("mechanism", "What Really Happens Behind {subject}"),
    ("perspective", "{subject}: Another Side of the Story"),
    ("questions", "The Important Questions About {subject}"),
)
ACCENTS = ["#e9a95b", "#69b6c7", "#c8849c", "#a5c46f", "#d4bf8b"]

class ChannelCreate(BaseModel):
    name: str = Field(min_length=3, max_length=90)
    niche: str = Field(min_length=3, max_length=120)
    language: str = Field(default="pt", pattern=r"^(pt|en|es)$")
    timezone: str = Field(default="America/Sao_Paulo", min_length=3, max_length=70)
    daily_quantity: int = Field(default=1, ge=1, le=4)
    hours: list[str] = Field(default_factory=lambda: ["18:00"], min_length=1, max_length=8)
    search_terms: list[str] = Field(default_factory=list, max_length=9)
    yt_channel_id: str = Field(default="", max_length=90)

class ChannelUpdate(BaseModel):
    daily_quantity: int = Field(ge=1, le=4)
    hours: list[str] = Field(min_length=1, max_length=8)
    timezone: str = Field(min_length=3, max_length=70)
    enabled: bool = False

class ResearchRequest(BaseModel):
    topic: str = Field(default="", max_length=240)
    create_package: bool = True

class ScheduleRequest(BaseModel):
    plan_id: str = Field(min_length=36, max_length=36)
    date: str = Field(min_length=10, max_length=10)
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    # This step queues a draft schedule, not a YouTube upload.

def now():
    return datetime.now(timezone.utc)

def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def validate_zone(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise HTTPException(422, "Fuso horário IANA inválido")
    return value

def validate_times(hours, amount):
    clean = list(dict.fromkeys(t.strip() for t in hours))
    if (len(clean) < amount or len(clean) > 8 or
        any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", h) for h in clean)):
        raise HTTPException(422, "Defina horários HH:MM diferentes para cada vídeo diário")
    return sorted(clean)

def schema(studio):
    with studio.db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS channel_network_channels (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, niche TEXT NOT NULL,
          language TEXT NOT NULL DEFAULT 'pt', timezone TEXT NOT NULL,
          daily_quantity INTEGER NOT NULL DEFAULT 1, hours_json TEXT NOT NULL,
          search_terms_json TEXT NOT NULL, yt_channel_id TEXT NOT NULL DEFAULT '',
          enabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS channel_network_ideas (
          id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES
            channel_network_channels(id) ON DELETE CASCADE,
          topic TEXT NOT NULL, angle TEXT NOT NULL, title TEXT NOT NULL,
          hook TEXT NOT NULL, analysis_json TEXT NOT NULL,
          thumbnail_local TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'draft',
          scheduled_at TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          UNIQUE(channel_id, title))""")
        c.execute("""CREATE TABLE IF NOT EXISTS channel_network_search_audit (
          id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
          created_at TEXT NOT NULL, query_count INTEGER NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS channel_network_search_cache (
          cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL,
          expires_at TEXT NOT NULL)""")
        columns={r["name"] for r in c.execute("PRAGMA table_info(channel_network_ideas)")}
        if "duration_minutes" not in columns:
            c.execute("ALTER TABLE channel_network_ideas ADD COLUMN duration_minutes INTEGER NOT NULL DEFAULT 5")
        c.execute("CREATE INDEX IF NOT EXISTS idx_channel_network_ideas_sched ON channel_network_ideas(channel_id,scheduled_at)")

def unpack_channel(row):
    c = dict(row)
    c["hours"] = json.loads(c.pop("hours_json"))
    c["search_terms"] = json.loads(c.pop("search_terms_json"))
    c["enabled"] = bool(c["enabled"])
    return c

def get_channel(studio, cid):
    with studio.db() as c:
        row = c.execute("SELECT * FROM channel_network_channels WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "Canal não cadastrado")
    return unpack_channel(row)

def list_channels(studio):
    with studio.db() as c:
        rows = c.execute("SELECT * FROM channel_network_channels ORDER BY name").fetchall()
    return [unpack_channel(x) for x in rows]

def ideas(studio, cid):
    with studio.db() as c:
        rows = c.execute("SELECT * FROM channel_network_ideas WHERE channel_id=? ORDER BY created_at DESC LIMIT 40", (cid,)).fetchall()
    result = []
    for row in rows:
        x = dict(row)
        x["analysis"] = json.loads(x.pop("analysis_json"))
        result.append(x)
    return result

def api_key():
    key = os.getenv("YOUTUBE_DATA_API_KEY", "").strip()
    if not key:
        raise HTTPException(409, "Pesquisa mundial inativa: configure YOUTUBE_DATA_API_KEY no Railway (Google Cloud). Nenhum dado de viralização foi inventado.")
    return key

def _bounded_calls(studio, cid, requested):
    # Enforce rate limits BEFORE any network calls, also across restarts.
    cut = iso(now() - timedelta(days=1))
    with studio.db() as c:
        total = c.execute("SELECT COALESCE(SUM(query_count),0) FROM channel_network_search_audit WHERE created_at>=?", (cut,)).fetchone()[0]
        if total + requested > MAX_DAILY_SEARCH_CALLS:
            raise HTTPException(429, "Limite de pesquisas do Estúdio alcançado hoje; preserve a quota da API.")
        c.execute("INSERT INTO channel_network_search_audit VALUES(?,?,?,?)",
                  (str(uuid.uuid4()), cid, iso(now()), requested))

def _yt_get(client, route, params, key):
    r = client.get(YT_API + route, params={**params, "key": key},
                   headers={"Accept": "application/json"})
    if r.status_code in (401, 403, 429):
        raise HTTPException(503, "API do YouTube indisponível ou cota/permissão insuficiente; verifique o Google Cloud.")
    r.raise_for_status()
    return r.json()

def query_variants(channel, topic):
    # Search global index without regionCode; give priority to configured
    # multilingual terms to make worldwide coverage useful.
    options = [topic]
    extras = channel.get("search_terms", [])
    if extras:
        options += extras
    elif channel.get("niche", "").casefold() != topic.casefold():
        options.append(channel["niche"])
    result = []
    for q in options:
        q = re.sub(r"\s+", " ", str(q)).strip()[:110]
        if len(q) >= 3 and q.casefold() not in {x.casefold() for x in result}:
            result.append(q)
    return result[:MAX_QUERIES_PER_RESEARCH]

def _report_snapshot(studio, channel, topic):
    key = api_key()
    queries = query_variants(channel, topic)
    if not queries:
        raise HTTPException(422, "Informe um tema para pesquisar")
    cachekey = hashlib.sha256(
        json.dumps({"cid": channel["id"], "terms": queries, "window": 96},
                   sort_keys=True).encode()).hexdigest()
    with studio.db() as db:
        cache = db.execute("SELECT payload, expires_at FROM channel_network_search_cache WHERE cache_key=?", (cachekey,)).fetchone()
    if cache and cache["expires_at"] > iso(now()):
        return json.loads(cache["payload"])
    _bounded_calls(studio, channel["id"], len(queries))
    cutoff = now() - timedelta(hours=QUERY_WINDOW_HOURS)
    discovered = {}
    # Search uses viewCount relevance, plus a fixed publishedAfter filter.
    # Search results are non-exhaustive; never claim globally most watched.
    try:
        with httpx.Client(timeout=18, follow_redirects=False) as client:
            for term in queries:
                data = _yt_get(client, "/search", {
                    "part": "snippet", "type": "video", "q": term,
                    "publishedAfter": iso(cutoff), "maxResults": MAX_RESULTS_PER_SEARCH,
                    "order": "viewCount", "safeSearch": "moderate"
                }, key)
                for item in data.get("items", []):
                    vid = (item.get("id") or {}).get("videoId")
                    if not re.fullmatch(r"[\w-]{11}", vid or ""):
                        continue
                    sn = item.get("snippet") or {}
                    published = sn.get("publishedAt")
                    if not published:
                        continue
                    discovered[vid] = {
                        "id": vid, "title": str(sn.get("title") or "")[:150],
                        "published_at": published, "channel_title":
                            str(sn.get("channelTitle") or "")[:120],
                        "url": "https://www.youtube.com/watch?v=" + vid,
                        "language": sn.get("defaultAudioLanguage") or
                                    sn.get("defaultLanguage") or ""
                    }
            if discovered:
                details = _yt_get(client, "/videos", {
                    "part": "statistics,snippet", "id": ",".join(list(discovered)[:50])
                }, key)
                for video in details.get("items", []):
                    vid = video.get("id")
                    if vid not in discovered:
                        continue
                    stats = video.get("statistics") or {}
                    item = discovered[vid]
                    item["views"] = int(stats.get("viewCount") or 0)
                    item["likes"] = int(stats.get("likeCount") or 0)
                    item["comments"] = int(stats.get("commentCount") or 0)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise HTTPException(503, "Falha na pesquisa oficial do YouTube: " + type(exc).__name__)
    ranked = []
    for item in discovered.values():
        try:
            created = datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
        except ValueError:
            continue
        age = (now() - created).total_seconds() / 3600
        if age < 0 or age > QUERY_WINDOW_HOURS + 1:
            continue
        views = int(item.get("views") or 0)
        if not views:
            continue
        velocity = round(views / max(1.0, age), 1)
        rate = round(100 * (int(item.get("likes") or 0) +
                      2 * int(item.get("comments") or 0)) / max(1, views), 2)
        # Heuristic, NOT historical growth: current views divided by video age.
        item["age_hours"] = round(age, 1)
        item["views_per_hour_estimate"] = velocity
        item["engagement_proxy_percent"] = rate
        item["trend_score"] = round(math.log1p(velocity) * 10 +
                                     min(10, rate) * 2, 2)
        ranked.append(item)
    ranked.sort(key=lambda x: x["trend_score"], reverse=True)
    result = {"topic": topic, "period_hours": 96,
              "queried_at": iso(now()), "queries": queries,
              "method": "YouTube Data API; views/h and interaction proxy",
              "worldwide_note": "Busca internacional sem região obrigatória. Não é um ranking oficial mundial nem garante viralização.",
              "results": ranked[:25]}
    with studio.db() as db:
        db.execute("INSERT OR REPLACE INTO channel_network_search_cache VALUES (?,?,?)",
                   (cachekey, json.dumps(result, ensure_ascii=False),
                    iso(now()+timedelta(hours=2))))
    return result

def original_concept(studio, channel, subject, snapshot):
    if not snapshot["results"]:
        raise HTTPException(422, "Nenhum vídeo recente com métricas públicas nessa pesquisa. Tente outros termos.")
    with studio.db() as c:
        used = {r[0] for r in c.execute("SELECT title FROM channel_network_ideas WHERE channel_id=?", (channel["id"],))}
    top = snapshot["results"][0]
    # Build a distinct framing, not a title rewrite or copied story.
    subject = re.sub(r"\s+", " ", subject).strip(" .")[:95]
    templates = (INSPIRATION_MODELS_ES if channel.get("language")=="es" else
                 INSPIRATION_MODELS_EN if channel.get("language")=="en" else INSPIRATION_MODELS)
    for angle, template in templates:
        title = template.format(subject=subject)
        if title not in used:
            break
    else:
        raise HTTPException(409, "Já existem cinco ângulos para esse tema. Insira um assunto novo para evitar conteúdo repetitivo.")
    if title.casefold() in {x["title"].casefold() for x in snapshot["results"]}:
        raise HTTPException(409, "O título está excessivamente próximo da fonte pesquisada.")
    return {
        "angle": angle, "title": title[:110],
        "hook": (
            f"¿Qué no estamos viendo sobre {subject}? Explorar causas, límites y pruebas verificables."
            if channel.get("language")=="es" else
            f"What are we missing about {subject}? Examine causes and checkable evidence."
            if channel.get("language")=="en" else
            f"Por que {subject} importa agora? Separar contexto, consequências e evidências verificáveis."
        ),
        "source_urls": [x["url"] for x in snapshot["results"][:5]],
        "inspiration": {
            "reference_title": top["title"], "reference_url": top["url"],
            "score": top["trend_score"], "trend_window_hours": 96,
            "used_for": "Estudo de desempenho e estrutura, não reprodução de imagens, roteiro ou thumbnail."
        }
    }

def thumbnail(studio, channel, idea_id, subject, angle):
    # Entirely original, no competitor thumbnail image copied.
    target = Path(studio.DATA) / "channel_network_thumbs"
    target.mkdir(parents=True, exist_ok=True)
    file = target / (idea_id + ".jpg")
    w,h = 1280,720
    image = Image.new("RGB",(w,h),"#0b1424")
    d = ImageDraw.Draw(image)
    accent = ACCENTS[int(hashlib.sha256(channel["id"].encode()).hexdigest()[:4],16)%len(ACCENTS)]
    for i in range(0,w,12):
        t=i/w
        col=(int(7+15*t),int(18+18*t),int(36+29*t))
        d.rectangle((i,0,i+12,h),fill=col)
    d.ellipse((820,-160,1580,580), outline=accent,width=30)
    d.ellipse((890,-90,1480,490), outline="#32485d",width=5)
    d.polygon([(710,520),(1140,215),(1280,260),(1280,720),(790,720)],fill="#172b3d")
    try:
        bold="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        body="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        f_big=ImageFont.truetype(bold,74)
        f_small=ImageFont.truetype(bold,23)
        f_sub=ImageFont.truetype(body,24)
    except OSError:
        f_big=ImageFont.load_default();f_small=f_big;f_sub=f_big
    subject = re.sub(r"\s+", " ", subject)[:90].upper()
    # Prefer topic words over clickbait invented facts.
    limit=18
    lines=textwrap.wrap(subject,width=limit,break_long_words=False)[:4]
    d.rounded_rectangle((54,54,250,101),radius=12,fill=accent)
    d.text((74,65),{"es":"HISTORIA","en":"STORY","pt":"DOCUMENTÁRIO"}.get(channel.get("language"),"VÍDEO"),
           font=f_small,fill="#081522")
    for i,line in enumerate(lines):
        d.text((58,155+i*103),line,font=f_big,fill="#ffffff",stroke_width=3,stroke_fill="#10151b")
    d.rectangle((56,620,700,629),fill=accent)
    d.text((58,650),channel["name"].upper()[:35],font=f_sub,fill="#c4d3e2")
    image.save(file, "JPEG", quality=88, optimize=True)
    return str(file.relative_to(studio.DATA))

def create_idea(studio, channel, topic, snapshot):
    angle = original_concept(studio, channel, topic, snapshot)
    idea_id = str(uuid.uuid4())
    poster = thumbnail(studio,channel,idea_id,topic,angle["angle"])
    with studio.db() as c:
        c.execute("""INSERT INTO channel_network_ideas
        (id,channel_id,topic,angle,title,hook,analysis_json,thumbnail_local,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (idea_id,channel["id"],topic,angle["angle"],angle["title"],
         angle["hook"],json.dumps(angle,ensure_ascii=False),poster,iso(now())))
    return {"id":idea_id,"title":angle["title"],"angle":angle["angle"],
            "thumbnail_url":"/api/network/thumbnails/"+idea_id,
            "status":"draft", "research":angle["inspiration"]}

def plan_schedule(studio, channel, idea_id, date, hour):
    # Publishing credentials are not available. Save intent safely.
    t = datetime.strptime(date+" "+hour,"%Y-%m-%d %H:%M")
    zone=ZoneInfo(channel["timezone"])
    desired=t.replace(tzinfo=zone)
    utc=desired.astimezone(timezone.utc)
    if utc <= now()+timedelta(minutes=30):
        raise HTTPException(422, "Agende pelo menos 30 minutos no futuro")
    if desired.astimezone(zone).strftime("%Y-%m-%d %H:%M") != t.strftime("%Y-%m-%d %H:%M"):
        raise HTTPException(422, "Horário inexistente devido à mudança de fuso")
    with studio.db() as db:
        idea = db.execute("SELECT id FROM channel_network_ideas WHERE id=? AND channel_id=?",(idea_id,channel["id"])).fetchone()
        if not idea:
            raise HTTPException(404,"Ideia não pertence ao canal")
        count=db.execute("SELECT COUNT(*) FROM channel_network_ideas WHERE channel_id=? AND scheduled_at>=? AND scheduled_at<?",
                         (channel["id"], iso(datetime.combine(t.date(),datetime.min.time(),zone).astimezone(timezone.utc)),
                          iso((datetime.combine(t.date(),datetime.min.time(),zone)+timedelta(days=1)).astimezone(timezone.utc)))).fetchone()[0]
        if count>=channel["daily_quantity"]:
            raise HTTPException(409,"Quantidade diária já preenchida para este canal")
        collision=db.execute("SELECT id FROM channel_network_ideas WHERE channel_id=? AND scheduled_at=? AND id!=?",
                             (channel["id"],iso(utc),idea_id)).fetchone()
        if collision:
            raise HTTPException(409,"Já existe vídeo planejado para este horário")
        db.execute("UPDATE channel_network_ideas SET scheduled_at=?, status='scheduled_draft' WHERE id=?",
                   (iso(utc),idea_id))
    return {"id":idea_id,"scheduled_at_utc":iso(utc),"local_time":desired.isoformat(),
            "status":"scheduled_draft","published":False,
            "note":"Horário guardado; upload e publicação exigem conexão OAuth e motor de produção habilitado."}

def install(studio):
    schema(studio)

    @studio.app.get("/api/network/status")
    def status():
        with studio.db() as c:
            n=c.execute("SELECT COUNT(*) FROM channel_network_channels").fetchone()[0]
            drafts=c.execute("SELECT COUNT(*) FROM channel_network_ideas").fetchone()[0]
        return {"channels_registered":n,"ideas":drafts,
                "research_ready":bool(os.getenv("YOUTUBE_DATA_API_KEY")),
                "oauth_ready":bool(os.getenv("GOOGLE_YT_CLIENT_ID") and os.getenv("GOOGLE_YT_CLIENT_SECRET")),
                "publication_enabled":False,
                "full_script_ready":bool(os.getenv("GEMINI_API_KEY")),
                "window_hours":96,"max_research_calls_daily":MAX_DAILY_SEARCH_CALLS,
                "note":"Pesquisa oficial exige uma chave Google. Planejamento não significa postagem no YouTube."}

    @studio.app.get("/api/network/channels")
    def channels():
        return {"channels":list_channels(studio)}

    @studio.app.post("/api/network/channels")
    def create(payload:ChannelCreate):
        validate_zone(payload.timezone)
        hrs=validate_times(payload.hours,payload.daily_quantity)
        cid=str(uuid.uuid4())
        with studio.db() as db:
            db.execute("""INSERT INTO channel_network_channels
              (id,name,niche,language,timezone,daily_quantity,hours_json,search_terms_json,
               yt_channel_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               (cid,payload.name.strip(),payload.niche.strip(),payload.language,payload.timezone,
                payload.daily_quantity,json.dumps(hrs),json.dumps([x[:110] for x in payload.search_terms]),
                payload.yt_channel_id,iso(now()),iso(now())))
        return get_channel(studio,cid)

    @studio.app.put("/api/network/channels/{cid}")
    def update(cid:str,payload:ChannelUpdate):
        get_channel(studio,cid)
        validate_zone(payload.timezone)
        hrs=validate_times(payload.hours,payload.daily_quantity)
        with studio.db() as db:
            db.execute("""UPDATE channel_network_channels
              SET timezone=?,daily_quantity=?,hours_json=?,enabled=?,updated_at=? WHERE id=?""",
             (payload.timezone,payload.daily_quantity,json.dumps(hrs),int(payload.enabled),iso(now()),cid))
        return get_channel(studio,cid)

    @studio.app.get("/api/network/channels/{cid}/ideas")
    def list_ideas(cid:str):
        get_channel(studio,cid)
        return {"ideas":ideas(studio,cid)}

    @studio.app.post("/api/network/channels/{cid}/research")
    def research(cid:str,payload:ResearchRequest):
        channel=get_channel(studio,cid)
        topic=payload.topic.strip() or channel["niche"]
        snapshot=_report_snapshot(studio,channel,topic)
        result={"channel_id":cid,"research":snapshot}
        if payload.create_package:
            result["idea"]=create_idea(studio,channel,topic,snapshot)
        return result

    @studio.app.get("/api/network/thumbnails/{idea_id}")
    def preview_thumbnail(idea_id:str):
        with studio.db() as db:
            row=db.execute("SELECT thumbnail_local FROM channel_network_ideas WHERE id=?",(idea_id,)).fetchone()
        if not row:
            raise HTTPException(404,"Thumbnail não encontrada")
        root=(Path(studio.DATA)/"channel_network_thumbs").resolve()
        p=(Path(studio.DATA)/row["thumbnail_local"]).resolve()
        if not p.is_relative_to(root) or not p.is_file():
            raise HTTPException(404,"Thumbnail indisponível")
        return Response(p.read_bytes(),media_type="image/jpeg",headers={"Cache-Control":"private, max-age=300"})

    @studio.app.post("/api/network/channels/{cid}/schedule")
    def schedule(cid:str,payload:ScheduleRequest):
        return plan_schedule(studio,get_channel(studio,cid),payload.plan_id,payload.date,payload.time)

if __name__=="__main__":
    assert validate_times(["18:00","12:00"],2)==["12:00","18:00"]
    assert len(query_variants({"niche":"documentários","search_terms":["History in English","Historia en español"]},"História"))==3
    assert len(INSPIRATION_MODELS)==5
    assert "Lo que" in INSPIRATION_MODELS_ES[0][1]
    appfile=Path("/app/app.py")
    if not appfile.is_file():
        raise SystemExit("Missing app.py")
    mark="\nimport studio_channel_network as _channel_network\n"
    data=appfile.read_text(encoding="utf-8")
    if mark not in data:
        appfile.write_text(data+mark+
                           '_channel_network.install(__import__("sys").modules[__name__])\n',
                           encoding="utf-8")
