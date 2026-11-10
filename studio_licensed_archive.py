"""Automatic rights-filtered Wikimedia Commons B-roll library.

Downloads only through the Estudio's existing approved Wikimedia media fetcher.
Search and metadata requests are bounded, cached briefly in-process, and do not
pretend that a Commons upload originated on YouTube. CC BY, CC0 and public
domain assets are supported; CC BY-SA/NC/ND are excluded in this first pass.
"""
from __future__ import annotations
import os
import re
import time
import html
import threading
import unicodedata
from urllib.parse import urlsplit
import httpx

API="https://commons.wikimedia.org/w/api.php"
UA="MeuEstudioIA/1.0 (https://github.com/amendokim/estudio; licensed documentary media)"
MAX_SEARCHES=8
MAX_CANDIDATES=40
MAX_BYTES=8*1024*1024
CACHE_SECONDS=3600
CACHE={}
LOCK=threading.RLock()
STOP={"video","videos","footage","stock","4k","hd","the","and","with","from",
      "this","about","para","sobre","do","da","dos","das","and","documentario"}

def norm(value):
    s=unicodedata.normalize("NFKD",str(value or "").casefold())
    return "".join(c for c in s if not unicodedata.combining(c))

def words(value):
    return {x for x in re.findall(r"[a-z0-9]{3,}",norm(value)) if x not in STOP}

def license_ok(label):
    s=norm(label).strip().replace("–","-").replace("—","-")
    return s in ("cc0","cc0 1.0","public domain","pd","public domain mark") or (
        s.startswith("cc by ") and not any(x in s for x in ("-sa","-nc","-nd")))

def licensed_video(page,info):
    if str(info.get("mime") or "") not in ("video/webm","video/mp4","video/ogg"):
        return None
    size=int(info.get("size") or 0)
    if not 1000<size<=MAX_BYTES:
        return None
    url=str(info.get("url") or "")
    u=urlsplit(url)
    if u.scheme!="https" or u.hostname!="upload.wikimedia.org" or u.username or u.password:
        return None
    metadata=info.get("extmetadata") or {}
    label=html.unescape(re.sub(r"<[^>]*>"," ",str(
        (metadata.get("LicenseShortName") or {}).get("value") or ""))).strip()
    if not license_ok(label):
        return None
    page_url=str(info.get("descriptionurl") or "")
    if not page_url.startswith("https://commons.wikimedia.org/wiki/File:"):
        return None
    title=str(page.get("title") or "").removeprefix("File:").replace("_"," ")
    if not words(title):
        return None
    author=html.unescape(re.sub(r"<[^>]*>"," ",str(
        (metadata.get("Artist") or {}).get("value") or ""))).strip()[:120] or "Wikimedia Commons"
    # Note: source attribution remains the original file page, NOT a YouTube URL.
    return {"media_url":url,"media_local":"","media_kind":"video",
            "media_provider":"wikimedia","author":(author+" | "+label+
                " | arquivo ilustrativo Commons")[:230],
            "source_page":page_url,"archive_title":title,
            "archive_license":label,"archive_size":size}

def query_plan(project):
    scenes=project.get("scenes") or []
    queries=[]
    def add(item):
        q=" ".join(str(item or "").split()).strip()
        if len(words(q))>=2 and q.casefold() not in [x.casefold() for x in queries]:
            queries.append(q[:75])
    if len(scenes)>0:
        # Sample the actual scene intent at evenly distributed script positions.
        picks=[round(i*(len(scenes)-1)/max(1,MAX_SEARCHES-1))
               for i in range(MAX_SEARCHES)]
        for idx in picks:
            add(scenes[idx].get("query") or "")
    # Named location/subject is more useful than a full editorial title.
    add(project.get("topic") or "")
    add(project.get("title") or "")
    return queries[:MAX_SEARCHES]

def relevance(scene,asset):
    scene_terms=words(str(scene.get("query") or "")+" "+
                      str(scene.get("text") or "")[:160])
    title_terms=words(asset.get("archive_title"))
    overlap=len(scene_terms & title_terms)
    # Shared generic words such as 'video' never create false relevance.
    return overlap*8

def discover(project,refresh=False):
    if os.getenv("STUDIO_COMMONS_BROLL_ENABLED","1")!="1":
        return []
    pid=str(project.get("id") or "")
    with LOCK:
        cached=CACHE.get(pid)
        if cached and not refresh and time.monotonic()-cached[0]<CACHE_SECONDS:
            return cached[1]
    found={}
    plan=query_plan(project)
    with httpx.Client(timeout=12,follow_redirects=False) as client:
        for query in plan:
            try:
                r=client.get(API,headers={"User-Agent":UA},params={
                    "action":"query","format":"json","generator":"search",
                    "gsrsearch":query+" filetype:video","gsrnamespace":"6",
                    "gsrlimit":"25","prop":"imageinfo",
                    "iiprop":"url|mime|size|extmetadata",
                    "iiextmetadatafilter":"LicenseShortName|Artist",
                })
                r.raise_for_status()
                for page in (r.json().get("query",{}).get("pages") or {}).values():
                    for inf in page.get("imageinfo") or []:
                        item=licensed_video(page,inf)
                        if item:
                            found[item["source_page"]]=item
                        if len(found)>=MAX_CANDIDATES:
                            break
                    if len(found)>=MAX_CANDIDATES:break
            except (httpx.HTTPError,ValueError,KeyError,TypeError):
                continue
            if len(found)>=MAX_CANDIDATES:break
    result=list(found.values())
    with LOCK:CACHE[pid]=(time.monotonic(),result)
    print("[commons-broll] project=%s verified=%d queries=%d" %
          (pid,len(result),len(plan)),flush=True)
    return result

def matches_for_scene(scene,assets,max_results=4):
    ranked=sorted(((relevance(scene,a),a) for a in assets),
                  key=lambda x:(-x[0],x[1]["source_page"]))
    return [a for score,a in ranked if score>=8][:max_results]

def fill_short_preview(studio,pid):
    """Improve a short preview with distinct licensed Commons footage.

    Only replaces generic stock or missing media with at least TWO concrete
    scene/title matches. Leaves YouTube-origin provenance and ratio untouched.
    The caller explicitly opts into a preview without mandatory 70/30.
    """
    project=studio.get_project(pid)
    assets=discover(project)
    if not assets:
        return {"found":0,"replaced":0,"note":"Nenhum Commons reutilizável pertinente"}
    seen={str(x.get("source_page") or "") for x in project.get("scenes") or []
          if x.get("source_page")}
    replaced=0
    limit=max(1,len(project.get("scenes") or [])//3)
    for scene in project.get("scenes") or []:
        if replaced>=limit:break
        if scene.get("media_provider") not in ("pixabay","pexels","original",""):
            continue
        candidates=[a for a in matches_for_scene(scene,assets,8)
                    if relevance(scene,a)>=16 and a["source_page"] not in seen
                    and studio._approved_url(a["media_url"],"wikimedia")]
        if not candidates:continue
        asset=candidates[0]
        with studio.db() as c:
            c.execute("""UPDATE scenes SET media_url=?,media_local='',
                media_kind='video',media_provider='wikimedia',source_page=?,
                author=?,thumb='' WHERE id=? AND project_id=?""",
                (asset["media_url"],asset["source_page"],asset["author"],
                 scene["id"],pid))
        seen.add(asset["source_page"])
        replaced+=1
    print("[commons-broll] short preview project=%s replaced=%d" %
          (pid,replaced),flush=True)
    return {"found":len(assets),"replaced":replaced,
            "note":"Uso de materiais licenciados; não são contabilizados como YouTube"}

def self_test():
    sample={"title":"File:Paraguay city street.webm","imageinfo":[]}
    i={"mime":"video/webm","size":10240,
       "url":"https://upload.wikimedia.org/wikipedia/commons/a/a1/Paraguay.webm",
       "descriptionurl":"https://commons.wikimedia.org/wiki/File:Paraguay_city_street.webm",
       "extmetadata":{"LicenseShortName":{"value":"CC BY 4.0"},
                      "Artist":{"value":"Filmmaker"}}}
    asset=licensed_video(sample,i)
    assert asset and asset["media_provider"]=="wikimedia"
    assert "YouTube" not in asset["author"]
    assert not license_ok("CC BY-NC 4.0")
    assert not license_ok("CC BY-SA 4.0")
    assert licensed_video(sample,{**i,"url":"https://evil.example/download.mp4"}) is None
    assert licensed_video(sample,{**i,"size":MAX_BYTES+1}) is None
    p={"scenes":[{"query":"Paraguay city street","text":"Cidade e trafego"}]}
    assert matches_for_scene(p["scenes"][0],[asset])
    print("[commons-broll] SELF-TEST PASS licensing, URLs, relevance, provenance",flush=True)

if __name__=="__main__":self_test()
