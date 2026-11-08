"""Per-channel Google OAuth and private scheduled YouTube uploads.

No tokens are stored in plaintext. Uploads are DISABLED until the owner
configures Google OAuth and STUDIO_NETWORK_UPLOAD_ENABLED=1 in Railway.
Never uploads competitor video, auto-publishes unrendered drafts, or spends
on third-party models. Each connected channel authenticates separately.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

AUTH_BASE = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_BASE = "https://oauth2.googleapis.com/token"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
RESUMABLE_API = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL_API = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
SCOPES = ("https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly")
THREAD_STARTED=False
THREAD_LOCK=threading.Lock()

class LinkProject(BaseModel):
    project_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")

def now():
    return datetime.now(timezone.utc)

def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00","Z")

def db_setup(studio):
    with studio.db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS channel_network_oauth_state(
          state_hash TEXT PRIMARY KEY,channel_id TEXT NOT NULL,
          expires_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS channel_network_auth(
          channel_id TEXT PRIMARY KEY,
          yt_channel_id TEXT NOT NULL,
          yt_channel_name TEXT NOT NULL,
          refresh_token_encrypted TEXT NOT NULL,
          granted_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS channel_network_uploads(
          idea_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
          project_id TEXT NOT NULL, video_id TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL, scheduled_at TEXT NOT NULL,
          error TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")

def client_config():
    client_id=os.getenv("GOOGLE_YT_CLIENT_ID","").strip()
    secret=os.getenv("GOOGLE_YT_CLIENT_SECRET","").strip()
    if not client_id or not secret:
        raise HTTPException(409,"Google OAuth ainda não configurado: faltam GOOGLE_YT_CLIENT_ID e GOOGLE_YT_CLIENT_SECRET")
    return client_id, secret

def redirect_uri():
    configured=os.getenv("GOOGLE_YT_REDIRECT_URI","").strip()
    domain=os.getenv("RAILWAY_PUBLIC_DOMAIN","").strip()
    uri=configured or ("https://"+domain+"/oauth/youtube/channel" if domain else "")
    parsed=urlsplit(uri)
    if (parsed.scheme!="https" or parsed.username or parsed.password or
        not parsed.hostname or parsed.query or parsed.fragment or
        parsed.path!="/oauth/youtube/channel"):
        raise HTTPException(409,"Configure GOOGLE_YT_REDIRECT_URI com HTTPS e caminho /oauth/youtube/channel")
    return uri

def token_cipher():
    # Prefer independent immutable key; studio password is fallback for existing
    # single-owner deployments. Change of either secret invalidates old tokens.
    secret=os.getenv("STUDIO_NETWORK_TOKEN_SECRET") or os.getenv("STUDIO_PASSWORD","")
    if len(secret)<12:
        raise HTTPException(409,"Configure STUDIO_NETWORK_TOKEN_SECRET com pelo menos 12 caracteres")
    salt=("studio-youtube-token-v1:"+os.getenv("RAILWAY_PROJECT_ID","local")).encode()
    raw=hashlib.pbkdf2_hmac("sha256",secret.encode(),salt,200000,dklen=32)
    return Fernet(base64.urlsafe_b64encode(raw))

def auth_state(channel_id):
    value=secrets.token_urlsafe(32)
    return value,hashlib.sha256(value.encode()).hexdigest()

def connect_url(studio,channel_id):
    import studio_channel_network as net
    net.get_channel(studio,channel_id)
    client_id,_=client_config()
    redirect=redirect_uri()
    token_cipher() # Fail early if no secure token persistence available.
    raw,digest=auth_state(channel_id)
    with studio.db() as db:
        db.execute("DELETE FROM channel_network_oauth_state WHERE expires_at<?",(iso(now()),))
        db.execute("INSERT INTO channel_network_oauth_state VALUES(?,?,?)",
                   (digest,channel_id,iso(now()+timedelta(minutes=10))))
    return AUTH_BASE+"?"+urlencode({
        "client_id":client_id, "redirect_uri":redirect, "response_type":"code",
        "scope":" ".join(SCOPES), "access_type":"offline",
        "prompt":"consent", "state":raw
    })

def complete_auth(studio,state,code):
    if len(state)>256 or len(code)>2048:
        raise HTTPException(400,"Credenciais OAuth inválidas")
    digest=hashlib.sha256(state.encode()).hexdigest()
    with studio.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT channel_id,expires_at FROM channel_network_oauth_state WHERE state_hash=?",(digest,)).fetchone()
        db.execute("DELETE FROM channel_network_oauth_state WHERE state_hash=?",(digest,))
    if not row or row["expires_at"]<iso(now()):
        raise HTTPException(403,"Autorização expirada ou inválida")
    client_id,secret=client_config()
    with httpx.Client(timeout=25,follow_redirects=False) as client:
        res=client.post(TOKEN_BASE,data={
            "client_id":client_id,"client_secret":secret,"code":code,
            "redirect_uri":redirect_uri(),"grant_type":"authorization_code"})
        if res.status_code != 200:
            raise HTTPException(502,"Falha na autorização do Google; verifique o cliente OAuth e o URI de retorno")
        tokens=res.json()
        refresh=tokens.get("refresh_token")
        access=tokens.get("access_token")
        if not refresh or not access:
            raise HTTPException(409,"O Google não retornou acesso offline; reautorize o canal")
        who=client.get(YOUTUBE_API+"/channels",params={"part":"snippet","mine":"true"},
                       headers={"Authorization":"Bearer "+access})
        if who.status_code!=200:
            raise HTTPException(502,"Não foi possível identificar o canal autorizado")
        channels=who.json().get("items") or []
        if len(channels)!=1:
            raise HTTPException(409,"Selecione um único canal na autorização do Google")
        identity=channels[0]
    import studio_channel_network as net
    ch=net.get_channel(studio,row["channel_id"])
    yt_id=identity.get("id") or ""
    if not re.fullmatch(r"UC[a-zA-Z0-9_-]{22}",yt_id):
        raise HTTPException(502,"Identificação do canal inválida")
    if ch.get("yt_channel_id") and ch["yt_channel_id"] != yt_id:
        raise HTTPException(409,"O canal Google autorizado difere do ID associado a este cadastro")
    encrypted=token_cipher().encrypt(refresh.encode()).decode()
    channel_name=str((identity.get("snippet") or {}).get("title") or "")[:120]
    with studio.db() as db:
        linked=db.execute("SELECT channel_id FROM channel_network_auth WHERE yt_channel_id=? AND channel_id!=?",
                          (yt_id,row["channel_id"])).fetchone()
        if linked:
            raise HTTPException(409,"Este canal do YouTube já está associado a outro cadastro no Estúdio")
        db.execute("""INSERT OR REPLACE INTO channel_network_auth
           VALUES(?,?,?,?,?)""",(row["channel_id"],yt_id,channel_name,encrypted,iso(now())))
        db.execute("UPDATE channel_network_channels SET yt_channel_id=?,updated_at=? WHERE id=?",
                   (yt_id,iso(now()),row["channel_id"]))
    return {"channel_id":row["channel_id"],"yt_channel_id":yt_id,"name":channel_name}

def authenticated_token(studio,channel_id):
    client_id,secret=client_config()
    with studio.db() as db:
        row=db.execute("SELECT refresh_token_encrypted FROM channel_network_auth WHERE channel_id=?",
                       (channel_id,)).fetchone()
    if not row:
        raise HTTPException(409,"Canal não conectado por Google OAuth")
    try:
        refresh=token_cipher().decrypt(row["refresh_token_encrypted"].encode()).decode()
    except Exception:
        raise HTTPException(409,"Token de canal indisponível: reconecte sua conta")
    with httpx.Client(timeout=25,follow_redirects=False) as client:
        res=client.post(TOKEN_BASE,data={
            "client_id":client_id,"client_secret":secret,"refresh_token":refresh,
            "grant_type":"refresh_token"})
    if res.status_code!=200:
        raise HTTPException(409,"Token de canal expirado/revogado: autorize novamente")
    access=res.json().get("access_token")
    if not access:
        raise HTTPException(409,"Sem token de acesso ao YouTube")
    return access

def linked(studio,channel_id):
    with studio.db() as db:
        row=db.execute("SELECT yt_channel_id,yt_channel_name,granted_at FROM channel_network_auth WHERE channel_id=?",
                       (channel_id,)).fetchone()
    return dict(row) if row else None

def _safe_session_url(value):
    p=urlsplit(value or "")
    return (p.scheme=="https" and p.hostname in ("www.googleapis.com","youtube.googleapis.com")
            and p.path.startswith("/upload/youtube/v3/videos")
            and not p.username and not p.password)

def upload_preflight(studio,channel_id,idea_id):
    import studio_channel_network as net
    channel=net.get_channel(studio,channel_id)
    if not channel["enabled"]:
        raise HTTPException(409,"Agenda deste canal está desativada")
    if os.getenv("STUDIO_NETWORK_UPLOAD_ENABLED")!="1":
        raise HTTPException(409,"Publicação automática desativada por segurança. Ative STUDIO_NETWORK_UPLOAD_ENABLED=1 após configurar OAuth e custos.")
    if not linked(studio,channel_id):
        raise HTTPException(409,"Conecte este canal com Google OAuth")
    with studio.db() as db:
        idea=db.execute("SELECT * FROM channel_network_ideas WHERE id=? AND channel_id=?",
                        (idea_id,channel_id)).fetchone()
    if not idea:
        raise HTTPException(404,"Proposta não encontrada")
    idea=dict(idea)
    if not idea["project_id"]:
        raise HTTPException(409,"Vincule um projeto de vídeo renderizado a esta proposta")
    if not idea["scheduled_at"] or datetime.fromisoformat(idea["scheduled_at"].replace("Z","+00:00")) < now()+timedelta(minutes=30):
        raise HTTPException(409,"Agende publicação futura com no mínimo 30 minutos de antecedência")
    p=studio.get_project(idea["project_id"])
    if p["status"]!="concluído":
        raise HTTPException(409,"O MP4 ainda não foi concluído")
    file=(studio.project_dir(p["id"])/"export"/"video_final.mp4").resolve()
    if not file.is_file() or file.is_symlink() or not (10_000 < file.stat().st_size < 300*1048576):
        raise HTTPException(409,"MP4 não encontrado ou acima do limite do worker atual")
    return idea,channel,p,file

def publish(studio,channel_id,idea_id):
    idea,channel,project,file=upload_preflight(studio,channel_id,idea_id)
    started=iso(now())
    with studio.db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing=db.execute("SELECT status,video_id FROM channel_network_uploads WHERE idea_id=?",(idea_id,)).fetchone()
        if existing:
            raise HTTPException(409,"Essa publicação já foi iniciada: verifique o histórico para evitar upload duplicado")
        db.execute("""INSERT INTO channel_network_uploads
          (idea_id,channel_id,project_id,status,scheduled_at,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?)""",
          (idea_id,channel_id,project["id"],"upload_started",idea["scheduled_at"],started,started))
    # Persist first to prevent duplicates if worker crashes during an upload.
    try:
        access=authenticated_token(studio,channel_id)
        references=json.loads(idea["analysis_json"]).get("source_urls") or []
        # Primary original packaging is uploaded now; the other two
        # title/thumbnail pairs are saved for YouTube Studio's *manual*
        # native A/B test. The public Data API cannot initiate that test.
        try:
            import studio_publishing_pack
            package=studio_publishing_pack.metadata_for_upload(studio,idea_id)
        except Exception as exc:
            print("[channel-upload] packaging fallback: "+type(exc).__name__,flush=True)
            package={"title":idea["title"],"tags":[],
                     "thumbnail_path":None,
                     "description":(idea["hook"]+"\n\n"+
                         "\n".join(references[:5]))[:4500]}
        details={
            "snippet":{
                "title":package["title"][:100],
                "description":package["description"][:4500],
                "tags":package.get("tags") or [],
                "categoryId":"27",
                "defaultLanguage":channel["language"]
            },
            "status":{"privacyStatus":"private","publishAt":idea["scheduled_at"],
                      "selfDeclaredMadeForKids":False}
        }
        with httpx.Client(timeout=35,follow_redirects=False) as c:
            init=c.post(RESUMABLE_API,
                params={"part":"snippet,status","uploadType":"resumable"},
                headers={"Authorization":"Bearer "+access,
                         "Content-Type":"application/json",
                         "X-Upload-Content-Type":"video/mp4",
                         "X-Upload-Content-Length":str(file.stat().st_size)},
                json=details)
        if init.status_code not in (200,201) or not _safe_session_url(init.headers.get("location","")):
            raise RuntimeError("YouTube não iniciou o upload de forma verificável")
        session=init.headers["location"]
        def chunks(handle):
            while True:
                data=handle.read(1024*1024)
                if not data: break
                yield data
        with file.open("rb") as handle, httpx.Client(timeout=300,follow_redirects=False) as c:
            uploaded=c.put(session,content=chunks(handle),
                           headers={"Authorization":"Bearer "+access,
                                    "Content-Type":"video/mp4",
                                    "Content-Length":str(file.stat().st_size)})
        if uploaded.status_code not in (200,201):
            raise RuntimeError("Não foi possível confirmar upload no YouTube. Não repetiremos automaticamente.")
        yt_id=uploaded.json().get("id") or ""
        if not re.fullmatch(r"[\w-]{11}",yt_id):
            raise RuntimeError("YouTube não retornou ID válido; verifique a conta antes de qualquer repetição")
        with studio.db() as db:
            db.execute("""UPDATE channel_network_uploads
              SET status='scheduled_youtube',video_id=?,updated_at=? WHERE idea_id=?""",
              (yt_id,iso(now()),idea_id))
            db.execute("UPDATE channel_network_ideas SET status='scheduled_youtube' WHERE id=?",(idea_id,))
        # Thumbnail is best-effort and failure never causes a second video upload.
        warning=""
        image_path=Path(package.get("thumbnail_path") or
                         Path(studio.DATA)/idea["thumbnail_local"]).resolve()
        thumb_root=(Path(studio.DATA)/"channel_network_thumbs").resolve()
        if image_path.is_relative_to(thumb_root) and image_path.is_file():
            try:
                data=image_path.read_bytes()
                with httpx.Client(timeout=45,follow_redirects=False) as c:
                    result=c.post(THUMBNAIL_API,params={"videoId":yt_id},
                                  headers={"Authorization":"Bearer "+access,"Content-Type":"image/jpeg"},
                                  content=data)
                if result.status_code not in (200,201):
                    warning="Thumbnail não definida automaticamente; aplicar no YouTube Studio."
            except (httpx.HTTPError,OSError):
                warning="Thumbnail pendente; upload do vídeo confirmado."
        return {"youtube_video_id":yt_id,"url":"https://www.youtube.com/watch?v="+yt_id,
                "scheduled_at":idea["scheduled_at"],"status":"scheduled_youtube",
                "thumbnail_warning":warning}
    except Exception as exc:
        # A crash after sending bytes can leave a video on YouTube.
        # Mark it unknown; never auto retry, so duplicates cannot be generated.
        with studio.db() as db:
            db.execute("""UPDATE channel_network_uploads
               SET status='requires_reconciliation',error=?,updated_at=? WHERE idea_id=?""",
               (type(exc).__name__+": "+str(exc)[:270],iso(now()),idea_id))
        if isinstance(exc,HTTPException):
            raise
        raise HTTPException(502,"Falha de upload; verifique o YouTube Studio antes de qualquer tentativa de repetição")

def uploader_worker(studio):
    while True:
        try:
            if os.getenv("STUDIO_NETWORK_UPLOAD_ENABLED")=="1":
                with studio.db() as db:
                    rows=db.execute("""SELECT i.id,i.channel_id
                      FROM channel_network_ideas i
                      JOIN channel_network_channels c ON c.id=i.channel_id
                      JOIN channel_network_auth a ON a.channel_id=c.id
                      LEFT JOIN channel_network_uploads u ON u.idea_id=i.id
                      WHERE c.enabled=1 AND i.status='scheduled_draft'
                        AND i.project_id!='' AND i.scheduled_at!=''
                        AND u.idea_id IS NULL
                      ORDER BY i.scheduled_at ASC LIMIT 4""").fetchall()
                for row in rows:
                    try:
                        publish(studio,row["channel_id"],row["id"])
                    except Exception as exc:
                        print("[channel-upload] skipped: "+type(exc).__name__,flush=True)
        except Exception as exc:
            print("[channel-upload] queue scan unavailable: "+type(exc).__name__,flush=True)
        time.sleep(90)

def install(studio):
    db_setup(studio)

    @studio.app.get("/api/network/channels/{cid}/connection")
    def connection(cid:str):
        import studio_channel_network as net
        net.get_channel(studio,cid)
        return {"channel_id":cid,"connected":bool(linked(studio,cid)),
                "identity":linked(studio,cid),
                "oauth_ready":bool(os.getenv("GOOGLE_YT_CLIENT_ID") and os.getenv("GOOGLE_YT_CLIENT_SECRET")),
                "upload_enabled":os.getenv("STUDIO_NETWORK_UPLOAD_ENABLED")=="1",
                "redirect_uri":redirect_uri() if os.getenv("GOOGLE_YT_CLIENT_ID") else None}

    @studio.app.get("/api/network/channels/{cid}/connect")
    def connect(cid:str):
        return RedirectResponse(connect_url(studio,cid),status_code=302)

    @studio.app.get("/oauth/youtube/channel")
    def callback(state:str="",code:str="",error:str=""):
        # This route is Basic-auth exempt; only one-time 256-bit state protects it.
        if error:
            return HTMLResponse("<h1>Permissão não concedida</h1><p>O canal não foi conectado.</p>",status_code=403)
        result=complete_auth(studio,state,code)
        return RedirectResponse("/rede?connected="+result["channel_id"],status_code=303)

    @studio.app.post("/api/network/channels/{cid}/ideas/{idea_id}/link")
    def attach_project(cid:str,idea_id:str,payload:LinkProject):
        import studio_channel_network as net
        net.get_channel(studio,cid)
        project=studio.get_project(payload.project_id)
        if project.get("status")!="concluído":
            raise HTTPException(409,"Conclua o MP4 antes de vincular a ideia")
        with studio.db() as db:
            row=db.execute("SELECT project_id FROM channel_network_ideas WHERE id=? AND channel_id=?",(idea_id,cid)).fetchone()
            if not row:
                raise HTTPException(404,"Proposta não encontrada")
            if row["project_id"] and row["project_id"]!=payload.project_id:
                raise HTTPException(409,"Esta ideia já tem outro projeto associado")
            db.execute("UPDATE channel_network_ideas SET project_id=? WHERE id=?",
                       (payload.project_id,idea_id))
        return {"linked":True,"project_id":payload.project_id,
                "note":"Apenas associação; não faz upload."}

    @studio.app.post("/api/network/channels/{cid}/ideas/{idea_id}/publish")
    def submit(cid:str,idea_id:str):
        return publish(studio,cid,idea_id)

    @studio.app.get("/api/network/uploads")
    def uploads():
        with studio.db() as db:
            rows=db.execute("""SELECT idea_id,channel_id,project_id,
              video_id,status,scheduled_at,error,created_at,updated_at
              FROM channel_network_uploads ORDER BY updated_at DESC LIMIT 50""").fetchall()
        return {"uploads":[dict(x) for x in rows]}

    @studio.app.on_event("startup")
    def start_worker():
        global THREAD_STARTED
        with THREAD_LOCK:
            if not THREAD_STARTED:
                threading.Thread(target=uploader_worker,args=(studio,),
                                 name="youtube-scheduled-upload",daemon=True).start()
                THREAD_STARTED=True

if __name__=="__main__":
    assert not _safe_session_url("https://evil.example/upload/youtube/v3/videos")
    assert _safe_session_url("https://www.googleapis.com/upload/youtube/v3/videos?upload_id=abc")
    appfile=Path("/app/app.py")
    if not appfile.exists():raise SystemExit("Missing app.py")
    mark="\nimport studio_channel_publish as _channel_publish\n"
    code=appfile.read_text(encoding="utf-8")
    if mark not in code:
        appfile.write_text(code+mark+
                           '_channel_publish.install(__import__("sys").modules[__name__])\n',
                           encoding="utf-8")
