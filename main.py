"""
main.py — Ponto de entrada e loop principal da Alice.

Responsabilidades deste arquivo:
- Configurar encoding do terminal (Windows)
- Injetar caminhos CUDA no PATH
- Carregar variáveis do .env
- Inicializar o modelo Whisper e todos os serviços
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
import os
import queue
import re
import site
import sys
import threading
import warnings

from dotenv import load_dotenv
from faster_whisper import WhisperModel

# ── Alice: funções / serviços ─────────────────────────────────────────────────
import functions.Tools as funcoes_tools
from functions.ContentManager import ContentManager
from functions.EmotionEngine import EmotionEngine
from functions.MicListener import MicListener
from functions.OllamaClient import OllamaClient
from functions.TTSService import TTSService
from functions.VTS_Connector import VTSConnector
from cerebro.audio_ambiente import AudioAmbiente
from cerebro.clickhouse_logger import ClickhouseLogger
from cerebro.logger import log, set_clickhouse_logger
from cerebro.memoria import MemoriaLongaPrazo
from cerebro.graph_memory import GraphMemory
from cerebro.sonho import Sonho
from cerebro.summarizer import Summarizer
from cerebro.visao import VisaoComputacional
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
# Quando detectadas, o loop inicia um diálogo de dois passos:
#   1) Alice pergunta a cidade.
#   2) Usuário responde → Alice chama as ferramentas e responde.
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

# ── INJEÇÃO CUDA ──────────────────────────────────────────────────────────────
# Para Windows reconhecer as bibliotecas baixadas no .venv
try:
    site_packages = next(
        (p for p in sys.path if "site-packages" in p), site.getsitepackages()[
            0]
    )
    os.environ["PATH"] += os.pathsep + os.path.join(
        site_packages, "nvidia", "cublas", "bin"
    )
    os.environ["PATH"] += os.pathsep + os.path.join(
        site_packages, "nvidia", "cudnn", "bin"
    )
except Exception:
    pass

# ── Carrega modelo Whisper (GPU) ──────────────────────────────────────────────
log.info("⏳ Carregando Whisper (CUDA)...")
modelo_whisper = WhisperModel("small", device="cuda", compute_type="float16")

# ── Configurações lidas do .env ───────────────────────────────────────────────
MODELO_IA = os.getenv("MODELO_IA", "llama3")
VOZ_INSTRUCT = os.getenv(
    "VOZ_INSTRUCT", "female, young, soft voice, brazilian portuguese accent"
)
ARQUIVO_AUDIO = os.path.join("audio", "resposta.wav")
TEMPO_PROATIVO = int(os.getenv("TEMPO_PROATIVO", "90"))
TEMPO_HEARTBEAT_VISAO = int(os.getenv("TEMPO_HEARTBEAT_VISAO", "120"))
# 5 min de inatividade para sonhar
TEMPO_SONHO = int(os.getenv("TEMPO_SONHO", "300"))
MAX_HISTORICO = 20

_indice_mic = os.getenv("INDICE_MICROFONE")
INDICE_MICROFONE = int(
    _indice_mic) if _indice_mic and _indice_mic.isdigit() else None

_limiar_min = os.getenv("LIMIAR_MICROFONE_MINIMO", "500")
LIMIAR_MICROFONE_MINIMO = int(_limiar_min) if _limiar_min.isdigit() else 500

DISPOSITIVO_SAIDA_AUDIO = os.getenv("DISPOSITIVO_SAIDA_AUDIO")

# ── Instanciação dos serviços globais ─────────────────────────────────────────
vts_connector = VTSConnector()

emotion_engine = EmotionEngine()
log.info(
    f"🧠 EmotionEngine carregado | "
    f"Humor: {emotion_engine.estado_atual['humor']} | "
    f"Amizade: {emotion_engine.estado_atual['amizade_com_usuario']}/100"
)

# ClickHouse como sink de log e memória primária; SQLite como backup
clickhouse_log = ClickhouseLogger()
set_clickhouse_logger(clickhouse_log)
memoria_longa = MemoriaLongaPrazo(clickhouse_log)

visao = VisaoComputacional()
summarizer = Summarizer()
grafo = GraphMemory()

# Sinaliza para MicListener e AudioAmbiente que Alice está falando (previne auto-escuta)
_alice_falando = threading.Event()

audio_ambiente = AudioAmbiente(modelo_whisper, alice_falando=_alice_falando)

# ── ContentManager: personalidades + respostas (cache em memória) ─────────────
# Carregado uma vez aqui e compartilhado com OllamaClient.
# Chame content_manager.recarregar() a qualquer momento para hot-reload.
content_manager = ContentManager()

# ── Filas de comunicação GUI ↔ asyncio ────────────────────────────────────────
# GUI → asyncio (mensagens do usuário)
FILA_ENTRADA_GUI: queue.Queue = queue.Queue()
# asyncio → GUI (respostas da Alice)
FILA_SAIDA_GUI: queue.Queue = queue.Queue()
# GUI → asyncio (sinal de reiniciar)
FILA_REINICIAR: queue.Queue = queue.Queue()

# Referência à janela (preenchida em AliceSession.run())
janela_ref = None


# ─────────────────────────────────────────────────────────────────────────────
class AliceSession:
    """
    Encapsula o estado mutable da sessão e o loop principal de conversa.

    Estado interno:
        historico    — últimas N mensagens trocadas (injetadas no prompt do LLM)
        turno_count  — controla quando extrair fatos e detectar nome (a cada 3 turnos)
        _cancelar    — asyncio.Event compartilhado com OllamaClient e TTSService
                       para cancelar o turno em andamento quando o usuário clica Reiniciar

    Serviços instanciados aqui (recebem _cancelar via construtor):
        ollama       — OllamaClient
        tts          — TTSService
        mic          — MicListener
    """

    def __init__(self):
        self.historico: list = []
        self.turno_count: int = 0
        self._cancelar = asyncio.Event()
        self._ciclos_proativos: int = 0  # escalada de tom durante silêncio
        # timestamp da última interação real (para sonho)
        self._ultima_interacao: float = 0.0

        self.ollama = OllamaClient(
            modelo=MODELO_IA,
            emotion_engine=emotion_engine,
            visao=visao,
            audio_ambiente=audio_ambiente,
            cancelar_turno=self._cancelar,
            content_manager=content_manager,  # cache compartilhado
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

    def _registrar_interacao(self):
        """Atualiza o timestamp da última interação real (voz ou GUI)."""
        import time

        self._ultima_interacao = time.time()

    def _inatividade_segundos(self) -> float:
        """Retorna segundos desde a última interação real. Usado pelo Sonho."""
        import time

        if self._ultima_interacao == 0.0:
            return 0.0
        return time.time() - self._ultima_interacao

    async def _heartbeat_visao_loop(self):
        """
        Task em background: captura a tela periodicamente e, se detectar
        uma mudança relevante, injeta um token especial na fila de entrada
        para que o loop principal gere um comentário espontâneo.

        Cooldown: TEMPO_HEARTBEAT_VISAO segundos (padrão 120s).
        Não dispara enquanto Alice estiver falando (_alice_falando).
        """
        await asyncio.sleep(TEMPO_HEARTBEAT_VISAO)  # aguarda estabilização inicial
        while True:
            await asyncio.sleep(TEMPO_HEARTBEAT_VISAO)
            if not visao.ativa:
                continue
            if _alice_falando.is_set():
                continue
            await visao.descrever_tela(forcar=True)
            if visao.houve_mudanca_relevante():
                log.info(
                    "👁️  [Heartbeat] Mudança na tela detectada — gerando comentário."
                )
                FILA_ENTRADA_GUI.put("__heartbeat_visao__")

    async def _responder_vazio(self) -> str:
        """Retorna uma frase aleatória de fallback quando o input foi vazio."""
        return content_manager.get_resposta_vazia()

    def _verificar_reiniciar(self) -> bool:
        """
        Processa sinal de Reiniciar vindo da GUI.

        Cancela o turno em andamento, limpa histórico e filas,
        notifica o usuário e reseta o evento para o próximo turno.
        Retorna True se a sessão foi reiniciada.
        """
        if FILA_REINICIAR.empty():
            return False

        FILA_REINICIAR.get_nowait()
        self._cancelar.set()
        self.historico.clear()

        while not FILA_SAIDA_GUI.empty():
            try:
                FILA_SAIDA_GUI.get_nowait()
            except Exception:
                break

        FILA_SAIDA_GUI.put("🔄 Conversa reiniciada! Pode falar.")
        self._cancelar.clear()
        log.info("🔄 Conversa reiniciada pelo usuário (turno anterior cancelado).")
        return True

    async def _processar_hora_clima(self, pergunta: str) -> str:
        """
        Resolve hora e clima sem perguntar ao usuário pela cidade.

        Lógica:
          1) Tenta detectar uma cidade no próprio texto da pergunta.
          2) Se não encontrar, usa a cidade padrão (Campo Grande, MS).
          3) Executa get_current_time + get_weather e monta contexto para o LLM.
        """
        # Detecta cidade mencionada pelo usuário no próprio texto
        cidade = funcoes_tools.tools.location.detectar_cidade_no_texto(
            pergunta)

        if cidade:
            log.info(f"🗺️  [Hora/Clima] Cidade detectada no texto: '{cidade}'")
        else:
            cidade = funcoes_tools.tools.location.CIDADE_PADRAO
            log.info(f"🗺️  [Hora/Clima] Usando cidade padrão: '{cidade}'")

        try:
            resultado_hora = str(
                funcoes_tools.tools.location.get_current_time(cidade))
        except Exception as e:
            resultado_hora = "horário indisponível no momento"
            log.error(f"🕐 [Tool] get_current_time falhou para '{cidade}': {e}")

        try:
            resultado_clima = str(
                funcoes_tools.tools.location.get_weather(cidade))
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

    # ── Loop Principal ─────────────────────────────────────────────────────────

    async def run(self):
        """Inicializa todos os serviços e executa o loop de conversa."""
        global janela_ref

        log.info("--- 🌸 ALICE INICIADA ---")
        log.info(f"Modelo ativo: {MODELO_IA}")

        # Inicia a janela GUI em thread daemon
        janela_ref = iniciar_janela(
            FILA_ENTRADA_GUI,
            FILA_SAIDA_GUI,
            emotion_engine,
            memoria_longa,
            fila_reiniciar=FILA_REINICIAR,
        )
        log.info("🖥️  Janela de chat aberta!")

        await vts_connector.conectar()

        if audio_ambiente.iniciar():
            log.info("🔊 Áudio ambiente ativo!")
        else:
            log.warning(
                "⚠️  Áudio ambiente não disponível (sem loopback WASAPI).")

        if visao.ativa:
            log.info(
                f"👁️  Visão ativa ({visao.modelo_visao}). Primeira captura...")
            await visao.descrever_tela(forcar=True)
        else:
            log.warning(
                "👁️  Visão: sem modelo. Instale com: ollama pull llava:7b")

        # Detecta modo de operação antes do loop (inicializa cache de disponibilidade)
        if self.tts.disponivel:
            log.info("🔊 Modo COMPLETO: voz + chat ativados.")
        else:
            log.warning(
                "🔇 Modo CHAT-ONLY: microfone desativado. Alice responderá apenas pelo chat."
            )

        # Pré-carrega o modelo OmniVoice no startup para evitar atraso na primeira fala
        await self.tts.pre_carregar()

        loop = asyncio.get_event_loop()

        # Inicia o vision heartbeat em background (só faz algo se visão estiver ativa)
        asyncio.create_task(self._heartbeat_visao_loop())

        # Inicia o loop de reflexão offline ("Sonho") em background
        sonho = Sonho(modelo=MODELO_IA, memoria=memoria_longa)
        asyncio.create_task(
            sonho.loop(
                inatividade_fn=self._inatividade_segundos,
                fila_saida_gui=FILA_SAIDA_GUI,
                tempo_sonho=TEMPO_SONHO,
            )
        )
        self._registrar_interacao()  # baseline: inicia o contador de inatividade

        # ── Loop de conversa ──────────────────────────────────────────────────
        while True:
            # Verifica sinal de reiniciar conversa (botão da GUI)
            if self._verificar_reiniciar():
                continue

            # 1. Aguarda qualquer tipo de input (voz, GUI ou timer proativo)
            pergunta, fonte = await self.mic.obter_pergunta(loop, self.tts.disponivel)

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

            # Vision heartbeat — comentário espontâneo sobre mudança na tela
            if pergunta == "__heartbeat_visao__":
                resp = await self.ollama.gerar_comentario_visao()
                if resp:
                    log.info(f"👁️  [Heartbeat] {resp}")
                    FILA_SAIDA_GUI.put(f"👁️ {resp}")
                    await self.tts.falar(resp)
                continue

            # Comportamento proativo (tom escalona a cada ciclo de silêncio)
            if fonte == "proativo":
                resp = await self.ollama.gerar_fala_proativa(
                    ciclo=self._ciclos_proativos
                )
                self._ciclos_proativos += 1
                log.info(f"💜 [PROATIVO ciclo={self._ciclos_proativos}] {resp}")
                FILA_SAIDA_GUI.put(f"💜 {resp}")
                await self.tts.falar(resp)
                continue

            # Usuário respondeu — reseta escalada proativa e atualiza contador de inatividade
            self._ciclos_proativos = 0
            self._registrar_interacao()

            print(f"{'Você digitou' if fonte == 'gui' else 'Você disse'}: {pergunta}")

            # Diálogo de Hora/Clima: pede a cidade antes de chamar as ferramentas
            if _INTENT_HORA_CLIMA.search(pergunta):
                pergunta = await self._processar_hora_clima(pergunta)

            # 2. Descobrir tom de voz / personalidade
            humor = await self.ollama.descobrir_humor(pergunta)
            log.debug(f"💡 Humor selecionado: {humor}")

            # 3. Atualiza visão da tela periodicamente (sem bloquear)
            if visao.ativa:
                asyncio.create_task(visao.descrever_tela())

            # 4. Montar contexto e gerar resposta
            ctx_emocional = emotion_engine.construir_prompt_contexto()
            bloco_memoria = memoria_longa.construir_bloco_memoria(pergunta)
            bloco_grafo = grafo.construir_bloco_grafo(pergunta)
            bloco_visao = await visao.construir_bloco_visao()
            bloco_audio = audio_ambiente.construir_bloco_audio()
            bloco_ctx = bloco_memoria + bloco_grafo + bloco_visao + bloco_audio

            resp_texto, tk_p, tk_r = await self.ollama.pensar(
                pergunta, humor, self.historico, ctx_emocional, bloco_ctx
            )

            # Turno cancelado pelo Reiniciar — descarta e volta ao início do loop
            if resp_texto is None:
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

            # 6. EmotionEngine + VTube Studio
            emocao = emotion_engine.analisar_tag_emocao(resp_texto)
            emotion_engine.registrar_turno(
                f"Usuário disse: '{pergunta[:60]}...' Alice respondeu como: {humor}"
            )
            asyncio.create_task(vts_connector.ativar_expressao(emocao))

            # 7. Atualizar histórico da sessão
            self.historico.append({"role": "user", "content": pergunta})
            self.historico.append({"role": "assistant", "content": resp_texto})

            # Sumariza quando o histórico está próximo do limite (MAX_HISTORICO - 4)
            # O summarizer condensa as mensagens mais antigas em um brief de 3-5 linhas
            # e mantém as 8 mais recentes intactas — reduz latência sem perder contexto.
            if len(self.historico) >= MAX_HISTORICO - 4:
                self.historico = await summarizer.compactar_se_necessario(
                    self.historico, MODELO_IA, limiar=MAX_HISTORICO - 4
                )

            # Truncamento de segurança (caso summarizer falhe)
            if len(self.historico) > MAX_HISTORICO:
                self.historico = self.historico[-MAX_HISTORICO:]

            log.debug(f"📝 Histórico: {len(self.historico) // 2} trocas")

            # 8. Extrair fatos + detectar nome a cada 3 turnos
            self.turno_count += 1
            if self.turno_count % 3 == 0:
                await self.ollama.extrair_e_salvar_fatos(
                    self.historico, memoria_longa, grafo
                )
                await self.ollama.detectar_nome_usuario(self.historico)

            # 8b. Salvar interação completa no ClickHouse (assíncrono, fail-silently)
            asyncio.create_task(
                clickhouse_log.registrar_turno(
                    pergunta=pergunta,
                    resposta=resp_texto,
                    humor=humor,
                    model=MODELO_IA,
                    tokens_prompt=tk_p,
                    tokens_resposta=tk_r,
                )
            )

            # 9. Falar e depois resetar expressão VTS
            await self.tts.falar(resp_texto)
            asyncio.create_task(vts_connector.resetar_expressao())


# ── Entry point ───────────────────────────────────────────────────────────────


async def main():
    session = AliceSession()
    await session.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("\n🌙 Alice desligando...")
