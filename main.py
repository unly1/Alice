"""
main.py — Ponto de entrada e loop principal da Alice.

Responsabilidades deste arquivo:
- Configurar encoding do terminal (Windows)
- Injetar caminhos CUDA no PATH
- Carregar variáveis do .env
- Inicializar o modelo Whisper e todos os serviços sob demanda
- Instanciar AliceSession e iniciar o loop principal

O loop de conversa, o gerenciamento de estado e a lógica de negócio vivem em:
  functions/OllamaClient.py   — chamadas ao LLM (Ollama)
  functions/TTSService.py     — síntese e reprodução de voz
  functions/MicListener.py    — captura de microfone + roteamento de input
  functions/ContentManager.py — personalidades e respostas (cache em memória)
  functions/EmotionEngine.py  — estado emocional persistido
  functions/VTS_Connector.py  — controle do VTube Studio
"""

import asyncio
import inspect
import os
import queue
import re
import site
import sys
import threading
import time
import warnings

from dotenv import load_dotenv

# ── Alice: funções / serviços ─────────────────────────────────────────────────
import functions.Tools as funcoes_tools
from cerebro.audio_ambiente import AudioAmbiente
from cerebro.clickhouse_logger import ClickhouseLogger
from cerebro.graph_memory import GraphMemory
from cerebro.logger import log, set_clickhouse_logger
from cerebro.memoria import MemoriaLongaPrazo
from cerebro.sonho import Sonho
from cerebro.summarizer import Summarizer
from functions.ContentManager import ContentManager
from functions.EmotionEngine import EmotionEngine
from functions.MicListener import MicListener
from functions.OllamaClient import OllamaClient
from functions.RespostasProntas import RespostasProntas
from functions.TTSService import TTSService
from interface.janela import iniciar_janela

# Garante que o terminal Windows lide com caracteres Unicode (ex: resultados de busca web)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Silencia aviso do pydub sobre ffmpeg (dependência do OmniVoice — não é usado pela Alice)
warnings.filterwarnings(
    "ignore", message="Couldn't find ffmpeg or avconv", category=RuntimeWarning
)

# ── Detecção de intenção de tempo/clima ──────────────────────────────────────
_INTENT_HORA_CLIMA = re.compile(
    r"\b("
    r"hora|horas|que horas|que hora|horário|horario"
    r"|tempo|clima|temperatura|previsão|previsao"
    r"|faz frio|faz calor|como está o tempo|chovendo|chover"
    r")\b",
    re.IGNORECASE,
)

# ── Carrega variáveis do .env ─────────────────────────────────────────────────
load_dotenv()


def _configurar_cuda_path() -> None:
    """Injeta diretórios CUDA do .venv no PATH do Windows."""
    try:
        site_packages = next(
            (p for p in sys.path if "site-packages" in p),
            site.getsitepackages()[0],
        )
        for sub in ("cublas", "cudnn"):
            bin_path = os.path.join(site_packages, "nvidia", sub, "bin")
            if bin_path not in os.environ.get("PATH", ""):
                os.environ["PATH"] += os.pathsep + bin_path
    except Exception:
        pass


_configurar_cuda_path()

# ── Configurações lidas do .env (sem I/O pesado no import) ────────────────────
MODELO_IA = os.getenv("MODELO_IA", "llama3")
VOZ_INSTRUCT = os.getenv(
    "VOZ_INSTRUCT", "female, young, soft voice, brazilian portuguese accent"
)
ARQUIVO_AUDIO = os.path.join("audio", "resposta.wav")
TEMPO_PROATIVO = int(os.getenv("TEMPO_PROATIVO", "90"))
TEMPO_HEARTBEAT_VISAO = int(os.getenv("TEMPO_HEARTBEAT_VISAO", "120"))
TEMPO_SONHO = int(os.getenv("TEMPO_SONHO", "300"))
MAX_HISTORICO = 20

_indice_mic = os.getenv("INDICE_MICROFONE")
INDICE_MICROFONE = int(_indice_mic) if _indice_mic and _indice_mic.isdigit() else None

_limiar_min = os.getenv("LIMIAR_MICROFONE_MINIMO", "500")
LIMIAR_MICROFONE_MINIMO = int(_limiar_min) if _limiar_min.isdigit() else 500

DISPOSITIVO_SAIDA_AUDIO = os.getenv("DISPOSITIVO_SAIDA_AUDIO")

# ── Eventos e Filas de comunicação GUI ↔ asyncio ──────────────────────────────
_alice_falando = threading.Event()
FILA_ENTRADA_GUI: queue.Queue = queue.Queue()
FILA_SAIDA_GUI: queue.Queue = queue.Queue()
FILA_REINICIAR: queue.Queue = queue.Queue()

# ── Referências globais de serviços (inicializadas sob demanda) ───────────────
modelo_whisper = None
emotion_engine: EmotionEngine | None = None
clickhouse_log: ClickhouseLogger | None = None
memoria_longa: MemoriaLongaPrazo | None = None
summarizer: Summarizer | None = None
grafo: GraphMemory | None = None
audio_ambiente: AudioAmbiente | None = None
content_manager: ContentManager | None = None
respostas_prontas: RespostasProntas | None = None
janela_ref = None
_servicos_inicializados = False


def inicializar_servicos() -> None:
    """
    Inicializa modelo Whisper (CUDA), ClickHouse e singletons de serviço sob demanda.
    Evita efeitos colaterais pesados de GPU/rede durante um simples `import main`.
    """
    global _servicos_inicializados, modelo_whisper, emotion_engine, clickhouse_log
    global memoria_longa, summarizer, grafo, audio_ambiente, content_manager, respostas_prontas

    if _servicos_inicializados:
        return

    from faster_whisper import WhisperModel

    log.info("⏳ Carregando Whisper (CUDA)...")
    modelo_whisper = WhisperModel("small", device="cuda", compute_type="float16")

    emotion_engine = EmotionEngine()
    log.info(
        f"🧠 EmotionEngine carregado | "
        f"Humor: {emotion_engine.estado_atual['humor']} | "
        f"Amizade: {emotion_engine.estado_atual['amizade_com_usuario']}/100"
    )

    clickhouse_log = ClickhouseLogger()
    set_clickhouse_logger(clickhouse_log)
    emotion_engine.conectar_clickhouse(clickhouse_log)
    memoria_longa = MemoriaLongaPrazo(clickhouse_log)

    summarizer = Summarizer()
    grafo = GraphMemory()
    audio_ambiente = AudioAmbiente(modelo_whisper, alice_falando=_alice_falando)
    content_manager = ContentManager()
    respostas_prontas = RespostasProntas()
    _servicos_inicializados = True


# ─────────────────────────────────────────────────────────────────────────────
class AliceSession:
    """
    Encapsula o estado mutável da sessão e o loop principal de conversa.

    Estado interno:
        historico    — últimas N mensagens trocadas (injetadas no prompt do LLM)
        turno_count  — controla quando extrair fatos e detectar nome (a cada 3 turnos)
        _cancelar    — asyncio.Event compartilhado com OllamaClient e TTSService
                       para cancelar o turno em andamento quando o usuário clica Reiniciar
    """

    def __init__(self):
        inicializar_servicos()

        self.historico: list = []
        self.turno_count: int = 0
        self._cancelar = asyncio.Event()
        self._ciclos_proativos: int = 0
        self._ultima_interacao: float = 0.0
        self.contexto_comando: str | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._memoria_lock = asyncio.Lock()

        self.ollama = OllamaClient(
            modelo=MODELO_IA,
            emotion_engine=emotion_engine,
            visao=None,
            audio_ambiente=audio_ambiente,
            cancelar_turno=self._cancelar,
            content_manager=content_manager,
        )
        self.tts = TTSService(
            instruct=VOZ_INSTRUCT,
            arquivo_audio=ARQUIVO_AUDIO,
            cancelar_turno=self._cancelar,
            dispositivo_saida=DISPOSITIVO_SAIDA_AUDIO,
            alice_falando=_alice_falando,
        )
        self.mic = MicListener(
            modelo_whisper=modelo_whisper,
            indice_mic=INDICE_MICROFONE,
            limiar_min=LIMIAR_MICROFONE_MINIMO,
            fila_gui=FILA_ENTRADA_GUI,
            tempo_proativo=TEMPO_PROATIVO,
            alice_falando=_alice_falando,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _spawn_task(self, coro) -> asyncio.Task:
        """Cria uma task em background mantendo referência viva até concluir."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _registrar_interacao(self) -> None:
        """Atualiza o timestamp da última interação real (voz ou GUI)."""
        self._ultima_interacao = time.time()

    def _inatividade_segundos(self) -> float:
        """Retorna segundos desde a última interação real. Usado pelo Sonho."""
        if self._ultima_interacao == 0.0:
            return 0.0
        return time.time() - self._ultima_interacao

    async def _executar_metodo_ollama_bg(self, metodo, *args) -> None:
        """
        Executa métodos de extração do OllamaClient sem bloquear o event loop
        e sem criar event loops aninhados com asyncio.run().
        """
        if inspect.iscoroutinefunction(metodo):
            await metodo(*args)
        else:
            await asyncio.to_thread(metodo, *args)

    async def _extrair_memoria(self, historico: list) -> None:
        """
        Extrai fatos/triplas e detecta o nome do usuário de forma serializada.
        """
        async with self._memoria_lock:
            try:
                await self._executar_metodo_ollama_bg(
                    self.ollama.extrair_e_salvar_fatos,
                    historico,
                    memoria_longa,
                    grafo,
                )
                await self._executar_metodo_ollama_bg(
                    self.ollama.detectar_nome_usuario,
                    historico,
                )
            except Exception as e:
                log.warning(f"🧠 Falha ao extrair memória em segundo plano: {e}")

    async def _responder_vazio(self) -> str:
        """Retorna uma frase aleatória de fallback quando o input foi vazio."""
        return content_manager.get_resposta_vazia()

    def _acionar_reiniciar(self) -> None:
        """
        Consome todos os sinais pendentes em FILA_REINICIAR, aciona _cancelar.set()
        para interromper imediatamente `ollama.pensar()` ou `tts.falar()` em curso,
        limpa o histórico, reseta `contexto_comando` e esvazia filas pendentes.
        Nota: `_cancelar` permanece setado até o início do próximo turno.
        """
        while not FILA_REINICIAR.empty():
            try:
                FILA_REINICIAR.get_nowait()
            except queue.Empty:
                break

        self._cancelar.set()
        self.historico.clear()
        self.contexto_comando = None

        for fila in (FILA_ENTRADA_GUI, FILA_SAIDA_GUI):
            while not fila.empty():
                try:
                    fila.get_nowait()
                except queue.Empty:
                    break

        FILA_SAIDA_GUI.put("🔄 Conversa reiniciada! Pode falar.")
        log.info("🔄 Conversa reiniciada pelo usuário (turno atual cancelado).")

    def _verificar_reiniciar(self) -> bool:
        """
        Verifica no início do turno se houve pedido de Reiniciar.
        Se _cancelar já foi acionado pelo monitor em background durante o turno anterior,
        apenas limpa o evento para permitir o novo turno.
        """
        reiniciou = False
        if not FILA_REINICIAR.empty():
            self._acionar_reiniciar()
            reiniciou = True

        if self._cancelar.is_set():
            self._cancelar.clear()
            return True

        return reiniciou

    async def _monitorar_reiniciar(self) -> None:
        """
        Monitora FILA_REINICIAR em background a cada 50ms para acionar
        `self._cancelar.set()` imediatamente enquanto um turno está em andamento.
        """
        while True:
            await asyncio.sleep(0.05)
            if not FILA_REINICIAR.empty():
                self._acionar_reiniciar()

    async def _processar_hora_clima(self, pergunta: str) -> str:
        """
        Resolve hora e clima sem bloquear o event loop (executa get_weather em thread).
        """
        cidade = funcoes_tools.tools.location.detectar_cidade_no_texto(pergunta)

        if cidade:
            log.info(f"🗺️  [Hora/Clima] Cidade detectada no texto: '{cidade}'")
        else:
            cidade = funcoes_tools.tools.location.CIDADE_PADRAO
            log.info(f"🗺️  [Hora/Clima] Usando cidade padrão: '{cidade}'")

        try:
            resultado_hora = str(
                funcoes_tools.tools.location.get_current_time(cidade)
            )
        except Exception as e:
            resultado_hora = "horário indisponível no momento"
            log.error(f"🕐 [Tool] get_current_time falhou para '{cidade}': {e}")

        try:
            resultado_clima = str(
                await asyncio.to_thread(
                    funcoes_tools.tools.location.get_weather, cidade
                )
            )
        except Exception as e:
            resultado_clima = "previsão do tempo indisponível no momento"
            log.error(f"🌤️  [Tool] get_weather falhou para '{cidade}': {e}")

        log.info(f"🕐 Hora: {resultado_hora} | 🌤️  Clima: {resultado_clima}")

        return (
            f"{pergunta}\n"
            f"[Cidade: {cidade}]\n"
            f"[RESULTADO get_current_time]: {resultado_hora}\n"
            f"[RESULTADO get_weather]: {resultado_clima}\n"
            "Com base nesses dados, responda de forma natural: "
            "primeiro informe a hora local e depois o relatório de temperatura, "
            "fazendo uma transição natural entre os dois. "
            "Não cite nomes de marcadores [TOOL] ou [RESULTADO] na resposta."
        )

    async def _encerrar_recursos(self) -> None:
        """Para reprodução de música e cancela tasks de background no shutdown."""
        try:
            await asyncio.to_thread(funcoes_tools.tools.music.parar_musica)
        except Exception as e:
            log.debug(f"Erro ao parar música no encerramento: {e}")

        tasks_ativas = [t for t in self._bg_tasks if not t.done()]
        for task in tasks_ativas:
            task.cancel()
        if tasks_ativas:
            await asyncio.gather(*tasks_ativas, return_exceptions=True)

    # ── Loop Principal ─────────────────────────────────────────────────────────

    async def run(self):
        """Inicializa todos os serviços e executa o loop de conversa."""
        global janela_ref

        log.info("--- 🌸 ALICE INICIADA ---")
        log.info(f"Modelo ativo: {MODELO_IA}")

        janela_ref = iniciar_janela(
            FILA_ENTRADA_GUI,
            FILA_SAIDA_GUI,
            emotion_engine,
            memoria_longa,
            fila_reiniciar=FILA_REINICIAR,
        )
        log.info("🖥️  Janela de chat aberta!")

        if audio_ambiente.iniciar():
            log.info("🔊 Áudio ambiente ativo!")
        else:
            log.warning(
                "⚠️  Áudio ambiente não disponível (sem loopback WASAPI)."
            )

        if self.tts.disponivel:
            log.info("🔊 Modo COMPLETO: voz + chat ativados.")
        else:
            log.warning(
                "🔇 Modo CHAT-ONLY: microfone desativado. Alice responderá apenas pelo chat."
            )

        await self.tts.pre_carregar()

        if self.tts.disponivel:
            await respostas_prontas.preparar(self.tts)

        loop = asyncio.get_running_loop()

        # Monitor assíncrono do botão Reiniciar (cancela turnos em andamento)
        self._spawn_task(self._monitorar_reiniciar())

        # Loop de reflexão offline ("Sonho") em background
        sonho = Sonho(modelo=MODELO_IA, memoria=memoria_longa)
        self._spawn_task(
            sonho.loop(
                inatividade_fn=self._inatividade_segundos,
                fila_saida_gui=FILA_SAIDA_GUI,
                tempo_sonho=TEMPO_SONHO,
            )
        )
        self._registrar_interacao()

        try:
            # ── Loop de conversa ──────────────────────────────────────────────
            while True:
                if self._verificar_reiniciar():
                    continue

                # 1. Aguarda qualquer tipo de input (voz, GUI ou timer proativo)
                pergunta, fonte = await self.mic.obter_pergunta(
                    loop, self.tts.disponivel
                )

                if self._verificar_reiniciar():
                    continue

                # Input vazio do mic → fallback curto e continua
                if not pergunta and fonte == "voz":
                    resp = await self._responder_vazio()
                    print(f"IA (Confusa): {resp}")
                    await self.tts.falar(resp)
                    continue

                # Comando de saída
                if pergunta.lower().replace(".", "").strip() in {
                    "sair",
                    "parar",
                    "tchau",
                    "encerrar",
                }:
                    break

                # Comportamento proativo (tom escalona a cada ciclo de silêncio)
                if fonte == "proativo":
                    resp = await self.ollama.gerar_fala_proativa(
                        ciclo=self._ciclos_proativos
                    )
                    if self._cancelar.is_set():
                        continue
                    self._ciclos_proativos += 1
                    log.info(
                        f"💜 [PROATIVO ciclo={self._ciclos_proativos}] {resp}"
                    )
                    FILA_SAIDA_GUI.put(f"💜 {resp}")
                    await self.tts.falar(resp)
                    continue

                # Usuário respondeu — reseta escalada proativa e atualiza inatividade
                self._ciclos_proativos = 0
                self._registrar_interacao()

                print(
                    f"{'Você digitou' if fonte == 'gui' else 'Você disse'}: {pergunta}"
                )

                if fonte == "voz" and pergunta:
                    FILA_SAIDA_GUI.put(f"🎤 Você disse: {pergunta}")

                # ── RESPOSTAS PRONTAS (áudio já gerado, sem passar pelo LLM) ──
                pronta = (
                    None
                    if self.contexto_comando
                    else respostas_prontas.buscar(pergunta)
                )
                if pronta:
                    log.info(
                        f"⚡ [Resposta pronta: {pronta['id']}] {pronta['texto'][:80]}"
                    )
                    FILA_SAIDA_GUI.put(pronta["texto"])
                    self.historico.append({"role": "user", "content": pergunta})
                    self.historico.append(
                        {"role": "assistant", "content": pronta["texto"]}
                    )
                    if len(self.historico) > MAX_HISTORICO:
                        self.historico = self.historico[-MAX_HISTORICO:]
                    await respostas_prontas.falar(pronta, self.tts)
                    continue

                # ── INTERCEPTAÇÃO DE COMANDOS LOCAIS ──────────────────────────
                resposta_cmd = None

                if self.contexto_comando:
                    contexto = self.contexto_comando
                    self.contexto_comando = None

                    from functions.Tools import (
                        execute_empty_recycle_bin,
                        execute_wikipedia_search,
                        execute_youtube_search,
                        tools,
                    )

                    if contexto == "youtube":
                        resposta_cmd = await execute_youtube_search(
                            pergunta, self.tts.falar
                        )
                    elif contexto == "wikipedia":
                        resposta_cmd = await execute_wikipedia_search(
                            pergunta, self.tts.falar
                        )
                    elif contexto == "tocar_musica":
                        msg = await asyncio.to_thread(
                            tools.music.tocar_musica, pergunta
                        )
                        await self.tts.falar(msg)
                        resposta_cmd = f"🎵 {msg}"
                    elif contexto == "volume":
                        try:
                            vol = int(re.findall(r"\d+", pergunta)[0])
                            msg = await asyncio.to_thread(
                                tools.music.volume, vol
                            )
                        except Exception:
                            msg = "Não entendi o volume informado. Deve ser um número de 0 a 100."
                        await self.tts.falar(msg)
                        resposta_cmd = f"🔊 {msg}"
                    elif contexto == "confirmar_lixeira":
                        resposta_cmd = await execute_empty_recycle_bin(
                            pergunta, self.tts.falar
                        )
                else:
                    from functions.Tools import process_command

                    def definir_contexto(ctx):
                        self.contexto_comando = ctx

                    resposta_cmd = await process_command(
                        pergunta,
                        self.tts.falar,
                        on_waiting_input=definir_contexto,
                    )

                if resposta_cmd:
                    if resposta_cmd == "EXIT":
                        break

                    if resposta_cmd.startswith("WAIT:"):
                        ctx = resposta_cmd.split(":")[1]
                        if ctx == "youtube":
                            msg_gui = "🎥 O que deseja buscar no YouTube?"
                        elif ctx == "wikipedia":
                            msg_gui = "📚 O que deseja pesquisar na Wikipedia?"
                        elif ctx == "tocar_musica":
                            msg_gui = "🎵 Qual música você quer ouvir?"
                        elif ctx == "volume":
                            msg_gui = "🔊 Qual o nível do volume?"
                        elif ctx == "confirmar_lixeira":
                            msg_gui = "🗑️ Confirmar esvaziamento da lixeira? (diga 'confirmar' ou 'cancelar')"
                        else:
                            msg_gui = "Aguardando entrada..."
                    else:
                        msg_gui = resposta_cmd

                    FILA_SAIDA_GUI.put(msg_gui)
                    continue
                # ──────────────────────────────────────────────────────────────

                # Diálogo de Hora/Clima: enriquece a pergunta com hora e clima reais
                pergunta_original = pergunta
                if _INTENT_HORA_CLIMA.search(pergunta):
                    pergunta = await self._processar_hora_clima(pergunta)

                if self._cancelar.is_set():
                    continue

                # 2. Descobrir tom de voz / personalidade
                humor = await self.ollama.descobrir_humor(pergunta_original)
                log.debug(f"💡 Humor selecionado: {humor}")

                if self._cancelar.is_set():
                    continue

                # 4. Montar contexto e gerar resposta
                ctx_emocional = emotion_engine.construir_prompt_contexto()
                bloco_memoria = memoria_longa.construir_bloco_memoria(
                    pergunta_original
                )
                bloco_grafo = grafo.construir_bloco_grafo(pergunta_original)
                bloco_audio = audio_ambiente.construir_bloco_audio()
                bloco_ctx = bloco_memoria + bloco_grafo + bloco_audio

                resp_texto, tk_p, tk_r = await self.ollama.pensar(
                    pergunta, humor, self.historico, ctx_emocional, bloco_ctx
                )

                # Turno cancelado pelo Reiniciar — descarta e volta ao início do loop
                if resp_texto is None or self._cancelar.is_set():
                    continue

                if not resp_texto or not resp_texto.strip():
                    resp_texto = await self._responder_vazio()
                    log.warning(f"🤔 [Branco] {resp_texto}")
                else:
                    log.info(
                        f"🤖 [{humor}] {resp_texto[:120]} | 📊 {tk_p}↑ + {tk_r}↓ tokens"
                    )

                # 5. Envia resposta para a GUI
                FILA_SAIDA_GUI.put(resp_texto)

                # 6. EmotionEngine
                emotion_engine.analisar_tag_emocao(resp_texto)
                emotion_engine.registrar_turno(
                    f"Usuário disse: '{pergunta_original[:60]}...' Alice respondeu como: {humor}"
                )

                # 7. Atualizar histórico da sessão
                self.historico.append(
                    {"role": "user", "content": pergunta_original}
                )
                self.historico.append(
                    {"role": "assistant", "content": resp_texto}
                )

                if len(self.historico) >= MAX_HISTORICO - 4:
                    self.historico = await summarizer.compactar_se_necessario(
                        self.historico, MODELO_IA, limiar=MAX_HISTORICO - 4
                    )

                if len(self.historico) > MAX_HISTORICO:
                    self.historico = self.historico[-MAX_HISTORICO:]

                log.debug(f"📝 Histórico: {len(self.historico) // 2} trocas")

                # 8b. Salvar interação completa no ClickHouse (assíncrono, fail-silently)
                self._spawn_task(
                    clickhouse_log.registrar_turno(
                        pergunta=pergunta_original,
                        resposta=resp_texto,
                        humor=humor,
                        model=MODELO_IA,
                        tokens_prompt=tk_p,
                        tokens_resposta=tk_r,
                    )
                )

                # 9. Falar
                await self.tts.falar(resp_texto)

                # 10. Extrair fatos + detectar nome a cada 3 turnos, em segundo plano
                self.turno_count += 1
                if self.turno_count % 3 == 0:
                    self._spawn_task(
                        self._extrair_memoria(list(self.historico))
                    )
        finally:
            await self._encerrar_recursos()


# ── Entry point ───────────────────────────────────────────────────────────────


async def main():
    session = AliceSession()
    await session.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("\n🌙 Alice desligando...")