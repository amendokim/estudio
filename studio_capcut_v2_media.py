"""CapCut V2 bounded media enrichment. Explicit owner action only; never runs at startup.

Sources with documented APIs: Pexels video/photo, Pixabay video/photo,
Wikimedia Commons (public domain / CC). Optional Gemini image generation:
max two clearly labeled conceptual illustrations, never factual evidence.

No YouTube downloading or unofficial stock scraping; source/credits retained.
This prepares assets only. Rendering and draft import happen on user's PC.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

VERSION = "capcut-media-fusion-v2"
MAX_SCENES = 12
MAX_IMAGES = 2
MAX_ASSETS_BYTES = 80 * 1024 * 1024
MAX_ITEM_BYTES = 11 * 1024 * 1024
TIMEOUT = 16
LOCK = threading.Lock()
RUNNING = {}
RESULTS = {}

class PrepareRequest(BaseModel):
    max_scenes: int = Field(default=8, ge=1, le=MAX_SCENES)
    ai_images: int = Field(default=0, ge=0, le=MAX_IMAGES)
    ai_direction: bool = False

def stamp():
    return datetime.now(timezone.utc).isoformat()

def folder(studio, pid):
    root = Path(studio.project_dir(pid)).resolve()
    storage = Path(studio.MEDIA).resolve()
    if not root.is_relative_to(storage):
        raise ValueError("Projeto fora do volume")
    return root / "capcut_v2"

def _clean(text):
    return " ".join(str(text or "").split())[:160]

def _norm(text):
    s = unicodedata.normalize("NFKD", str(text or "").lower())
    return re.sub(r"[^a-z0-9 ]+", " ", "".join(x for x in s if not unicodedata.combining(x)))

def _tags(text):
    stop = {"video", "imagem", "foto", "para", "pelo", "pela", "com", "mais", "this",
            "that", "from", "picture", "stock", "footage", "visual", "image", "scene",
            "shots", "people", "the", "and", "una", "uma", "das", "dos", "que",
            "portugal", "documentario", "narrador"}
    names={"paraguai":"paraguay","brasil":"brazil","economia":"economy",
           "fronteira":"border","crise":"crisis","comercio":"commerce",
           "imigracao":"migration","familia":"family","politica":"politics",
           "lojas":"shops","cidade":"city","estrada":"road","pessoas":"people"}
    return {names.get(t,t) for t in _norm(text).split()
            if len(t) >= 4 and t not in stop}

def _term(scene, subject):
    query = str(scene.get("query") or scene.get("search_query") or "").strip()
    if not query:
        query = str(scene.get("text") or "").strip()[:100]
    tokens = _norm(query).split()
    tokens = [x for x in tokens if x not in {"site", "youtube", "pixabay", "pexels"}]
    if len(tokens) > 7:
        tokens = tokens[:7]
    replacements={"paraguai":"paraguay","brasil":"brazil","economia":"economy",
                  "fronteira":"border","crise":"crisis","comercio":"commerce",
                  "imigracao":"migration","familia":"family"}
    return " ".join(replacements.get(t,t) for t in tokens)[:90] or subject[:70]

def _semantic(query, title, scene_text=""):
    q = _tags(query)
    t = _tags(title)
    overlap = q & t
    # Factual geographic claims must not be illustrated as a different place.
    for variants in ({"paraguai", "paraguay"}, {"brasil", "brazil"},
                     {"argentina"}, {"bolivia"}, {"ciudad", "este"},
                     {"iguaçu", "iguacu", "iguazu"}):
        if variants & (_tags(scene_text) | q) and not variants & t:
            return 0
    return min(10, 3 * len(overlap))

def _approved(url):
    try:
        u = urlsplit(url)
        host = (u.hostname or "").lower()
        return (u.scheme == "https" and u.port in (None, 443) and
            not u.username and not u.password and host in {
                "videos.pexels.com", "images.pexels.com", "cdn.pixabay.com",
                "upload.wikimedia.org", "images.pixabay.com"})
    except ValueError:
        return False

def _request(client, url, **kw):
    r = client.get(url, timeout=TIMEOUT, follow_redirects=False, **kw)
    r.raise_for_status()
    return r.json()

def _pexels(client, query, api_key):
    if not api_key:
        return []
    result = []
    head = {"Authorization": api_key}
    for media, endpoint in (("video", "https://api.pexels.com/v1/videos/search"),
                            ("image", "https://api.pexels.com/v1/search")):
        try:
            data = _request(client, endpoint, headers=head,
                            params={"query": query, "per_page": 8, "orientation":"landscape"})
            items = data.get("videos", []) if media == "video" else data.get("photos", [])
            for it in items:
                if media == "video":
                    options = sorted((v for v in it.get("video_files", [])
                        if v.get("file_type") == "video/mp4" and
                        (v.get("width") or 0) >= 960 and (v.get("height") or 0) >= 540),
                        key=lambda v: abs((v.get("width") or 0)-1280))
                    if not options:
                        continue
                    url = options[0].get("link")
                    title = it.get("url", "").replace("-", " ")
                    seconds = it.get("duration", 0)
                else:
                    url = (it.get("src") or {}).get("large2x") or (it.get("src") or {}).get("large")
                    title = it.get("alt") or it.get("url", "").replace("-", " ")
                    seconds = 0
                if _approved(url):
                    result.append(dict(source="pexels", type=media, url=url,
                      page=it.get("url"), title=_clean(title), author=str(
                      (it.get("user") or {}).get("name") if media == "video" else it.get("photographer") or "Pexels"),
                      license="Pexels License", seconds=seconds))
        except (httpx.HTTPError, ValueError, KeyError) as err:
            print("[capcut-v2] Pexels search unavailable: " + type(err).__name__, flush=True)
    return result

def _pixabay(client, query, api_key):
    if not api_key:
        return []
    found = []
    for media, endpoint in (("video","https://pixabay.com/api/videos/"),
                           ("image","https://pixabay.com/api/")):
        try:
            data = _request(client, endpoint, params={"key":api_key,"q":query[:100],
                "per_page":10, "safesearch":"true"})
            for hit in data.get("hits", []):
                if media == "video":
                    options = hit.get("videos") or {}
                    spec = options.get("medium") or options.get("small") or {}
                    url = spec.get("url")
                    if (spec.get("width") or 0)<960:
                        continue
                else:
                    url = hit.get("largeImageURL") or hit.get("webformatURL")
                if _approved(url):
                    found.append(dict(source="pixabay", type=media, url=url,
                        page=hit.get("pageURL"), title=_clean(hit.get("tags")),
                        author=_clean(hit.get("user")), license="Pixabay Content License",
                        seconds=hit.get("duration") or 0))
        except (httpx.HTTPError, ValueError, KeyError) as err:
            print("[capcut-v2] Pixabay search unavailable: " + type(err).__name__, flush=True)
    return found

def _commons(client, query):
    result = []
    try:
        data = _request(client, "https://commons.wikimedia.org/w/api.php",params={
            "action":"query", "generator":"search", "gsrsearch":query,
            "gsrnamespace":6, "gsrlimit":10, "prop":"imageinfo",
            "iiprop":"url|mime|extmetadata", "iiurlwidth":1600, "format":"json"})
        pages = (data.get("query") or {}).get("pages") or {}
        for p in pages.values():
            info = (p.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata") or {}
            license_name = html.unescape((meta.get("LicenseShortName") or {}).get("value") or "")
            license_url = (meta.get("LicenseUrl") or {}).get("value") or ""
            allowed = (license_name.upper().startswith(("CC BY", "CC0")) or
                       "PUBLIC DOMAIN" in license_name.upper())
            if not allowed or "NC" in license_name.upper() or "ND" in license_name.upper():
                continue
            kind = "image" if str(info.get("mime") or "").startswith("image/") else ""
            url = info.get("thumburl") or info.get("url")
            title = str(p.get("title") or "").removeprefix("File:")
            if kind and _approved(url):
                result.append(dict(source="wikimedia", type=kind, url=url,
                     page=info.get("descriptionurl"), title=_clean(title),
                     author=html.unescape(re.sub("<[^>]*>", " ", (meta.get("Artist") or {}).get("value") or "Wikimedia")),
                     license=license_name, license_url=license_url))
    except (httpx.HTTPError, ValueError, KeyError) as err:
        print("[capcut-v2] Commons search unavailable: " + type(err).__name__, flush=True)
    return result

def _file_ext(asset):
    if asset["type"] == "video":
        return ".mp4"
    path=urlsplit(asset.get("url") or "").path.lower()
    if path.endswith(".png"):
        return ".png"
    if path.endswith(".webp"):
        return ".webp"
    return ".jpg"

def _download(client, asset, path, budget):
    if not _approved(asset["url"]):
        raise ValueError("URL fora dos domínios permitidos")
    remaining = min(MAX_ITEM_BYTES, MAX_ASSETS_BYTES - budget["bytes"])
    if remaining < 10000:
        raise RuntimeError("Teto de download atingido")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".download")
    try:
        with client.stream("GET", asset["url"], follow_redirects=False, timeout=30) as res:
            res.raise_for_status()
            if not (200 <= res.status_code < 300):
                raise RuntimeError("HTTP inesperado")
            downloaded = 0
            with temp.open("wb") as output:
                for part in res.iter_bytes(65536):
                    downloaded += len(part)
                    if downloaded > remaining:
                        raise RuntimeError("Arquivo excede limite")
                    output.write(part)
        head = temp.read_bytes()[:40]
        if asset["type"] == "image":
            if not (head.startswith(bytes((255,216))) or
                    head.startswith(bytes((137,80,78,71))) or
                    head.startswith(b"RIFF") and b"WEBP" in head):
                raise RuntimeError("Imagem inválida")
        else:
            if b"ftyp" not in head[:32]:
                raise RuntimeError("Vídeo não MP4")
        if downloaded < 10000:
            raise RuntimeError("Arquivo muito pequeno")
        temp.replace(path)
        budget["bytes"] += downloaded
        return downloaded
    finally:
        temp.unlink(missing_ok=True)

def _ai_illustration(client, scene, destination):
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY ausente")
    prompt = ("Create ONE cinematic, photorealistic editorial CONCEPT ART image. "
      "No visible letters, text, logos or watermarks. Wide 16:9 composition. "
      "This is an illustrative visualization, NEVER a photograph of a real event "
      "and NEVER evidence. Do not depict identifiable politicians or public figures. "
      "Concept for this narration: " + _clean(scene.get("text")) + ". "
      "Visual reference: " + _clean(scene.get("query")) + ".")
    model = "gemini-3.1-flash-image"
    res = client.post("https://generativelanguage.googleapis.com/v1beta/models/" +
        model + ":generateContent", headers={"x-goog-api-key":key},
        json={"contents":[{"role":"user","parts":[{"text":prompt}]}],
              "generationConfig":{"responseModalities":["IMAGE","TEXT"],
                                  "imageConfig":{"aspectRatio":"16:9"}}},
        timeout=65)
    res.raise_for_status()
    data = res.json()
    for candidate in data.get("candidates", []):
        for part in (candidate.get("content") or {}).get("parts") or []:
            img = part.get("inlineData") or {}
            if img.get("mimeType") in ("image/png","image/jpeg") and img.get("data"):
                raw = base64.b64decode(img["data"],validate=True)
                if not 10000 < len(raw) < 7*1024*1024:
                    raise RuntimeError("Imagem IA fora do limite")
                ext = ".png" if img["mimeType"] == "image/png" else ".jpg"
                dest = destination.with_suffix(ext)
                dest.write_bytes(raw)
                return dest
    raise RuntimeError("Gemini não retornou imagem")

def _direction(project,scenes):
    """One explicit paid Gemini text call for visual brief, bounded to 12 scenes."""
    if not os.getenv("GEMINI_API_KEY"):
        raise RuntimeError("GEMINI_API_KEY ausente")
    from studio_factory import gemini_json
    raw = []
    for idx,scene in enumerate(scenes,1):
        raw.append({"index":idx, "text":_clean(scene.get("text")),
                    "existing_query":_clean(scene.get("query"))})
    prompt = ("You are a documentary VISUAL DIRECTOR. Return ONLY JSON with key 'scenes': "
      "an array of one entry per input, each with integer index, English image "
      "search_query (3-7 concrete words, including names of places if mentioned), "
      "short factual on_screen label (max 5 words), and a visual_kind from "
      "['video','photo','map','graphic','illustration']. "
      "Only suggest imagery faithful to the EXACT spoken text. Prefer real "
      "footage of named locations; if none likely, use map/graphic. "
      "Never invent fake visual evidence, fabricated protests or public figures. "
      "Keep text factual, use no generic unrelated business footage. "
      "Video title: "+_clean(project.get("title"))+"\nScenes: "+
      json.dumps(raw,ensure_ascii=False))
    response=gemini_json(prompt)
    result={}
    entries=response.get("scenes") or []
    if not isinstance(entries,list):
        raise RuntimeError("Director returned invalid scenes")
    for item in entries:
        if not isinstance(item,dict):
            continue
        idx=item.get("index")
        query=_clean(item.get("search_query"))
        kind=str(item.get("visual_kind") or "")
        label=_clean(item.get("on_screen"))[:65]
        if (isinstance(idx,int) and 1<=idx<=len(scenes) and len(query)>4
                and len(query)<100 and kind in
                ("video","photo","map","graphic","illustration")):
            # Named geographic entities must be present in the search query.
            originals=_tags(scenes[idx-1].get("text") or "")
            targets=_tags(query)
            named=originals & {"paraguay","brazil","argentina","bolivia","ciudad"}
            if named and not named <= targets:
                continue
            result[idx]={"query":query,"kind":kind,"label":label}
    return result

def _prepare(studio, pid, request):
    path = folder(studio, pid)
    project = studio.get_project(pid)
    scenes = (project.get("scenes") or [])[:request.max_scenes]
    if not scenes:
        raise RuntimeError("Projeto sem cenas")
    path.mkdir(parents=True, exist_ok=True)
    prepared = []
    budget = {"bytes": 0}
    used = set()
    ai_left = request.ai_images
    direction={}
    if request.ai_direction:
        try:
            direction=_direction(project,scenes)
        except (RuntimeError,ValueError,KeyError,httpx.HTTPError) as err:
            print("[capcut-v2] AI direction unavailable: "+type(err).__name__,flush=True)
    with httpx.Client(headers={"User-Agent":"MeuEstudioIA/2.0 (CapCut media credits)"}) as client:
        for idx, scene in enumerate(scenes,1):
            brief=direction.get(idx) or {}
            query = brief.get("query") or _term(scene, str(project.get("title") or ""))
            state = dict(scene=idx, query=query, visual_kind=brief.get("kind",""),
                         on_screen=brief.get("label",""),
                         status="missing", type="",
                         source="", file="", title="", page="", license="", author="",
                         illustration=False)
            pool = (_pexels(client,query,os.getenv("PEXELS_API_KEY","")) +
                    _pixabay(client,query,os.getenv("PIXABAY_API_KEY","")) +
                    _commons(client,query))
            ranked = []
            for asset in pool:
                if asset["url"] in used:
                    continue
                relevance = _semantic(query, asset["title"], scene.get("text") or "")
                # Require grounded correspondence. No arbitrary stock fallback.
                if relevance >= 3:
                    ranked.append((relevance + (2 if asset["type"]=="video" else 0), asset))
            ranked.sort(key=lambda x:-x[0])
            for relevance, asset in ranked[:5]:
                dest = path / ("scene_%03d_%s%s" % (idx,asset["source"],_file_ext(asset)))
                try:
                    if not dest.exists():
                        _download(client,asset,dest,budget)
                    used.add(asset["url"])
                    state.update(status="ready",type=asset["type"],source=asset["source"],
                        file=dest.name, title=asset["title"],page=asset["page"],
                        license=asset["license"],license_url=asset.get("license_url",""),
                        author=asset["author"], relevance=relevance)
                    break
                except (OSError, RuntimeError, ValueError, httpx.HTTPError) as err:
                    print("[capcut-v2] Skipped asset: " + type(err).__name__,flush=True)
            if state["status"]=="missing" and ai_left:
                try:
                    dest = _ai_illustration(client,scene,path/("scene_%03d_ai" % idx))
                    if budget["bytes"] + dest.stat().st_size <= MAX_ASSETS_BYTES:
                        budget["bytes"] += dest.stat().st_size
                        state.update(status="ready",type="image",source="gemini_image",
                            file=dest.name, title="Ilustração conceitual",
                            page="",license="Generated; labeled illustration",
                            author="Gemini",illustration=True)
                        ai_left -= 1
                    else:
                        dest.unlink(missing_ok=True)
                except (httpx.HTTPError, RuntimeError, ValueError, OSError) as err:
                    print("[capcut-v2] AI image unavailable: " + type(err).__name__,flush=True)
            prepared.append(state)
            with LOCK:
                RESULTS[pid] = {"status":"running","completed":idx,
                   "total":len(scenes),"ready":sum(x["status"]=="ready" for x in prepared)}
    plan = {"version":VERSION,"project_id":pid,"created_at":stamp(),
            "scenes":prepared,"bytes_downloaded":budget["bytes"],
            "ai_images_created":sum(x["source"]=="gemini_image" for x in prepared),
            "ai_direction_used":bool(direction),
            "missing_scenes":[x["scene"] for x in prepared if x["status"]!="ready"],
            "sources":sorted(set(x["source"] for x in prepared if x["source"]))}
    tmp = path / "plan.tmp"
    tmp.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding="utf-8")
    tmp.replace(path/"plan.json")
    return plan

def _work(studio,pid,request):
    try:
        plan = _prepare(studio,pid,request)
        with LOCK:
            RESULTS[pid] = {"status":"complete","plan":plan}
        print("[capcut-v2] COMPLETE project="+pid[:8]+" sourced="+
              str(len(plan["scenes"])-len(plan["missing_scenes"]))+
              " ai="+str(plan["ai_images_created"]),flush=True)
    except Exception as exc:
        with LOCK:
            RESULTS[pid] = {"status":"failed","error":type(exc).__name__+": "+str(exc)[:180]}
        print("[capcut-v2] FAILED "+type(exc).__name__,flush=True)
    finally:
        with LOCK:
            RUNNING.pop(pid,None)

PAGE = """<!doctype html><html lang="pt-br"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Estúdio IA — CapCut V2</title><style>
body{font:16px system-ui,sans-serif;background:#101522;color:#fff;max-width:860px;margin:32px auto;padding:14px}
section{background:#1b2333;border:1px solid #344055;border-radius:14px;padding:20px;margin-top:16px}
input,select,button{font:inherit;padding:10px;margin:5px;border-radius:8px}button{cursor:pointer}
small{color:#b5c5dc}a{color:#9cc9ff}pre{white-space:pre-wrap;overflow-wrap:anywhere}
</style><h1>CapCut V2 · Media Fusion</h1>
<p>Seleciona vídeos e fotos relevantes de fontes licenciadas. Quando solicitado, gera até 2 ilustrações por IA, com cobrança na API Gemini.</p>
<section><label>ID do projeto: <input id="pid" size="42" placeholder="UUID do projeto"></label>
<label>Cenas: <select id="count"><option>4</option><option selected>8</option><option>12</option></select></label>
<label><input type="checkbox" id="director"> Dirigir cenas com Gemini (1 chamada paga)</label>
<label><input type="checkbox" id="ai"> Gerar até 2 imagens conceituais com Gemini (pago)</label>
<label>Senha do Estúdio: <input id="password" type="password"></label>
<button onclick="start()">Preparar mídias (não renderiza)</button></section>
<section><button onclick="status()">Consultar</button>
<button onclick="download()">Baixar pacote editável CapCut</button>
<pre id="out">Pronto para preparar.</pre></section>
<small>Fontes: Pixabay, Pexels (adicione PEXELS_API_KEY) e Wikimedia Commons. Não baixa YouTube; nenhum envio ao CapCut é automático. Revise licença e correspondência das imagens.</small>
<script>
const base='/api/capcut-v2/projects/';
const pid=()=>document.getElementById('pid').value.trim();
const out=x=>document.getElementById('out').textContent=typeof x==='string'?x:JSON.stringify(x,null,2);
async function start(){try{let r=await fetch(base+encodeURIComponent(pid())+'/prepare',{method:'POST',headers:{'Content-Type':'application/json','X-Studio-Password':document.getElementById('password').value},body:JSON.stringify({max_scenes:Number(document.getElementById('count').value),ai_images:document.getElementById('ai').checked?2:0,ai_direction:document.getElementById('director').checked})});out(await r.json())}catch(e){out(e.message)}}
async function status(){try{let r=await fetch(base+encodeURIComponent(pid())+'/status');out(await r.json())}catch(e){out(e.message)}}
function download(){window.location='/api/capcut/projects/'+encodeURIComponent(pid())+'/download'}
</script></html>"""

def install(studio):
    @studio.app.get("/capcut-v2", response_class=HTMLResponse)
    def page():
        return HTMLResponse(PAGE)

    @studio.app.post("/api/capcut-v2/projects/{pid}/prepare")
    def prepare(pid:str, request:PrepareRequest, x_studio_password:str|None=Header(default=None)):
        expected = os.getenv("STUDIO_PASSWORD","")
        if not expected or not x_studio_password or not secrets.compare_digest(
                expected.encode(), x_studio_password.encode()):
            raise HTTPException(403,"Senha do Estúdio obrigatória")
        project = studio.get_project(pid)
        if project.get("status")!="concluído":
            raise HTTPException(409,"Projeto deve estar concluído")
        with LOCK:
            if pid in RUNNING:
                raise HTTPException(409,"Projeto já em preparação")
            RUNNING[pid]=True
            RESULTS[pid]={"status":"queued","total":request.max_scenes}
        threading.Thread(target=_work,args=(studio,pid,request),daemon=True).start()
        return {"status":"queued","project_id":pid,
                "paid_image_calls_max":request.ai_images,
                "paid_direction_calls_max":1 if request.ai_direction else 0,
                "download_url":f"/api/capcut/projects/{pid}/download"}

    @studio.app.get("/api/capcut-v2/projects/{pid}/status")
    def status(pid:str):
        studio.get_project(pid)
        with LOCK:
            current=RESULTS.get(pid)
        if current:
            return current
        manifest=folder(studio,pid)/"plan.json"
        if manifest.exists():
            return {"status":"complete","plan":json.loads(manifest.read_text("utf-8"))}
        return {"status":"not_prepared","available_sources":["pixabay", "wikimedia"]+
          (["pexels"] if os.getenv("PEXELS_API_KEY") else []),
          "ai_optional":bool(os.getenv("GEMINI_API_KEY"))}

def self_test():
    assert _approved("https://cdn.pixabay.com/photo/a.jpg")
    assert _approved("https://images.pexels.com/photos/a.jpeg")
    assert not _approved("http://cdn.pixabay.com/a")
    assert not _approved("https://cdn.pixabay.com.evil.org/a")
    assert not _approved("https://127.0.0.1/a")
    assert _semantic("Paraguay frontier","Paraguay border crossing") >= 3
    assert _semantic("Paraguai fronteira","Paraguay border crossing") >= 3
    assert _semantic("Paraguay frontier","beautiful generic sunset") == 0
    assert _semantic("Brazil economy","Paraguay crisis") == 0
    print("[capcut-v2] SELF-TEST PASS matching, licensed providers, URL safety, paid opt-in",flush=True)

if __name__=="__main__":
    self_test()
    p=Path("/app/app.py")
    if p.is_file():
        source=p.read_text()
        marker="import studio_capcut_v2_media as _capcut_v2_media"
        if marker not in source:
            with p.open("a") as f:
                f.write("\n"+marker+"\n_capcut_v2_media.install(__import__('sys').modules[__name__])\n")
