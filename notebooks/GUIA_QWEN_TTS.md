# Estúdio IA — Teste gratuito de voz Qwen3-TTS (5 idiomas)

Notebook: [Abrir o teste no Google Colab](https://colab.research.google.com/github/amendokim/estudio/blob/main/notebooks/qwen3_tts_5_idiomas.ipynb)

## O que é

Um teste manual e pontual do **Qwen3-TTS 0.6B CustomVoice**, sob licença Apache-2.0, sem cadastro de API e sem pagar por caracteres gerados. O modelo é baixado na sessão do Google Colab, onde o áudio é gerado. Não altera a implantação do Estúdio IA no Railway.

Idiomas: pt, en, es, fr e de. Cinco amostras de documentário. Voz inicial: Aiden; alternativamente Ryan. Vozes padrão de outras línguas podem ter sotaque não nativo.

## Usar

1. Abra o notebook no Google Colab usando a mesma conta que utiliza para seus testes.
2. Vá em **Ambiente de execução > Alterar o tipo de ambiente de execução > T4 GPU**. Se não houver GPU gratuita, retorne em outra sessão; não ative opções pagas.
3. Use **Ambiente de execução > Executar tudo**.
4. Ouça cada amostra nos players que aparecem abaixo da célula principal.
5. Baixe o arquivo `teste_qwen3_estudio_ia.zip`, que contém cinco áudios WAV e um relatório JSON.
6. Caso queira testar outra voz, altere `SPEAKER = "Aiden"` para `"Ryan"` e execute somente a célula das amostras novamente.

## Critérios de comparação

Avaliar individualmente para cada idioma: sotaque, pronúncia de nomes, pausas, expressividade, ausência de artefatos, duração e tempo de geração. Não inferir que um idioma tem qualidade nativa apenas por constar da lista de idiomas suportados.

## Limitações

O Colab gratuito não garante GPU, disponibilidade nem execução em segundo plano. É adequado ao experimento, **não** a uma fábrica de 5 × 30 minutos por dia. Para produção automatizada será necessário um serviço de inferência (servidor CPU/GPU próprio ou dedicado), limites de fila e monitoramento de tempo/custo. Os pesos do Qwen3-TTS são gratuitos, mas hardware e energia não necessariamente.

Fontes:

- [Qwen3-TTS, repositório oficial](https://github.com/QwenLM/Qwen3-TTS)
- [Modelo 0.6B CustomVoice — licença e idiomas](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice)
- [Política de recursos do Colab gratuito](https://research.google.com/colaboratory/intl/pt-BR/faq.html)
