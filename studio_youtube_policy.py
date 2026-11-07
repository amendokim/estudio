"""Automatic YouTube-origin archive policy for Meu Estudio IA.

Targets 30% of the runtime using separately hosted, demonstrably licensed videos
that originally appeared on YouTube. Does NOT download from YouTube, assume a
"ten seconds" exemption, or treat a YouTube CC search result as a downloadable file.
Priority: (1) verified CC BY / CC0 / public domain,
          (2) documented permission via safe local source manifest,
          (3) criticism/analysis: review queue only,
          (4) standard-licensed videos without prior permission: candidates only;
              substantive legal review required before reuse.  No YouTube download.
It never blocks a documentary or forces unsuitable footage to meet the target.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

import httpx

TARGET_DEFAULT = 30
MAX_YOUTUBE_CLIP_SECONDS = 10
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
HISTORIC_BRIDGE = "File:Ponte da Amizade completa 50 anos.webm"
USER_AGENT = "MeuEstudioIA/0.8 (https://github.com/amendokim/estudio; rights-audited-video)"
PRIORITIES = (
    {"priority": 1, "category": "creative_commons_public_domain",
     "automatic": True, "note": "Apenas licença reutilizável verificada e arquivo obtido de fonte autorizada."},
    {"priority": 2, "category": "documented_permission",
     "automatic": True, "note": "Somente arquivos locais com comprovante de autorização cadastrado."},
    {"priority": 3, "category": "criticism_analysis",
     "automatic": True, "auto_decision": "skip_if_unverified",
     "note": "Análise automática do contexto: se a exceção não puder ser fundamentada, usar mídia licenciada."},
    {"priority": 4, "category": "standard_licence_unlicensed",
     "automatic": True, "auto_decision": "skip_if_unverified",
     "note": "Última prioridade: considerar referência, mas descartar automaticamente quando não houver "
             "base de uso verificável e arquivo obtido legalmente. Não realiza download do YouTube."},
)
_cache = {}


def target_percent() -> int:
    try:
        value = int(os.environ.get("STUDIO_YOUTUBE_TARGET_PERCENT", str(TARGET_DEFAULT)))
    except ValueError:
        return TARGET_DEFAULT
    return min(70, max(0, value))


def plain(value):
    return re.sub(r"<[^>]*>", " ", html.unescape(str(value or ""))).strip()


def normalized(value):
    s = unicodedata.normalize("NFKD", str(value or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def allowed_license(label):
    s = normalized(label).strip().replace("–", "-").replace("—", "-")
    if s.startswith("cc by ") and "nc" not in s and "nd" not in s and "sa" not in s:
        return True
    return s in ("cc0", "cc0 1.0", "public domain", "pd", "public domain mark")


def authorized_media_url(url):
    """Do not permit arbitrarily provided domains/redirects to the video downloader."""
    p = urlsplit(url or "")
    return p.scheme == "https" and p.hostname == "upload.wikimedia.org" and not p.username and not p.password


def origin_is_youtube(metadata, title):
    if title == HISTORIC_BRIDGE:
        return True  # Reviewed CC BY 3.0 video: Wikimedia File page has original YouTube ID.
    for key in ("Source", "Credit", "Description", "Attribution"):
        text = plain((metadata or {}).get(key, {}).get("value", ""))
        if re.search(r"(?:youtube\.com/watch|youtu\.be/|youtube\.com/embed/)", text, re.I):
            return True
    return False


def relevant_to_project(title, project):
    # File title must clearly match the main named subject, not merely generic stock.
    name = normalized(title).replace("_", " ")
    context = normalized(project.get("title", "") + " " + project.get("topic", ""))
    if "ponte da amizade" in context:
        return "ponte da amizade" in name
    stop = {"video", "film", "documentario", "documentary", "historia", "historico",
            "sobre", "para", "brasil", "mundo", "the", "with", "uma", "um", "dos",
            "desta", "atraves", "piloto", "cinematografico", "minutos", "partes"}
    words = [x for x in set(re.findall(r"[a-z0-9]{5,}", context)) if x not in stop]
    words.sort(key=len, reverse=True)
    return bool(words and any(w in name for w in words[:5]))


def clip_from_commons(page, info, project):
    title = str(page.get("title", ""))
    if info.get("mime") not in ("video/webm", "video/mp4", "video/ogg"):
        return None
    size = int(info.get("size") or 0)
    if not (1000 < size <= 35 * 1024 * 1024):
        return None
    url = info.get("url") or ""
    if not authorized_media_url(url):
        return None
    md = info.get("extmetadata") or {}
    license_name = plain(md.get("LicenseShortName", {}).get("value", ""))
    if not allowed_license(license_name):
        return None
    if not origin_is_youtube(md, title) or not relevant_to_project(title, project):
        return None
    page_url = info.get("descriptionurl") or ""
    if not page_url.startswith("https://commons.wikimedia.org/wiki/File:"):
        return None
    author = plain(md.get("Artist", {}).get("value", ""))[:120] or "Autor identificado no Wikimedia Commons"
    return {"title": title, "url": url, "source_page": page_url, "license": license_name,
            "author": author, "thumb": info.get("thumburl") or "",
            "category": "creative_commons_public_domain", "evidence": page_url}


def _commons_candidates(project):
    context = normalized(project.get("title", "") + " " + project.get("topic", ""))
    cache_key = "ponte_da_amizade" if "ponte da amizade" in context else context[:70]
    if cache_key in _cache and time.monotonic() - _cache[cache_key][0] < 21600:
        return _cache[cache_key][1]
    result = []
    try:
        terms = '"Ponte da Amizade"' if cache_key == "ponte_da_amizade" else " ".join(
            re.findall(r"[\w-]{5,}", project.get("title", ""))[:3]
        )
        if not terms:
            return []
        params = {
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": terms + " filetype:video", "gsrnamespace": "6",
            "gsrlimit": "12", "prop": "imageinfo",
            "iiprop": "url|mime|size|extmetadata", "iiextmetadatafilter":
            "LicenseShortName|Artist|Source|Credit|Description",
            "iiurlwidth": "320",
        }
        with httpx.Client(timeout=16, follow_redirects=False) as client:
            response = client.get(COMMONS_API, params=params,
                                  headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            pages = (response.json().get("query", {}).get("pages") or {}).values()
            for page in pages:
                for info in page.get("imageinfo") or ():
                    clip = clip_from_commons(page, info, project)
                    if clip:
                        result.append(clip)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        print("[youtube-policy] Commons search unavailable: " + type(exc).__name__, flush=True)
    # The known original report is checked separately; never trust search ranking alone.
    if cache_key == "ponte_da_amizade":
        try:
            import studio_commons_video
            for found in studio_commons_video.find_historic_bridge_video():
                url = found.get("url", "")
                if authorized_media_url(url):
                    result.append({
                        "title": HISTORIC_BRIDGE, "url": url,
                        "source_page": found["page"], "license": "CC BY 3.0",
                        "author": "TVNBR", "thumb": found.get("thumb") or "",
                        "category": "creative_commons_public_domain",
                        "evidence": found["page"],
                    })
        except (httpx.HTTPError, KeyError, ValueError, OSError):
            pass
    # Avoid false quota fulfillment by repeating one video as multiple sources.
    unique = list({x["url"]: x for x in result}.values())[:12]
    _cache[cache_key] = (time.monotonic(), unique)
    return unique


def _local_authorized_sources(studio):
    """Optional owner-curated manifest; unverified entries are not imported."""
    manifest = Path(studio.DATA) / "youtube_authorized_sources.json"
    try:
        if not manifest.is_file() or manifest.stat().st_size > 150000:
            return []
        rows = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            return []
        result = []
        root = Path(studio.MEDIA).resolve()
        for row in rows[:100]:
            if not isinstance(row, dict) or row.get("status") != "verified":
                continue
            path = (root / str(row.get("media_local") or "")).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                continue
            if not str(row.get("evidence") or "").startswith("https://"):
                continue
            yt = row.get("youtube_url") or ""
            if urlsplit(yt).hostname not in ("youtube.com", "www.youtube.com", "youtu.be"):
                continue
            result.append({**row, "media_local": str(path.relative_to(root)),
                           "category": "documented_permission"})
        return result
    except (OSError, ValueError, TypeError):
        return []


def recognized_archival_scene(scene):
    if scene.get("media_kind") != "video":
        return False
    provider = scene.get("media_provider")
    page = (scene.get("source_page") or "").casefold()
    author = (scene.get("author") or "").casefold()
    return ((provider == "wikimedia" and
             ("ponte_da_amizade_completa_50_anos" in page or "originalmente publicado no youtube" in author))
            or (provider == "authorized_youtube" and "autorização" in author))


def duration(scene):
    try:
        return max(1.0, float(scene.get("duration") or 1))
    except (ValueError, TypeError):
        return 1.0


def report(project):
    scenes = project.get("scenes") or []
    total = sum(duration(s) for s in scenes)
    selected = sum(duration(s) for s in scenes if recognized_archival_scene(s))
    target = target_percent()
    return {"project_id": project["id"], "target_percent": target,
            "actual_percent": round(100 * selected / total, 1) if total else 0.0,
            "selected_seconds": round(selected, 1), "total_seconds": round(total, 1),
            "max_youtube_clip_seconds": MAX_YOUTUBE_CLIP_SECONDS,
            "automatic_legal_fallback": "skip_unverified_and_use_licensed_stock",
            "overlong_archive_scenes": [
                idx for idx, scene in enumerate(scenes, 1)
                if recognized_archival_scene(scene) and duration(scene) > MAX_YOUTUBE_CLIP_SECONDS
            ],
            "missing_seconds": round(max(0, total * target / 100 - selected), 1),
            "target_met": bool(total and selected >= total * target / 100),
            "priorities": PRIORITIES,
            "note": "30% é uma meta, nunca autorização para usar material sem licença. "
                    "Fontes sem autorização podem ser candidatas de última prioridade, sujeitas à revisão. "
                    "Não são baixadas nem incorporadas automaticamente."}


def _suitable_scene(scene, title):
    # Prefer scenes on the archive's actual subject, avoid replacing original maps or assets.
    if scene.get("media_provider") in ("wikimedia", "original", "authorized_youtube", "local"):
        return False
    if scene.get("media_local") and scene.get("media_provider") not in ("pixabay", "pexels"):
        return False
    title_norm = normalized(title)
    context = normalized((scene.get("query") or "") + " " + (scene.get("text") or ""))
    if "ponte da amizade" in title_norm:
        return ("ponte" in context or "bridge" in context) and \
               ("paraguai" in context or "amizade" in context or "foz" in context or "fronteira" in context)
    useful = [x for x in re.findall(r"[a-z]{5,}", title_norm) if x not in
              ("video", "youtube", "filme", "reportagem", "documentario")]
    return any(word in context for word in useful[:4])


def enforce(studio, pid):
    project = studio.get_project(pid)
    if project.get("status") == "concluído":
        return report(project)
    before = report(project)
    if before["target_met"] or len(project.get("scenes") or []) > 120:
        return before
    candidates = _commons_candidates(project)
    local = _local_authorized_sources(studio)
    used = {s.get("media_url") for s in project["scenes"] if s.get("media_url")}
    used_local = {s.get("media_local") for s in project["scenes"] if s.get("media_local")}
    selected = before["selected_seconds"]
    target_seconds = before["total_seconds"] * before["target_percent"] / 100
    # Preserve order: Creative Commons / public domain first, then prior documented permission.
    for group in (candidates, local):
        for asset in group:
            if selected >= target_seconds:
                break
            if asset.get("url") in used or (asset.get("media_local") and asset["media_local"] in used_local):
                continue
            for scene in project["scenes"]:
                if duration(scene) > MAX_YOUTUBE_CLIP_SECONDS:
                    continue
                if not _suitable_scene(scene, asset["title"]):
                    continue
                try:
                    with studio.db() as conn:
                        if asset["category"] == "creative_commons_public_domain":
                            conn.execute("""UPDATE scenes SET media_url=?, media_local='',
                             media_kind='video', media_provider='wikimedia',source_page=?,
                             author=?,thumb=? WHERE id=? AND project_id=?""",
                             (asset["url"], asset["source_page"],
                              asset["author"][:120] + " | " + asset["license"] +
                              " | vídeo originalmente publicado no YouTube",
                              asset.get("thumb") or "", scene["id"], pid))
                            used.add(asset["url"])
                        else:
                            conn.execute("""UPDATE scenes SET media_url='', media_local=?,
                             media_kind='video', media_provider='authorized_youtube',source_page=?,
                             author=?,thumb='' WHERE id=? AND project_id=?""",
                             (asset["media_local"], asset["evidence"],
                              str(asset.get("author") or "Autor autorizado")[:120] +
                              " | autorização documentada", scene["id"], pid))
                            used_local.add(asset["media_local"])
                    selected += duration(scene)
                    # Never reuse a clip to artificially reach the target.
                    break
                except (OSError, ValueError, KeyError):
                    break
        if selected >= target_seconds:
            break
    after = report(studio.get_project(pid))
    print("[youtube-policy] project=%s goal=%s%% achieved=%s%%" %
          (pid, after["target_percent"], after["actual_percent"]), flush=True)
    return after



# A candidate URL is reference metadata, not a licence or downloaded asset.
# No use of copyright-protected streams by this policy.
from urllib.parse import parse_qs
from pydantic import BaseModel, Field
from fastapi import HTTPException
import uuid
from datetime import datetime, timezone


class UnlicensedCandidate(BaseModel):
    youtube_url: str = Field(min_length=15, max_length=250)
    rationale: str = Field(min_length=15, max_length=900)
    # This is an aesthetic preference, NOT a copyright safety threshold.
    suggested_seconds: int = Field(default=8, ge=1, le=30)


def safe_video_reference(url: str) -> str:
    try:
        parsed = urlsplit(url.strip())
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
            raise ValueError("URL deve usar HTTPS")
        host = (parsed.hostname or "").lower()
        if host in ("www.youtube.com", "youtube.com", "m.youtube.com"):
            if parsed.path != "/watch":
                raise ValueError("Envie um link de vídeo normal")
            ids = parse_qs(parsed.query).get("v") or []
            video_id = ids[0] if len(ids) == 1 else ""
        elif host == "youtu.be":
            video_id = parsed.path.lstrip("/")
        else:
            raise ValueError("Somente links oficiais do YouTube")
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError("ID de vídeo inválido")
        return "https://www.youtube.com/watch?v=" + video_id
    except (ValueError, TypeError):
        raise ValueError("Link YouTube inválido ou não suportado")


def candidate_schema(studio):
    with studio.db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS youtube_review_candidates (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            youtube_url TEXT NOT NULL,
            rationale TEXT NOT NULL,
            suggested_seconds INTEGER NOT NULL,
            source_priority INTEGER NOT NULL DEFAULT 4,
            state TEXT NOT NULL DEFAULT 'pending_review',
            created_at TEXT NOT NULL,
            UNIQUE(project_id, youtube_url)
        )""")


def candidate_list(studio, project_id):
    with studio.db() as conn:
        rows = conn.execute(
            """SELECT id, youtube_url, rationale, suggested_seconds,
                      source_priority, state, created_at
               FROM youtube_review_candidates WHERE project_id=?
               ORDER BY created_at DESC LIMIT 50""", (project_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def install(studio):
    candidate_schema(studio)
    original = studio._auto_find_media
    original_render = studio.render_task

    def capped_render(pid):
        # Narration can change scene durations AFTER media selection.
        # Verify again immediately before FFmpeg and replace any oversized
        # YouTube-origin source with licensed stock automatically.
        project = studio.get_project(pid)
        if project.get("status") != "concluído":
            overlong = [scene for scene in project.get("scenes") or []
                        if recognized_archival_scene(scene)
                        and duration(scene) > MAX_YOUTUBE_CLIP_SECONDS]
            if overlong:
                with studio.db() as conn:
                    for scene in overlong:
                        conn.execute("""UPDATE scenes SET media_url='',media_local='',
                           media_kind='',media_provider='',source_page='',author='',thumb=''
                           WHERE id=? AND project_id=?""", (scene["id"], pid))
                print("[youtube-policy] Replaced %d overlong archive scenes with stock candidates" %
                      len(overlong), flush=True)
                # The underlying selector observes the same ten-second cap.
                studio._auto_find_media(pid, max(16, len(project.get("scenes") or [])))
                final = studio.get_project(pid)
                if any(recognized_archival_scene(x) and
                       duration(x) > MAX_YOUTUBE_CLIP_SECONDS
                       for x in final.get("scenes") or []):
                    raise RuntimeError("YouTube archive scene exceeded the ten-second hard cap")
        return original_render(pid)

    studio.render_task = capped_render

    def use_policy(pid, limit):
        result = original(pid, limit)
        try:
            enforce(studio, pid)
        except (httpx.HTTPError, ValueError, KeyError, OSError, TypeError) as exc:
            print("[youtube-policy] archive selection skipped: " + type(exc).__name__, flush=True)
        return result
    studio._auto_find_media = use_policy

    @studio.app.get("/api/youtube-policy")
    def get_policy():
        return {"target_percent": target_percent(), "priority_order": PRIORITIES,
                "youtube_api_key_required_for_commons": False,
                "downloads_from_youtube_enabled": False,
                "criticism_analysis_auto_enabled": False,
                "unlicensed_candidates_enabled": True,
                "standard_license_auto_render_enabled": False,
                "standard_license_background_enabled": False,
                "max_youtube_clip_seconds": MAX_YOUTUBE_CLIP_SECONDS,
                "automatic_decisions": True,
                "uncertain_copyright_fallback": "licensed_stock"}

    @studio.app.get("/api/youtube-policy/projects/{pid}")
    def get_report(pid: str):
        return report(studio.get_project(pid))

    @studio.app.get("/api/youtube-policy/projects/{pid}/candidates")
    def list_review_candidates(pid: str):
        studio.get_project(pid)
        items = candidate_list(studio, pid)
        return {"project_id": pid, "items": items, "review_required": True,
                "note": "Candidatos não autorizam reuso ou download; revise caso a caso."}

    @studio.app.post("/api/youtube-policy/projects/{pid}/candidates")
    def add_review_candidate(pid: str, payload: UnlicensedCandidate):
        studio.get_project(pid)
        try:
            reference = safe_video_reference(payload.youtube_url)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if len(candidate_list(studio, pid)) >= 50:
            raise HTTPException(429, "Limite de 50 referências por projeto")
        now = datetime.now(timezone.utc).isoformat()
        with studio.db() as conn:
            if conn.execute(
                "SELECT id FROM youtube_review_candidates WHERE project_id=? AND youtube_url=?",
                (pid, reference)
            ).fetchone():
                raise HTTPException(409, "Este link já está na lista")
            candidate_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO youtube_review_candidates
                   (id,project_id,youtube_url,rationale,suggested_seconds,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (candidate_id, pid, reference, payload.rationale.strip(),
                 payload.suggested_seconds, now)
            )
        return {"id": candidate_id, "state": "pending_review",
                "source_priority": 4, "youtube_url": reference,
                "used_in_video": False, "downloaded": False}



if __name__ == "__main__":
    # Build-time smoke checks ensure licensing rejects NC/ND/SA variants and unsafe URLs.
    assert MAX_YOUTUBE_CLIP_SECONDS == 10
    assert allowed_license("CC BY 3.0")
    assert allowed_license("CC0")
    assert allowed_license("Public domain")
    assert not allowed_license("CC BY-NC 4.0")
    assert not allowed_license("CC BY-SA 4.0")
    assert not allowed_license("CC BY-ND 4.0")
    assert not authorized_media_url("https://youtube.com/watch?v=abc")
    assert authorized_media_url("https://upload.wikimedia.org/wikipedia/commons/1/12/test.webm")
    assert safe_video_reference("https://youtu.be/dQw4w9WgXcQ") == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert safe_video_reference("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10") == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    from pathlib import Path
    appfile = Path("/app/app.py")
    if not appfile.is_file():
        raise SystemExit("Missing Studio app.py")
    mark = "\nimport studio_youtube_policy as _youtube_policy\n"
    code = appfile.read_text(encoding="utf-8")
    if mark not in code:
        appfile.write_text(code + mark +
                           '_youtube_policy.install(__import__("sys").modules[__name__])\n',
                           encoding="utf-8")
