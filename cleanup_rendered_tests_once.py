"""One-time owner-authorized removal of prior test VIDEO outputs only.
Keeps project records, scripts, audio, thumbnails, database, and account secrets.
Never touches new projects, unknown files or paths outside allowlisted IDs.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path("/app/data").resolve()
PROJECTS = ROOT / "projects"
OLD_TEST_IDS = (
    "aad807ab-18c8-44a5-b287-8ac97ce1a4c2",
    "2b7fd986-2634-4e48-8c7f-aff527dff651",
    "4f79c143-e8fe-4e01-8953-45e6bc4f80e1",
)
MARKER = ROOT / ".old_video_cleanup_2026_11_07.json"
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv", ".m4v"}
_UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


def main():
    if MARKER.is_file():
        print("[video-cleanup] Already completed once; no action", flush=True)
        return
    total_bytes, removed = 0, []
    for pid in OLD_TEST_IDS:
        if not _UUID.fullmatch(pid):
            raise RuntimeError("Invalid allowlisted project ID")
        target = PROJECTS / pid
        if not target.is_dir() or target.is_symlink():
            continue
        if target.resolve() != PROJECTS.resolve() / pid:
            raise RuntimeError("Unexpected project directory")
        for dirpath, dirs, filenames in os.walk(target, topdown=True, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(dirpath) / d).is_symlink()]
            for name in filenames:
                p = Path(dirpath) / name
                if p.suffix.lower() not in VIDEO_SUFFIXES or p.is_symlink():
                    continue
                try:
                    if not p.resolve().is_relative_to(target.resolve()):
                        continue
                    size = p.stat().st_size
                    p.unlink()
                    total_bytes += size
                    removed.append({"project_id": pid, "file": str(p.relative_to(ROOT)),
                                    "size_bytes": size})
                except FileNotFoundError:
                    continue
    report = {"date": datetime.now(timezone.utc).isoformat(),
              "files_deleted": len(removed),
              "bytes_reclaimed": total_bytes,
              "projects_affected": sorted({x["project_id"] for x in removed}),
              "deleted": removed}
    tmp = MARKER.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(MARKER)
    print("[video-cleanup] Removed %d old video files, reclaimed %.1f MB; "
          "all scripts, project rows and narration preserved" %
          (len(removed), total_bytes / 1048576), flush=True)


if __name__ == "__main__":
    main()
