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
COPY studio_cinematic_pilot.py /app/studio_cinematic_pilot.py
RUN python /app/studio_cinematic_pilot.py
COPY studio_quality.py /app/studio_quality.py
RUN python /app/studio_quality.py
COPY studio_youtube_policy.py /app/studio_youtube_policy.py
RUN python -m py_compile /app/studio_youtube_policy.py && python /app/studio_youtube_policy.py
COPY studio_edit_polish.py /app/studio_edit_polish.py
RUN python -m py_compile /app/studio_edit_polish.py && python /app/studio_edit_polish.py
COPY studio_channel_network.py /app/studio_channel_network.py
RUN python -m py_compile /app/studio_channel_network.py && python /app/studio_channel_network.py
COPY studio_channel_dashboard.py /app/studio_channel_dashboard.py
RUN python -m py_compile /app/studio_channel_dashboard.py && python /app/studio_channel_dashboard.py
COPY studio_channel_publish.py /app/studio_channel_publish.py
RUN python -m py_compile /app/studio_channel_publish.py && python /app/studio_channel_publish.py
COPY studio_channel_pipeline.py /app/studio_channel_pipeline.py
RUN python -m py_compile /app/studio_channel_pipeline.py && python /app/studio_channel_pipeline.py
COPY studio_channel_bootstrap.py /app/studio_channel_bootstrap.py
RUN python -m py_compile /app/studio_channel_bootstrap.py && python /app/studio_channel_bootstrap.py
COPY studio_channel_api_probe.py /app/studio_channel_api_probe.py
RUN python -m py_compile /app/studio_channel_api_probe.py && python /app/studio_channel_api_probe.py
COPY studio_gemini_pilot.py /app/studio_gemini_pilot.py
RUN python -m py_compile /app/studio_gemini_pilot.py && python /app/studio_gemini_pilot.py
COPY studio_five_min_pilot.py /app/studio_five_min_pilot.py
RUN python -m py_compile /app/studio_five_min_pilot.py && python /app/studio_five_min_pilot.py
COPY studio_render_quota_guard.py /app/studio_render_quota_guard.py
RUN python -m py_compile /app/studio_render_quota_guard.py && python /app/studio_render_quota_guard.py
COPY studio_visual_direction.py /app/studio_visual_direction.py
RUN python -m py_compile /app/studio_visual_direction.py && python /app/studio_visual_direction.py
COPY studio_editorial_preview.py /app/studio_editorial_preview.py
RUN python -m py_compile /app/studio_editorial_preview.py && python /app/studio_editorial_preview.py
COPY studio_semantic_media.py /app/studio_semantic_media.py
RUN python -m py_compile /app/studio_semantic_media.py && python /app/studio_semantic_media.py
COPY studio_dynamic_montage.py /app/studio_dynamic_montage.py
RUN python -m py_compile /app/studio_dynamic_montage.py && python /app/studio_dynamic_montage.py
COPY studio_semantic_stock_smoke.py /app/studio_semantic_stock_smoke.py
RUN python -m py_compile /app/studio_semantic_stock_smoke.py && python /app/studio_semantic_stock_smoke.py
COPY studio_automated_direction.py /app/studio_automated_direction.py
RUN python -m py_compile /app/studio_youtube_policy.py /app/studio_automated_direction.py && python /app/studio_automated_direction.py
COPY studio_publishing_pack.py /app/studio_publishing_pack.py
RUN python -m py_compile /app/studio_publishing_pack.py && python /app/studio_publishing_pack.py
COPY studio_control_room.py /app/studio_control_room.py
RUN python -m py_compile /app/studio_control_room.py && python /app/studio_control_room.py
COPY studio_capcut_local_importer.py /app/studio_capcut_local_importer.py
RUN python -m py_compile /app/studio_capcut_local_importer.py
COPY studio_capcut_export.py /app/studio_capcut_export.py
RUN python -m py_compile /app/studio_capcut_export.py && python /app/studio_capcut_export.py
COPY studio_music_director.py /app/studio_music_director.py
RUN python -m py_compile /app/studio_music_director.py && python /app/studio_music_director.py
COPY studio_music_chapters.py /app/studio_music_chapters.py
RUN python -m py_compile /app/studio_music_chapters.py && python /app/studio_music_chapters.py
COPY studio_fudoshin_30min_pilot.py /app/studio_fudoshin_30min_pilot.py
RUN python -m py_compile /app/studio_fudoshin_30min_pilot.py && python /app/studio_fudoshin_30min_pilot.py
COPY studio_elevenlabs_tts.py /app/studio_elevenlabs_tts.py
RUN python -m py_compile /app/studio_elevenlabs_tts.py && python /app/studio_elevenlabs_tts.py
COPY studio_voice_curator.py /app/studio_voice_curator.py
RUN python -m py_compile /app/studio_voice_curator.py && python /app/studio_voice_curator.py
COPY studio_free_voice_test.py /app/studio_free_voice_test.py
RUN python -m py_compile /app/studio_free_voice_test.py && python /app/studio_free_voice_test.py
COPY studio_google_wavenet.py /app/studio_google_wavenet.py
RUN python -m py_compile /app/studio_google_wavenet.py && python /app/studio_google_wavenet.py
COPY studio_dynamic_preview_pilot.py /app/studio_dynamic_preview_pilot.py
RUN python -m py_compile /app/studio_dynamic_preview_pilot.py && python /app/studio_dynamic_preview_pilot.py
EXPOSE 8000
COPY cleanup_rendered_tests_once.py /app/cleanup_rendered_tests_once.py
RUN python -m py_compile /app/cleanup_rendered_tests_once.py
COPY cleanup_all_old_projects_keep_five_min.py /app/cleanup_all_old_projects_keep_five_min.py
RUN python -m py_compile /app/cleanup_all_old_projects_keep_five_min.py
CMD ["sh", "-c", "python /app/cleanup_old_projects_once.py && python /app/cleanup_rendered_tests_once.py && python /app/cleanup_all_old_projects_keep_five_min.py && exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
