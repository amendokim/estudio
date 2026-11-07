"""Optional S3/R2 archive of completed MP4 files for Meu Estúdio IA.

No cloud spending or uploads unless valid credentials and explicit user action,
or STUDIO_AUTO_ARCHIVE=1. The local MP4 is NEVER deleted automatically.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import RedirectResponse


def enabled() -> bool:
    required = ('STUDIO_S3_BUCKET', 'STUDIO_S3_ENDPOINT',
                'STUDIO_S3_ACCESS_KEY_ID', 'STUDIO_S3_SECRET_ACCESS_KEY')
    return all(os.getenv(key) for key in required)


def client():
    if not enabled():
        raise RuntimeError('Armazenamento externo não configurado. Nenhum upload foi executado.')
    endpoint = os.environ['STUDIO_S3_ENDPOINT'].strip()
    if not endpoint.startswith('https://') or '@' in endpoint or '?' in endpoint:
        raise RuntimeError('O endpoint do armazenamento precisa usar HTTPS.')
    import boto3
    from botocore.config import Config
    return boto3.client('s3', endpoint_url=endpoint,
                        aws_access_key_id=os.environ['STUDIO_S3_ACCESS_KEY_ID'],
                        aws_secret_access_key=os.environ['STUDIO_S3_SECRET_ACCESS_KEY'],
                        region_name=os.getenv('STUDIO_S3_REGION', 'auto'),
                        config=Config(signature_version='s3v4',
                                      connect_timeout=15,read_timeout=90,
                                      retries={'max_attempts':3,'mode':'standard'}))


def bucket() -> str:
    return os.environ['STUDIO_S3_BUCKET']


def init(studio):
    with studio.db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS factory_archives (
          project_id TEXT PRIMARY KEY, object_key TEXT NOT NULL,
          file_sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
          updated_at TEXT NOT NULL)''')


def source_path(studio,pid:str) -> Path:
    uuid.UUID(pid)
    p = studio.get_project(pid)
    if p['status'] != 'concluído':
        raise HTTPException(409,'Somente vídeos concluídos podem ser arquivados')
    path = studio.project_dir(pid)/'export'/'video_final.mp4'
    if not path.is_file() or path.stat().st_size < 1024:
        raise HTTPException(404,'MP4 local não encontrado')
    return path


def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(2*1024*1024),b''):
            h.update(chunk)
    return h.hexdigest()


def upload(studio,pid:str) -> dict:
    path = source_path(studio,pid)
    s3=client()
    h=sha256_file(path)
    with studio.db() as c:
        previous=c.execute('SELECT * FROM factory_archives WHERE project_id=?',(pid,)).fetchone()
    if previous and previous['file_sha256']==h:
        try:
            s3.head_object(Bucket=bucket(),Key=previous['object_key'])
            return {'status':'stored','size_bytes':previous['size_bytes'],
                    'note':'Arquivo já armazenado; nenhuma transferência adicional.'}
        except Exception:
            pass
    key=f'documentarios/{uuid.UUID(pid)}/video_final.mp4'
    from boto3.s3.transfer import TransferConfig
    s3.upload_file(str(path),bucket(),key,
                   ExtraArgs={'ContentType':'video/mp4', 'Metadata':{'sha256':h}},
                   Config=TransferConfig(multipart_threshold=8*1024*1024,
                                         multipart_chunksize=8*1024*1024,
                                         max_concurrency=2,use_threads=True))
    size=path.stat().st_size
    s3.head_object(Bucket=bucket(),Key=key)
    with studio.db() as c:
        c.execute('''INSERT INTO factory_archives(project_id,object_key,file_sha256,size_bytes,updated_at)
          VALUES(?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
          object_key=excluded.object_key,file_sha256=excluded.file_sha256,
          size_bytes=excluded.size_bytes,updated_at=excluded.updated_at''',
          (pid,key,h,size,datetime.now(timezone.utc).isoformat()))
    return {'status':'stored','size_bytes':size,'note':'MP4 arquivado, cópia local preservada.'}


def register(studio):
    init(studio)
    original=studio.render_task

    def archive_after_render(pid):
        result=original(pid)
        if os.getenv('STUDIO_AUTO_ARCHIVE','0')=='1' and enabled():
            try:
                if studio.get_project(pid)['status']=='concluído':
                    print('[archive] '+str(upload(studio,pid)),flush=True)
            except Exception as exc:
                # The local MP4 is a successful render even if remote storage is down.
                print('[archive] upload failed, retaining local MP4: '+str(exc)[:250],flush=True)
        return result

    studio.render_task=archive_after_render

    @studio.app.get('/api/factory/storage')
    def status():
        with studio.db() as c:
            count=c.execute('SELECT COUNT(*) FROM factory_archives').fetchone()[0]
        return {'storage_configured':enabled(),
                'auto_archive':os.getenv('STUDIO_AUTO_ARCHIVE','0')=='1',
                'archived_projects':count,
                'local_free_mb':round(shutil.disk_usage(studio.DATA).free/1048576),
                'local_files_never_deleted_automatically':True}

    @studio.app.post('/api/factory/archive/{pid}')
    def archive(pid:str):
        if not enabled():
            raise HTTPException(409,'Configure um bucket S3/R2 antes de arquivar vídeos')
        return upload(studio,pid)

    @studio.app.get('/api/factory/archive/{pid}/download')
    def archive_download(pid:str):
        uuid.UUID(pid)
        studio.get_project(pid)
        with studio.db() as c:
            row=c.execute('SELECT object_key FROM factory_archives WHERE project_id=?',(pid,)).fetchone()
        if not row:
            raise HTTPException(404,'Projeto não arquivado no armazenamento externo')
        url=client().generate_presigned_url('get_object',
                 Params={'Bucket':bucket(),'Key':row['object_key']},ExpiresIn=600)
        return RedirectResponse(url,status_code=307)


if __name__ == '__main__':
    target=Path('/app/app.py')
    if not target.is_file():
        raise SystemExit('/app/app.py missing')
    marker='\nimport studio_storage as _storage\n'
    s=target.read_text(encoding='utf-8')
    if marker not in s:
        target.write_text(s+marker+'_storage.register(__import__("sys").modules[__name__])\n',
                          encoding='utf-8')