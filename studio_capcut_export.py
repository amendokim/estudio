"""Portable CapCut handoff: ZIP of existing editable studio assets.

Not a CapCut-native project: CapCut does not offer a generally available,
guaranteed import contract for arbitrary timeline project files. The handoff
contains narration, per-scene voice clips, captions, existing local media,
a detailed chronological edit plan, source credits, and missing-asset links.

ZIP is produced directly into the HTTP response (no archive written to the
small Railway volume). Existing MP4s, DB rows and original assets are never
mutated; no paid AI, no remote stock download and no upload to CapCut.
"""
from __future__ import annotations

import csv
import io
import json
import os
import queue
import re
import shutil
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

MAX_ITEMS = 220
MAX_BYTES = 500 * 1024 * 1024
MAX_ITEM_BYTES = 105 * 1024 * 1024
BUFFER_BYTES = 65536
SAFE_EXT = {".mp3",".wav",".m4a",".aac",".ogg",
            ".mp4",".webm",".mov",".mkv",
            ".png",".jpg",".jpeg",".webp",".svg",
            ".srt",".ass",".vtt",".json",".txt"}
VIDEO_EXT = {".mp4",".webm",".mov",".mkv"}
AUDIO_EXT = {".mp3",".wav",".m4a",".aac",".ogg"}
IMAGE_EXT = {".png",".jpg",".jpeg",".webp",".svg"}
PROJECT_ID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")

def _clean(text, cap=160):
    return re.sub(r"[\r\n\t]+"," ",str(text or "")).strip()[:cap]

def _csv_bytes(rows,headers):
    text=io.StringIO(newline="")
    writer=csv.DictWriter(text,fieldnames=headers,extrasaction="ignore",
                          lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff"+text.getvalue()).encode("utf-8")

def _resolve_inside(candidate,root):
    """No traversal, symlinks or outside-of-volume references."""
    path=Path(candidate)
    if not path.is_absolute():
        path=Path(root)/path
    root=Path(root).resolve()
    try:
        check=path.resolve(strict=True)
    except (OSError,RuntimeError):
        return None
    if not check.is_relative_to(root) or check.is_symlink() or not check.is_file():
        return None
    if path.is_symlink() or path.suffix.lower() not in SAFE_EXT:
        return None
    try:
        stat=check.stat()
    except OSError:
        return None
    if not 0 < stat.st_size <= MAX_ITEM_BYTES:
        return None
    return check

def _entry(path,arc):
    if (any(x in arc for x in ("..","\\","\0"))
            or arc.startswith("/") or "\r" in arc or "\n" in arc):
        return None
    return (path,arc)

def _gather(studio,project):
    pid=project["id"]
    if not PROJECT_ID.fullmatch(pid):
        raise HTTPException(400,"ID do projeto inválido")
    if project.get("status")!="concluído":
        raise HTTPException(409,"Prepare o pacote somente depois que o projeto estiver concluído")
    root=Path(studio.project_dir(pid)).resolve()
    media=Path(studio.MEDIA).resolve()
    if not root.is_relative_to(media) or not root.is_dir():
        raise HTTPException(404,"Arquivos do projeto não estão disponíveis neste servidor")
    rows=[]
    licenses=[]
    files={}
    missing=[]
    def add(path,arc):
        resolved=_resolve_inside(path,media)
        if not resolved or not resolved.is_relative_to(root):
            return False
        if not _entry(resolved,arc):
            return False
        if arc in files:
            return True
        files[arc]=resolved
        return True

    def optional_directory(directory,prefix,maxcount=140):
        if not directory.is_dir() or directory.is_symlink():
            return
        count=0
        for child in sorted(directory.iterdir()):
            if count>=maxcount:break
            if child.is_file() and child.suffix.lower() in SAFE_EXT:
                if add(child,f"{prefix}/{child.name}"):count+=1

    # Narration is important even if scene assets have been cleaned.
    voice=project.get("narration") or ""
    if voice:
        try:
            voicefile=studio.safe_local_media(voice)
        except (RuntimeError,ValueError,OSError,HTTPException):
            voicefile=None
        if voicefile and not add(voicefile,"01_audio/narracao_completa"+Path(voicefile).suffix.lower()):
            missing.append("Narração completa não está armazenada localmente")
    optional_directory(root/"audio","01_audio/por_cena",140)
    # Existing soundtrack, if present and local to this project.
    bg=project.get("soundtrack")
    if bg:
        try:background=studio.safe_local_media(bg)
        except (RuntimeError,ValueError,OSError,HTTPException):background=None
        if background:
            add(background,"01_audio/trilha"+Path(background).suffix.lower())
    for subtitle in ("legendas.srt","legendas.ass","subtitles.srt"):
        add(root/"export"/subtitle,f"02_legendas/{subtitle}")

    # Include pre-rendered ORIGINAL assets and already cached stock if present.
    # Never make a new download of someone else's video in the export endpoint.
    optional_directory(root/"uploads","03_midias_locais",100)
    optional_directory(root/"clips","03_midias_cache",80)
    optional_directory(root/"media","03_midias_cache_extra",80)
    optional_directory(root/"export"/"longform_segments","03_tomadas_renderizadas",120)
    # Portable baseline so the local CapCut importer can reconstruct a
    # fully timed timeline even if older stock clips were not cached.
    # Audio is muted there; the original voice remains a separate edit track.
    add(root/"export"/"video_final.mp4",
        "03_midias_locais/video_final_base.mp4")

    try:
        import studio_semantic_media as semantic
        import studio_automated_direction as director
        channel=semantic.channel_context(studio,project)
        board,_=semantic.load_plan(studio,project,allow_ai=False,write=False)
        shots=board.get("scenes") or []
    except (ImportError,RuntimeError,ValueError):
        channel={"name":"Estúdio IA","niche":"","language":""}
        shots=[]
        director=None
    start=0.
    scenes=project.get("scenes") or []
    for i,scene in enumerate(scenes,1):
        seconds=max(0.25,float(scene.get("duration") or 8))
        shot=shots[i-1] if i<=len(shots) else {}
        try:
            rec=director.classify_scene(project,channel,scene,shot) if director else {}
        except (KeyError,ValueError,TypeError):
            rec={}
        provider=scene.get("media_provider") or ""
        local_name=""
        local=scene.get("media_local") or ""
        if local:
            try:
                path=studio.safe_local_media(local)
            except (RuntimeError,ValueError,OSError,HTTPException):
                path=None
            if path:
                path=Path(path)
                arc=f"03_midias_locais/cena_{i:03d}_{path.name}"
                if add(path,arc):
                    local_name=arc
        if not local_name and local:
            missing.append(f"Cena {i:03d}: arquivo local indisponível")
        url=scene.get("media_url") or ""
        if url and not local_name:
            missing.append(f"Cena {i:03d}: B-roll referenciado por URL; não havia cópia local separada")
        rows.append({
            "cena":i,
            "inicio_s":round(start,3),
            "fim_s":round(start+seconds,3),
            "duracao_s":round(seconds,3),
            "texto_narrado":_clean(scene.get("text"),1800),
            "busca_visual":_clean(scene.get("query"),170),
            "intencao_visual":_clean(shot.get("visual_intent"),70),
            "origem_recomendada":_clean(rec.get("preferred_source"),65),
            "estilo_de_corte":_clean(rec.get("cut_style"),65),
            "cortes_sugeridos":rec.get("cut_target") or 1,
            "origem_usada":_clean(provider,50),
            "arquivo_local_no_zip":local_name,
            "url_do_clipe_para_referencia":url if urlsplit(url).scheme=="https" else "",
            "pagina_de_licenca":_clean(scene.get("source_page"),450),
            "credito":_clean(scene.get("author"),230)
        })
        licenses.append({
            "cena":i,"origem":provider,
            "pagina_da_fonte":_clean(scene.get("source_page"),450),
            "credito":_clean(scene.get("author"),230),
            "arquivo_no_zip":local_name})
        start+=seconds

    # Add V2-approved real clips / clearly labeled AI illustrations as editable
    # source assets, rather than falling back to the previously rendered MP4.
    v2_plan_path = root/"capcut_v2"/"plan.json"
    v2_plan = None
    if v2_plan_path.is_file():
        try:
            read_plan=json.loads(v2_plan_path.read_text(encoding="utf-8"))
            if (read_plan.get("version")=="capcut-media-fusion-v2" and
                    read_plan.get("project_id")==pid):
                v2_plan=read_plan
                for record in read_plan.get("scenes",[]):
                    index=int(record.get("scene") or 0)
                    if not 1<=index<=len(rows) or record.get("status")!="ready":
                        continue
                    filename=Path(str(record.get("file") or "")).name
                    if not filename or filename!=record.get("file"):
                        continue
                    original=root/"capcut_v2"/filename
                    if not original.is_file():
                        continue
                    arc=f"03_midias_locais/v2_{index:03d}_{filename}"
                    if add(original,arc):
                        rows[index-1]["arquivo_local_no_zip"]=arc
                        rows[index-1]["origem_usada"]=str(record.get("source") or "")[:50]
                        rows[index-1]["v2_illustration"]=bool(record.get("illustration"))
                        rows[index-1]["v2_media_type"]=str(record.get("type") or "")
                        licenses.append({"cena":index,
                            "origem":record.get("source") or "",
                            "pagina_da_fonte":record.get("page") or "",
                            "credito":record.get("author") or "",
                            "arquivo_no_zip":arc,
                            "licenca":record.get("license") or "",
                            "licenca_url":record.get("license_url") or ""})
        except (OSError,ValueError,TypeError,KeyError) as exc:
            print("[capcut-export] V2 assets not included: "+
                  type(exc).__name__,flush=True)
            v2_plan=None

    # Deduplicate and size-bound the whole package. Preserve audio/captions,
    # then scene assets in directory order; don't silently remove essential data.
    prioritized=sorted(files.items(),key=lambda kv:
        (0 if kv[0].startswith(("01_audio","02_legendas")) else 1,kv[0]))
    included={}
    skipped=[]
    total=0
    for arc,path in prioritized:
        if len(included)>=MAX_ITEMS:
            skipped.append(arc+" (maximum number of files)");continue
        try:
            size=path.stat().st_size
        except OSError:
            skipped.append(arc+" (missing)");continue
        if total+size > MAX_BYTES:
            skipped.append(arc+" (package size limit)");continue
        included[arc]=path
        total+=size

    missing.extend("Omitido por segurança: "+x for x in skipped)
    media_count=sum(name.startswith("03_midias") for name in included)
    meta={
        "version":"capcut-handoff-v1",
        "generated_at":datetime.now(timezone.utc).isoformat(),
        "project_id":pid,
        "project_title":project.get("title") or "",
        "channel":channel,
        "ratio":project.get("ratio"),
        "scene_count":len(rows),
        "timeline_seconds":round(start,2),
        "local_asset_count":media_count,
        "files_local":len(included),
        "bytes_local":total,
        "missing_assets":missing,
        "important":"Portable media handoff, NOT a native CapCut editable project. "
                    "Clips and narration need importing and placement on timeline.",
        "rights":"Licences/attributions must be checked before publication. "
                 "The pack contains only files already stored for this project.",
        "capcut_v2_assets": bool(v2_plan),
        "v2_editable_source_count":sum(
            1 for row in rows if str(row.get("arquivo_local_no_zip") or "").startswith(
                "03_midias_locais/v2_"))
    }
    instructions=(
      "ESTÚDIO IA — PACOTE DE EDIÇÃO PARA CAPCUT\n\n"
      "O QUE TEM NESTE ZIP\n"
      "01_audio/: narração completa e trechos disponíveis, sem gerar áudio novo.\n"
      "02_legendas/: legendas SRT e ASS quando salvas.\n"
      "03_midias_locais/: clipes e gráficos já salvos para o projeto.\n"
      "04_plano/: ordem das cenas, tempos, fontes e roteiro.\n\n"
      "COMO USAR (PC recomendado; também é possível no celular)\n"
      "1. Descompacte o ZIP. Abra um novo projeto 16:9 no CapCut.\n"
      "2. Importe os vídeos/imagens de 03_midias_locais e os áudios de 01_audio.\n"
      "3. Coloque a narração completa na trilha de voz.\n"
      "4. Siga ordem_cenas.csv para inserir as mídias nos intervalos corretos.\n"
      "5. Importe legendas.srt, quando disponível, ou copie o texto do roteiro.\n"
      "6. Adicione transições, efeitos Pro e vinhetas e revise a sincronização.\n"
      "7. Confira creditos_e_licencas.csv antes de qualquer publicação.\n"
      "8. No CapCut Desktop, também pode criar um rascunho via IMPORTAR_NO_CAPCUT.py.\n\n"
      "IMPORTANTE\n"
      "- Não é um projeto nativo (.ccp) do CapCut, nem importa cortes sozinho.\n"
      "- Arquivos de bancos não guardados localmente aparecem na coluna de URL.\n"
      "- A exportação NÃO baixa vídeos do YouTube nem gera cópias adicionais.\n"
      "- Não alteramos o vídeo final que já foi aprovado.\n"
      "- Os recursos CapCut Pro dependem dos direitos e limites da sua conta.\n"
      "- Proteja os materiais licenciados: não redistribua stock isoladamente.\n"
    )
    metadata={
      "04_plano/LEIA_PRIMEIRO.txt":instructions.encode("utf-8"),
      "04_plano/manifesto.json":json.dumps(meta,ensure_ascii=False,indent=2).encode("utf-8"),
      "04_plano/ordem_cenas.csv":_csv_bytes(rows,list(rows[0].keys()) if rows else
         ["cena","inicio_s","fim_s","duracao_s","texto_narrado"]),
      "04_plano/creditos_e_licencas.csv":_csv_bytes(
          licenses,["cena","origem","pagina_da_fonte","credito","arquivo_no_zip"]),
      "04_plano/roteiro.txt":("\n\n".join(f"[{i+1:03d}] {x.get('text') or ''}"
          for i,x in enumerate(scenes))+"\n").encode("utf-8"),
    }
    if v2_plan:
        metadata["04_plano/plano_capcut_v2.json"]=json.dumps(
            v2_plan,ensure_ascii=False,indent=2).encode("utf-8")
    if missing:
        metadata["04_plano/arquivos_para_revisar.txt"]=(
            "\n".join(missing)+"\n").encode("utf-8")

    # Desktop-only native draft bridge: a user explicitly runs this on
    # the machine with CapCut installed. Server never writes local draft files.
    local_importer=Path("/app/studio_capcut_local_importer.py")
    if local_importer.is_file() and local_importer.stat().st_size<150000:
        metadata["IMPORTAR_NO_CAPCUT.py"]=local_importer.read_bytes()
        metadata["IMPORTAR_NO_CAPCUT_WINDOWS.bat"]=(
            "@echo off\r\n"
            "cd /d \"%~dp0\"\r\n"
            "echo Criar rascunho CapCut Desktop - primeiro FECHE o CapCut.\r\n"
            "echo Requer Python e pycapcut.\r\n"
            "py -3 IMPORTAR_NO_CAPCUT.py\r\n"
            "pause\r\n").encode("utf-8")
        metadata["IMPORTAR_NO_CAPCUT_MAC.command"]=(
            "#!/bin/sh\n"
            "cd \"$(dirname \"$0\")\" || exit 1\n"
            "echo 'Feche o CapCut antes de criar o rascunho.'\n"
            "python3 IMPORTAR_NO_CAPCUT.py\n"
            "echo 'Pressione Enter para sair'; read answer\n").encode("utf-8")
        metadata["LEIA_COMO_CRIAR_RASCUNHO.txt"]=(
            "CAPCUT DESKTOP (Windows/macOS) - RASCUNHO EDITÁVEL EXPERIMENTAL\n\n"
            "1. Instale Python 3.11+ se não tiver.\n"
            "2. Execute no terminal: python -m pip install pycapcut\n"
            "   No macOS use python3.\n"
            "3. Feche o CapCut Desktop. Descompacte e mantenha esta pasta.\n"
            "4. Windows: clique IMPORTAR_NO_CAPCUT_WINDOWS.bat.\n"
            "   macOS: rode python3 IMPORTAR_NO_CAPCUT.py no Terminal.\n"
            "5. Reabra o CapCut e procure pelo novo rascunho 'Estudio IA - ...'.\n"
            "6. O V2 utiliza clipes/fotos originais em faixas editáveis quando disponíveis.\n"
            "   B-rolls originais somente onde arquivos separados estão disponíveis.\n"
            "7. Não exclua nem mova esta pasta depois de importar: caminhos\n"
            "   dos materiais referenciam os arquivos extraídos.\n\n"
            "Não é uma integração oficial do CapCut; versões recentes podem\n"
            "exigir reajustes. O script não modifica rascunhos existentes.\n"
            "No celular este método não funciona.\n").encode("utf-8")
        meta["capcut_desktop_importer_included"]=True
        meta["native_draft_compatibility"]="experimental_unverified_on_user_device"
    return meta,included,metadata

class _QueueSink:
    """Non-seekable ZIP sink with bounded RAM buffering and abort support."""
    def __init__(self,outbox,abort):
        self.outbox=outbox
        self.abort=abort
    def write(self,data):
        raw=memoryview(data)
        for at in range(0,len(raw),BUFFER_BYTES):
            chunk=bytes(raw[at:at+BUFFER_BYTES])
            while True:
                if self.abort.is_set():
                    raise ConnectionError("Download cancelled")
                try:
                    self.outbox.put(chunk,timeout=.25)
                    break
                except queue.Full:
                    continue
        return len(data)
    def flush(self):
        pass

def _stream_zip(entries,notes):
    outbox=queue.Queue(maxsize=6)
    abort=threading.Event()
    sentinel=object()
    def producer():
        try:
            with zipfile.ZipFile(_QueueSink(outbox,abort),"w",
                    compression=zipfile.ZIP_STORED,allowZip64=True) as z:
                for filename,blob in notes.items():
                    z.writestr(filename,blob)
                for filename,path in entries.items():
                    if abort.is_set():break
                    try:
                        with path.open("rb") as handle, z.open(filename,"w",force_zip64=True) as target:
                            while True:
                                chunk=handle.read(BUFFER_BYTES)
                                if not chunk:break
                                target.write(chunk)
                    except OSError:
                        if abort.is_set():break
                        z.writestr("04_plano/AVISO_ARQUIVO_INDISPONIVEL_"+str(len(filename))+".txt",
                            (filename+" ficou indisponível durante o download.").encode())
        except (ConnectionError,ValueError,RuntimeError,OSError) as exc:
            # Connection failures shouldn't trigger a background rerender.
            if not abort.is_set():
                print("[capcut-export] Stream stopped: "+type(exc).__name__,flush=True)
        finally:
            while not abort.is_set():
                try:
                    outbox.put(sentinel,timeout=.25)
                    break
                except queue.Full:pass
    thread=threading.Thread(target=producer,name="capcut-zip-stream",daemon=True)
    thread.start()
    try:
        while True:
            item=outbox.get()
            if item is sentinel:break
            yield item
    finally:
        abort.set()

def self_test():
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder)
        original=root/"note.txt"
        original.write_text("Canal Código Fudoshin - edit plan",encoding="utf-8")
        assert _resolve_inside(original,root)==original
        assert _resolve_inside(root/"../escape.txt",root) is None
        assert _entry(original,"04_plano/a.csv")
        assert not _entry(original,"../secret.txt")
        binary=b"".join(_stream_zip({"03_midias_locais/note.txt":original},
                                    {"04_plano/test.txt":b"ok"}))
        with zipfile.ZipFile(io.BytesIO(binary)) as z:
            assert z.read("04_plano/test.txt")==b"ok"
            assert z.read("03_midias_locais/note.txt").startswith(b"Canal")
    print("[capcut-export] SELF-TEST PASS safe paths, no-temp ZIP stream, edit handoff",flush=True)

def install(studio):
    @studio.app.on_event("startup")
    def verify_existing_capcut_handoff():
        # Read-only validation of the two owner-approved example projects.
        # This makes it possible to confirm actual asset availability without
        # downloading ZIPs or consuming more Railway disk.
        for project_id in (
            "b625c7bc-3542-4233-9901-7567501996c0",
            "8c855a6d-c95a-4642-bf9c-a46074404935",
        ):
            try:
                project=studio.get_project(project_id)
                report,local_files,_=_gather(studio,project)
                print("[capcut-export] READY project="+project_id[:8]+
                      " local_files="+str(report["files_local"])+
                      " local_MB="+str(round(report["bytes_local"]/1048576,1))+
                      " missing_references="+str(len(report["missing_assets"]))+
                      " scene_count="+str(report["scene_count"]),flush=True)
            except Exception as exc:
                print("[capcut-export] CHECK FAILED project="+project_id[:8]+
                      " exception="+type(exc).__name__,flush=True)

    @studio.app.get("/api/capcut/projects/{pid}")
    def summary(pid:str):
        if not PROJECT_ID.fullmatch(pid):raise HTTPException(400,"Projeto inválido")
        p=studio.get_project(pid)
        info,_,_= _gather(studio,p)
        return {"package":info,"download_url":f"/api/capcut/projects/{pid}/download",
                "disk_copy_created":False,"requires_capcut_import":True}

    @studio.app.get("/api/capcut/projects/{pid}/download")
    def download(pid:str):
        if not PROJECT_ID.fullmatch(pid):raise HTTPException(400,"Projeto inválido")
        p=studio.get_project(pid)
        info,entries,notes=_gather(studio,p)
        # Avoid repeated downloads by caches or intermediaries.
        title=re.sub(r"[^a-z0-9-]+","-",
                     str(p.get("title") or "video").casefold())[:45].strip("-")
        name=f"estudio-capcut-{title or pid[:8]}.zip"
        headers={"Content-Disposition":f'attachment; filename="{name}"',
                 "Cache-Control":"private, no-store, max-age=0",
                 "X-Content-Type-Options":"nosniff"}
        return StreamingResponse(_stream_zip(entries,notes),
                                 media_type="application/zip",headers=headers)

if __name__=="__main__":
    self_test()
    app=Path("/app/app.py")
    if not app.exists():raise SystemExit("Missing core studio app.py")
    marker="\nimport studio_capcut_export as _capcut_export\n"
    code=app.read_text(encoding="utf-8")
    if marker not in code:
        app.write_text(code+marker+
            '_capcut_export.install(__import__("sys").modules[__name__])\n',
            encoding="utf-8")
