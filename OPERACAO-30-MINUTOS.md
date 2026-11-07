# Plano operacional — Fábrica de Documentários

## Situação atual (novembro de 2026)

O Estúdio IA está em Railway com um volume de 500 MB. Os vídeos curtos funcionam.
A interface `/fabrica` contém fila persistente, roteiros em lote opcionais,
análise de recursos, inspeção por capítulos e controles de arquivamento externo.

**Não habilite renderização de 30 minutos no volume de 500 MB.** O limite
inicial `STUDIO_FACTORY_MAX_RENDER_MINUTES=5` é intencional. A estimativa de
espaço de trabalho para 30 min é ~2,8 GB, mas o valor real varia por mídia.

## Motor de vídeos longos

`studio_longform.py` contém o renderizador FFmpeg ativado para projetos a partir
de 6 minutos, *apenas* quando a análise de capacidade da Fábrica permite:

- Clips persistidos por cena e verificados por hash (retoma após falhas);
- Narração TTS por cena com checkpoint e tentativas limitadas;
- Montagem final diretamente dos clipes, sem duplicar o vídeo combinado;
- Cobertura mínima de 80% de vídeo selecionado;
- Divisão lógica em capítulos, com endpoint de inspeção;
- Em sucesso, elimina clipes intermediários, preservando o MP4 final;
- Se falhar, mantém os clipes gerados para retomada.

Não publica vídeos automaticamente no YouTube. O uso de imagens de arquivo precisa
respeitar licenças, atribuição, coerência e política de conteúdo reutilizado.

## Variáveis opcionais de produção

- `STUDIO_FACTORY_MAX_RENDER_MINUTES=60`: **somente em máquina com disco suficiente**.
- `STUDIO_FACTORY_MIN_FREE_MB=150`: limite de segurança adicional.
- `STUDIO_LONGFORM_PRESET=veryfast`: preset de codificação (`ultrafast` a `fast`).
- `STUDIO_LONGFORM_MEDIA_SCAN_LIMIT=64`: máximo de cenas de mídia por execução.
- `STUDIO_LONGFORM_KEEP_SEGMENTS=0`: retém intermediários apenas durante falhas.
- `STUDIO_FACTORY_WORKER_ENABLED=1`: trabalhador da fila.
- `STUDIO_FACTORY_DAILY_SCRIPT_LIMIT=5`: limite diário de roteiros via Gemini.
- `GEMINI_API_KEY`: opcional; sem chave, lote de roteiros não executa e não cobra.
- `GEMINI_MODEL=gemini-2.5-flash-lite`: modelo econômico permitido.

O uso do ChatGPT Plus não inclui créditos de API externa.

## Bucket S3 ou Cloudflare R2 (desligado por padrão)

O adaptador `studio_storage.py` não realiza uploads sem credenciais.
Configurar as variáveis no serviço, nunca no repositório nem na conversa:

- `STUDIO_S3_BUCKET`
- `STUDIO_S3_ENDPOINT` (HTTPS)
- `STUDIO_S3_REGION=auto`
- `STUDIO_S3_ACCESS_KEY_ID`
- `STUDIO_S3_SECRET_ACCESS_KEY`
- `STUDIO_AUTO_ARCHIVE=0` (mantenha 0 até autorizar a rotina automática)

A interface permite arquivar MP4 concluído com confirmação e cria um link de
download assinado por 10 minutos. **A cópia local não é apagada automaticamente.**
O custo do bucket é cobrado pelo provedor escolhido.

## Próxima migração para VPS de baixo custo

1. Contratar VPS com CPU e espaço adequados à carga de FFmpeg, **após** verificar orçamento.
2. Fazer backup seguro de `/app/data`, incluindo SQLite, projetos e vídeos.
3. Restaurar o mesmo aplicativo e os dados no novo servidor, com TLS e senha.
4. Reconfigurar o endpoint MCP/OAuth do plugin para o novo domínio.
5. Testar um vídeo de 10 minutos e um de 30 minutos; medir CPU, RAM, disco, tempo
   de renderização, custos, voz, coerência das imagens e falhas.
6. Somente depois habilitar produção de 5–10 vídeos por dia, com publicação revisada.

Não há compromisso de capacidade de 150–300 vídeos/mês antes desses testes.
