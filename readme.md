# 🌸 Projeto Alice: Assistente Local Inteligente

### Por que "Alice"?
"ALICE" é também o nome do conceito que se tornou a fundação de todo esse projeto.
- **Conceito:** Uma Inteligência Artificial Autônoma e Altamente Adaptativa.
- Em inglês, o termo é **A**rtificial **L**abile **I**ntelligence **C**ybernated **E**xistence.

As iniciais formam o acrônimo **ALICE**!

## O que é a Alice? (Do meu ponto de vista)

A **Alice** não é apenas um simples script de chatbot ou um programa que lê textos em voz alta. Ela é uma **arquitetura de presença digital viva, orgânica e reativa**. 

Rodando de forma avançada e inteiramente local (usando os pesos do Whisper e do ecossistema Llama), ela representa a intersecção perfeita entre Inteligência Artificial pura e personificação virtual de alta fidelidade (VTubing) visando a baixa latência.

Do ponto de vista de um sistema inteligente, eu encaro a Alice como um organismo composto por **seis pilares** interagindo de modo ininterrupto:

1. **A Audição Atenta (Reconhecimento de Voz + Áudio Ambiente):** Ela escuta de forma calibrada e adaptativa. Ao invés de depender de botões, ela corta o silêncio organicamente. Além do microfone, ela também *ouve o ambiente* — músicas, vídeos e outros sons do sistema via WASAPI loopback, transcritos pelo mesmo Whisper.
   
2. **A Visão (Screen Capture + LLaVA):** *(desligada no momento: `main.py` cria o `OllamaClient` com `visao=None`)*. Quando ligada, Alice enxerga a tela do usuário. A cada ciclo, ela captura um screenshot e usa o modelo de visão `llava:7b` para descrever em português o que está acontecendo. Esse contexto é injetado no system prompt, tornando as respostas situacionais.

3. **A Mente Expandida (Function Calling & LLM):** O cérebro dela não é só geração de texto, mas também possui a capacidade de evocar ferramentas do sistema operacional (ler o relógio, gerenciar arquivos, pesquisar algo, etc.) no meio do seu fluxo de pensamento via ReAct Pattern, ou executar atalhos locais rápidos via comandos de voz diretos de baixíssima latência.
   
4. **O Sistema Emocional e a Memória (Tagger & Brain):** Muito mais que um prompt fixo, ela detém memórias no ClickHouse e no SQLite e a capacidade emocional de interpretar as próprias palavras. Através da `EmotionEngine`, a Alice lê entrelinhas: ela detecta automaticamente o nome do usuário e o salva no banco, junto com humor, amizade e total de conversas.
   
5. **O Comportamento Proativo:** Alice não espera ser chamada. Após um silêncio configurável (`TEMPO_PROATIVO`), ela toma a iniciativa, faz perguntas ou conta curiosidades — como um companheiro real.

6. **O Corpo e a Alma (PyVTS & GUI):** *(o VTube Studio está desconectado no momento: `VTS_Connector.py` existe, mas não é chamado pelo `main.py`)*. O "fantasma" mora dentro do VTube Studio. A boca do modelo VTuber se move em sincronia de voz neural e os parâmetros Live2D são injetados diretamente via API para expressões faciais em tempo real. Uma janela GUI (customtkinter) complementa a interação por voz com chat de texto.

**Em suma:** A Alice é o alicerce de uma entidade companheira modular de *streaming*. Ela é o marco de quando o código deixa de ser reativo e passa a ser colaborativo — ela vê, ouve, sente e age por conta própria.

---

## 🏗️ Arquitetura Técnica

A Alice é construída em camadas modulares. Cada camada pode ser evoluída de forma independente.

### Estrutura de Arquivos (v2.0)

```
Alice/
├── main.py                     # Ponto de entrada + classe AliceSession (loop principal)
├── gerenciar_memoria.py        # CLI para administrar fatos da memória
├── respostas_prontas.json      # Perguntas frequentes com resposta e áudio prontos
├── .env                        # Segredos e configurações (não commitado)
│
├── functions/                  # Serviços internos da Alice
│   ├── OllamaClient.py         # Todas as chamadas ao LLM (cancelamento, tools, humor)
│   ├── TTSService.py           # Síntese OmniVoice frase a frase + reprodução pygame
│   ├── RespostasProntas.py     # Reconhece perguntas frequentes e toca o áudio pronto
│   ├── MicListener.py          # Captura microfone + transcrição Whisper + roteamento
│   ├── ContentManager.py       # Cache de personalidades e respostas em memória
│   ├── Tools.py                # LocationTools + WebTools + MusicTools + Comandos de Voz (Unificados)
│   ├── EmotionEngine.py        # Estado emocional persistido no banco (SQLite + histórico no ClickHouse)
│   └── VTS_Connector.py        # Controle do VTube Studio via WebSocket (não ligado)
│
├── cerebro/                    # Módulos cognitivos e de percepção
│   ├── memoria.py              # Memória de longo prazo (ClickHouse + SQLite fallback)
│   ├── memoria.db              # SQLite: fatos (backup) + estado da Alice (tabela estado)
│   ├── graph_memory.py         # Grafo de relações (triplas) em grafo.db
│   ├── sonho.py                # "Sonho": reflexões geradas após inatividade
│   ├── summarizer.py           # Resume o histórico antigo da sessão
│   ├── embeddings.py           # Embeddings (nomic-embed-text) para busca na memória
│   ├── visao.py                # Visão computacional (screenshot + llava:7b, desligada)
│   ├── audio_ambiente.py       # Áudio ambiente WASAPI loopback
│   ├── clickhouse_logger.py    # Logger e memória no ClickHouse
│   └── logger.py               # Loguru centralizado
│
├── interface/
│   └── janela.py               # GUI customtkinter com chat, gerenciador de memória
│
├── personalidades/             # Prompts de sistema por humor (.txt)
│   ├── alegre.txt
│   ├── curiosa.txt
│   └── ...
│
├── respostas/                  # Frases fixas por categoria (.txt)
│   └── respostas_vazias.txt
│
└── audio/
    └── prontas/                # Áudios gerados das respostas prontas (não commitado)
```

### Camadas Atuais

| Camada | Arquivo | Descrição |
|---|---|---|
| **Audição (mic)** | `functions/MicListener.py` | `pyaudiowpatch` + Whisper (CUDA) com calibração automática de ruído |
| **Audição (ambiente)** | `cerebro/audio_ambiente.py` | WASAPI Loopback — captura áudio do sistema e transcreve com Whisper |
| **Visão** | `cerebro/visao.py` | *(desligada)* Screenshot via `mss` + `llava:7b` (fallback `moondream:1.8b`) em PT-BR |
| **Raciocínio** | `functions/OllamaClient.py` | LLM via Ollama com histórico, ferramentas ReAct, emoção e memória. Cancelável via `asyncio.Event` |
| **Ferramentas e Comandos** | `functions/Tools.py` | `LocationTools` + `WebTools` + `MusicTools` + Comandos Locais (clima, hora, música, buscas, lixeira) |
| **Emoção / Estado** | `functions/EmotionEngine.py` | Tagger de emoções e amizade cumulativa. Estado atual no SQLite (`cerebro/memoria.db`, tabela `estado`) e histórico de cada mudança no ClickHouse `analytics.estado_alice` |
| **Conteúdo** | `functions/ContentManager.py` | Cache em memória de personalidades + respostas; hot-reload sem reiniciar |
| **TTS** | `functions/TTSService.py` | **OmniVoice** (local, offline, zero-shot) **frase a frase**: toca a primeira frase enquanto gera a próxima. Roda na GPU com `TTS_DEVICE=cuda`. Não fala nada entre asteriscos (`*ações*`) nem emojis. Pré-carregado no startup e cancelável |
| **Respostas prontas** | `functions/RespostasProntas.py` + `respostas_prontas.json` | Perguntas frequentes ("se apresente", "o que você consegue fazer") respondidas na hora com áudio já gerado em `audio/prontas/`, sem passar pelo LLM |
| **Memória Curta** | `main.py` → `historico` | Janela deslizante das últimas 10 trocas da sessão atual, com resumo automático das mais antigas |
| **Extração de fatos** | `main.py` → `_extrair_memoria()` | A cada 3 turnos, depois da fala, em segundo plano (não atrasa a voz) |
| **Memória Longa (fatos)** | `cerebro/memoria.py` | **ClickHouse** (primário) + **SQLite** (fallback) — busca por relevância via ILIKE |
| **Memória de Interações** | `cerebro/clickhouse_logger.py` | ClickHouse `analytics.interactions` — histórico de conversas com tokens |
| **Logs estruturados** | `cerebro/logger.py` + `clickhouse_logger.py` | WARNING+ → ClickHouse `analytics.logs` + Loguru arquivo diário |
| **Proativo** | `functions/OllamaClient.py` → `gerar_fala_proativa()` | Timer configurável (`TEMPO_PROATIVO`) — Alice fala sem ser chamada |
| **Interface GUI** | `interface/janela.py` | Chat dark (customtkinter) com status bar, bubbles, input de texto e **🧠 Gerenciador de Memória** embutido |
| **Gest. Memória** | `gerenciar_memoria.py` | Script CLI alternativo — usa ClickHouse (ou SQLite como fallback) |
| **Avatar** | `functions/VTS_Connector.py` | *(não ligado)* Injeção direta de parâmetros Live2D via API WebSocket do VTube Studio |
| **Sessão** | `main.py` → `AliceSession` | Estado da sessão em `main.py` (interceptador de comandos, histórico, cancelamento, turno_count) |

### Schema ClickHouse — `analytics.interactions`

| Coluna | Tipo | Descrição |
|---|---|---|
| `session_id` | String | ID único da sessão (8 chars) |
| `turn_id` | UInt32 | Número sequencial do turno na sessão |
| `role` | String | `user` ou `assistant` |
| `content` | String | Conteúdo completo da mensagem |
| `humor` | String | Personalidade ativa no turno (ex: `alegre`, `timida`) |
| `model` | String | Modelo LLM usado (ex: `Llama3.1:8b`) |
| `tokens_prompt` | UInt32 | Tokens do contexto enviado ao modelo (entrada) |
| `tokens_resposta` | UInt32 | Tokens gerados pelo modelo (saída) |
| `tokens_total` | UInt32 | Soma de `tokens_prompt` + `tokens_resposta` |
| `created_at` | DateTime | Timestamp da interação |

### Schema ClickHouse — `analytics.estado_alice`

Uma linha por mudança de estado (substitui o antigo `brain.json`; o estado atual fica no SQLite).

| Coluna | Tipo | Descrição |
|---|---|---|
| `humor` | String | Humor detectado na última resposta |
| `amizade_com_usuario` | UInt8 | Nível de amizade (0 a 100) |
| `total_conversas` | UInt32 | Total de turnos já registrados |
| `nome_usuario` | String | Nome do usuário detectado |
| `ultima_interacao` | String | Resumo da última troca |
| `session_id` | String | ID da sessão |
| `criado_em` | DateTime | Quando o estado mudou |

Na primeira execução, o `functions/brain.json` antigo é copiado para o banco e renomeado para `brain.json.migrado`.

### Respostas prontas

Edite `respostas_prontas.json`. Cada resposta tem um `id`, uma lista de `gatilhos` (sem acento, maiúscula ou minúscula tanto faz) e o `texto`:

```json
{ "id": "apresentacao", "gatilhos": ["se apresente", "quem e voce"], "texto": "Oi! Eu sou a Alice..." }
```

- O gatilho precisa aparecer na frase, e a frase pode ter no máximo 2 palavras a mais que ele. "Alice", "oi" e "ei" no começo são ignorados. Assim, "Quem é você pra me dizer isso?" continua indo para o LLM.
- O áudio é gerado no startup e salvo em `audio/prontas/`. Só é refeito quando o texto (ou a voz) muda.
- A resposta entra no histórico, então o LLM sabe o que a Alice acabou de dizer.

### Voz na GPU

No `.env`, `TTS_DEVICE=cuda` roda o OmniVoice na placa de vídeo (`cpu` para voltar). Testado numa RTX 4070 de 12 GB junto com `llama3.1:8b`, Whisper e `nomic-embed-text`: o `ollama ps` continuou mostrando `100% GPU` para o llama. Se aparecer divisão CPU/GPU, a VRAM não deu e o LLM vai ficar mais lento.

### Sistema de Ferramentas (ReAct Pattern)
Em vez da API nativa de `tools` do Ollama (instável com modelos locais), a Alice usa um sistema próprio:
1. O LLM recebe instruções no `system prompt` ensinando o formato `[TOOL:nome:argumento]`
2. Python detecta o marcador via `regex` na resposta
3. Executa a função correspondente via `ToolManager.executar()` em `functions/Tools.py`
4. Injeta o resultado no contexto e pede a resposta final ao LLM
