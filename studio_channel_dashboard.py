"""Private multi-channel management UI; all actions are real network endpoints."""
from __future__ import annotations
from fastapi.responses import HTMLResponse
from pathlib import Path

UI = r'''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rede de Canais — Meu Estúdio IA</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{background:#081423;color:#eaf4ff;font:15px/1.5 system-ui,sans-serif;margin:0}
main{max-width:1190px;margin:auto;padding:30px 18px 70px}
h1{font-size:32px;letter-spacing:-1px;margin-bottom:4px}h2{font-size:21px}
a{color:#96d6fa}.sub{color:#a6b8cc}.pane{background:#111f31;border:1px solid #283d54;border-radius:17px;padding:22px;margin:20px 0}
.layout{display:grid;grid-template-columns:340px minmax(0,1fr);gap:20px}
label{display:block;color:#bbcee1;font-size:13px;margin:12px 0}
input,textarea,select{display:block;width:100%;border:1px solid #3b526b;padding:10px;
 border-radius:9px;background:#071425;color:#fff;font:inherit;margin-top:4px}
button{padding:10px 14px;border:0;border-radius:9px;background:#46b9db;color:#09202b;
 font-weight:700;cursor:pointer;margin:6px 4px 6px 0}button:disabled{opacity:.5;cursor:wait}
button.secondary{background:#273e55;color:#def4ff}
.row{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:center}
.card{border:1px solid #2b445c;border-radius:14px;padding:16px;margin-top:12px;background:#0b192b}
.badge{background:#24445b;border-radius:999px;padding:4px 9px;font-size:12px;color:#bbdefa}
.muted{font-size:13px;color:#a5bed2}
.pills{display:flex;flex-wrap:wrap;gap:6px}
.thumb{display:block;width:250px;max-width:100%;border-radius:9px;margin:12px 0}
h3{font-size:17px;margin:6px 0}.error{color:#ffb8ae}
#notice{margin:10px 0;min-height:1.5em}
@media(max-width:810px){.layout{grid-template-columns:1fr}main{padding:16px}}
</style></head><body><main>
<div class="row"><div><a href="/fabrica">← Fábrica de documentários</a>
<h1>Rede de Canais IA</h1><div class="sub">Tendências internacionais • últimos 4 dias • edição e planejamento por canal</div></div>
<span class="badge" id="status">Verificando integração</span></div>
<div id="notice"></div>
<div class="layout">
<section>
<div class="pane"><h2>Cadastrar canal</h2><form id="create">
<label>Nome do canal<input name="name" required minlength="3" placeholder="Histórias de Fronteira"></label>
<label>Nicho<input name="niche" required minlength="3" placeholder="História, mistérios e geografia"></label>
<label>Idioma<select name="language"><option value="pt">Português</option>
<option value="en">English</option><option value="es">Español</option></select></label>
<label>Fuso horário<input name="timezone" value="America/Sao_Paulo"></label>
<label>Quantidade por dia<input name="daily_quantity" type="number" min="1" max="4" value="1"></label>
<label>Horários separados por vírgula<input name="hours" value="18:00" placeholder="09:00,18:00"></label>
<label>Termos de busca em outros idiomas (um por linha)
<textarea name="search_terms" rows="4" placeholder="world history&#10;historia mundial"></textarea></label>
<button type="submit">Adicionar canal</button></form></div>
<div class="pane"><h2>Como funciona</h2>
<p class="muted">A análise estima visualizações por hora e engajamento dos vídeos publicados nas últimas 96 horas.
Não existe uma lista completa de vídeos virais mundiais, por isso combinamos buscas e métricas.</p>
<p class="muted">Títulos, propostas de roteiro e miniaturas são originais. Não copiamos thumbnails nem baixamos vídeos dos concorrentes.</p>
<p class="muted">Conectar os canais e publicar no YouTube exige OAuth e configuração do Google Cloud. Os horários iniciais são apenas sugestões: ajuste e salve a quantidade de cada canal antes de ativar a agenda. Salvar um horário aqui não faz upload.</p>
</div>
</section>
<section>
<div class="pane"><div class="row"><h2>Canais cadastrados</h2><button class="secondary" id="reload">Atualizar</button></div><div id="channels"></div></div>
<div class="pane"><h2>Pesquisar e criar proposta</h2>
<label>Canal<select id="activeChannel"></select></label>
<label>Assunto do novo vídeo<input id="topic" placeholder="Ponte da Amizade e a fronteira Brasil–Paraguai"></label>
<label>Duração planejada<select id="duration">
<option value="2">2 minutos — teste</option><option value="5" selected>5 minutos</option>
<option value="10">10 minutos</option><option value="30">30 minutos</option></select></label>
<button id="order">Quero um vídeo — pesquisar, preparar e reservar horário</button>
<button class="secondary" id="research">Apenas pesquisar e criar proposta</button>
<div id="results"></div></div>
<div class="pane"><h2>Projetos e calendário</h2><p class="muted">Os agendamentos nesta fase são rascunhos locais até que o canal seja autorizado via Google OAuth.</p>
<div id="calendar"></div></div>
</section></div>
</main><script>
const $=(id)=>document.getElementById(id);
async function request(url,init){const response=await fetch(url,init);let data=await response.json();
if(!response.ok)throw Error(data.detail||JSON.stringify(data));return data}
function message(t,error=false){$('notice').textContent=t;$('notice').className=error?'error':'sub'}
function el(tag,text){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n}
function btn(txt,fn,kind){const b=el('button',txt);if(kind)b.className=kind;b.onclick=fn;return b}
async function refresh(){
 try{const [state,items]=await Promise.all([request('/api/network/status'),request('/api/network/channels')]);
 $('status').textContent='Pesquisa '+(state.research_ready?'disponível':'aguardando API')+' • '+items.channels.length+' canais';
 $('channels').replaceChildren();$('activeChannel').replaceChildren();
 for(const c of items.channels){
  const opt=el('option',c.name+' — '+c.niche);opt.value=c.id;$('activeChannel').append(opt);
  const card=el('div');card.className='card';
  card.append(el('h3',c.name));
  card.append(el('p',c.niche+' | '+c.language+' | '+c.timezone));
  card.append(el('p',c.daily_quantity+' vídeo(s) por dia, horários: '+c.hours.join(', ')));
  const count=el('input');count.type='number';count.min='1';count.max='4';count.value=String(c.daily_quantity);
  count.style.maxWidth='95px';
  const times=el('input');times.value=c.hours.join(',');times.style.maxWidth='290px';
  const timetable=el('div');timetable.className='row';
  const howmany=el('label','Vídeos por dia (1 a 4)');howmany.append(count);
  const whattime=el('label','Horários HH:MM (um para cada vídeo)');whattime.append(times);
  timetable.append(howmany,whattime);card.append(timetable);
  card.append(btn('Salvar horários',async()=>{
   try{
    const quantity=Number(count.value);
    const hours=times.value.split(',').map(x=>x.trim()).filter(Boolean);
    await request('/api/network/channels/'+c.id,{method:'PUT',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({daily_quantity:quantity,hours,timezone:c.timezone,enabled:c.enabled})});
    message('Agenda atualizada. Não houve upload no YouTube.');
    refresh();
   }catch(e){message(e.message,true)}
  },'secondary'));
  card.append(el('p','Identificador no YouTube: '+(c.yt_channel_id||'não conectado')));
  const conn=await request('/api/network/channels/'+c.id+'/connection');
  card.append(el('p','OAuth: '+(conn.connected ? 'conectado como '+conn.identity.yt_channel_name :
    'pendente')+(conn.upload_enabled?' • uploads autorizados':' • uploads desligados')));
  if(conn.oauth_ready&&!conn.connected){
   const a=el('a','Conectar conta do YouTube');
   a.href='/api/network/channels/'+c.id+'/connect';a.style.display='block';a.style.margin='6px 0';
   card.append(a);
  }
  const toggle=btn(c.enabled?'Desativar agenda':'Ativar agenda',async()=>{
   try{await request('/api/network/channels/'+c.id,{method:'PUT',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({daily_quantity:c.daily_quantity,hours:c.hours,
    timezone:c.timezone,enabled:!c.enabled})});refresh()}catch(e){message(e.message,true)}},'secondary');
  card.append(toggle);$('channels').append(card)
 }
 if(items.channels.length)await showIdeas($('activeChannel').value);
 }catch(e){message(e.message,true)}
}
async function showIdeas(id){
 try{const data=await request('/api/network/channels/'+id+'/ideas');
 $('calendar').replaceChildren();
 for(const x of data.ideas){
  const card=el('div');card.className='card';card.append(el('h3',x.title));
  card.append(el('p','Ângulo: '+x.angle+' | Estado: '+x.status));
  const pic=el('img');pic.className='thumb';pic.src='/api/network/thumbnails/'+x.id;
  pic.alt='Miniatura original gerada para a proposta';card.append(pic);
  const sources=(x.analysis.source_urls||[]);
  if(sources.length){const p=el('p','Referências analisadas: ');for(const u of sources.slice(0,3)){
   const a=el('a',new URL(u).searchParams.get('v')||'Vídeo');a.href=u;a.target='_blank';
   a.rel='noopener noreferrer';p.append(a,el('span','  '));}card.append(p)}
  card.append(el('p','Duração alvo: '+(x.duration_minutes||5)+' minutos • projeto: '+(x.project_id||'pendente')));
  if(x.scheduled_at){card.append(el('p','Planejado para: '+new Date(x.scheduled_at).toLocaleString('pt-BR')))}
  else{
   const date=el('input');date.type='date';date.value=new Date(Date.now()+86400000).toISOString().slice(0,10);
   const time=el('input');time.type='time';time.value='18:00';date.style.maxWidth='175px';time.style.maxWidth='130px';
   card.append(date,time,btn('Reservar horário',async()=>{try{
    const p=await request('/api/network/channels/'+id+'/schedule',
     {method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({plan_id:x.id,date:date.value,time:time.value})});
    message('Horário salvo: '+p.local_time+'. Ainda não houve upload.');showIdeas(id)
   }catch(e){message(e.message,true)}}))
  }
  if(!x.project_id){
   const t=el('p','Quando estiver pronto um MP4, você poderá vinculá-lo aqui. A publicação só ocorre com OAuth e a opção de upload ativada.');
   t.className='muted';card.append(t);
   const input=el('input');input.placeholder='ID do projeto renderizado';input.style.maxWidth='380px';
   card.append(input,btn('Vincular MP4',async()=>{
    try{
     await request('/api/network/channels/'+id+'/ideas/'+x.id+'/link',
       {method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({project_id:input.value.trim()})});
     message('Projeto vinculado. Upload automático ainda depende da autorização do canal.');
     showIdeas(id);
    }catch(err){message(err.message,true)}
   },'secondary'))
  }
  $('calendar').append(card)
 }
 if(!data.ideas.length)$('calendar').append(el('p','Ainda não há propostas para este canal.'));
 }catch(e){message(e.message,true)}
}
$('create').onsubmit=async(e)=>{
 e.preventDefault();const f=new FormData(e.target);
 const payload={name:f.get('name'),niche:f.get('niche'),language:f.get('language'),
 timezone:f.get('timezone'),daily_quantity:Number(f.get('daily_quantity')),
 hours:String(f.get('hours')).split(',').map(x=>x.trim()).filter(Boolean),
 search_terms:String(f.get('search_terms')).split('\n').map(x=>x.trim()).filter(Boolean)};
 try{await request('/api/network/channels',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify(payload)});message('Canal cadastrado.');refresh()}
 catch(err){message(err.message,true)}
};
$('order').onclick=async()=>{
 const id=$('activeChannel').value;
 if(!id)return message('Cadastre um canal primeiro.',true);
 const button=$('order');button.disabled=true;
 message('Criando ordem por canal e analisando vídeos recentes...');
 try{
  const result=await request('/api/network/channels/'+id+'/order',{
   method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({topic:$('topic').value,
       duration_minutes:Number($('duration').value),
       auto_schedule:true,auto_script:true})
  });
  message('Proposta registrada: '+result.idea.title+' • '+
          result.status+'. '+(result.message||''));
  await showIdeas(id);
 }catch(e){message(e.message,true)}
 finally{button.disabled=false}
};
$('research').onclick=async()=>{
 const id=$('activeChannel').value;if(!id)return message('Cadastre um canal primeiro.',true);
 const b=$('research');b.disabled=true;message('Consultando o YouTube Data API...');
 try{const data=await request('/api/network/channels/'+id+'/research',{
 method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({topic:$('topic').value,create_package:true})});
 $('results').replaceChildren();
 const intro=el('p','Últimas '+data.research.period_hours+' horas • '+data.research.results.length+
 ' referências analisadas. Não é ranking oficial mundial.');
 $('results').append(intro);
 for(const item of data.research.results.slice(0,8)){
  const p=el('p');const a=el('a',item.title);a.href=item.url;a.target='_blank';a.rel='noopener noreferrer';
  p.append(a,el('span',' • '+item.views.toLocaleString('pt-BR')+
   ' visualizações • '+item.views_per_hour_estimate.toLocaleString('pt-BR')+' vis./h estimadas'));
  $('results').append(p)
 }
 if(data.idea)$('results').prepend(el('h3','Nova proposta original: '+data.idea.title));
 message('Pesquisa concluída. Ideia e thumbnail originais registradas.');showIdeas(id)
 }catch(e){message(e.message,true)}finally{b.disabled=false}
};
$('reload').onclick=refresh;
$('activeChannel').onchange=()=>showIdeas($('activeChannel').value);
refresh();
</script></body></html>'''

def install(studio):
    @studio.app.get("/rede")
    def dashboard():
        return HTMLResponse(UI)

if __name__=="__main__":
    assert "Rede de Canais IA" in UI
    appfile=Path("/app/app.py")
    if not appfile.exists():
        raise SystemExit("Missing app.py")
    marker="\nimport studio_channel_dashboard as _channel_dashboard\n"
    src=appfile.read_text(encoding="utf-8")
    if marker not in src:
        appfile.write_text(src+marker+
                           '_channel_dashboard.install(__import__("sys").modules[__name__])\n',encoding="utf-8")
