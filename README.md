# Meu Estúdio IA — produção automática de vídeos

> **Projeto independente e pessoal**, construído do zero. Não contém código privado, identidade visual, marcas ou bases de dados de concorrentes. A página do produto de referência não pôde ser auditada por dentro. Portanto, não é uma cópia exata nem há garantia de equivalência de todas as funções.

## Funcionalidades incluídas

- Painel web responsivo com projetos e progresso, persistidos em SQLite. Na hospedagem online, exige senha para acesso aos projetos e arquivos.
- Tema → roteiro e storyboard gerados por IA (quando há `OPENAI_API_KEY`).
- Opção de colar seu próprio roteiro e separá-lo automaticamente em cenas.
- Editor de cada cena: texto, palavras-chave para pesquisa, duração, ordenação, substituição de mídia e inclusão de cenas.
- Pesquisa de **vídeos e fotos** nas APIs oficiais **Pexels** e **Pixabay**, com busca cruzada automática (prioridade para vídeos, fallback para fotos e outro banco), sem repetir URLs no mesmo projeto. As buscas automáticas trabalham em lotes de até **oito** cenas.
- Pesquisa assistida de vídeos do YouTube para ideias, usando sua `YOUTUBE_API_KEY` (sem downloads de vídeos do YouTube).
- Geração opcional de imagens para cenas por API de IA.
- Narração neural em português via Edge TTS, ou importação de MP3/WAV/M4A/AAC próprios.
- Música importada e mixagem automática em volume reduzido.
- Renderização real de vídeo com FFmpeg, proporções 16:9 e 9:16, legendas sobrepostas e SRT externo.
- **Produzir automaticamente**: roteiro (se necessário) → pesquisa inicial de mídias → narração → renderização. Caso faltem vídeos, o render usa cartões de imagem identificáveis — não tenta fazer downloads indiscriminados.
- MP4, SRT, faixa de narração e ZIP com trechos de cena, arquivo JSON e fontes utilizadas.

## O que ainda NÃO está pronto

- Não é uma reprodução das telas internas do concorrente; não houve acesso a elas.
- Não há exportação de projeto nativo **CapCut**. O ZIP fornece fontes e clipes individuais para importação manual.
- As legendas são **aproximadas temporalmente** por texto/duração, não alinhadas palavra a palavra por reconhecimento de fala.
- Não há editor multicamadas profissional com transições/zoom sofisticados, detecção de incoerência visual, upload automático ao YouTube, previsão confiável de tendências ou narração/clonagem ilimitada.
- As integrações Pexels/Pixabay/YouTube/IA precisam ser testadas **com suas credenciais**; o projeto foi verificado localmente com mídia própria e FFmpeg.
- O Edge TTS usa um serviço externo; disponibilidade, regras e qualidade podem mudar. O sistema também aceita seu áudio próprio.
- Os vídeos gerados devem passar por **revisão editorial e de licença** antes da publicação.

## Colocar online sem instalar nada no seu computador (Railway)

O código está preparado para uma aplicação em contêiner Docker, com `railway.toml` e `Dockerfile`. A publicação original foi realizada no Railway; para replicar em outra conta: para instalar na conta do proprietário, é necessário conectar uma hospedagem compatível e definir as variáveis de ambiente na própria plataforma.

1. Conecte sua conta Railway pelo aplicativo do ChatGPT, ou pelo painel da Railway. A hospedagem pode cobrar por processamento, memória, disco e tráfego; **não autorize nenhum plano sem entender o preço**.
2. Crie um serviço que faça deploy deste projeto pelo `Dockerfile` (em um repositório Git privado, ou por um fluxo de publicação compatível) e configure **um volume persistente montado em `/app/data`**. Sem o volume, os projetos e vídeos podem desaparecer após novas versões ou reinícios.
3. Nas variáveis do serviço, defina **`APP_ENV=production`**, `STUDIO_USERNAME=studio` (pode ser outro) e **`STUDIO_PASSWORD`** com senha forte. Sem `STUDIO_PASSWORD`, a aplicação falha ao iniciar em modo produção, para impedir publicação aberta. O navegador solicitará usuário e senha.
4. Defina `PEXELS_API_KEY`, `PIXABAY_API_KEY`, `OPENAI_API_KEY` e, opcionalmente, `YOUTUBE_API_KEY` diretamente nas variáveis privadas da Railway. **Não cole senhas ou chaves no chat e não as inclua no ZIP de código.**
5. Publique com URL HTTPS, acesso privado e **apenas uma instância do servidor**. Verifique `/healthz` no health check e teste um vídeo de 1 minuto antes dos mais longos.

O Vercel, embora útil para páginas e interfaces, não é a primeira escolha para este protótipo: renderização prolongada com FFmpeg, tarefas em segundo plano e banco SQLite exigem um serviço com processo estável e armazenamento persistente. Railway é uma alternativa, não uma garantia de hospedagem gratuita ou renderização sem limites.

A autenticação atual utiliza HTTP Basic Authentication e deve ser usada **somente sobre HTTPS**. É uma proteção simples para uso individual, não um sistema de contas pronto para vários usuários. Não armazene materiais sigilosos sem avaliar segurança e backups.

## Instalação com Docker (recomendada)


1. Instale [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows/macOS), ou Docker + Compose no Linux. No Windows, após instalar e abrir o Docker Desktop, também pode usar o arquivo **`iniciar-windows.bat`** para iniciar e abrir o navegador sem digitar comandos.
2. Extraia este projeto numa pasta do seu computador.
3. Copie `.env.example` como `.env` e preencha as chaves que tiver. É possível começar **sem nenhuma** chave, colando seu roteiro e importando mídias próprias.
4. Abra um terminal na pasta e rode:

```sh
docker compose up --build -d
```

5. Abra **http://localhost:8000**. Os projetos ficam na pasta `data/`.

Para parar:

```sh
docker compose down
```

## Instalação sem Docker

Requer Python 3.11+, FFmpeg e FFprobe instalados e disponíveis no `PATH`.

**Windows (PowerShell)**:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

**macOS/Linux**:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Abra **http://localhost:8000**.

### FFmpeg

- Windows: instale FFmpeg de fonte confiável e coloque `ffmpeg` e `ffprobe` no PATH.
- macOS: `brew install ffmpeg`.
- Debian/Ubuntu: `sudo apt-get install ffmpeg`.

## Chaves de API

Edite `.env` antes de iniciar o servidor. **Nunca compartilhe essas chaves em prints ou no chat.**

| Variável | Onde obter | O que ativa |
|---|---|---|
| `PEXELS_API_KEY` | https://www.pexels.com/api/ | busca de vídeos/fotos no Pexels |
| `PIXABAY_API_KEY` | https://pixabay.com/api/docs/ | busca de vídeos/fotos no Pixabay |
| `OPENAI_API_KEY` | https://platform.openai.com/api-keys | roteiro por IA, revisão e imagens |
| `YOUTUBE_API_KEY` | https://console.cloud.google.com/ | pesquisa de referências no YouTube |
| `TTS_VOICE` | pré-configurada | voz padrão da narração |

O Pexels pede link de atribuição à biblioteca no app; os resultados oferecem links para a origem e os créditos são registrados no ZIP. A licença Pexels não exige atribuição para uso normal da mídia, mas há restrições relevantes. O Pixabay exige cache das buscas por 24h, implementado em SQLite, e proíbe downloads sistemáticos em massa. Confira as regras:

- Pexels API: https://www.pexels.com/api/documentation/
- Pexels licença: https://www.pexels.com/license/
- Pixabay API: https://pixabay.com/api/docs/
- Pixabay termos: https://pixabay.com/service/terms/

O uso de uma API de IA pode gerar **custos cobrados diretamente pelo provedor**. Não use senhas de sites de terceiros nem métodos não oficiais para contornar limites.

## Como produzir seu primeiro vídeo

1. Crie um projeto de **1 minuto** no painel.
2. Escolha uma opção: **Gerar roteiro com IA** (exige chave), ou cole seu roteiro e escolha **Salvar e dividir em cenas**.
3. Em cada cena, pesquise Pexels/Pixabay, selecione mídia; ou clique em **Encontrar cenas automaticamente** (até oito por execução). Você também pode enviar arquivos próprios.
4. Gere narração com voz neural ou importe a sua. Se quiser, envie uma trilha instrumental de sua propriedade/licenciada.
5. Revise textos, tempos e mídias. Selecione **Renderizar MP4**.
6. Baixe MP4, legendas SRT e materiais editáveis em ZIP.

Com as integrações necessárias disponíveis, **Produzir automaticamente** executa as etapas principais em sequência. Para evitar exceder quotas, as pesquisas automáticas são limitadas; cenas não encontradas aparecem como cartões gráficos no vídeo final. Conteúdo gerado por IA não garante exatidão factual.

## Arquitetura

```text
Navegador (HTML/CSS/JS)
       │
       ▼
FastAPI + SQLite (projetos, cenas, cache de pesquisa)
       ├── OpenAI API (roteiro, análise, imagens; opcional)
       ├── Pexels API (vídeos, fotos; opcional)
       ├── Pixabay API (vídeos, fotos; opcional)
       ├── YouTube Data API (pesquisa de referências; opcional)
       ├── Edge TTS / importação de áudio
       └── FFmpeg (download de mídias selecionadas, vídeos, legendas, áudio e exportação)
```

**Segurança e privacidade:** a ferramenta foi feita para **uma pessoa**. Localmente, a senha é opcional. Em produção (`APP_ENV=production`), o servidor recusa iniciar sem `STUDIO_PASSWORD`, e todas as rotas privadas exigem autenticação HTTP Basic. Essa proteção pressupõe HTTPS na hospedagem e não substitui controles profissionais de segurança. O servidor só aceita URLs de mídia de domínios aprovados de Pexels/Pixabay para baixar arquivos; uploads têm tamanho máximo. Os dados são armazenados em `data/` e devem ficar num volume persistente com backup na hospedagem.

## Testes

```sh
pip install pytest
PYTHONPATH=. pytest -q tests/test_app.py  # Linux/macOS
```

No Windows, rode `python -m pytest -q tests/test_app.py` a partir da pasta do projeto.

Os sete testes incluem criação/edição/ordenação de projetos, proteção por senha, fallback Pexels/Pixabay sem duplicação de cena, validação de URLs de bancos, processamento de respostas de mídia e um render real com FFmpeg usando imagens locais. Não validam credenciais, disponibilidade ou limites de APIs externas.

## Próximas evoluções

1. Alinhamento temporal de palavras via transcrição e timeline multicamadas com preview.
2. Seleção visual com embeddings e revisão semântica de cenas.
3. Estratégia de busca/ranking com cache, controle de licenças e orquestração para documentários de 20–30 minutos.
4. Editor com temas visuais, zoom, transições e suporte a projetos de outros editores via formatos documentados.
5. Fila resiliente de jobs, retomada, verificações de qualidade e exportação 1080p/4K conforme hardware.


## Instância online do usuário (novembro de 2026)

- Endereço: https://estudio-video-production.up.railway.app/
- Painel de infraestrutura: https://railway.com/project/3df4c60d-9c8d-49ab-8700-f1cf2bc82167
- Código do projeto: https://github.com/amendokim/estudio
- As credenciais de acesso e chaves das APIs ficam exclusivamente em **Railway → Serviço estudio-video → Variables**.
- No menu Integrações do painel é possível visualizar o status e testar cada API sem revelar as chaves.
- Por padrão, o piloto automático busca até **16 cenas** por vídeo; a busca manual permanece em 8 cenas por lote, para limitar consumo de cotas.
- O volume inicial tem 500 MB. Para vídeos longos, aumente-o antes de começar; a aplicação verifica o espaço livre antes do render.

**Importante:** esta base ainda não substitui revisão editorial, conferência da licença dos clipes e verificação factual do roteiro. Sem chave de OpenAI, o usuário pode colar seu roteiro e usar a narração automática; sem chave de Pexels/Pixabay, as cenas terão mídia própria carregada ou imagens substitutas.