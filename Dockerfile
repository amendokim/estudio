FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY src_bundle.b64 /tmp/src_bundle.b64
RUN python -c "import base64,lzma,tarfile,io,pathlib; b=base64.b64decode(pathlib.Path('/tmp/src_bundle.b64').read_text()); tarfile.open(fileobj=io.BytesIO(lzma.decompress(b))).extractall('/app')" && rm /tmp/src_bundle.b64
COPY studio_commons_video.py /app/studio_commons_video.py
RUN python /app/studio_commons_video.py
RUN mkdir -p /app/data
COPY cleanup_old_projects_once.py /app/cleanup_old_projects_once.py
COPY studio_factory.py /app/studio_factory.py
RUN python /app/studio_factory.py
COPY studio_longform.py /app/studio_longform.py
RUN python /app/studio_longform.py
COPY studio_storage.py /app/studio_storage.py
RUN python /app/studio_storage.py
EXPOSE 8000
CMD ["sh", "-c", "python /app/cleanup_old_projects_once.py && exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
