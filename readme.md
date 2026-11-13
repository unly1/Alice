# 🌸 Projeto Alice: Assistente Local Inteligente

### Por que "Alice"?
"ALICE" é a fundação conceitual deste projeto:
- **Conceito:** Uma Inteligência Artificial Autônoma e Altamente Adaptativa.
- **Origem:** **A**rtificial **L**abile **I**ntelligence **C**ybernated **E**xistence.

As iniciais formam o acrônimo **ALICE**!

---

## 🌟 O que é a Alice?

A **Alice** não é apenas um chatbot ou leitor de texto em voz alta. Ela é uma **arquitetura de presença digital viva, orgânica e reativa**. 

Executada inteiramente de forma local (utilizando os pesos do Whisper, Llama 3.1 e OmniVoice na GPU), ela une Inteligência Artificial avançada a uma personificação virtual de alta fidelidade (VTubing via Live2D) com foco contínuo em baixa latência e concorrência segura.

O organismo da Alice é composto por **seis pilares fundamentais**:

1. **A Audição Atenta (Reconhecimento de Voz + Áudio Ambiente):** Escuta calibrada e contínua via `pyaudiowpatch` com stream PortAudio persistente. O Whisper (CUDA em float16 com trava de GPU unificada `WHISPER_LOCK`) transcreve comandos de voz sem corte de palavras. Além do microfone, ouve o áudio do sistema (WASAPI Loopback) com descarte automático de janelas durante a própria fala.
2. **A Visão Situacional (Screen Capture + LLaVA):** Captura de tela via `mss` integrada ao modelo multimodal (`llava:7b` / `moondream`), contextualizando as respostas da Alice com o que está visível no monitor do usuário.
3. **A Mente Expandida (ReAct Pattern & Ferramentas do SO):** O cérebro da Alice executa comandos operacionais (relógio, janelas, volume, clima, gerenciador de lixeira em duas etapas e pesquisas) via marcação ReAct `[TOOL:nome:arg]` e atalhos de voz locais instantâneos.
4. **O Sistema Emocional e a Memória Híbrida:** Memória de longo prazo resiliente em ClickHouse (com deduplicação determinística via UUID5) e SQLite (com WAL mode e integridade garantida). Relações entre entidades são mapeadas no Grafo (`GraphMemory`). A `EmotionEngine` analisa entrelinhas, gerencia o afeto acumulado e expressa o humor da Alice.
5. **O Comportamento Proativo e os "Sonhos":** Após períodos de inatividade (`TEMPO_PROATIVO`), a Alice inicia conversas ou comenta novidades. Nos momentos de repouso, o módulo de "Sonho" reflete sobre interações passadas e consolida novos aprendizados na memória.
6. **O Corpo e a Expressão (VTube Studio & GUI Reativa):** Integração ativa via WebSocket com o **VTube Studio** (`functions/VTS_Connector.py`). A voz sintetizada frase a frase pelo **OmniVoice** gera modulação labial direta (Lip Sync RMS a ~25 Hz) e ativa expressões faciais Live2D instantâneas. Uma interface escura moderna em `customtkinter` fornece visualização de status reativo em 7 estados, chat copiável, controle de áudio/microfone e visualização de memórias/grafo.

---

## 🏗️ Arquitetura Técnica

A Alice é construída em camadas modulares e isoladas. Todas as operações com banco de dados, inferência de visão e geração de áudio são desacopladas da thread principal da interface gráfica por meio de filas thread-safe (`FILA_SAIDA_GUI`) e loops assíncronos (`asyncio`).

### Estrutura de Diretórios

```
Alice/
├── main.py                     # Orquestrador central e loop assíncrono (AliceSession)
├── gerenciar_memoria.py        # CLI administrativo para memória e fatos
├── respostas_prontas.json      # Catálogo de 9 intenções imediatas com áudio pré-sintetizado
├── requirements.txt            # Dependências fixadas e compatíveis com CUDA
├── .env                        # Chaves, credenciais e parâmetros locais (não commitado)
│
├── functions/                  # Módulos operacionais e de hardware
│   ├── MicListener.py          # Microfone, calibração contínua e Whisper (CUDA)
│   ├── TTSService.py           # Síntese OmniVoice frase a frase, mixer persistente e callback RMS
│   ├── VTS_Connector.py        # WebSocket VTube Studio, Lip Sync (~25Hz) e expressões Live2D
│   ├── OllamaClient.py         # LLM (Llama 3.1), heurística local de humor, ReAct e keep-alive
│   ├── EmotionEngine.py        # Tagger de humor, amizade e escrita unificada por turno
│   ├── ContentManager.py       # Gerenciamento em cache de personalidades e respostas
│   ├── RespostasProntas.py     # Despachador de respostas instantâneas sem passar pelo LLM
│   └── Tools.py                # Ferramentas do sistema (música, relógio, clima, lixeira segura)
│
├── cerebro/                    # Cognição, memória e percepção
│   ├── memoria.py              # Memória de longo prazo híbrida (ClickHouse + SQLite WAL)
│   ├── memoria.db              # SQLite local: fatos de longo prazo e estado atual
│   ├── graph_memory.py         # Base de conhecimento em grafo (triplas semânticas)
│   ├── grafo.db                # SQLite local do grafo de relações
│   ├── clickhouse_logger.py    # Persistência analítica estruturada no ClickHouse
│   ├── audio_ambiente.py       # WASAPI Loopback com reamostragem e anti-aliasing
│   ├── visao.py                # Captura de tela e análise visual via LLaVA
│   ├── summarizer.py           # Resumo automático do histórico de conversas antigas
│   ├── sonho.py                # Módulo de reflexão pós-sessão e consolidação de fatos
│   ├── embeddings.py           # Embeddings vetoriais locais (nomic-embed-text)
│   ├── logger.py               # Configuração centralizada de Loguru
│   └── clickhouse/             # Orquestração do ClickHouse no Docker Desktop
│       ├── docker-compose.yaml # Compose oficial (24.8-LTS, bind seguro 127.0.0.1)
│       └── init.sql            # Definição DDL de analytics.interactions, memoria, logs e estado
│
├── interface/
│   └── janela.py               # GUI customtkinter: 7 estados, chat copiável, abas de memória e grafo
│
├── personalidades/             # Prompts de sistema formatados por humor (.txt)
│   ├── alegre.txt, curiosa.txt, brava.txt, empatica.txt, etc.
│
├── respostas/                  # Respostas complementares e fallbacks (.txt)
│   └── respostas_vazias.txt
│
├── tools/                      # Utilitários de diagnóstico, infraestrutura e banco
│   ├── autenticar_vts.py       # Autenticação interativa e diagnóstico com VTube Studio
│   ├── configurar_clickhouse.py# Setup automatizado de credenciais e container Docker
│   ├── backup_banco.py         # Backup rotativo de ClickHouse, SQLite e .env
│   ├── restaurar_banco.py      # Restauração com mescla segura de credenciais
│   └── listar_dispositivos_audio.py
│
└── tests/                      # Suíte de testes automatizados (107 testes unitários e de integração)
    ├── test_tools.py           # Validação de regexes, whitelist e segurança de comandos
    ├── test_memoria.py         # Deduplicação UUID5, isolamento de sentinelas e grafo
    ├── test_vts.py             # Mocks de autenticação pyvts, clamping de lip sync e expressões
    ├── test_respostas.py       # Resolução de 9 intenções prontas e fallbacks
    ├── test_ollama_client.py   # Heurística léxica local (0 chamadas LLM) e loop ReAct
    └── test_audio_locks.py     # Integridade de WHISPER_LOCK e cálculo de RMS numpy
```

---

## 🧩 Camadas e Responsabilidades

| Camada | Arquivo Principal | Detalhes Técnicos |
|---|---|---|
| **Audição (Mic)** | `functions/MicListener.py` | PortAudio persistente, calibração dinâmica de ruído, suporte a Mute e Whisper CUDA com lock exclusivo. |
| **Audição (Sistema)** | `cerebro/audio_ambiente.py` | WASAPI Loopback com descarte de janelas sobrepostas à fala do TTS e reamostragem sem serrilhamento. |
| **Visão Computacional** | `cerebro/visao.py` | Captura de tela assíncrona com `mss` e inferência de visão sem bloqueio de I/O. |
| **Raciocínio & LLM** | `functions/OllamaClient.py` | Loop ReAct de no máximo 2 turnos, keep-alive configurável, cancelamento ágil e classificação de humor léxica local sem latência externa. |
| **Voz (TTS)** | `functions/TTSService.py` | **OmniVoice** frase a frase em float16 na GPU (`TTS_DEVICE=cuda`), `pygame.mixer` persistente, interrupção imediata (`parar_fala`) e callback de RMS a ~25 Hz. |
| **VTuber & Lip Sync** | `functions/VTS_Connector.py` | Conexão WebSocket na porta 8001 via `pyvts`, injeção direta de parâmetros (`MouthOpen`, `MouthSmile`, sobrancelhas e olhos) e modulação labial contínua. |
| **Respostas Prontas** | `functions/RespostasProntas.py` | 9 intenções fixas instantâneas (apresentação, horas, criador, ajuda, etc.) despachadas diretamente sem custo de LLM. |
| **Memória & Grafo** | `cerebro/memoria.py` e `graph_memory.py` | Sincronização híbrida ClickHouse/SQLite com modo WAL, deduplicação estável por UUID5 e buscas semânticas sem distinção de caixa. |
| **Interface de Usuário** | `interface/janela.py` | CustomTkinter dark, indicador reativo de 7 estados (Ouvindo, Pensando, Falando, etc.), balões selecionáveis/copiáveis e aba interativa do grafo. |
| **Qualidade & Testes** | `tests/` | Suíte com 107 testes executados em ~1.2s via `unittest`, garantindo 100% de estabilidade e conformidade com `ruff`. |

---

## ⚡ Otimizações de Latência e Desempenho

- **Zero Chamadas LLM para Humor:** A detecção de humor básica utiliza heurísticas léxicas locais rápidas em `OllamaClient.py`, economizando centenas de milissegundos por turno.
- **Lip Sync por Modulação RMS:** O cálculo de energia acústica em tempo real via `numpy` alimenta a abertura da boca no VTube Studio sem necessidade de modelos pesados de tracking facial.
- **Proteção de VRAM e Locks CUDA:** O uso unificado de `WHISPER_LOCK` impede contenção na GPU durante inferências concorrentes entre o microfone, áudio ambiente e TTS.
- **Keep-Alive do Ollama:** O modelo principal permanece aquecido na memória (`OLLAMA_KEEP_ALIVE=30m`), evitando o atraso de carregamento de pesos a cada turno.

---

## 🛠️ Instalação e Execução

### Pré-requisitos
- **Windows 10/11** com Python 3.11 instalado.
- **NVIDIA GPU** com drivers atualizados e suporte a CUDA (recomendado 8 GB+ VRAM).
- **Docker Desktop** ativo (para o ClickHouse analítico).
- **Ollama** instalado e executando os modelos (`llama3.1:8b`, `nomic-embed-text`).
- **VTube Studio** (opcional, para visualização do avatar Live2D).

### Passo a Passo

1. **Configurar Ambiente Virtual e Dependências:**
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configurar o ClickHouse Analítico:**
   ```powershell
   python tools/configurar_clickhouse.py
   ```
   *O script configura credenciais no `.env`, sobe o container Docker (`127.0.0.1:8123`) e cria as tabelas necessárias.*

3. **Autenticar com o VTube Studio:**
   Abra o VTube Studio, ative a API WebSocket na porta 8001 (Configurações ⚙️) e execute:
   ```powershell
   python tools/autenticar_vts.py
   ```
   *Clique em **Allow** no pop-up do VTube Studio para gravar o token de acesso no `.env`.*

4. **Executar a Suíte de Testes:**
   ```powershell
   python -m unittest discover tests -v
   ```

5. **Iniciar a Alice:**
   ```powershell
   python main.py
   ```

---

## 💾 Rotina de Backup e Restauração

O projeto conta com ferramentas de backup para garantir que históricos, memórias e credenciais não sejam perdidos:

- **Backup Completo:**
  ```powershell
  python tools/backup_banco.py
  ```
  *Exporta todas as tabelas do ClickHouse, copia `memoria.db`, `grafo.db` e `.env` para `D:\Backups\Alice\alice_<data>_<hora>\`.*

- **Restauração de Dados:**
  ```powershell
  python tools/restaurar_banco.py
  ```
  *Restaura o backup mais recente preservando com segurança credenciais da máquina atual.*

- **Agendamento Automático no Windows (Opcional):**
  ```powershell
  schtasks /Create /SC DAILY /ST 23:00 /TN "Alice Backup" /TR "C:\Users\**\Documents\Alice\.venv\Scripts\python.exe C:\Users\**\Documents\Alice\tools\backup_banco.py"
  ```
