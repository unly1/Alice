# Agent Manager — Projeto Alice
> Documento de referência técnica para agentes de IA (como Gemini, Claude, etc.) que trabalhem neste repositório.
> Atualizado em: Abril 2026 | Versão do roadmap implementada: Fase 1 ✅ · Fase 2 ✅ · Fase 3 ✅ · Fase 4 ✅ · Fase 5 ⏳ (pendente) · Fase 6 ✅

---

## 1. Visão Geral

Alice é uma assistente pessoal com voz, visão e memória, rodando 100% local (salvo Edge TTS).

**Stack principal:**
| Camada | Tecnologia |
|---|---|
| LLM | Ollama (`llama3.1:8b` por padrão via `MODELO_IA` no `.env`) |
| STT | faster-whisper (CUDA, modelo `small`) |
| TTS | Edge TTS (`pt-BR-FranciscaNeural`) via Microsoft cloud |
| Visão | VisaoComputacional (screenshot + LLM) |
| Memória | ClickHouse (`analytics.memoria`) + SQLite fallback |
| Embeddings | `nomic-embed-text` via Ollama |
| Áudio loopback | WASAPI via pyaudiowpatch |
| Avatar | VTube Studio via WebSocket (`VTS_Connector`) |
| GUI | Tkinter (`interface/janela.py`) |
| Música | yt-dlp + mpv |

---

## 2. Estrutura de Arquivos

```
Alice/
├── main.py                        # Ponto de entrada; AliceSession; loop principal
├── gerenciar_memoria.py           # CLI para listar/remover fatos da memória
│
├── cerebro/                       # Subsistemas internos ("o cérebro")
│   ├── audio_ambiente.py          # Captura WASAPI loopback + transcrição Whisper
│   ├── clickhouse_logger.py       # Persistência ClickHouse (interactions, memoria, logs)
│   ├── embeddings.py              # Wrapper nomic-embed-text via Ollama
│   ├── logger.py                  # Loguru + sink ClickHouse
│   ├── memoria.py                 # Interface pública de memória (ClickHouse + SQLite)
│   └── visao.py                   # Screenshot periódico + descrição via LLM
│
├── functions/                     # Serviços e ferramentas externas
│   ├── ContentManager.py          # Personalidades e respostas (cache em memória, hot-reload)
│   ├── EmotionEngine.py           # Estado emocional persistido em brain.json
│   ├── MicListener.py             # Captura microfone + roteamento de input (voz/GUI/proativo)
│   ├── OllamaClient.py            # Todas as chamadas ao LLM (ReAct pattern, extração de fatos)
│   ├── Streamer.py                # (streaming auxiliar)
│   ├── TTSService.py              # Edge TTS + pygame (síntese e reprodução de voz)
│   ├── Tools.py                   # ToolManager: get_time, get_weather, search_web, música
│   ├── VTS_Connector.py           # VTube Studio WebSocket
│   └── music_tools.py             # MusicTools: yt-dlp + mpv (tocar, parar, volume)
│
├── interface/
│   └── janela.py                  # GUI Tkinter
│
├── tools/
│   └── listar_dispositivos_audio.py
│
├── roadmap/
│   └── roadmap_alice.txt          # Roadmap completo de paridade com Kira
│
└── agent_manager.md               # Este arquivo
```

---

## 3. Fluxo de Execução (main.py)

```
main.py
  │
  ├─ Carrega Whisper (CUDA)
  ├─ Lê .env (MODELO_IA, VOZ_SUGERIDA, TTS_RATE, TTS_PITCH, ...)
  ├─ Instancia serviços globais:
  │     VTSConnector, EmotionEngine, ClickhouseLogger, MemoriaLongaPrazo
  │     VisaoComputacional, AudioAmbiente
  │     threading.Event _alice_falando  ← compartilhado entre TTS/Mic/AudioAmbiente
  │
  └─ AliceSession.__init__()
        ├─ OllamaClient(modelo, emotion_engine, visao, audio_ambiente, cancelar_turno)
        ├─ TTSService(voz, arquivo_audio, cancelar_turno, alice_falando=_alice_falando)
        └─ MicListener(whisper, indice_mic, limiar, fila_gui, tempo_proativo, alice_falando)

  AliceSession.run() [loop asyncio]
        ├─ mic.obter_pergunta() → aguarda voz | GUI | timer proativo
        ├─ ollama.descobrir_humor(texto) → personalidade
        ├─ memoria.buscar_fatos_relevantes(texto) → bloco_memoria
        ├─ ollama.pensar(texto, personalidade, historico, bloco_memoria)
        │     └─ ReAct: detecta [TOOL:nome:arg] → executa → 2ª chamada LLM
        ├─ tts.falar(resposta)
        ├─ clickhouse.registrar_turno(pergunta, resposta, ...)
        └─ a cada 3 turnos: extrair_e_salvar_fatos() + detectar_nome_usuario()
```

---

## 4. Padrão de Ferramentas (ReAct)

O LLM escreve marcadores no formato `[TOOL:nome:argumento]`.
O Python detecta via regex `_REGEX_TOOL` em `OllamaClient.pensar()` e executa via `funcoes_tools.DISPONIVEIS`.

**Ferramentas registradas em `functions/Tools.py` → `ToolManager.DISPONIVEIS`:**

| Marcador | Função | Descrição |
|---|---|---|
| `[TOOL:get_current_time:CIDADE]` | `LocationTools.get_current_time` | Hora local (fuso IANA) |
| `[TOOL:get_weather:CIDADE]` | `LocationTools.get_weather` | Clima via wttr.in |
| `[TOOL:search_web:QUERY]` | `WebTools.search_web` | DuckDuckGo + Wikipedia fallback |
| `[TOOL:tocar_musica:QUERY]` | `MusicTools.tocar_musica` | Busca YouTube (yt-dlp) e toca com mpv |
| `[TOOL:parar_musica:]` | `MusicTools.parar_musica` | Para o processo mpv ativo |
| `[TOOL:volume:0-100]` | `MusicTools.volume` | Ajusta volume via mpv IPC |

---

## 5. Prevenção de Auto-Escuta (Fase 1)

`threading.Event _alice_falando` é criado em `main.py` e passado para:
- `TTSService.falar()` → seta o evento antes de reproduzir, limpa no `finally`
- `MicListener._escutar()` → descarta frames enquanto evento está setado
- `AudioAmbiente._transcrever()` → retorna imediatamente se evento está setado

Sem isso, Alice ouvia a própria voz via microfone ou WASAPI loopback e entrava em loop infinito.

---

## 6. TTS (Fase 1)

Melhorias em `functions/TTSService.py`:
- **Rate limiting**: mínimo de 0.3s entre chamadas ao Edge TTS (`_tts_delay_min`)
- **Retry com backoff exponencial**: 3 tentativas (imediata, 1s, 2s) antes de desistir
- Emojis e ações de roleplay (`*sorri*`) são removidos antes do TTS para evitar verbalização

---

## 7. Memória Semântica (Fase 2)

### Tabela `analytics.memoria` (ClickHouse)
```sql
id         UUID
fato       String
embedding  Array(Float32)   -- adicionada via migração automática
criado_em  DateTime
```

### Fluxo de embedding
1. `cerebro/embeddings.py` → `gerar_embedding(texto)` chama `ollama.embeddings(model="nomic-embed-text")`
2. `clickhouse_logger.registrar_fato_sync()` → salva fato com embedding
3. `clickhouse_logger.buscar_fatos_relevantes(contexto)`:
   - **Com nomic disponível**: `cosineDistance(embedding, query_embedding)` + complemento ILIKE para fatos legado
   - **Sem nomic**: fallback puro ILIKE (comportamento anterior)
4. Na conexão inicial: `reembeddar_fatos_legado()` — thread daemon que preenche embeddings de fatos antigos (sem embedding)

### Degradação graciosa
Se `nomic-embed-text` não estiver carregado no Ollama, `gerar_embedding()` retorna `[]` e o sistema usa ILIKE silenciosamente.

---

## 8. Módulo de Música (Fase 3)

**`functions/music_tools.py`** — classe `MusicTools` (singleton via `ToolManager`):

| Método | Comportamento |
|---|---|
| `tocar_musica(query)` | yt-dlp busca `ytsearch1:query` → extrai URL do stream de áudio → lança `mpv --no-video` como subprocess não-bloqueante |
| `parar_musica()` | `processo.terminate()` + `wait(3s)` ou `kill()` se travar |
| `volume(nivel)` | JSON via mpv IPC named pipe `\\.\pipe\mpv-alice` (Windows) ou Unix socket (Linux) |

**Dependências externas:**
```bash
pip install yt-dlp
# mpv instalado no PATH: https://mpv.io
```

**Estado**: `_processo: subprocess.Popen | None` com `threading.Lock()`.

---

## 9. Sumarização Periódica do Contexto (Fase 2 — item 4)

**`cerebro/summarizer.py`** — classe `Summarizer`:

**Problema**: o histórico cresce até `MAX_HISTORICO=20` mensagens brutas. Com system prompt longo (identidade + personalidade + ferramentas + memória + visão + áudio), o contexto total fica enorme, aumentando latência.

**Solução**: quando `len(historico) >= MAX_HISTORICO - 4` (≥ 16 msgs), o `Summarizer` condensa as mensagens mais antigas em um brief de 3-5 linhas via Ollama, mantendo as 8 mais recentes intactas.

**Formato injetado no histórico após compactação:**
```python
[
  {"role": "user",      "content": "[RESUMO DA CONVERSA ANTERIOR]\n...resumo..."},
  {"role": "assistant", "content": "Entendido! Tenho esse contexto em mente."},
  # ...últimas 8 mensagens intactas...
]
```

**Integração em `main.py`:**
```python
summarizer = Summarizer()  # instância global

# No loop, após atualizar historico (passo 7):
if len(self.historico) >= MAX_HISTORICO - 4:
    self.historico = await summarizer.compactar_se_necessario(
        self.historico, MODELO_IA, limiar=MAX_HISTORICO - 4
    )
# Truncamento de segurança se summarizer falhar:
if len(self.historico) > MAX_HISTORICO:
    self.historico = self.historico[-MAX_HISTORICO:]
```

**Degradação graciosa**: se o Ollama falhar durante a sumarização, o histórico original é retornado e o truncamento de segurança (`self.historico[-MAX_HISTORICO:]`) assume o controle.

---

## 10. Vision Heartbeat Proativo (Fase 4 — item 8)

**Problema**: Alice injeta a descrição da tela no prompt, mas nunca comenta espontaneamente sobre mudanças visuais sem que o usuário pergunte.

**Solução**: Task background `_heartbeat_visao_loop()` que roda a cada `TEMPO_HEARTBEAT_VISAO` segundos (padrão: 120s), compara a nova descrição com a anterior via **similaridade de Jaccard** e, se a mudança for relevante, injeta um token especial na fila de entrada para que o loop principal gere um comentário.

**Fluxo:**
```
_heartbeat_visao_loop() [background]
  └─ await asyncio.sleep(TEMPO_HEARTBEAT_VISAO)
  └─ visao.descrever_tela(forcar=True)
  └─ visao.houve_mudanca_relevante()  ← Jaccard < 0.5
  └─ FILA_ENTRADA_GUI.put("__heartbeat_visao__")
        ↓
main loop detecta pergunta == "__heartbeat_visao__"
  └─ ollama.gerar_comentario_visao()
  └─ tts.falar(resp)
```

**Detecção de mudança (`visao.houve_mudanca_relevante()`):**
- Compara `_descricao_anterior` vs `ultima_descricao` por similaridade de Jaccard entre conjuntos de palavras
- `limiar=0.5`: retorna True se menos de 50% das palavras forem comuns

**Proteções:**
- Não dispara enquanto `_alice_falando` estiver setado
- Não dispara se `visao.ativa` for False
- Aguarda `TEMPO_HEARTBEAT_VISAO` na inicialização antes do primeiro check (evita disparo imediato)

**Variável de ambiente:** `TEMPO_HEARTBEAT_VISAO` (padrão: `120` segundos)

---

## 11. Escalada de Estágios Proativos (Fase 4 — item 7)

**Problema**: Alice tinha apenas um nível de proatividade — sempre o mesmo tom casual, independente de quanto tempo o usuário ficasse em silêncio.

**Solução**: `_ciclos_proativos` (contador em `AliceSession`) incrementa a cada disparo proativo e reseta quando o usuário responde. O contador é passado para `gerar_fala_proativa(ciclo=N)`, que escolhe o estágio:

| Ciclo | Estágio | Tom |
|---|---|---|
| 0–1 | `casual` | Carinhoso, curioso, espontâneo |
| 2–3 | `provocativo` | Irônico, bem-humorado, ligeiramente provocador |
| 4+ | `caotico` | Absurdo, surreal, dramaticamente engraçado |

**Fluxo em `main.py`:**
```python
if fonte == "proativo":
    resp = await self.ollama.gerar_fala_proativa(ciclo=self._ciclos_proativos)
    self._ciclos_proativos += 1   # escala a cada silêncio
    ...

# Ao receber resposta real do usuário:
self._ciclos_proativos = 0        # reseta para ciclo casual
```

**Fallbacks por estágio**: se o Ollama falhar, cada estágio tem uma lista de frases fixas adequadas ao tom (casual / provocativo / caótico).

---

## 12. Reflexão Offline — "Sonho" (Fase 4 — item 10)

**Problema**: Alice só processa e usa a memória quando o usuário está presente. Fatos acumulados não geram novos insights; a memória é passiva.

**Solução**: Durante inatividade prolongada (padrão: 5 minutos), Alice "sonha" — lê todos os fatos da memória, pede ao LLM um insight genuinamente novo, salva como fato e exibe na GUI.

**Arquivo**: `cerebro/sonho.py` (novo)

**Fluxo:**
```
inatividade >= TEMPO_SONHO
    → busca todos os fatos da memória
    → LLM gera insight em 1-2 frases (temp=0.85)
    → salva como "[Reflexão] <insight>"
    → GUI recebe "🌙 *<insight>*"
    → aguarda TEMPO_SONHO antes do próximo sonho
```

**Integração em `main.py`:**
- `self._ultima_interacao` — timestamp atualizado a cada resposta real do usuário
- `self._inatividade_segundos()` — retorna `time.time() - _ultima_interacao`
- `asyncio.create_task(sonho.loop(...))` — inicia em background junto com o heartbeat
- `self._registrar_interacao()` — chamado ao receber input real (não proativo)

**Fallback**: com menos de 3 fatos na memória, o sonho é adiado silenciosamente.

**Variável de ambiente**:
```
TEMPO_SONHO=300   # 5 minutos (padrão)
```

---

## 13. GraphRAG — Memória em Grafo (Fase 4 — item 11)

**Problema**: A memória vetorial armazena fatos isolados. "Marcos é desenvolvedor" e "Marcos usa Python" existem como strings separadas — a Alice não entende que são dois atributos da mesma entidade.

**Solução**: Um grafo de relações explícitas `(sujeito) --[RELACAO]--> (objeto)` permite recuperar tudo que se sabe sobre uma entidade de forma estruturada.

**Arquivo**: `cerebro/graph_memory.py` (novo) — SQLite, sem dependência nova.

**Triplas de exemplo:**
```
(Marcos) --[TRABALHA_EM]--> (empresa de tecnologia)
(Marcos) --[USA]----------> (Python)
(Marcos) --[CRIOU]--------> (projeto Alice)
(Alice)  --[CONHECE]------> (Marcos)
```

**Fluxo de extração** (a cada 3 turnos, em `OllamaClient.extrair_e_salvar_fatos()`):
1. LLM extrai fatos para a memória vetorial (sem mudança)
2. LLM extrai triplas `SUJEITO | RELACAO | OBJETO` da mesma conversa
3. Triplas novas são salvas no grafo (duplicatas ignoradas por UNIQUE constraint)

**Fluxo de recuperação** (a cada turno, em `main.py`):
```python
bloco_grafo = grafo.construir_bloco_grafo(pergunta)
bloco_ctx = bloco_memoria + bloco_grafo + bloco_visao + bloco_audio
```

**Busca por contexto**: extrai palavras-chave da pergunta (≥4 chars) e faz `LIKE` no sujeito e objeto do grafo — retorna até 15 triplas mais recentes.

**Fail-silently**: se nenhuma tripla for encontrada, `construir_bloco_grafo()` retorna `""`.

---

## 14. Variáveis de Ambiente (.env)

| Variável | Padrão | Descrição |
|---|---|---|
| `MODELO_IA` | `llama3` | Modelo Ollama para o LLM principal |
| `VOZ_SUGERIDA` | `pt-BR-FranciscaNeural` | Voz Edge TTS |
| `TTS_RATE` | `-10%` | Velocidade da voz (negativo = mais lento) |
| `TTS_PITCH` | `-5Hz` | Tom da voz (negativo = mais grave) |
| `TEMPO_PROATIVO` | `90` | Segundos de silêncio antes da fala proativa |
| `TEMPO_HEARTBEAT_VISAO` | `120` | Segundos entre capturas do heartbeat de visão |
| `TEMPO_SONHO` | `300` | Segundos de inatividade para acionar a reflexão offline |
| `INDICE_MICROFONE` | _(auto)_ | Índice do dispositivo de entrada (opcional) |
| `LIMIAR_MICROFONE_MINIMO` | `500` | RMS mínimo para detectar fala |
| `DISPOSITIVO_SAIDA_AUDIO` | _(padrão)_ | Nome do dispositivo de saída (SDL) |
| `CLICKHOUSE_HOST` | `localhost` | Host do ClickHouse |
| `CLICKHOUSE_HTTP_PORT` | `8123` | Porta HTTP do ClickHouse |
| `CLICKHOUSE_USER_APP` | `alice` | Usuário do ClickHouse |
| `CLICKHOUSE_PASSWORD_APP` | `alice123` | Senha do ClickHouse |
| `CLICKHOUSE_DB_APP` | `analytics` | Database do ClickHouse |

---

## 15. Roadmap — Status de Implementação

| # | Item | Status | Arquivo(s) |
|---|---|---|---|
| 1 | Prevenção de auto-escuta | ✅ Feito | `TTSService.py`, `MicListener.py`, `audio_ambiente.py`, `main.py` |
| 2 | Rate limiting do TTS | ✅ Feito | `TTSService.py` |
| 3 | Memória vetorial (embeddings) | ✅ Feito | `cerebro/embeddings.py` (novo), `clickhouse_logger.py` |
| 4 | Sumarização periódica do contexto | ✅ Feito | `cerebro/summarizer.py` (novo), `main.py` |
| 5 | Módulo de música (yt-dlp + mpv) | ✅ Feito | `functions/music_tools.py` (novo), `Tools.py`, `OllamaClient.py` |
| 6 | Integração Twitch | ⏳ Pendente | `twitch_bot.py`, `twitch_tools.py` (a criar) |
| 7 | Escalada de estágios proativos | ✅ Feito | `OllamaClient.py`, `main.py` |
| 8 | Vision heartbeat proativo | ✅ Feito | `cerebro/visao.py`, `OllamaClient.py`, `main.py` |
| 9 | TTS 100% local (Kokoro/Coqui/Piper) | ⏳ Pendente | `functions/TTSService.py` |
| 10 | Reflexão offline ("Sonho") | ✅ Feito | `cerebro/sonho.py` (novo), `main.py` |
| 11 | GraphRAG | ✅ Feito | `cerebro/graph_memory.py` (novo), `OllamaClient.py`, `main.py` |

---

## 16. Convenções do Projeto

- **Fail-silently**: recursos opcionais (ClickHouse, nomic, mpv, VTube Studio) falham silenciosamente e loggam WARNING. Alice nunca crasha por causa deles.
- **Thread safety**: operações no ClickHouse usam `threading.Lock()`. Estado do processo mpv idem.
- **Async/sync boundary**: chamadas bloqueantes (Ollama, ClickHouse, microfone) são executadas via `loop.run_in_executor(None, ...)` para não bloquear o loop asyncio.
- **Cancelamento de turno**: `asyncio.Event _cancelar` é verificado em todos os pontos longos de `OllamaClient.pensar()` e `TTSService.falar()`. Permite interromper resposta + fala instantaneamente.
- **Linguagem**: todo código e logs em português brasileiro (PT-BR).
- **Estilo e Linter**: O projeto utiliza `ruff` para formatação (`ruff format .`) e linting (`ruff check .`). Todo agente que editar o código deve rodar o ruff para garantir a padronização.

---

## 17. Registro de Manutenção Contínua (Agentes)

**Abril 2026 - Migração de TTS: edge-tts → OmniVoice:**
- Substituído `edge-tts` (Microsoft Neural TTS, cloud) pelo `OmniVoice` (k2-fsa, TTS local offline, zero-shot, multilíngue).
- `TTSService.py` reescrito: novo parâmetro `instruct` (descrição textual da voz), carregamento lazy do modelo com `_carregar_modelo()`, geração via `model.generate(text, instruct)`, salvamento como WAV com `soundfile`.
- Adicionado método `pre_carregar()` chamado no startup do `main.py` para evitar atraso na primeira fala.
- Arquivo de áudio alterado de `resposta.mp3` para `resposta.wav`; variável de env `VOZ_SUGERIDA` substituída por `VOZ_INSTRUCT`.
- `.env` atualizado: `VOZ_INSTRUCT=female, young adult, portuguese accent` (valores válidos do OmniVoice).
- Instalado no `.venv`: `omnivoice==0.1.4`, `torch==2.8.0+cu128`, `torchaudio==2.8.0+cu128`, `soundfile`.
- Aviso do `pydub` sobre ffmpeg silenciado via `warnings.filterwarnings` em `main.py` (ffmpeg não é utilizado pela Alice).

**Abril 2026 - Correção de Ambiente e Padronização:**
- Resolvido erro de inicialização `ModuleNotFoundError` instalando a dependência `soundfile` diretamente no `.venv`.
- Corrigida violação do linter (24 erros de regra `E402`) no `main.py` ao realocar configurações declarativas e executáveis (`sys.stdout.reconfigure` e `warnings.filterwarnings`) para após os imports padrões.
- Formatação de código do repositório feita executando `ruff format .` e `ruff check . --fix`.
