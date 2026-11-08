"""Storage guard for the single owner-approved five-minute 540p preview.

No global duration limit changes beyond the explicit project's factory estimate.
Short-form FFmpeg/clip fetch operations are stopped before the disk reaches the
last 90 MB. This is a guarded experiment on a 500 MB Railway volume, not an
endorsement for routine 5-minute production on small storage.
"""
from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

TARGET="8c855a6d-c95a-4642-bf9c-a46074404935"
START_MB=400
RESERVE_MB=90
_LOCAL=threading.local()

def free_mb(studio):
    return shutil.disk_usage(studio.DATA).free/1048576

def install(studio):
    old_autopilot=studio.autopilot_task
    old_render=studio.render_task
    old_ffmpeg=studio.run_ffmpeg
    old_fetch=studio.fetch_clip

    def guard(studio_, minimum=RESERVE_MB):
        available=free_mb(studio_)
        if available<minimum:
            raise RuntimeError("Piloto 540p interrompido para preservar o volume: "
                               f"{available:.0f} MB livres; limite preventivo {minimum} MB")

    def protected_ffmpeg(*args, **kwargs):
        if getattr(_LOCAL,"active",False):
            guard(studio)
        return old_ffmpeg(*args,**kwargs)

    def protected_fetch(scene,pid,*args,**kwargs):
        if pid==TARGET:
            guard(studio)
        result=old_fetch(scene,pid,*args,**kwargs)
        if pid==TARGET:
            guard(studio)
        return result

    def protected_autopilot(pid):
        if pid!=TARGET:
            return old_autopilot(pid)
        if os.getenv("STUDIO_RENDER_1080P","0")=="1":
            raise RuntimeError("Preview experimental precisa manter 540p")
        guard(studio,START_MB)
        _LOCAL.active=True
        try:
            print(f"[compact-guard] BEGIN 5-minute 540p pilot with {free_mb(studio):.1f}MB free",
                  flush=True)
            return old_autopilot(pid)
        finally:
            _LOCAL.active=False
            print(f"[compact-guard] END available={free_mb(studio):.1f}MB",flush=True)

    def protected_render(pid):
        if pid != TARGET:
            return old_render(pid)
        guard(studio)
        _LOCAL.active=True
        try:
            return old_render(pid)
        finally:
            _LOCAL.active=False

    studio.run_ffmpeg=protected_ffmpeg
    studio.fetch_clip=protected_fetch
    studio.autopilot_task=protected_autopilot
    studio.render_task=protected_render

if __name__=="__main__":
    app=Path("/app/app.py")
    if not app.is_file():
        raise SystemExit("Missing app.py")
    marker="\nimport studio_render_quota_guard as _quota_guard\n"
    src=app.read_text(encoding="utf-8")
    if marker not in src:
        app.write_text(src+marker+
                       '_quota_guard.install(__import__("sys").modules[__name__])\n',
                       encoding="utf-8")
