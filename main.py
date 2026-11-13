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
from cerebro.visao import VisaoComputacional
from functions.ContentManager import ContentManager
from functions.EmotionEngine import EmotionEngine
from functions.MicListener import MicListener
from functions.OllamaClient import OllamaClient
from functions.RespostasProntas import RespostasProntas
from functions.TTSService import TTSService
from functions.VTS_Connector import VTSConnector
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
VISAO_ATIVA = os.getenv("VISAO_ATIVA", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
VTS_ATIVO = os.getenv("VTS_ATIVO", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

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
visao: VisaoComputacional | None = None
vts_connector: VTSConnector | None = None
janela_ref = None
_servicos_inicializados = False


class _FilaSaidaSonhoProxy:
    """
    Proxy sobre FILA_SAIDA_GUI passado ao módulo `Sonho` para garantir que
    qualquer mensagem emitida pelo sonho seja enviada como dict estruturado
    com `categoria="sonho"`, sem precisar alterar `cerebro/sonho.py`.
    """

    def __init__(self, fila_real: queue.Queue):
        self._fila = fila_real

    def put(self, item, block: bool = True, timeout: float | None = None) -> None:
        if isinstance(item, dict):
            item.setdefault("categoria", "sonho")
            self._fila.put(item, block=block, timeout=timeout)
        else:
            self._fila.put(
                {
                    "tipo": "mensagem",
                    "autor": "Alice",
                    "texto": str(item),
                    "categoria": "sonho",
                },
                block=block,
                timeout=timeout,
            )

    def __getattr__(self, name: str):
        return getattr(self._fila, name)


def _instanciar_com_kwargs_compativeis(cls, **kwargs):
    """
    Instancia `cls` passando apenas os kwargs suportados pela sua assinatura atual
    (ou todos se aceitar **kwargs), garantindo compatibilidade entre agentes.
    """
    try:
        sig = inspect.signature(cls.__init__)
        aceita_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in sig.parameters.values()
        )
        if aceita_var_kw:
            return cls(**kwargs)
        filtrados = {
            k: v for k, v in kwargs.items() if k in sig.parameters
        }
        return cls(**filtrados)
    except Exception:
        return cls(**kwargs)


def inicializar_servicos() -> None:
    """
    Inicializa modelo Whisper (CUDA), ClickHouse, Visão, VTS e singletons sob demanda.
    Evita efeitos colaterais pesados de GPU/rede durante um simples `import main`.
    """
    global _servicos_inicializados, modelo_whisper, emotion_engine, clickhouse_log
    global memoria_longa, summarizer, grafo, audio_ambiente, content_manager
    global respostas_prontas, visao, vts_connector

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

    if VISAO_ATIVA:
        try:
            visao = VisaoComputacional()
        except Exception as e:
            log.warning(f"⚠️  [Visão] Falha ao inicializar VisaoComputacional: {e}")
            visao = None
    else:
        visao = None

    if VTS_ATIVO:
        try:
            vts_connector = VTSConnector()
        except Exception as e:
            log.warning(f"⚠️  [VTS] Falha ao instanciar VTSConnector: {e}")
            vts_connector = None
    else:
        vts_connector = None

    _servicos_inicializados = True


# ─────────────────────────────────────────────────────────────────────────────
class AliceSession:
    """
    Encapsula o estado mutável da sessão, integrações multimodais (Visão + VTS)
    e o protocolo de eventos em tempo real com a GUI.
    """

    def __init__(self):
        inicializar_servicos()

        self.historico: list = []
        self.turno_count: int = 0
        self._cancelar = asyncio.Event()
        self._encerrar = asyncio.Event()
        self._ciclos_proativos: int = 0
        self._ultima_interacao: float = 0.0
        self.contexto_comando: str | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._memoria_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._em_processamento_turno: bool = False

        self.visao = visao
        self.vts = vts_connector

        self.ollama = OllamaClient(
            modelo=MODELO_IA,
            emotion_engine=emotion_engine,
            visao=self.visao,
            audio_ambiente=audio_ambiente,
            cancelar_turno=self._cancelar,
            content_manager=content_manager,
        )
        self.tts = _instanciar_com_kwargs_compativeis(
            TTSService,
            instruct=VOZ_INSTRUCT,
            arquivo_audio=ARQUIVO_AUDIO,
            cancelar_turno=self._cancelar,
            dispositivo_saida=DISPOSITIVO_SAIDA_AUDIO,
            alice_falando=_alice_falando,
            on_audio_rms=self._on_lip_sync,
            on_lip_sync=self._on_lip_sync,
            lip_sync_callback=self._on_lip_sync,
        )
        self.mic = _instanciar_com_kwargs_compativeis(
            MicListener,
            modelo_whisper=modelo_whisper,
            indice_mic=INDICE_MICROFONE,
            limiar_min=LIMIAR_MICROFONE_MINIMO,
            fila_gui=FILA_ENTRADA_GUI,
            tempo_proativo=TEMPO_PROATIVO,
            alice_falando=_alice_falando,
            on_estado_change=self._emitir_estado,
            on_estado=self._emitir_estado,
            estado_callback=self._emitir_estado,
        )

        # Garante vínculo de callbacks caso os serviços exponham atributos ou setters
        for attr in ("on_audio_rms", "_on_audio_rms", "on_lip_sync", "lip_sync_callback", "_on_lip_sync"):
            if hasattr(self.tts, attr) and getattr(self.tts, attr) is None:
                setattr(self.tts, attr, self._on_lip_sync)
        if hasattr(self.tts, "set_lip_sync_callback"):
            try:
                self.tts.set_lip_sync_callback(self._on_lip_sync)
            except Exception:
                pass

        if hasattr(self.mic, "registrar_callback_estado"):
            try:
                self.mic.registrar_callback_estado(self._emitir_estado)
            except Exception:
                pass
        for attr in ("on_estado_change", "_on_estado_change", "on_estado", "estado_callback", "_on_estado"):
            if hasattr(self.mic, attr) and getattr(self.mic, attr) is None:
                setattr(self.mic, attr, self._emitir_estado)

    # ── Emissão de Eventos para a GUI (FILA_SAIDA_GUI) ────────────────────────

    def _emitir_estado(self, estado: str) -> None:
        """Envia evento de transição de estado em tempo real para a GUI."""
        FILA_SAIDA_GUI.put({"tipo": "estado", "estado": estado})

    def _emitir_input(self, habilitado: bool) -> None:
        """Habilita ou desabilita o campo de input na GUI."""
        FILA_SAIDA_GUI.put({"tipo": "input", "habilitado": bool(habilitado)})

    def _emitir_mensagem(
        self,
        texto: str,
        autor: str = "Alice",
        categoria: str = "normal",
    ) -> None:
        """Envia mensagem estruturada para a GUI."""
        FILA_SAIDA_GUI.put(
            {
                "tipo": "mensagem",
                "autor": autor,
                "texto": texto,
                "categoria": categoria,
            }
        )

    def _estado_repouso(self) -> str:
        """Retorna o estado padrão de escuta/repouso conforme disponibilidade de áudio."""
        if not self.tts.disponivel:
            return "chat_only"
        esta_mutado = getattr(self.mic, "esta_mutado", None)
        if esta_mutado is not None:
            if bool(esta_mutado):
                return "ocioso"
        else:
            mutado_attr = getattr(self.mic, "mutado", False)
            if hasattr(mutado_attr, "is_set"):
                if mutado_attr.is_set():
                    return "ocioso"
            elif bool(mutado_attr):
                return "ocioso"
        return "ouvindo"

    # ── Integração VTube Studio (Expressões + Lip Sync) ───────────────────────

    def _on_lip_sync(self, abertura: float) -> None:
        """
        Callback de Lip Sync invocado pelo TTSService durante a fala (0.0 a 1.0).
        Pode ser chamado de thread de áudio ou do event loop.
        """
        if not self.vts or not getattr(self.vts, "conectado", False):
            return

        valor = max(0.0, min(1.0, float(abertura)))

        async def _aplicar_boca():
            try:
                for nome_metodo in (
                    "atualizar_lip_sync",
                    "set_mouth_open",
                    "definir_abertura_boca",
                    "lip_sync",
                ):
                    metodo = getattr(self.vts, nome_metodo, None)
                    if callable(metodo):
                        res = metodo(valor)
                        if inspect.isawaitable(res):
                            await res
                        return
                if hasattr(self.vts, "_injetar_parametros"):
                    await self.vts._injetar_parametros(
                        [{"id": "MouthOpen", "value": valor}]
                    )
            except Exception as e:
                log.debug(f"🎭 [VTS LipSync] Falha ao atualizar MouthOpen: {e}")

        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(_aplicar_boca(), self._loop)
            except Exception:
                pass

    async def _vts_ativar_expressao(self, emocao: str) -> None:
        """Ativa expressão no VTube Studio se conectado."""
        if not self.vts or not getattr(self.vts, "conectado", False):
            return
        try:
            await self.vts.ativar_expressao(emocao)
        except Exception as e:
            log.debug(f"🎭 [VTS] Erro ao ativar expressão '{emocao}': {e}")

    async def _vts_resetar_expressao(self) -> None:
        """Reseta expressão do VTube Studio para Neutro após a fala."""
        if not self.vts or not getattr(self.vts, "conectado", False):
            return
        try:
            await self.vts.resetar_expressao()
        except Exception as e:
            log.debug(f"🎭 [VTS] Erro ao resetar expressão: {e}")

    # ── Fala Sincronizada (Estado + TTS + Lip Sync + Desbloqueio de Input) ────

    async def _falar_sincronizado(
        self,
        texto_ou_pronta,
        *,
        is_pronta: bool = False,
        emocao: str | None = None,
        desbloquear_input: bool = True,
    ) -> None:
        """
        Coordena o ciclo completo de fala:
        1) Emite estado 'falando' e ativa expressão no VTS (se houver).
        2) Executa TTS (`tts.falar` ou `respostas_prontas.falar`).
        3) Ao terminar (ou ser interrompido por `parar_fala`/`reiniciar`),
           fecha a boca no VTS, reseta expressão, restaura estado e
           só então envia `{"tipo": "input", "habilitado": True}`.
        """
        self._emitir_estado("falando")
        if emocao:
            self._spawn_task(self._vts_ativar_expressao(emocao))

        try:
            if is_pronta:
                await respostas_prontas.falar(texto_ou_pronta, self.tts)
            else:
                await self.tts.falar(str(texto_ou_pronta))
        finally:
            self._on_lip_sync(0.0)
            if emocao:
                self._spawn_task(self._vts_resetar_expressao())
            self._emitir_estado(self._estado_repouso())
            if desbloquear_input:
                self._emitir_input(True)

    # ── Helpers de Tarefas e Memória ──────────────────────────────────────────

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
        """Extrai fatos/triplas e detecta o nome do usuário de forma serializada."""
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

    # ── Controle de Ações da GUI (parar_fala, toggle_mute, reiniciar, encerrar)

    def _parar_fala_atual(self) -> None:
        """Interrompe imediatamente apenas a fala atual sem reiniciar a conversa."""
        parar_fn = getattr(self.tts, "parar_fala", None)
        if callable(parar_fn):
            try:
                parar_fn()
                log.info("⏹️  [Ação] Fala interrompida pelo usuário.")
                return
            except Exception as e:
                log.warning(f"⚠️  Erro ao chamar tts.parar_fala(): {e}")

        # Fallback seguro caso TTSService ainda não exponha parar_fala()
        try:
            import pygame

            if pygame.mixer.get_init():
                pygame.mixer.stop()
        except Exception:
            pass

    def _alternar_mute_mic(self) -> None:
        """Alterna o estado de mute do MicListener."""
        toggle_fn = getattr(self.mic, "toggle_mute", None) or getattr(
            self.mic, "alternar_mute", None
        )
        if callable(toggle_fn):
            novo_estado = toggle_fn()
        else:
            atual = bool(getattr(self.mic, "mutado", False))
            novo_estado = not atual
            setattr(self.mic, "mutado", novo_estado)
            if novo_estado and hasattr(self.mic, "_parar"):
                self.mic._parar.set()

        status_txt = "🔇 Microfone mutado." if novo_estado else "🎙️ Microfone ativado."
        log.info(f"[Ação] {status_txt}")
        self._emitir_estado(self._estado_repouso())

    def _acionar_reiniciar(self) -> None:
        """
        Consome sinais pendentes em FILA_REINICIAR, interrompe fala/LLM em curso,
        limpa histórico, reseta `contexto_comando` e restaura o estado e input da GUI.
        """
        while not FILA_REINICIAR.empty():
            try:
                FILA_REINICIAR.get_nowait()
            except queue.Empty:
                break

        self._cancelar.set()
        self._parar_fala_atual()
        self.historico.clear()
        self.contexto_comando = None

        for fila in (FILA_ENTRADA_GUI, FILA_SAIDA_GUI):
            while not fila.empty():
                try:
                    fila.get_nowait()
                except queue.Empty:
                    break

        self._emitir_mensagem(
            "🔄 Conversa reiniciada! Pode falar.",
            autor="Alice",
            categoria="sistema",
        )
        self._emitir_estado(self._estado_repouso())
        self._emitir_input(True)
        log.info("🔄 Conversa reiniciada pelo usuário (turno atual cancelado).")

    def _acionar_encerrar(self) -> None:
        """Sinaliza encerramento limpo da sessão sem os._exit(0)."""
        log.info("🛑 [Ação] Encerramento solicitado pela GUI.")
        self._encerrar.set()
        self._cancelar.set()
        self._parar_fala_atual()
        if hasattr(self.mic, "_parar"):
            self.mic._parar.set()

    def _processar_acao_gui(self, acao: str) -> str | None:
        """
        Executa uma ação de controle vinda de `FILA_ENTRADA_GUI`.
        Retorna `"REINICIAR"` ou `"ENCERRAR"` quando o loop principal deve interromper o turno.
        """
        acao = (acao or "").strip().lower()
        if acao == "parar_fala":
            self._parar_fala_atual()
            return "PARAR_FALA"
        if acao == "toggle_mute":
            self._alternar_mute_mic()
            return "TOGGLE_MUTE"
        if acao == "reiniciar":
            self._acionar_reiniciar()
            return "REINICIAR"
        if acao == "encerrar":
            self._acionar_encerrar()
            return "ENCERRAR"
        log.warning(f"⚠️  [GUI] Ação desconhecida ignorada: '{acao}'")
        return None

    def _inspecionar_acoes_entrada_em_andamento(self) -> None:
        """
        Inspeciona `FILA_ENTRADA_GUI` enquanto um turno está em andamento (ex: pensando/falando)
        para processar ações imediatas (`parar_fala`, `toggle_mute`, `reiniciar`, `encerrar`)
        preservando mensagens normais de texto na fila para o próximo turno.
        """
        if FILA_ENTRADA_GUI.empty():
            return

        mensagens_texto: list = []
        while not FILA_ENTRADA_GUI.empty():
            try:
                item = FILA_ENTRADA_GUI.get_nowait()
            except queue.Empty:
                break

            if isinstance(item, dict) and item.get("tipo") == "acao":
                resultado = self._processar_acao_gui(str(item.get("acao", "")))
                if resultado in {"REINICIAR", "ENCERRAR"}:
                    mensagens_texto.clear()
                    break
            else:
                mensagens_texto.append(item)

        for msg in mensagens_texto:
            FILA_ENTRADA_GUI.put(msg)

    def _verificar_reiniciar(self) -> bool:
        """
        Verifica no início do turno se houve pedido de Reiniciar.
        Se `_cancelar` já foi acionado durante o turno anterior, limpa o evento para o novo turno.
        """
        reiniciou = False
        if not FILA_REINICIAR.empty():
            self._acionar_reiniciar()
            reiniciou = True

        if self._cancelar.is_set() and not self._encerrar.is_set():
            self._cancelar.clear()
            return True

        return reiniciou

    async def _monitorar_eventos_controle(self) -> None:
        """
        Monitora `FILA_REINICIAR` e ações de controle em `FILA_ENTRADA_GUI` a cada 50ms
        para reagir imediatamente durante chamadas longas (`ollama.pensar`, `tts.falar`).
        """
        while not self._encerrar.is_set():
            await asyncio.sleep(0.05)
            if not FILA_REINICIAR.empty():
                self._acionar_reiniciar()
            if self._em_processamento_turno:
                self._inspecionar_acoes_entrada_em_andamento()

    # ── Heartbeat de Visão Computacional ──────────────────────────────────────

    async def _loop_heartbeat_visao(self) -> None:
        """
        Captura periodicamente a tela em background quando `visao` está ativa.
        Se detectar mudança relevante durante silêncio, comenta proativamente.
        """
        if not self.visao or TEMPO_HEARTBEAT_VISAO <= 0:
            return

        await asyncio.sleep(TEMPO_HEARTBEAT_VISAO)
        while not self._encerrar.is_set():
            try:
                if (
                    not self._em_processamento_turno
                    and not _alice_falando.is_set()
                    and self._inatividade_segundos() >= 15
                ):
                    await self.visao.descrever_tela()
                    if self.visao.houve_mudanca_relevante():
                        comentario = await self.ollama.gerar_comentario_visao()
                        if comentario and not self._em_processamento_turno:
                            log.info(f"👁️  [Visão Proativa] {comentario}")
                            self._emitir_mensagem(
                                f"👁️ {comentario}",
                                autor="Alice",
                                categoria="proativa",
                            )
                            await self._falar_sincronizado(
                                comentario, desbloquear_input=False
                            )
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug(f"⚠️  [Visão Heartbeat] Erro: {e}")

            await asyncio.sleep(TEMPO_HEARTBEAT_VISAO)

    # ── Hora e Clima ──────────────────────────────────────────────────────────

    async def _processar_hora_clima(self, pergunta: str) -> str:
        """Resolve hora e clima sem bloquear o event loop (executa get_weather em thread)."""
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

    # ── Encerramento Limpo de Recursos ────────────────────────────────────────

    async def _fechar_componente(self, obj, nome: str) -> None:
        """Chama `fechar()` (síncrono ou assíncrono) num serviço se disponível."""
        if obj is None:
            return
        fechar_fn = getattr(obj, "fechar", None) or getattr(
            obj, "desconectar", None
        )
        if not callable(fechar_fn):
            return
        try:
            res = fechar_fn()
            if inspect.isawaitable(res):
                await res
        except Exception as e:
            log.debug(f"Erro ao fechar {nome}: {e}")

    async def _encerrar_recursos(self) -> None:
        """Para reprodução de música, fecha serviços (Mic, TTS, VTS) e cancela tasks."""
        self._encerrar.set()
        self._cancelar.set()

        try:
            await asyncio.to_thread(funcoes_tools.tools.music.parar_musica)
        except Exception as e:
            log.debug(f"Erro ao parar música no encerramento: {e}")

        tasks_ativas = [t for t in self._bg_tasks if not t.done()]
        for task in tasks_ativas:
            task.cancel()
        if tasks_ativas:
            await asyncio.gather(*tasks_ativas, return_exceptions=True)

        await self._fechar_componente(self.mic, "MicListener")
        await self._fechar_componente(self.tts, "TTSService")
        await self._fechar_componente(self.vts, "VTSConnector")
        await self._fechar_componente(audio_ambiente, "AudioAmbiente")

    # ── Loop Principal ─────────────────────────────────────────────────────────

    async def run(self):
        """Inicializa todos os serviços e executa o loop de conversa."""
        global janela_ref

        self._loop = asyncio.get_running_loop()
        self._em_processamento_turno = False

        log.info("--- 🌸 ALICE INICIADA ---")
        log.info(f"Modelo ativo: {MODELO_IA}")

        janela_ref = iniciar_janela(
            FILA_ENTRADA_GUI,
            FILA_SAIDA_GUI,
            emotion_engine,
            memoria_longa,
            fila_reiniciar=FILA_REINICIAR,
            grafo_ref=grafo,
        )
        log.info("🖥️  Janela de chat aberta!")

        if audio_ambiente.iniciar():
            log.info("🔊 Áudio ambiente ativo!")
        else:
            log.warning(
                "⚠️  Áudio ambiente não disponível (sem loopback WASAPI)."
            )

        # Conecta ao VTube Studio de forma graciosa (não bloqueia nem falha se fechado)
        if self.vts is not None:
            try:
                await self.vts.conectar()
            except Exception as e:
                log.warning(f"⚠️  [VTS] VTube Studio indisponível: {e}")

        if self.tts.disponivel:
            log.info("🔊 Modo COMPLETO: voz + chat ativados.")
        else:
            log.warning(
                "🔇 Modo CHAT-ONLY: microfone desativado. Alice responderá apenas pelo chat."
            )

        await self.tts.pre_carregar()

        if self.tts.disponivel:
            await respostas_prontas.preparar(self.tts)

        # Emite estado inicial para a GUI
        self._emitir_estado(self._estado_repouso())
        self._emitir_input(True)

        # Monitor assíncrono de controle (Reiniciar, Parar Fala, Mute, Encerrar)
        self._spawn_task(self._monitorar_eventos_controle())

        # Loop de reflexão offline ("Sonho") em background usando proxy que marca categoria="sonho"
        fila_sonho = _FilaSaidaSonhoProxy(FILA_SAIDA_GUI)
        sonho = Sonho(modelo=MODELO_IA, memoria=memoria_longa)
        self._spawn_task(
            sonho.loop(
                inatividade_fn=self._inatividade_segundos,
                fila_saida_gui=fila_sonho,
                tempo_sonho=TEMPO_SONHO,
            )
        )

        # Loop de heartbeat de Visão Computacional (se ativa)
        if self.visao and getattr(self.visao, "ativa", False):
            self._spawn_task(self._loop_heartbeat_visao())

        self._registrar_interacao()

        try:
            # ── Loop de conversa ──────────────────────────────────────────────
            while not self._encerrar.is_set():
                self._em_processamento_turno = False

                if self._verificar_reiniciar():
                    continue

                self._emitir_estado(self._estado_repouso())

                # 1. Aguarda qualquer tipo de input (voz, GUI, ação de controle ou timer proativo)
                entrada_raw, fonte = await self.mic.obter_pergunta(
                    self._loop, self.tts.disponivel
                )

                if self._encerrar.is_set():
                    break

                if self._verificar_reiniciar():
                    continue

                # Trata dicts estruturados vindos de FILA_ENTRADA_GUI
                if isinstance(entrada_raw, dict):
                    tipo_evento = entrada_raw.get("tipo")
                    if tipo_evento == "acao":
                        acao_res = self._processar_acao_gui(
                            str(entrada_raw.get("acao", ""))
                        )
                        if acao_res == "ENCERRAR":
                            break
                        continue
                    pergunta = str(entrada_raw.get("texto", "")).strip()
                else:
                    pergunta = str(entrada_raw or "").strip()

                self._em_processamento_turno = True

                # Input vazio do mic → fallback curto e continua
                if not pergunta and fonte == "voz":
                    resp = await self._responder_vazio()
                    print(f"IA (Confusa): {resp}")
                    await self._falar_sincronizado(
                        resp, desbloquear_input=False
                    )
                    continue

                # Comando textual de saída
                if pergunta.lower().replace(".", "").strip() in {
                    "sair",
                    "parar",
                    "tchau",
                    "encerrar",
                }:
                    self._acionar_encerrar()
                    break

                # Comportamento proativo (tom escalona a cada ciclo de silêncio)
                if fonte == "proativo":
                    self._emitir_estado("pensando")
                    resp = await self.ollama.gerar_fala_proativa(
                        ciclo=self._ciclos_proativos
                    )
                    if self._cancelar.is_set() or self._encerrar.is_set():
                        continue
                    self._ciclos_proativos += 1
                    log.info(
                        f"💜 [PROATIVO ciclo={self._ciclos_proativos}] {resp}"
                    )
                    self._emitir_mensagem(
                        f"💜 {resp}", autor="Alice", categoria="proativa"
                    )
                    await self._falar_sincronizado(
                        resp, desbloquear_input=False
                    )
                    continue

                # Usuário interagiu — bloqueia input até terminar a fala/resposta
                self._emitir_input(False)
                self._ciclos_proativos = 0
                self._registrar_interacao()

                print(
                    f"{'Você digitou' if fonte == 'gui' else 'Você disse'}: {pergunta}"
                )

                if fonte == "voz" and pergunta:
                    self._emitir_mensagem(
                        pergunta, autor="Você", categoria="normal"
                    )

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
                    self._emitir_mensagem(
                        pronta["texto"], autor="Alice", categoria="normal"
                    )
                    self.historico.append({"role": "user", "content": pergunta})
                    self.historico.append(
                        {"role": "assistant", "content": pronta["texto"]}
                    )
                    if len(self.historico) > MAX_HISTORICO:
                        self.historico = self.historico[-MAX_HISTORICO:]
                    await self._falar_sincronizado(
                        pronta, is_pronta=True, desbloquear_input=True
                    )
                    continue

                # ── INTERCEPTAÇÃO DE COMANDOS LOCAIS ──────────────────────────
                resposta_cmd = None
                falar_cmd = None

                async def _coletar_fala_cmd(msg_fala: str) -> None:
                    nonlocal falar_cmd
                    falar_cmd = msg_fala

                if self.contexto_comando:
                    contexto = self.contexto_comando
                    self.contexto_comando = None

                    from functions.Tools import (
                        execute_empty_recycle_bin,
                        execute_wikipedia_search,
                        execute_youtube_search,
                        tools,
                    )

                    self._emitir_estado("pensando")
                    if contexto == "youtube":
                        resposta_cmd = await execute_youtube_search(
                            pergunta, _coletar_fala_cmd
                        )
                    elif contexto == "wikipedia":
                        resposta_cmd = await execute_wikipedia_search(
                            pergunta, _coletar_fala_cmd
                        )
                    elif contexto == "tocar_musica":
                        msg = await asyncio.to_thread(
                            tools.music.tocar_musica, pergunta
                        )
                        falar_cmd = msg
                        resposta_cmd = f"🎵 {msg}"
                    elif contexto == "volume":
                        try:
                            vol = int(re.findall(r"\d+", pergunta)[0])
                            msg = await asyncio.to_thread(
                                tools.music.volume, vol
                            )
                        except Exception:
                            msg = "Não entendi o volume informado. Deve ser um número de 0 a 100."
                        falar_cmd = msg
                        resposta_cmd = f"🔊 {msg}"
                    elif contexto == "confirmar_lixeira":
                        resposta_cmd = await execute_empty_recycle_bin(
                            pergunta, _coletar_fala_cmd
                        )
                else:
                    from functions.Tools import process_command

                    def definir_contexto(ctx):
                        self.contexto_comando = ctx

                    resposta_cmd = await process_command(
                        pergunta,
                        _coletar_fala_cmd,
                        on_waiting_input=definir_contexto,
                    )

                if resposta_cmd:
                    if resposta_cmd == "EXIT":
                        self._acionar_encerrar()
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

                    self._emitir_mensagem(
                        msg_gui, autor="Alice", categoria="sistema"
                    )
                    if falar_cmd:
                        await self._falar_sincronizado(
                            falar_cmd, desbloquear_input=True
                        )
                    else:
                        self._emitir_estado(self._estado_repouso())
                        self._emitir_input(True)
                    continue
                # ──────────────────────────────────────────────────────────────

                # Inicia processamento pelo LLM → estado "pensando"
                self._emitir_estado("pensando")

                # Diálogo de Hora/Clima: enriquece a pergunta com hora e clima reais
                pergunta_original = pergunta
                if _INTENT_HORA_CLIMA.search(pergunta):
                    pergunta = await self._processar_hora_clima(pergunta)

                if self._cancelar.is_set() or self._encerrar.is_set():
                    continue

                # 2. Descobrir tom de voz / personalidade
                humor = await self.ollama.descobrir_humor(pergunta_original)
                log.debug(f"💡 Humor selecionado: {humor}")

                if self._cancelar.is_set() or self._encerrar.is_set():
                    continue

                # 4. Montar contexto (incluindo Visão Computacional se disponível) e gerar resposta
                ctx_emocional = emotion_engine.construir_prompt_contexto()
                bloco_memoria = memoria_longa.construir_bloco_memoria(
                    pergunta_original
                )
                bloco_grafo = grafo.construir_bloco_grafo(pergunta_original)
                bloco_audio = audio_ambiente.construir_bloco_audio()
                bloco_visao = (
                    await self.visao.construir_bloco_visao()
                    if self.visao
                    else ""
                )
                bloco_ctx = (
                    bloco_memoria + bloco_grafo + bloco_audio + bloco_visao
                )

                resp_texto, tk_p, tk_r = await self.ollama.pensar(
                    pergunta, humor, self.historico, ctx_emocional, bloco_ctx
                )

                # Turno cancelado pelo Reiniciar ou Encerrar — descarta e volta ao início do loop
                if (
                    resp_texto is None
                    or self._cancelar.is_set()
                    or self._encerrar.is_set()
                ):
                    continue

                if not resp_texto or not resp_texto.strip():
                    resp_texto = await self._responder_vazio()
                    log.warning(f"🤔 [Branco] {resp_texto}")
                else:
                    log.info(
                        f"🤖 [{humor}] {resp_texto[:120]} | 📊 {tk_p}↑ + {tk_r}↓ tokens"
                    )

                # 5. Envia resposta estruturada para a GUI
                self._emitir_mensagem(
                    resp_texto, autor="Alice", categoria="normal"
                )

                # 6. EmotionEngine (retorna tag de expressão para o VTube Studio)
                emocao_vts = emotion_engine.analisar_tag_emocao(resp_texto)
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

                # 9. Falar sincronizado (emite 'falando', aplica VTS + Lip Sync, e só desbloqueia input no fim)
                await self._falar_sincronizado(
                    resp_texto, emocao=emocao_vts, desbloquear_input=True
                )

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
