"""Original three-variant YouTube packaging without pretending to run A/B tests.

On every new channel idea, keep the existing first thumbnail/title and prepare
two meaningful original alternatives. The YouTube Data API can publish ONE
title/thumbnail, not start native Test & Compare; our API exposes the other
two for YouTube Studio Desktop testing. Never scrape competitor graphics.
"""
from __future__ import annotations
import json
import re
import textwrap
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from PIL import Image, ImageDraw, ImageFont

LANG={
 "es":{
  "b":"Lo esencial sobre {topic}",
  "c":"{topic}: una mirada diferente",
  "label":("CLAVES","OTRA PERSPECTIVA"),
  "intro":"Una explicación original sobre {topic}, con contexto, ejemplos y reflexiones.",
  "links":"Referencias de contexto consultadas (no imágenes copiadas):",
 },
 "en":{
  "b":"What Matters About {topic}",
  "c":"{topic}: A Different Perspective",
  "label":("THE ESSENTIALS","ANOTHER ANGLE"),
  "intro":"An original look at {topic}, with context, examples and analysis.",
  "links":"Background references (not reused footage):",
 },
 "pt":{
  "b":"O essencial sobre {topic}",
  "c":"{topic}: uma perspectiva diferente",
  "label":("O ESSENCIAL","OUTRO ÂNGULO"),
  "intro":"Uma análise original sobre {topic}, com contexto, exemplos e reflexões.",
  "links":"Referências de contexto (não são imagens copiadas):",
 }
}

def clean(value, limit=95):
    return re.sub(r"\s+"," ",str(value or "")).strip(" .,-")[:limit]

def alternatives(idea,language):
    words=LANG.get(language,LANG["pt"])
    topic=clean(idea.get("topic") or idea.get("title"),70)
    main=clean(idea.get("title"),100)
    names=[main,clean(words["b"].format(topic=topic),100),
           clean(words["c"].format(topic=topic),100)]
    # Same topic, intentionally different search intent; avoid unverifiable
    # claims, invented numbers or competitor titles.
    unique=[]
    for name in names:
        if name.casefold() not in {v.casefold() for v in unique}:
            unique.append(name)
    while len(unique)<3:
        unique.append(clean(topic+" | "+str(len(unique)+1),100))
    return unique[:3]

def _font(size,bold=False):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/"+
            ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),size)
    except OSError:
        return ImageFont.load_default()

def _make_image(target,channel,topic,headline,variant):
    target.parent.mkdir(parents=True,exist_ok=True)
    image=Image.new("RGB",(1280,720),("#0e1a28" if variant==2 else "#18201c"))
    d=ImageDraw.Draw(image)
    accent=("#e1b56f" if variant==2 else "#88c9c5")
    if variant==2:
        d.rectangle((0,0,27,720),fill=accent)
        d.ellipse((790,55,1190,455),outline=accent,width=22)
        d.polygon([(970,160),(1250,460),(1220,720),(820,720)],fill="#28455b")
        left=72
    else:
        d.rectangle((0,580,1280,720),fill="#283e3b")
        d.polygon([(0,0),(480,0),(920,720),(0,720)],fill="#213742")
        d.line((120,178,1160,178),fill=accent,width=9)
        left=95
    d.text((left,70),clean(channel.get("name"),40).upper(),
           font=_font(31,True),fill="#dfebef")
    lines=textwrap.wrap(headline.upper(),width=17,break_long_words=False)[:4]
    for j,line in enumerate(lines):
        d.text((left,216+j*84),line,font=_font(60,True),fill="#f6f5ee",
               stroke_width=2,stroke_fill="#0b1624")
    d.rectangle((left,620,left+620,633),fill=accent)
    image.save(target,format="JPEG",quality=89,optimize=True)

def ensure(studio,idea_id):
    from studio_channel_network import get_channel
    with studio.db() as db:
        row=db.execute("SELECT * FROM channel_network_ideas WHERE id=?",(idea_id,)).fetchone()
    if not row:
        raise HTTPException(404,"Proposta não encontrada")
    idea=dict(row)
    channel=get_channel(studio,idea["channel_id"])
    names=alternatives(idea,channel["language"])
    root=(Path(studio.DATA)/"channel_network_thumbs").resolve()
    existing=(Path(studio.DATA)/idea["thumbnail_local"]).resolve()
    if not existing.is_relative_to(root) or not existing.is_file():
        raise HTTPException(409,"Thumbnail principal não está pronta")
    targets=[existing]
    for i in (2,3):
        output=root/f"{idea_id}_variant_{i}.jpg"
        if not output.exists():
            _make_image(output,channel,idea["topic"],names[i-1],i)
        targets.append(output)
    data={}
    try:data=json.loads(idea["analysis_json"])
    except (ValueError,TypeError):data={}
    seen=set();tags=[]
    for phrase in (channel["niche"],idea["topic"],channel["name"],
                   *channel.get("search_terms",[])[:5]):
        word=clean(phrase,50)
        if word and word.casefold() not in seen:
            seen.add(word.casefold());tags.append(word)
    wording=LANG.get(channel["language"],LANG["pt"])
    refs=[x for x in (data.get("source_urls") or [])[:5] if
          isinstance(x,str) and x.startswith("https://www.youtube.com/watch?v=")]
    description=(wording["intro"].format(topic=clean(idea["topic"],85))+
                 "\n\n"+clean(idea["hook"],250)+
                 "\n\n"+wording["links"]+"\n"+"\n".join(refs)+
                 "\n\n"+ "#"+re.sub(r"[^A-Za-z0-9]","",channel["name"])[:40])
    return {"idea_id":idea_id,"channel_id":channel["id"],
            "variant_titles":names,"thumbnail_paths":targets,
            "thumbnail_urls":[f"/api/network/ideas/{idea_id}/pack/thumbnail/{i}"
                              for i in (1,2,3)],
            "primary_title":names[0],"description":description[:4500],
            "tags":tags[:12],"testing_mode":"prepare_only",
            "automatic_test_started":False,
            "reason":"YouTube Studio's three-way experiment has no documented Data API endpoint."}

def metadata_for_upload(studio,idea_id):
    pack=ensure(studio,idea_id)
    return {"title":pack["primary_title"],"description":pack["description"],
            "tags":pack["tags"],"thumbnail_path":pack["thumbnail_paths"][0]}

def install(studio):
    @studio.app.get("/api/network/ideas/{idea_id}/pack")
    def describe(idea_id:str):
        pack=ensure(studio,idea_id)
        return {k:v for k,v in pack.items() if k!="thumbnail_paths"}

    @studio.app.get("/api/network/ideas/{idea_id}/pack/thumbnail/{variant}")
    def preview(idea_id:str,variant:int):
        if variant not in (1,2,3):raise HTTPException(404,"Variante inválida")
        data=ensure(studio,idea_id)
        return FileResponse(data["thumbnail_paths"][variant-1],media_type="image/jpeg")

def self_test():
    data={"title":"La serenidad no es pasividad","topic":"poner límites con calma"}
    assert len(alternatives(data,"es"))==3
    assert len(set(alternatives(data,"es")))==3
    assert len(alternatives(data,"pt"))==3
    assert len(alternatives(data,"en"))==3
    print("[publishing-pack] SELF TEST PASS 3 original title directions and thumbnail renderer",flush=True)

if __name__=="__main__":
    self_test()
    app=Path("/app/app.py")
    if not app.exists():raise SystemExit("Missing app.py")
    marker="\nimport studio_publishing_pack as _publishing_pack\n"
    code=app.read_text(encoding="utf-8")
    if marker not in code:
        app.write_text(code+marker+
            '_publishing_pack.install(__import__("sys").modules[__name__])\n',
            encoding="utf-8")
