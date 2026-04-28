# 🌸 Projeto Alice: Assistente VTuber Local Inteligente

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

3. **A Mente Expandida (Function Calling & LLM):** O cérebro dela não é só geração de texto, mas também possui a capacidade de evocar ferramentas do sistema operacional (ler o relógio, gerenciar arquivos, pesquisar algo, etc.) no meio do seu fluxo de pensamento via ReAct Pattern.
   
4. **O Sistema Emocional e a Memória (Tagger & Brain):** Muito mais que um prompt fixo, ela detém memórias em JSON e SQLite e a capacidade emocional de interpretar as próprias palavras. Através da `EmotionEngine`, a Alice lê entrelinhas: ela detecta automaticamente o nome do usuário e o salva no `brain.json`.
   
5. **O Comportamento Proativo:** Alice não espera ser chamada. Após um silêncio configurável (`TEMPO_PROATIVO`), ela toma a iniciativa e comenta sobre a tela, faz perguntas ou conta curiosidades — como um companheiro real.

6. **O Corpo e a Alma (PyVTS & GUI):** O "fantasma" mora dentro do VTube Studio. A boca do modelo VTuber se move em sincronia de voz neural e os parâmetros Live2D são injetados diretamente via API para expressões faciais em tempo real. Uma janela GUI (customtkinter) complementa a interação por voz com chat de texto.

**Em suma:** A Alice é o alicerce de uma entidade companheira modular de *streaming*. Ela é o marco de quando o código deixa de ser reativo e passa a ser colaborativo — ela vê, ouve, sente e age por conta própria.



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
│   ├── Tools.py                # LocationTools + WebTools + ToolManager (classes)
│   ├── EmotionEngine.py        # Estado emocional persistido em brain.json
│   ├── VTS_Connector.py        # Controle do VTube Studio via WebSocket
│   └── brain.json              # Estado emocional persistido (humor, amizade, nome)
│
├── cerebro/                    # Módulos cognitivos e de percepção
│   ├── memoria.py              # Memória de longo prazo (ClickHouse + SQLite fallback)
│   ├── visao.py                # Visão computacional (screenshot + llava:7b)
│   ├── audio_ambiente.py       # Áudio ambiente WASAPI loopback
│   ├── clickhouse_logger.py    # Logger e memória no ClickHouse
│   └── logger.py              # Loguru centralizado
│
├── interface/
│   └── janela.py              # GUI customtkinter com chat, gerenciador de memória
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
| **Ferramentas** | `functions/Tools.py` | `LocationTools` + `WebTools` + `ToolManager` — hora, clima, busca web |
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
| **Sessão** | `main.py` → `AliceSession` | Estado da sessão encapsulado (histórico, cancelamento, turno_count) |

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

---

## 📋 Changelog de Evolução

### v2.6 — Migração de TTS: edge-tts → OmniVoice _(13/04/2026)_

#### `functions/TTSService.py` — Novo motor de voz local

- ✅ **`edge-tts` substituído pelo [OmniVoice](https://github.com/k2-fsa/OmniVoice)** (k2-fsa) — TTS local, offline, zero-shot, multilíngue (600+ idiomas)
- ✅ **Modo "Design de Voz":** voz configurada por descrição textual via `VOZ_INSTRUCT` no `.env` (ex: `female, young adult, portuguese accent`) — sem necessidade de arquivo de referência
- ✅ **Carregamento lazy do modelo** (`_carregar_modelo()`) com pré-carregamento no startup via `pre_carregar()` — evita atraso na primeira fala
- ✅ **Saída em WAV** (`resposta.wav`) salva com `soundfile`; reprodução mantida via `pygame`
- ✅ **Geração assíncrona** via `run_in_executor` — não bloqueia o event loop durante a inferência na GPU
- ✅ **Pipeline de limpeza expandido:** além de emojis e roleplay (`*asteriscos*`), agora remove formatação markdown antes de enviar ao modelo:
  - `**negrito**`, `*itálico*`, `***negrito-itálico***` → texto limpo
  - `# Títulos` → removidos
  - `` `código inline` `` → removidos
- ✅ **Aviso do pydub sobre ffmpeg** silenciado via `warnings.filterwarnings` (ffmpeg não é usado pela Alice)
- ✅ **Variáveis `rate` e `pitch` removidas** (não aplicáveis ao OmniVoice); `VOZ_SUGERIDA` substituída por `VOZ_INSTRUCT`

#### Dependências adicionadas ao `.venv`

| Pacote | Versão | Função |
|---|---|---|
| `omnivoice` | 0.1.4 | Motor TTS local zero-shot |
| `torch` | 2.8.0+cu128 | PyTorch com suporte CUDA 12.x |
| `torchaudio` | 2.8.0+cu128 | Processamento de áudio para PyTorch |
| `soundfile` | — | Salvar áudio WAV gerado pelo OmniVoice |

---

### v2.5 — Cognição Expandida: Grafo, Sonho e Proatividade Escalada _(13/04/2026)_

#### `cerebro/graph_memory.py` — GraphRAG: Memória em Grafo (novo)

- ✅ **Grafo de relações entre entidades** armazenado em SQLite local (`cerebro/grafo.db`)
- ✅ **Triplas `(sujeito, relação, objeto)`** extraídas automaticamente da conversa a cada 3 turnos via LLM
  - Formato: `SUJEITO | RELACAO | OBJETO` (ex: `Marcos | TRABALHA_EM | empresa de tecnologia`)
  - Relações duplicadas ignoradas silenciosamente por UNIQUE constraint
- ✅ **Busca por contexto:** extrai palavras-chave da pergunta atual e retorna triplas relevantes do grafo
- ✅ **`construir_bloco_grafo(contexto)`** injeta relações no system prompt junto com memória vetorial e visão
- ✅ **Fail-silently:** retorna `""` se não houver triplas — nenhum impacto se o grafo estiver vazio

#### `cerebro/sonho.py` — Reflexão Offline ("Sonho") (novo)

- ✅ **Task asyncio em background** que monitora inatividade a cada 30s
- ✅ **Após `TEMPO_SONHO` segundos sem interação real** (padrão: 300s / 5 min):
  - Lê todos os fatos da memória de longo prazo (até 30 fatos)
  - LLM gera um insight genuinamente novo em 1–2 frases (`temperature=0.85`)
  - Insight salvo como `[Reflexão] <texto>` na memória permanente
  - Exibido na GUI como `🌙 *<insight>*` — sem falar em voz alta
- ✅ **Mínimo de 3 fatos** exigido para o sonho ser útil; caso contrário, adiado silenciosamente
- ✅ **Configurável via `.env`:** `TEMPO_SONHO=300`

#### `functions/OllamaClient.py` — Escalada de Estágios Proativos

- ✅ **`gerar_fala_proativa(ciclo: int)`** escalona o tom em 3 estágios conforme o silêncio se prolonga:

  | Ciclo | Estágio | Tom |
  |---|---|---|
  | 0–1 | `casual` | Carinhoso, curioso, espontâneo |
  | 2–3 | `provocativo` | Irônico, bem-humorado, levemente provocador |
  | 4+ | `caótico` | Absurdo, surreal, dramaticamente engraçado |

- ✅ **Fallbacks por estágio:** frases fixas por tom caso o Ollama falhe
- ✅ **Reset automático** ao receber qualquer resposta real do usuário

#### `cerebro/visao.py` + `functions/OllamaClient.py` — Vision Heartbeat Proativo

- ✅ **`houve_mudanca_relevante()`** em `visao.py`: compara descrições de tela com similaridade de Jaccard entre conjuntos de palavras — dispara se < 50% de palavras em comum
- ✅ **`_heartbeat_visao_loop()`** em `main.py`: task background que captura tela a cada `TEMPO_HEARTBEAT_VISAO` segundos (padrão: 120s) e, se detectar mudança relevante, injeta token `__heartbeat_visao__` na fila de entrada
- ✅ **`gerar_comentario_visao()`** em `OllamaClient.py`: gera comentário espontâneo sobre o que Alice está vendo na tela — exibido na GUI com `👁️` sem interromper o fluxo normal
- ✅ **Guard `_alice_falando`:** heartbeat não dispara enquanto Alice estiver falando

#### `main.py` — Integração geral

- ✅ `GraphMemory` instanciado globalmente; `extrair_e_salvar_fatos()` recebe `grafo` como parâmetro opcional
- ✅ `bloco_grafo` injetado no contexto: `bloco_ctx = bloco_memoria + bloco_grafo + bloco_visao + bloco_audio`
- ✅ `Sonho.loop()` iniciado como task em background junto com o heartbeat de visão
- ✅ `_ultima_interacao` / `_registrar_interacao()` / `_inatividade_segundos()` — rastreamento de inatividade para o sonho
- ✅ Novas variáveis de ambiente: `TEMPO_HEARTBEAT_VISAO` (120s) e `TEMPO_SONHO` (300s)

---

### v2.4 — Correção de Microfone: Seleção Robusta de Dispositivo _(12/04/2026)_

#### `functions/MicListener.py` — Detecção inteligente de dispositivo de entrada

- ✅ **Causa raiz identificada:** `INDICE_MICROFONE='8'` no `.env` apontava para o **"Primary Sound Driver"** — um dispositivo de *saída* (mapeador genérico do Windows), não um microfone físico. O PortAudio retornava `[Errno -9998] Invalid number of channels` ao tentar abrir qualquer stream de entrada nele
- ✅ **`.env` corrigido:** `INDICE_MICROFONE` atualizado para o índice do microfone físico real (`Microphone (SHEM-BOY)`)
- ✅ **Fallback de canais:** quando um dispositivo reporta `maxInputChannels > 1` mas só aceita 1 canal no driver, o código agora testa em ordem `1ch → 2ch → nativo` antes de falhar — evita erro `-9998` em drivers mal declarados
- ✅ **Seleção automática quando sem índice configurado:** em vez de usar `get_default_input_device_info()` (que retorna o mapeador virtual), o `MicListener` agora varre todos os dispositivos e pula automaticamente:
  - `"primary sound driver"` — mapeador de saída do Windows
  - `"primary sound capture driver"` — mapeador de entrada virtual
  - `"microsoft sound mapper"` — mapeador genérico Microsoft
  - `"loopback"` — dispositivos de captura de saída (WASAPI)
- ✅ **Erro descritivo garantido:** se nenhum microfone físico for encontrado, o `RuntimeError` agora exibe o nome e índice do dispositivo problemático + lista de canais testados, facilitando o diagnóstico sem precisar ler stack traces

#### `tools/listar_dispositivos_audio.py`

Script de diagnóstico confirmado em uso — exibe todos os dispositivos com `Idx`, `Nome`, canais `In`/`Out` e `Rate`, separando entradas e saídas. Usar para identificar o índice correto antes de configurar `INDICE_MICROFONE` no `.env`.

| Dispositivo | Índice | Tipo | Status |
|---|---|---|---|
| Microsoft Sound Mapper - Input | 0 | Virtual | ❌ Ignorado |
| Microphone (SHEM-BOY) | **1** | Físico | ✅ Ativo |
| Elgato 4K X | 2 | Físico | disponível |
| Primary Sound Capture Driver | 5 | Virtual | ❌ Ignorado |
| Primary Sound Driver | 8 | Saída | ❌ Sem input |
| Speakers [Loopback] | 13 | Loopback | ❌ Ignorado |

---

### v2.3 — Qualidade de Voz: Remoção de Emojis e Humanização _(11/04/2026)_


#### `functions/TTSService.py` — Pipeline de limpeza de texto

- ✅ **Remoção de emojis antes do Edge TTS:**
  - Edge TTS vocalizava emojis como palavras (ex: 😊 → *"emoji sorridente"*, 🕰️ → *"relógio"*)
  - Regex `_EMOJI_RE` compilado em nível de módulo (uma única vez na inicialização) cobre todos os blocos Unicode relevantes: emoticons, pictogramas, símbolos de transporte, dingbats, bandeiras, sequências ZWJ, variantes de apresentação gráfica e o plano suplementar completo
  - Aplicado **somente ao texto enviado ao TTS** — a GUI/chat continua exibindo os emojis normalmente
  - Espaços duplos deixados pela remoção são colapsados automaticamente

#### `functions/TTSService.py` + `.env` — Humanização da voz

- ✅ **`rate` e `pitch` configuráveis via `.env`** (sem precisar alterar código):

  | Variável | Padrão | Efeito |
  |---|---|---|
  | `TTS_RATE` | `-10%` | Reduz velocidade → voz "respira" entre frases |
  | `TTS_PITCH` | `-5Hz` | Abaixa o tom → remove aspecto metálico/agudo |

  ```ini
  # .env
  TTS_RATE=-10%   # range útil: -5% a -15%
  TTS_PITCH=-5Hz  # range útil: -2Hz a -8Hz
  ```

- ✅ **`TTSService.__init__`** aceita `rate` e `pitch` com defaults embutidos como fallback
- ✅ **`main.py`** lê `TTS_RATE` e `TTS_PITCH` do `.env` e repassa ao construtor do `TTSService`
- ✅ **Log de inicialização** exibe os valores ativos: `🎙️ TTSService | Voz: ... | Rate: -10% | Pitch: -5Hz`

---

### v2.2 — Otimização de Modelos e Estabilidade _(11/04/2026)_

#### Separação de responsabilidades: LLM vs. Visão

- 🔄 **LLM principal:** testado `gemma4:e4b` como LLM conversacional → **revertido para `llama3.1:8b`**
  - `gemma4:e4b` aloca um KV-cache de 32k–131k tokens por padrão, consumindo RAM excessiva para uso conversacional contínuo
  - `llama3.1:8b` opera com contexto padrão de 8k no Ollama — adequado para o histórico e prompts da Alice sem restrições artificiais
- ✅ **Modelo de visão:** `gemma4:e4b` mantido como **prioridade máxima** em `cerebro/visao.py`
  - Como modelo de visão, é chamado pontualmente para descrever screenshots — sem o custo de KV-cache persistente
  - Modelos anteriores (`llava:7b`, `moondream`) permanecem na lista como fallback automático

#### Correção: ClickHouse — concorrência entre threads

- ✅ **`cerebro/clickhouse_logger.py` — `threading.Lock` adicionado**
  - `clickhouse_connect` não é thread-safe: `run_in_executor()` (turno de conversa) e o sink do Loguru (`registrar_log_sync`) acessavam o mesmo `self._client` simultaneamente, causando o erro `concurrent queries within the same session`
  - `self._lock = threading.Lock()` adicionado ao `__init__`, envolto em `with self._lock:` em todos os métodos que acessam `self._client` (`_inserir_sync`, `registrar_fato_sync`, `buscar_fatos_relevantes`, `buscar_todos_fatos_mem`, `remover_fato_mem`, `limpar_memorias`, `registrar_log_sync`)

---

### v2.1 — Emoções Dinâmicas & Identidade Persistente _(11/04/2026)_

#### Problema resolvido
Alice usava quase exclusivamente a personalidade `alegre` porque `descobrir_humor()` tinha um prompt genérico sem regras explícitas — o LLM sempre escolhia a opção mais "segura".

#### `functions/OllamaClient.py` — `descobrir_humor()`
- ✅ **Sistema de regras de prioridade:** prompt reescrito com 9 regras explícitas em ordem de prioridade
  - Elogio/carinho para a Alice → `timida`
  - Vitória/conquista compartilhada → `euforica`
  - Tristeza/frustração/desabafo → `empatica`
  - Erro de código/bug/problema técnico → `determinada`
  - Pergunta curiosa sobre tech ou o mundo → `curiosa`
  - Agradecimento → `grata`
  - Memórias/passado/nostalgia → `nostalgica`
  - Grosseria/impaciência/repetição → `brava`
  - Qualquer outro caso → `alegre` ← agora é fallback real, não padrão
- ✅ **`temperature: 0.0` no classificador:** chamada de seleção de humor agora é determinística (era herdada do padrão do modelo)

#### `functions/OllamaClient.py` — `pensar()`
- ✅ **Bloco de identidade base (`=== IDENTIDADE DA ALICE ===`):** injetado no início do system prompt antes de qualquer personalidade
  - Define: 17 anos, PT-BR informal com gírias leves, perfil geek
  - Garante expressividade (onomatopeias, emojis, pontuação expressiva) para Edge TTS
  - Regra absoluta: resposta final DEVE ser útil mesmo no modo `brava/tsundere`
  - Diretriz de engajamento: sempre fazer perguntas de acompanhamento

---

### v2.0 — Refatoração Completa em Classes _(08/04/2026)_

#### Arquitetura

- ✅ **`main.py` de 935 → ~260 linhas:** toda lógica de negócio extraída para classes dedicadas
- ✅ **Classe `AliceSession`** em `main.py`: encapsula o estado mutable da sessão
  - `historico` (janela deslizante de mensagens), `turno_count`, `_cancelar` (asyncio.Event)
  - `_verificar_reiniciar()` — processa botão Reiniciar da GUI de forma atômica
  - `_processar_hora_clima()` — detecta cidade no texto ou usa padrão; sem perguntar ao usuário
  - `run()` — loop principal de conversa com 9 passos numerados e comentados

#### Novos módulos em `functions/`

- ✅ **`functions/OllamaClient.py` — classe `OllamaClient`:**
  - `_ollama_em_thread()` — wrapper async que faz `await` do executor (coroutine válida para `create_task`)
  - `_chamar_ollama_cancelavel()` — padrão DRY: competição entre Ollama e `asyncio.Event` de cancelamento (eliminada duplicação que existia no `main.py` antigo)
  - `pensar()` — resposta principal com suporte a ferramentas (1ª + 2ª chamada ao Ollama)
  - `descobrir_humor()` — classifica personalidade ideal para cada mensagem
  - `extrair_e_salvar_fatos()` — extração de fatos a cada 3 turnos
  - `detectar_nome_usuario()` — detecção automática do nome do usuário
  - `gerar_fala_proativa()` — falas espontâneas baseadas em humor e visão

- ✅ **`functions/TTSService.py` — classe `TTSService`:**
  - `disponivel` (property) — verifica e cacheia disponibilidade de áudio na primeira chamada
  - `falar()` — TTS com edge_tts + reprodução pygame + parada imediata ao cancelar
  - Estado `_disponivel` encapsulado na classe (era global `_audio_disponivel` no `main.py`)

- ✅ **`functions/MicListener.py` — classe `MicListener`:**
  - `_escutar()` — captura de microfone com calibração de ruído e transcrição Whisper
  - `obter_pergunta()` — roteamento unificado: microfone + GUI + timer proativo
  - `_parar` (threading.Event) encapsulado (era global `_parar_microfone` no `main.py`)

- ✅ **`functions/ContentManager.py` — classe `ContentManager`:**
  - Cache em memória de todas as personalidades (`personalidades/*.txt`) e respostas (`respostas/*.txt`)
  - `listar_personalidades()` — leitura dinâmica do disco (detecta novos arquivos sem reiniciar)
  - `get_personalidade(nome)` — retorna prompt com cache + fallback automático
  - `get_resposta_vazia()` — sorteia frase da `respostas_vazias.txt`
  - `recarregar()` — hot-reload de todos os arquivos sem reiniciar o sistema
  - `status()` — dict com tamanho de cada item (útil para debug/GUI)

#### `functions/Tools.py` — Refatorado em 3 classes

- ✅ **`LocationTools`:**
  - `CIDADE_PADRAO = "Campo Grande"` — padrão fixo (MS, UTC-4). Não pergunta mais ao usuário
  - `_FUSO_DEFAULT = "America/Campo_Grande"` — timezone de fallback alinhado com a cidade padrão
  - `detectar_cidade_no_texto(texto)` — busca nome de cidade conhecida no texto; retorna `None` se não encontrar → usa padrão
  - `get_current_time(cidade=None)` — `None` → assume `CIDADE_PADRAO`
  - `get_weather(cidade=None)` — `None` → assume `CIDADE_PADRAO`
  - Busca multi-palavra ordenada por comprimento (prioriza "porto alegre" antes de "alegre")

- ✅ **`WebTools`:**
  - `search_web(query)` — DuckDuckGo + Wikipedia fallback (igual ao anterior, agora encapsulado)

- ✅ **`ToolManager`:**
  - `tools.executar(nome, argumento)` — interface centralizada com tratamento de erro embutido
  - `tools.DISPONIVEIS` — compatível com `OllamaClient` (backward compat total)
  - Singleton `tools` + aliases de módulo (`DISPONIVEIS`, `get_current_time`, etc.) para zero quebra de código

#### Comportamento de Hora/Clima atualizado

| Frase do usuário | Comportamento anterior | Comportamento novo |
|---|---|---|
| "que horas são?" | Pergunta a cidade | Usa **Campo Grande** silenciosamente |
| "ta frio hoje?" | Pergunta a cidade | Usa **Campo Grande** silenciosamente |
| "que horas são em Recife?" | Pergunta a cidade | Detecta **Recife** no texto e responde |
| "como está o tempo em Manaus?" | Pergunta a cidade | Detecta **Manaus** no texto e responde |

---

### v1.3 — Internet & Reiniciar _(08/04/2026)_
- ✅ **Ferramenta `search_web`:** busca em tempo real via DuckDuckGo com até 3 resultados + Wikipedia como fallback
  - `ddgs` (DuckDuckGo Search) region `br-pt`, snippets limitados a 200 chars para não explodir o contexto
  - Se ambos falharem, retorna mensagem amigável
- ✅ **Botão Reiniciar na GUI:** cancela turno em andamento (Ollama + áudio), limpa histórico e filas, notifica o usuário
  - `asyncio.Event _cancelar_turno` monitorado por `pensar_e_responder()` e `falar()` a cada etapa
  - Fila `FILA_REINICIAR` lida no início de cada iteração do loop principal
- ✅ **Encoding UTF-8 forçado no terminal Windows:** `sys.stdout.reconfigure(encoding="utf-8")` no topo do `main.py` — resolve `UnicodeEncodeError` com emojis e caracteres especiais em resultados de busca

### v1.2 — Memória e Logs no ClickHouse _(02/04/2026)_
- ✅ **`analytics.memoria` — nova tabela para fatos do usuário:**
  - Substitui o SQLite como fonte principal dos fatos de longo prazo
  - `cerebro/memoria.py` totalmente reescrito com dupla persistência: **ClickHouse** (primário) + **SQLite** (fallback automático e silencioso)
  - Se o Docker estiver offline, Alice usa SQLite sem travar ou mostrar erros
- ✅ **Busca por relevância (`construir_bloco_memoria(contexto)`):**
  - Recebe a pergunta atual do usuário como contexto
  - Filtra fatos via `ILIKE` nas palavras-chave da pergunta (≥4 chars)
  - Injeta no prompt do LLM **apenas os fatos relacionados à conversa atual**
  - Reduz tokens de entrada e melhora a qualidade das respostas
  - Fallback: se nenhum fato for relevante, retorna os 10 mais recentes
- ✅ **`analytics.logs` — nova tabela para logs estruturados:**
  - Loguru ganha um **quarto sink**: WARNING e acima vão para o ClickHouse
  - Implementado com `enqueue=True` (thread-safe, não bloqueia a Alice)
  - Sink ativado via `set_clickhouse_logger()` em `main.py` — evita import circular
  - DEBUG/INFO continuam apenas em arquivo local (`logs/`)
- ✅ **`cerebro/clickhouse_logger.py` expandido** com 6 novos métodos:
  - `registrar_fato_sync()`, `buscar_fatos_relevantes()`, `buscar_todos_fatos_mem()`, `remover_fato_mem()`, `limpar_memorias()`, `registrar_log_sync()`
- ✅ **`gerenciar_memoria.py` atualizado:** usa ClickHouse como fonte principal com fallback automático; IDs agora são UUIDs (mostra 8 chars, aceita prefixo)
- ✅ **Schema SQLite migrado:** coluna `id` de `INTEGER` para `TEXT (UUID)` + coluna `ativo` para soft-delete compatível com o ClickHouse
- ✅ **`init.sql` atualizado** com as 3 tabelas completas e corretas

### v1.1 — Rastreamento de Tokens no ClickHouse _(02/04/2026)_
- ✅ **`pensar_e_responder()` agora retorna tokens:** captura `prompt_eval_count` e `eval_count` diretamente do retorno nativo do Ollama
  - Quando a IA usa uma ferramenta (tool), os tokens das **duas chamadas** são acumulados e somados
  - Retorno alterado para tupla: `(texto_resposta, tokens_prompt, tokens_resposta)`
- ✅ **`ClickhouseLogger` atualizado:** campo único `tokens_used` substituído por três colunas separadas
  - `tokens_prompt` → tokens enviados ao modelo (contexto + histórico + system prompt)
  - `tokens_resposta` → tokens gerados pelo modelo na resposta
  - `tokens_total` → soma calculada automaticamente antes do INSERT
  - Log de debug mostra: `Tokens: 1731↑ + 122↓ = 1853 total`
- ✅ **Tabela `analytics.interactions` migrada:** novas colunas adicionadas via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
- ✅ **Log principal atualizado:** cada resposta exibe os tokens no terminal: `🤖 [alegre] ... | 📊 1731↑ + 122↓ tokens`

### v1.0 — Memória de Interações com ClickHouse _(01/04/2026)_
- ✅ **ClickHouse como memória histórica completa:** cada conversa é gravada com timestamp, humor, modelo de IA e ID de sessão
  - Container Docker configurado em `cerebro/clickhouse/docker-compose.yaml`
  - Schema criado automaticamente via `cerebro/clickhouse/init.sql` no primeiro boot
  - Tabela: `analytics.interactions` — engine `MergeTree`, ordenada por `(session_id, turn_id, created_at)`
- ✅ **`cerebro/clickhouse_logger.py` — `ClickhouseLogger`:**
  - Conexão lazy (só conecta na primeira interação)
  - **Fail-silently:** se o ClickHouse não estiver rodando, Alice continua normalmente sem travar
  - **Assíncrono:** inserts via `loop.run_in_executor()` — zero impacto na latência da resposta
  - Cada turno salva 2 linhas: `role=user` + `role=assistant`
- ✅ **Nova variável `LIMIAR_MICROFONE_MINIMO`:** piso mínimo de RMS configurado no `.env`
  - Evita falsos positivos em microfones com ruído ambiente alto
  - Padrão: `500` — aumente se a Alice começar a transcrever ruído, diminua se não te ouvir
- ✅ **`.env` atualizado** com variáveis `CLICKHOUSE_*` e `LIMIAR_MICROFONE_MINIMO`
- Novo dep: `clickhouse-connect==0.15.1`

### v0.7 — Expansão Sensorial Completa _(29/03/2026)_
- ✅ **Feature 1 — Auto-detect `nome_usuario`:** LLM extrai o primeiro nome a cada 3 turnos e salva automaticamente no `brain.json`
- ✅ **Feature 2 — Janela GUI:** `interface/janela.py` — chat dark (customtkinter) com bubbles coloridos, status bar em tempo real e input de texto alternativo ao microfone
- ✅ **Feature 3 — Comportamento Proativo:** `TEMPO_PROATIVO` no `.env`; Alice fala espontaneamente após silêncio (comenta tela, faz perguntas, conta curiosidades)
- ✅ **Feature 4 — Visão Computacional:** `cerebro/visao.py` com `llava:7b` via Ollama; screenshot + descrição em PT-BR injetada no system prompt
  - Prioridade: `llava:7b` (4.1GB, robusto) → fallback `moondream:1.8b` (1.8GB, limitado com terminais)
- ✅ **Feature 5 — Áudio Ambiente (WASAPI Loopback):** `cerebro/audio_ambiente.py`; captura áudio dos alto-falantes, transcreve com Whisper, Alice "ouve" músicas e vídeos
- ✅ **Bugfix:** detecção de modelo de visão corrigida para a nova API do ollama Python (`.model` vs `["name"]`)
- ✅ **Bugfix:** `paFloat32` → `paInt16` no stream WASAPI (resolve `[Errno -9996] Invalid device`)
- `pyaudio` substituído por `pyaudiowpatch` (mesma API + suporte WASAPI loopback)
- **Alice agora tem:** 👁️ visão + 🔊 audição ambiente + 💬 GUI + 🤖 proatividade + 🧠 memória

### v0.9 — Gerenciador de Memória _(29/03/2026)_
- ✅ **Novos métodos em `cerebro/memoria.py`:** `buscar_todos_fatos()`, `remover_fato(id)`, `limpar_memoria()`
- ✅ **Botão 🧠 Memória na GUI:** acessível direto no header da janela de chat durante a sessão
  - Sub-janela modal com lista completa de fatos + checkboxes para seleção
  - `☑ Todos` / `☐ Nenhum` para seleção em massa
  - `🗑️ Apagar selecionados` com diálogo de confirmação
  - `💣 Limpar tudo` com dupla confirmação — lista recarrega automaticamente
- ✅ **Script CLI `gerenciar_memoria.py`:** alternativa de terminal colorida (funciona mesmo sem a Alice em execução)
  - Menu interativo: listar, apagar por ID, reset completo
  - Seguro: pede confirmação antes de qualquer deleção

### v0.8 — Logs e Monitoramento (Loguru) _(29/03/2026)_
- ✅ **`cerebro/logger.py`:** configuração centralizada do Loguru — import único em todo o projeto (`from cerebro.logger import log`)
- ✅ **Terminal limpo (INFO+):** apenas mensagens relevantes no terminal; ruído interno (seleção de humor, transcrição, lista de microfones) vai só para arquivo
- ✅ **Arquivo diário `logs/alice_YYYY-MM-DD.log`:** DEBUG+ com rotação a meia-noite e retenção de 7 dias
- ✅ **Arquivo de erros `logs/alice_errors.log`:** ERROR+ com `backtrace=True` e `diagnose=True` (variáveis locais visíveis) e retenção de 30 dias
- ✅ **Thread-safe:** sinks com `enqueue=True` para threads daemon (audio, visão)
- ✅ **Migração completa:** todos os `print()` de todos os módulos substituídos por `log.info/debug/warning/error`
- Novo dep: `loguru==0.7.3`

### v0.6 — VTube Studio: Expressões Emocionais em Tempo Real _(29/03/2026)_
- ✅ **Opção B — Injeção Direta de Parâmetros:** em vez de hotkeys, injeta parâmetros Live2D diretamente via `InjectParameterDataValues` da API VTS
- ✅ **Mapeamento baseado em `hiyori.vtube.json`:** parâmetros VTS (`MouthSmile`, `EyeOpenLeft`, `BrowLeftY`...) mapeados para os Live2D do modelo
- ✅ **`MouthSmile = 1.0` controla sorriso + olhos squintados + blush — tudo de uma vez** (via mapping do vtube.json)
- ✅ **5 emoções mapeadas:** Riso, Tristeza, Irritada, Surpresa, Neutro
- ✅ **Reset automático para Neutro** após a Alice terminar de falar
- ✅ **Non-blocking:** disparos VTS via `asyncio.create_task()` sem latência extra
- ✅ **Autenticação robusta:** renovação automática de token inválido

### v0.5 — Fase 3: Memória de Longo Prazo _(29/03/2026)_
- ✅ **`cerebro/memoria.py`:** banco SQLite criado automaticamente em `cerebro/memoria.db`
- ✅ **Extração automática de fatos:** a cada 3 turnos, a IA analisa a conversa e extrai fatos concretos sobre o usuário (nome, gostos, profissão, etc.)
- ✅ **Fatos injetados no prompt:** a Alice consulta a memória antes de cada resposta e usa os fatos armazenados para personalizar a conversa
- ✅ **Persistência entre sessões:** os fatos sobrevivem ao fechamento do programa e são carregados na próxima inicialização

### v0.4 — Fase 2: EmotionEngine Integrado _(29/03/2026)_
- ✅ **brain.json dinâmico:** atualizado automaticamente após cada resposta da Alice
- ✅ **Tagger emocional expandido:** detecta Riso, Tristeza, Irritação, Surpresa e Neutro por palavras-chave
- ✅ **Amizade cumulativa:** `amizade_com_usuario` sobe +1 por turno de conversa (máx 100)
- ✅ **Contexto emocional no prompt:** Alice recebe seu estado interno (`humor`, `amizade`, `total de interações`) a cada resposta e o usa para guiar o tom
- Novos campos em `brain.json`: `total_conversas`, `nome_usuario`

### v0.3 — Fase 1: Memória de Sessão _(29/03/2026)_
- ✅ **Histórico de conversa:** Alice agora lembra o que foi dito nos últimos 10 turnos da conversa atual
- Implementado via `historico[]` com janela deslizante de `MAX_HISTORICO = 20` mensagens
- O histórico é injetado no contexto do LLM a cada resposta, dando coerência narrativa à sessão

### v0.2 — Sistema de Ferramentas Estável _(29/03/2026)_
- ✅ **ReAct Pattern:** Substituição da API nativa de `tools` do Ollama pelo sistema de marcadores `[TOOL:...]`
- Resolve o bug de "vazamento de JSON" onde o modelo escrevia o código da ferramenta em voz alta
- Filtro de `*ações de roleplay*` implementado na função `falar()` via regex

### v0.1 — Fundação _(Março 2026)_
- ✅ Reconhecimento de voz com Whisper (CUDA)
- ✅ Geração de resposta via Ollama (Llama/Qwen)
- ✅ Síntese de voz com Edge TTS
- ✅ Detecção de humor e personalidades dinâmicas por arquivo `.txt`
- ✅ Integração com VTube Studio via PyVTS
- ✅ Autenticação automática por `.env`
