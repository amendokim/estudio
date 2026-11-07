"""Fonte de vídeo autorizado: materiais Creative Commons hospedados no Wikimedia Commons.
Não baixa streams do YouTube; usa um arquivo histórico cujo upload original no YouTube
foi licenciado sob CC BY e que está disponível diretamente no Commons.
"""
import html
import re
import httpx

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
HISTORIC_FILE = "File:Ponte da Amizade completa 50 anos.webm"


def _plain(value):
    return re.sub(r"<[^>]*>", "", html.unescape(str(value or ""))).strip()


def _licensed_clip(info, page):
    if info.get("mime") not in ("video/webm", "video/mp4", "video/ogg"):
        return None
    license_name = _plain((info.get("extmetadata") or {}).get("LicenseShortName", {}).get("value", ""))
    norm = license_name.lower().replace("–", "-").replace("—", "-")
    if not (norm.startswith("cc by ") or norm in ("cc0", "public domain")):
        return None
    url = info.get("url", "")
    if not (url.startswith("https://upload.wikimedia.org/") and
            not url.endswith((".php", ".html"))):
        return None
    if int(info.get("size") or 0) > 35 * 1024 * 1024:
        return None
    author = _plain((info.get("extmetadata") or {}).get("Artist", {}).get("value", "")) or "TVNBR"
    return {
        "id": str(page.get("pageid", "")),
        "kind": "video",
        "provider": "wikimedia",
        "url": url,
        "thumb": info.get("thumburl") or "",
        "page": info.get("descriptionurl") or
                "https://commons.wikimedia.org/wiki/File:Ponte_da_Amizade_completa_50_anos.webm",
        "author": f"{author[:130]} | {license_name} | vídeo originalmente publicado no YouTube",
        "duration": 55,
    }


def find_historic_bridge_video():
    params = {
        "action": "query", "format": "json", "titles": HISTORIC_FILE,
        "prop": "imageinfo", "iiprop": "url|mime|size|extmetadata",
        "iiextmetadatafilter": "LicenseShortName|Artist",
        "iiurlwidth": 480,
    }
    with httpx.Client(timeout=25, follow_redirects=False) as client:
        response = client.get(
            COMMONS_API, params=params,
            headers={"User-Agent": "MeuEstudioIA/0.5 (https://github.com/amendokim/estudio; licensed-media-lookup)"},
        )
        response.raise_for_status()
        pages = response.json().get("query", {}).get("pages", {})
    result = []
    for page in (pages.values() if isinstance(pages, dict) else pages):
        for info in (page.get("imageinfo") or []):
            clip = _licensed_clip(info, page)
            if clip:
                result.append(clip)
    return result


def install(studio):
    """Executa no servidor após carregar o app sem alterar seus endpoints OAuth."""
    original = studio._auto_find_media

    def with_licensed_video(pid, limit):
        project = studio.get_project(pid)
        # Apenas um trecho real na cena de 2015; demais cenas usam bancos de mídia.
        for scene in project["scenes"]:
            query = scene.get("query", "").casefold()
            if not ("ponte da amizade" in query and "2015" in query):
                continue
            if scene.get("media_url") or scene.get("media_local"):
                continue
            try:
                candidates = find_historic_bridge_video()
                if not candidates:
                    continue
                pick = candidates[0]
                if any(x.get("media_url") == pick["url"] for x in project["scenes"]):
                    continue
                with studio.db() as connection:
                    connection.execute(
                        """UPDATE scenes SET media_url=?,media_kind=?,media_provider=?,
                           source_page=?,author=?,thumb=? WHERE id=?""",
                        (pick["url"], "video", "wikimedia", pick["page"],
                         pick["author"], pick["thumb"], scene["id"]),
                    )
                break
            except (httpx.HTTPError, ValueError, KeyError, OSError):
                # Falha no Commons não deve impedir a produção com Pixabay.
                break
        return repair_stock_video(studio, pid, limit, original(pid, limit))

    studio._auto_find_media = with_licensed_video


if __name__ == "__main__":
    # At Docker build time, register the extension without touching OAuth/MCP logic.
    from pathlib import Path
    application = Path("/app/app.py")
    if not application.exists():
        raise SystemExit("Missing app.py under /app")
    marker = "\nimport studio_commons_video as _licensed_video_extension\n"
    current = application.read_text(encoding="utf-8")
    if marker not in current:
        application.write_text(
            current + marker
            + '_licensed_video_extension.install(__import__("sys").modules[__name__])\n',
            encoding="utf-8",
        )

def repair_stock_video(studio, pid, limit, result):
    """Replace image-only coverage with small legally licensed Pixabay clips.
    No YouTube scraping and no OpenAI API usage. Generic clips are illustrative.
    """
    import os
    if not os.getenv("PIXABAY_API_KEY"):
        return result
    project = studio.get_project(pid)
    scenes = project["scenes"]
    if not scenes or len(scenes) > 120:
        return result
    runtime = sum(max(1.0, float(s.get("duration") or 8)) for s in scenes)
    video = sum(max(1.0, float(s.get("duration") or 8))
                for s in scenes if s.get("media_kind") == "video")
    if video / runtime >= 0.9:
        return result
    used = {s.get("media_url") for s in scenes if s.get("media_url")}
    searches = {}
    fallback = [
        "bridge aerial", "bridge traffic", "river aerial", "road traffic",
        "city aerial", "truck highway", "bridge", "market street",
        "river landscape", "bridge aerial"
    ]
    keywords = [
        (("rio", "paraná", "river", "água"), "river aerial"),
        (("caminh", "mercador", "comércio"), "truck highway"),
        (("cidade", "ciudad", "urbano"), "city aerial"),
        (("trânsito", "tráfego", "carro"), "road traffic"),
        (("constru", "inaugura", "história"), "bridge construction"),
        (("ponte", "bridge", "travessia"), "bridge"),
    ]
    replaced = 0
    with httpx.Client(timeout=18, follow_redirects=False) as client:
        for scene in scenes:
            if scene.get("media_kind") == "video":
                continue
            context = (scene.get("query") or "").casefold()
            pos = int(scene.get("position") or 0)
            terms = [term for triggers, term in keywords
                     if any(word in context for word in triggers)]
            terms += [fallback[pos % len(fallback)], "bridge", "traffic", "river"]
            selected = None
            for term in dict.fromkeys(terms):
                if term not in searches:
                    try:
                        response = client.get(
                            "https://pixabay.com/api/videos/",
                            params={"key": os.environ["PIXABAY_API_KEY"],
                                    "q": term, "lang": "en", "per_page": 20,
                                    "safesearch": "true", "page": 1},
                        )
                        response.raise_for_status()
                        searches[term] = response.json().get("hits", [])
                    except (ValueError, httpx.HTTPError):
                        searches[term] = []
                for hit in searches[term]:
                    videos = hit.get("videos") or {}
                    for rendition in ("small", "tiny", "medium"):
                        item = videos.get(rendition) or {}
                        url = item.get("url") or ""
                        size = int(item.get("size") or 0)
                        page = hit.get("pageURL") or ""
                        if not (url and page.startswith("https://pixabay.com/") and
                                studio._approved_url(url, "pixabay") and
                                url not in used and
                                (not size or size < 9 * 1024 * 1024)):
                            continue
                        selected = (
                            url, page, str(hit.get("user") or "Pixabay")[:100]
                            + " | Pixabay | Imagens ilustrativas",
                            item.get("thumbnail") or "",
                        )
                        break
                    if selected:
                        break
                if selected:
                    break
            if not selected:
                continue
            with studio.db() as conn:
                conn.execute(
                    """UPDATE scenes SET media_url=?,media_kind='video',
                       media_provider='pixabay',source_page=?,author=?,thumb=?,
                       media_local='' WHERE id=? AND project_id=?""",
                    (*selected, scene["id"], pid),
                )
            used.add(selected[0])
            replaced += 1
            video += max(1.0, float(scene.get("duration") or 8))
            if video / runtime >= 0.95:
                break
    print("[stock-video] coverage=%.1f%%, replacements=%d"
          % (100 * video / runtime, replaced), flush=True)
    return result
