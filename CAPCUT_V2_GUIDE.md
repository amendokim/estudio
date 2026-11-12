# Estúdio IA — CapCut V2 / Media Fusion

## O que já faz

- Pesquisa **Pixabay** (vídeos + fotografias), **Pexels** (vídeos + fotografias, quando `PEXELS_API_KEY` estiver definida), **Wikimedia Commons**, **Openverse** (mídia com licença aberta) e **NASA** (somente temas espaciais).
- Mantém regras de correspondência editorial e recusa downloads de domínios fora da lista permitida; **não** baixa vídeos do YouTube.
- Opcional: **1 chamada** ao Gemini para elaborar pesquisas visuais por cena e **até 2 imagens** de IA para lacunas — cada um exige ação consciente do proprietário; nenhum custo de Gemini é criado por padrão.
- Armazena planos em `<pasta do projeto>/capcut_v2/plan.json`, mídia preparada separadamente, autor/crédito/página/licença.
- Inclui na exportação ZIP arquivos-fonte separados para edição no CapCut.
- Importador local `IMPORTAR_NO_CAPCUT.py`: cria rascunho com faixas independentes para base, vídeos, ilustrações IA, textos, narração, músicas licenciadas, legendas, opacidade animada e cartelas de capítulos. Faz cortes reais e não aplica um zoom cego em todas as cenas.
- Não publica no YouTube, não altera vídeos originais e não renderiza na Railway.
- CI gera um rascunho usando arquivos sintéticos e confere o projeto JSON com pyCapCut — sem consumir APIs.

## Como usar no Estúdio

1. Acesse `/capcut-v2` no endereço da sua Railway e digite o UUID de um projeto **concluído**.
2. Informe a senha já utilizada no Estúdio.
3. Escolha 4, 8 ou 12 cenas para preparar.
4. Opcional: escolha a direção por Gemini (1 chamada paga) e/ou até duas ilustrações IA (outras até 2 chamadas pagas). Padrão: **desligadas**.
5. Clique **Preparar mídias**. Consulte o status e examine `missing_scenes`, `sources` e `scenes` na resposta.
6. Clique **Baixar pacote editável CapCut**. O ZIP usa `/api/capcut/projects/<uuid>/download`, que inclui os recursos V2 disponíveis.

**Fontes:** Pixabay, Wikimedia Commons e Openverse não exigem chave adicional; Pexels exige `PEXELS_API_KEY`. NASA é consultada quando o tema indica astronomia/espaço. Cada serviço pode limitar acessos. Mídias com direitos ou autorização incertos exigem revisão humana, principalmente Openverse, Wikimedia e NASA. Não há garantia de identificação visual por IA: pesquisa textual e curadoria automática são aproximadas.

## Como abrir no PC Windows

1. Instale CapCut Desktop e Python 3.11+. Confira em CapCut → Configurações o diretório dos rascunhos.
2. Baixe o ZIP, **descompacte inteiramente** e mantenha a pasta em local permanente (os caminhos são absolutos).
3. Feche o CapCut.
4. Execute `python -m pip install pycapcut` e depois `py IMPORTAR_NO_CAPCUT.py` no diretório descompactado (ou use o .bat). Se o formato do CapCut mudar, o instalador pode precisar ser atualizado.
5. Reabra CapCut; o rascunho aparecerá com o título `Estudio IA - ...`.
6. Revise **imagem x fala**, títulos, direitos/creditos e volume. Arquivos originais já removidos serão representados por trechos do MP4 antigo até encontrar fonte melhor.
7. Exporte **manualmente** no CapCut após conferir e aprovar.

## Áudio profissional

O importador **não adiciona** a trilha sintetizada antiga, porque o resultado foi considerado ruim. Se possuir música instrumental com licença apropriada, coloque o arquivo em:

`06_trilha_profissional/trilha_licenciada.mp3`

dentro do ZIP já descompactado, antes de executar o importador. Ele adicionará uma faixa editável com volume inicial baixo e fades. Só inclua música para a qual tenha direitos de uso no YouTube; verifique obrigações de crédito. A trilha deve cobrir o vídeo; caso seja mais curta, o restante fica sem cama musical até ser ajustado manualmente.

## Limites de segurança e qualidade

- Por ação: até **12 cenas**, **80 MB** de mídias preparadas, **11 MB** por arquivo, **2 imagens IA** e **1 planejamento IA** opcional.
- **Nenhuma chamada paga por padrão**. Limites de terceiros podem ser alterados pelo fornecedor.
- Rascunhos de pyCapCut não são uma integração oficial; a compatibilidade precisa ser validada na versão do CapCut instalada pelo usuário.
- Um teste automatizado de rascunho demonstra validade estrutural, **não garante qualidade profissional**. Essa depende de cenas encontradas, da qualidade das fontes e de revisão editorial.
- Ações de atualização são aditivas ao projeto, sem substituição do MP4 original.
- O teste gratuito de 8 cenas usa `STUDIO_CAPCUT_V2_FREE_PILOT=1` e `STUDIO_CAPCUT_V2_PILOT_PROJECT=<uuid>`, com registro idempotente no banco persistente.

## API

- `GET /capcut-v2`: interface
- `POST /api/capcut-v2/projects/{pid}/prepare`: exige cabeçalho `X-Studio-Password` com senha configurada em `STUDIO_PASSWORD`
- `GET /api/capcut-v2/projects/{pid}/status`
- `GET /api/capcut/projects/{pid}/download`: ZIP completo

```json
{"max_scenes": 8, "ai_images": 0, "ai_direction": false}
```

## Próximas melhorias depois da validação local

Busca semântica por embeddings/quadros em vez de correspondência textual; cena a cena aprovada por humano; camada de mapas animados; templates CapCut gerados a partir de rascunhos visuais aprovados; biblioteca profissional licenciada de trilhas; detecção automática de descompasso entre narração e imagem.
