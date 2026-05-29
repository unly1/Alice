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
   
2. **A Visão (Screen Capture + LLaVA):** Alice enxerga a tela do usuário. A cada ciclo, ela captura um screenshot e usa o modelo de visão `llava:7b` para descrever em português o que está acontecendo. Esse contexto é injetado no system prompt, tornando as respostas situacionais.

3. **A Mente Expandida (Function Calling & LLM):** O cérebro dela não é só geração de texto, mas também possui a capacidade de evocar ferramentas do sistema operacional (ler o relógio, gerenciar arquivos, pesquisar algo, etc.) no meio do seu fluxo de pensamento via ReAct Pattern, ou executar atalhos locais rápidos via comandos de voz diretos de baixíssima latência.
   
4. **O Sistema Emocional e a Memória (Tagger & Brain):** Muito mais que um prompt fixo, ela detém memórias em JSON e SQLite e a capacidade emocional de interpretar as próprias palavras. Através da `EmotionEngine`, a Alice lê entrelinhas: ela detecta automaticamente o nome do usuário e o salva no `brain.json`.
   
5. **O Comportamento Proativo:** Alice não espera ser chamada. Após um silêncio configurável (`TEMPO_PROATIVO`), ela toma a iniciativa e comenta sobre a tela, faz perguntas ou conta curiosidades — como um companheiro real.

6. **O Corpo e a Alma (PyVTS & GUI):** O "fantasma" mora dentro do VTube Studio. A boca do modelo VTuber se move em sincronia de voz neural e os parâmetros Live2D são injetados diretamente via API para expressões faciais em tempo real. Uma janela GUI (customtkinter) complementa a interação por voz com chat de texto.

**Em suma:** A Alice é o alicerce de uma entidade companheira modular de *streaming*. Ela é o marco de quando o código deixa de ser reativo e passa a ser colaborativo — ela vê, ouve, sente e age por conta própria.

---

## 🏗️ Arquitetura Técnica

A Alice é construída em camadas modulares. Cada camada pode ser evoluída de forma independente.

### Estrutura de Arquivos (v2.0)

```
Alice/
├── main.py                     # Ponto de entrada + classe AliceSession (loop principal)
├── gerenciar_memoria.py        # CLI para administrar fatos da memória
├── .env                        # Segredos e configurações (não commitado)
│
├── functions/                  # Serviços internos da Alice
│   ├── OllamaClient.py         # Todas as chamadas ao LLM (cancelamento, tools, humor)
│   ├── TTSService.py           # Síntese OmniVoice (local, offline) + reprodução pygame
│   ├── MicListener.py          # Captura microfone + transcrição Whisper + roteamento
│   ├── ContentManager.py       # Cache de personalidades e respostas em memória
│   ├── Tools.py                # LocationTools + WebTools + MusicTools + Comandos de Voz (Unificados)
│   ├── EmotionEngine.py        # Estado emocional persistido em brain.json
│   ├── VTS_Connector.py        # Controle do VTube Studio via WebSocket
│   └── brain.json              # Estado emocional persistido (humor, amizade, nome)
│
├── cerebro/                    # Módulos cognitivos e de percepção
│   ├── memoria.py              # Memória de longo prazo (ClickHouse + SQLite fallback)
│   ├── visao.py                # Visão computacional (screenshot + llava:7b)
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
└── respostas/                  # Frases fixas por categoria (.txt)
    └── respostas_vazias.txt
```

### Camadas Atuais

| Camada | Arquivo | Descrição |
|---|---|---|
| **Audição (mic)** | `functions/MicListener.py` | `pyaudiowpatch` + Whisper (CUDA) com calibração automática de ruído |
| **Audição (ambiente)** | `cerebro/audio_ambiente.py` | WASAPI Loopback — captura áudio do sistema e transcreve com Whisper |
| **Visão** | `cerebro/visao.py` | Screenshot via `mss` + `llava:7b` (fallback `moondream:1.8b`) em PT-BR |
| **Raciocínio** | `functions/OllamaClient.py` | LLM via Ollama com histórico, ferramentas ReAct, emoção e memória. Cancelável via `asyncio.Event` |
| **Ferramentas e Comandos** | `functions/Tools.py` | `LocationTools` + `WebTools` + `MusicTools` + Comandos Locais (clima, hora, música, buscas, lixeira) |
| **Emoção** | `functions/EmotionEngine.py` | Tagger de emoções, persistência em `brain.json`, amizade cumulativa |
| **Conteúdo** | `functions/ContentManager.py` | Cache em memória de personalidades + respostas; hot-reload sem reiniciar |
| **TTS** | `functions/TTSService.py` | **OmniVoice** (local, offline, zero-shot) com limpeza de markdown/roleplay/emojis + pré-carregamento no startup + controle de cancelamento |
| **Memória Curta** | `functions/OllamaClient.py` → `historico` | Janela deslizante das últimas 10 trocas da sessão atual |
| **Memória Longa (fatos)** | `cerebro/memoria.py` | **ClickHouse** (primário) + **SQLite** (fallback) — busca por relevância via ILIKE |
| **Memória de Interações** | `cerebro/clickhouse_logger.py` | ClickHouse `analytics.interactions` — histórico de conversas com tokens |
| **Logs estruturados** | `cerebro/logger.py` + `clickhouse_logger.py` | WARNING+ → ClickHouse `analytics.logs` + Loguru arquivo diário |
| **Proativo** | `functions/OllamaClient.py` → `gerar_fala_proativa()` | Timer configurável (`TEMPO_PROATIVO`) — Alice fala sem ser chamada |
| **Interface GUI** | `interface/janela.py` | Chat dark (customtkinter) com status bar, bubbles, input de texto e **🧠 Gerenciador de Memória** embutido |
| **Gest. Memória** | `gerenciar_memoria.py` | Script CLI alternativo — usa ClickHouse (ou SQLite como fallback) |
| **Avatar** | `functions/VTS_Connector.py` | Injeção direta de parâmetros Live2D via API WebSocket do VTube Studio |
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

### Sistema de Ferramentas (ReAct Pattern)
Em vez da API nativa de `tools` do Ollama (instável com modelos locais), a Alice usa um sistema próprio:
1. O LLM recebe instruções no `system prompt` ensinando o formato `[TOOL:nome:argumento]`
2. Python detecta o marcador via `regex` na resposta
3. Executa a função correspondente via `ToolManager.executar()` em `functions/Tools.py`
4. Injeta o resultado no contexto e pede a resposta final ao LLM
