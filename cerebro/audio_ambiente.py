"""
cerebro/audio_ambiente.py — Captura de Áudio Ambiente (WASAPI Loopback)

Captura o áudio que sai pelos alto-falantes (músicas, vídeos, conversas)
usando WASAPI Loopback via pyaudiowpatch.

Transcreve com o mesmo modelo Whisper já carregado em main.py e injeta
o texto detectado no contexto da Alice.
"""

from __future__ import annotations

import math
import threading
import time
import numpy as np
import pyaudiowpatch as pyaudio
from cerebro.logger import log

try:
    from scipy.signal import resample_poly as _scipy_resample_poly
except Exception:
    _scipy_resample_poly = None

# ─── Configurações ────────────────────────────────────────────────────────────
TAXA_WHISPER = 16000  # Whisper usa sempre 16kHz
DURACAO_SEGMENTO = 10  # segundos de áudio por transcrição
CHUNK = 1024  # frames por leitura
# RMS mínimo para considerar que há som (evita transcrever silêncio)
SILENCIO_LIMIAR = 0.004
MAX_BUFFER = 5  # máximo de transcrições armazenadas
COOLDOWN_POS_FALA = 0.4  # segundos extras após Alice parar de falar para evitar eco residual

# Lock global para serializar chamadas concorrentes ao mesmo modelo faster-whisper (CUDA/CTranslate2)
WHISPER_LOCK = threading.Lock()


def reamostrar_audio(audio: np.ndarray, taxa_origem: int, taxa_destino: int = TAXA_WHISPER) -> np.ndarray:
    """
    Reamostra sinal 1D float32 com filtro passa-baixa anti-aliasing.
    Usa scipy.signal.resample_poly quando disponível ou filtro FIR passa-baixa em numpy.
    """
    if taxa_origem == taxa_destino or audio.size == 0:
        return audio.astype(np.float32, copy=False)

    if _scipy_resample_poly is not None:
        divisor = math.gcd(int(taxa_origem), int(taxa_destino))
        up = int(taxa_destino) // divisor
        down = int(taxa_origem) // divisor
        return _scipy_resample_poly(audio, up, down).astype(np.float32)

    # Fallback numpy com filtro FIR passa-baixa (sinc janelado por Blackman) antes de interpolar
    ratio = taxa_destino / taxa_origem
    if ratio < 1.0 and audio.size > 16:
        fc = 0.45 * ratio  # frequência de corte abaixo de Nyquist da nova taxa
        num_taps = 31
        n = np.arange(num_taps) - (num_taps - 1) / 2.0
        h = np.sinc(2 * fc * n) * np.blackman(num_taps)
        h /= np.sum(h)
        audio = np.convolve(audio, h, mode="same")

    novo_len = max(1, int(round(len(audio) * ratio)))
    indices = np.linspace(0, len(audio) - 1, novo_len)
    return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


class AudioAmbiente:
    """
    Captura áudio do sistema (WASAPI loopback) e transcreve com Whisper.

    Roda em uma thread daemon separada, sem bloquear o loop asyncio principal.
    O texto detectado fica disponível via `construir_bloco_audio()` para
    ser injetado no contexto da Alice.
    """

    def __init__(
        self,
        modelo_whisper,
        alice_falando: threading.Event | None = None,
        whisper_lock: threading.Lock | None = None,
    ):
        self.modelo_whisper = modelo_whisper
        self._alice_falando = alice_falando
        self._whisper_lock = whisper_lock or getattr(
            modelo_whisper, "_alice_lock", WHISPER_LOCK
        )
        # buffer das últimas N transcrições
        self.transcricoes: list[str] = []
        self._parar = threading.Event()
        self._ativo = False
        self._ultimo_alice_falando: float = 0.0

    # ─── Controle ─────────────────────────────────────────────────────────────

    def iniciar(self) -> bool:
        """
        Inicia a captura em background.
        Retorna True se encontrou um dispositivo loopback e iniciou com sucesso.
        """
        try:
            pa = pyaudio.PyAudio()
            dispositivo = self._encontrar_loopback(pa)
            pa.terminate()

            if dispositivo is None:
                log.warning("⚠️  [Audio] Nenhum dispositivo loopback encontrado.")
                log.info(
                    "   → Verifique se o WASAPI está habilitado nas configurações de som do Windows."
                )
                return False

            self._parar.clear()
            t = threading.Thread(
                target=self._loop_captura,
                args=(dispositivo,),
                daemon=True,
                name="AudioAmbiente",
            )
            t.start()
            self._ativo = True
            log.info(f"🔊 [Audio] Captura ativa: {dispositivo['name']}")
            return True

        except Exception as e:
            log.error(f"❌ [Audio] Erro ao iniciar: {e}")
            return False

    def parar(self):
        """Para a captura."""
        self._parar.set()
        self._ativo = False

    def fechar(self):
        """Alias limpo para encerrar a captura de áudio ambiente."""
        self.parar()

    @property
    def ativo(self) -> bool:
        return self._ativo

    # ─── Dispositivo loopback ──────────────────────────────────────────────────

    def _encontrar_loopback(self, pa: pyaudio.PyAudio) -> dict | None:
        """
        Encontra o dispositivo loopback WASAPI correspondente à saída padrão.
        Usa o gerador exclusivo do pyaudiowpatch.
        """
        try:
            wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError:
            log.warning("⚠️  [Audio] WASAPI não disponível neste sistema.")
            return None

        # Dispositivo de saída padrão
        saida_padrao = pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])

        # Procura o loopback correspondente à saída padrão
        for loopback in (
            pa.get_loopback_device_info_generator()
        ):  # método exclusivo do pyaudiowpatch
            if saida_padrao["name"] in loopback["name"]:
                return loopback

        # Fallback: retorna qualquer loopback disponível
        for loopback in pa.get_loopback_device_info_generator():
            return loopback

        return None

    # ─── Loop de captura ──────────────────────────────────────────────────────

    def _alice_contaminou_janela(self) -> bool:
        """Retorna True se a Alice está falando ou falou há menos de COOLDOWN_POS_FALA."""
        agora = time.monotonic()
        if self._alice_falando and self._alice_falando.is_set():
            self._ultimo_alice_falando = agora
            return True
        return (agora - self._ultimo_alice_falando) < COOLDOWN_POS_FALA

    def _loop_captura(self, dispositivo: dict):
        """Thread que captura áudio em chunks e transcreve com Whisper."""
        pa = pyaudio.PyAudio()
        try:
            canais = max(1, int(dispositivo.get("maxInputChannels", 2)))
            taxa = int(dispositivo.get("defaultSampleRate", 44100))
            indice = int(dispositivo["index"])

            # paInt16 é o formato mais compatível com WASAPI loopback
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=canais,
                rate=taxa,
                input=True,
                input_device_index=indice,
                frames_per_buffer=CHUNK,
            )

            frames_por_segmento = int(taxa * DURACAO_SEGMENTO / CHUNK)

            while not self._parar.is_set():
                frames = []
                janela_contaminada = False

                for _ in range(frames_por_segmento):
                    if self._parar.is_set():
                        break
                    try:
                        data = stream.read(CHUNK, exception_on_overflow=False)
                        # Se a Alice falou durante qualquer chunk desta janela (ou no cooldown pós-fala),
                        # descarta os frames acumulados para evitar transcrever eco da própria voz
                        if self._alice_contaminou_janela():
                            janela_contaminada = True
                            frames.clear()
                            continue

                        # int16 → float32 [-1, 1] para o Whisper
                        chunk_f32 = (
                            np.frombuffer(data, dtype=np.int16).astype(np.float32)
                            / 32768.0
                        )
                        frames.append(chunk_f32)
                    except Exception:
                        pass

                if janela_contaminada or self._alice_contaminou_janela():
                    frames.clear()
                    continue

                if frames:
                    audio = np.concatenate(frames)
                    rms = float(np.sqrt(np.mean(audio**2)))
                    if rms > SILENCIO_LIMIAR:
                        self._transcrever(audio, taxa, canais)

            stream.stop_stream()
            stream.close()

        except Exception as e:
            log.error(f"⚠️  [Audio] Erro na captura: {e}")
        finally:
            pa.terminate()

    # ─── Transcrição ──────────────────────────────────────────────────────────

    def _transcrever(self, audio: np.ndarray, taxa: int, canais: int):
        """Converte para mono, reamostra com filtro passa-baixa para 16kHz e transcreve com Whisper."""
        # Não transcreve loopback se a Alice falou recentemente (previne auto-escuta via WASAPI)
        if self._alice_contaminou_janela():
            return
        try:
            # Converte para mono se necessário
            if canais > 1:
                total_frames = (audio.size // canais) * canais
                audio = audio[:total_frames].reshape(-1, canais).mean(axis=1)

            # Resample para 16kHz com filtro passa-baixa anti-aliasing
            if taxa != TAXA_WHISPER:
                audio = reamostrar_audio(audio, taxa, TAXA_WHISPER)

            if self._alice_contaminou_janela():
                return

            with self._whisper_lock:
                segmentos, _ = self.modelo_whisper.transcribe(
                    audio,
                    language="pt",
                    beam_size=1,
                    vad_filter=True,  # filtra silêncio automaticamente
                    vad_parameters={"min_silence_duration_ms": 500},
                )
                texto = " ".join(s.text for s in segmentos).strip()
            if texto and len(texto) > 8:
                hora = time.strftime("%H:%M")
                entrada = f"[{hora}] {texto}"
                self.transcricoes.append(entrada)
                if len(self.transcricoes) > MAX_BUFFER:
                    self.transcricoes = self.transcricoes[-MAX_BUFFER:]
                log.info(f"🔊 [Audio] {texto[:80]}{'...' if len(texto) > 80 else ''}")

        except Exception:
            pass  # silencioso para não derrubar a thread

    # ─── Contexto para o prompt ───────────────────────────────────────────────

    def construir_bloco_audio(self) -> str:
        """Retorna um bloco de contexto formatado para injeção no system prompt."""
        if not self.transcricoes:
            return ""
        recentes = self.transcricoes[-3:]
        linhas = "\n".join(f"  • {t}" for t in recentes)
        return (
            f"\n--- ÁUDIO AMBIENTE (o que está tocando ou sendo dito no sistema) ---\n"
            f"{linhas}\n"
            "---"
        )