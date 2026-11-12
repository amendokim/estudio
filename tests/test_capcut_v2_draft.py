"""Offline integration smoke: create an editable pyCapCut draft from synthetic media."""
from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path

def ffmpeg(*args):
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y",*map(str,args)],
                   check=True,timeout=100)

def main():
    with tempfile.TemporaryDirectory(prefix="capcut-v2-") as temp:
        root=Path(temp)
        for folder in ("03_midias_locais","01_audio","02_legendas","04_plano","drafts"):
            (root/folder).mkdir(parents=True)
        base=root/"03_midias_locais"/"video_final_base.mp4"
        video=root/"03_midias_locais"/"v2_001_video.mp4"
        still=root/"03_midias_locais"/"v2_002_image.png"
        voice=root/"01_audio"/"narracao_completa.mp3"
        ffmpeg("-f","lavfi","-i","testsrc=size=320x180:rate=24:duration=12",
               "-c:v","libx264","-pix_fmt","yuv420p",base)
        ffmpeg("-f","lavfi","-i","testsrc=size=320x180:rate=24:duration=7",
               "-c:v","libx264","-pix_fmt","yuv420p",video)
        ffmpeg("-f","lavfi","-i","color=c=blue:size=320x180",
               "-frames:v","1",still)
        ffmpeg("-f","lavfi","-i","sine=frequency=350:duration=12",
               "-c:a","libmp3lame",voice)
        (root/"04_plano"/"manifesto.json").write_text(json.dumps(
            {"version":"capcut-handoff-v1","project_title":"Teste Editorial Paraguai",
            "project_id":"12345678-1234-1234-1234-123456789abc","ratio":"16:9"}))
        rows=[
          {"cena":1,"inicio_s":"0","fim_s":"6","arquivo_local_no_zip":
          "03_midias_locais/v2_001_video.mp4","v2_illustration":False,
          "texto_narrado":"Fronteira entre Brasil e Paraguai"},
          {"cena":2,"inicio_s":"6","fim_s":"12","arquivo_local_no_zip":
          "03_midias_locais/v2_002_image.png","v2_illustration":True,
          "texto_narrado":"Ilustracao conceitual"},
        ]
        with (root/"04_plano"/"ordem_cenas.csv").open("w",newline="") as fp:
            writer=csv.DictWriter(fp,fieldnames=list(rows[0]))
            writer.writeheader();writer.writerows(rows)
        (root/"02_legendas"/"legendas.srt").write_text(
            "1\n00:00:00,000 --> 00:00:03,000\nFronteira Brasil Paraguai\n\n")
        spec=importlib.util.spec_from_file_location(
            "local_importer",Path(__file__).resolve().parents[1]/
            "studio_capcut_local_importer.py")
        importer=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(importer)
        importer.ROOT=root
        name=importer.make_draft(drafts_dir=root/"drafts",allow_subtitles=True)
        drafts=list((root/"drafts").rglob("draft_content.json"))
        if not drafts:
            raise RuntimeError("Nenhum rascunho editável foi criado")
        text=drafts[0].read_text(encoding="utf-8")
        if "B-roll real" not in text and "B-rolls" not in text:
            raise RuntimeError("Trilha de B-roll não encontrada no rascunho")
        print("[capcut-v2] INTEGRATION TEST PASS",name,
              "drafts=",len(drafts),"size=",len(text),flush=True)

if __name__=="__main__":
    main()
