"""
MicListener — captura de microfone, transcrição Whisper e roteamento de input.

Responsabilidades:
- Manter o stream PortAudio reutilizável entre turnos e calibrar ruído de fundo apenas na inicialização (ou sob demanda)
- Gravar áudio do microfone até detectar silêncio (VAD por RMS)
- Suportar pausa/mute thread-safe e notificar estados ('calibrando', 'ouvindo', 'transcrevendo', 'mutado', 'ocioso') para a GUI
- Transcrever o áudio capturado com faster-whisper (CUDA)
- Aguardar input de qualquer fonte: microfone, GUI (fila) ou timer proativo
- Encerrar graciosamente quando sinalizado pelo threading.Event interno ou fechar()
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections.abc import Callable

import numpy as np
import pyaudiowpatch as pyaudio

from cerebro.audio_ambiente import WHISPER_LOCK, reamostrar_audio
from cerebro.logger import log


def _calcular_rms_int16(data: bytes) -> int:
    """Calcula o RMS de um buffer PCM 16-bit signed (substitui audioop.rms(data, 2))."""
    if not data:
        return 0
    amostras = np.frombuffer(data, dtype=np.int16).astype(np.float64)
    if amostras.size == 0:
        return 0
    return int(np.sqrt(np.mean(amostras * amostras)))


class MicListener:
    """Gerencia a captura de microfone e o roteamento de input da Alice."""

    def __init__(
        self,
        modelo_whisper,
        indice_mic: int | None,
        limiar_min: int,
        fila_gui: queue.Queue,
        tempo_proativo: int,
        alice_falando: threading.Event | None = None,
        whisper_lock: threading.Lock | None = None,
        mutado: threading.Event | None = None,
        on_estado_change: Callable[[str], None] | None = None,
    ):
        self._whisper = modelo_whisper
        self._indice_mic = indice_mic
        self._limiar_min = limiar_min
        self._fila_gui = fila_gui
        self._tempo_proativo = tempo_proativo
        self._alice_falando = alice_falando
        self._whisper_lock = whisper_lock or getattr(
            modelo_whisper, "_alice_lock", WHISPER_LOCK
        )
        # Sinaliza à thread do microfone que deve parar o turno atual graciosamente
        self._parar = threading.Event()
        # Suporte a pausa/mute thread-safe (quando setado, o microfone está mutado)
        self.mutado = mutado if mutado is not None else threading.Event()
        # Callbacks e eventos de estado para a GUI ('calibrando', 'ouvindo', 'transcrevendo', 'mutado', 'ocioso')
        self._on_estado_change = on_estado_change
        self._callbacks_estado: list[Callable[[str], None]] = []
        if on_estado_change is not None:
            self._callbacks_estado.append(on_estado_change)
        self.estado_atual: str = "ocioso"
        self.eventos_estado: dict[str, threading.Event] = {
            "calibrando": threading.Event(),
            "ouvindo": threading.Event(),
            "transcrevendo": threading.Event(),
            "mutado": threading.Event(),
        }

        # Estado persistente do PortAudio e calibração entre turnos
        self._stream_lock = threading.Lock()
        self._pa: pyaudio.PyAudio | None = None
        self._stream = None
        self._rate: int = 16000
        self._chunk: int = 1600
        self._channels: int = 1
        self._limiar_calibrado: float | None = None
        self._precisa_recalibrar: bool = True

    # ── Controle de Estado, Mute e Calibração ─────────────────────────────────

    def registrar_callback_estado(self, callback: Callable[[str], None]) -> None:
        """Registra um callback `fn(estado: str)` chamado nas transições do microfone."""
        if callback not in self._callbacks_estado:
            self._callbacks_estado.append(callback)

    def _definir_estado(self, novo_estado: str) -> None:
        """Atualiza o estado atual, sincroniza eventos thread-safe e aciona callbacks da GUI."""
        self.estado_atual = novo_estado
        for nome, ev in self.eventos_estado.items():
            if nome == novo_estado:
                ev.set()
            else:
                ev.clear()
        for cb in list(self._callbacks_estado):
            try:
                cb(novo_estado)
            except Exception as e:
                log.debug(f"Callback de estado do MicListener falhou: {e}")

    def definir_mute(self, mutar: bool) -> None:
        """Ativa ou desativa o mute do microfone de forma thread-safe."""
        if mutar:
            self.mutado.set()
            self._definir_estado("mutado")
            log.info("🔇 Microfone mutado.")
        else:
            self.mutado.clear()
            log.info("🎤 Microfone desmutado.")

    def alternar_mute(self) -> bool:
        """Alterna o estado de mute e retorna True se ficou mutado."""
        novo = not self.mutado.is_set()
        self.definir_mute(novo)
        return novo

    @property
    def esta_mutado(self) -> bool:
        """Retorna True se o microfone estiver mutado."""
        return self.mutado.is_set()

    def solicitar_recalibracao(self) -> None:
        """Agenda recalibração de ruído ambiente para o início da próxima escuta."""
        self._precisa_recalibrar = True

    # ── Gerenciamento Persistente do Stream PortAudio ─────────────────────────

    def _garantir_stream(self) -> None:
        """Abre o dispositivo PortAudio apenas se ainda não estiver ativo."""
        if self._pa is not None and self._stream is not None:
            try:
                if self._stream.is_active():
                    return
                self._stream.start_stream()
                return
            except Exception:
                self._fechar_stream_interno()

        FORMAT = pyaudio.paInt16
        p = pyaudio.PyAudio()

        # Lista microfones disponíveis (apenas em modo debug)
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["maxInputChannels"] > 0:
                log.debug(f"🎤 Mic [{i}] {d['name']}")

        if self._indice_mic is not None:
            dev_info = p.get_device_info_by_index(self._indice_mic)
            indice_usar = self._indice_mic
        else:
            dev_info = None
            indice_usar = None
            NOMES_VIRTUAIS = {
                "primary sound driver",
                "primary sound capture driver",
                "microsoft sound mapper",
                "loopback",
            }
            for i in range(p.get_device_count()):
                d = p.get_device_info_by_index(i)
                ch_in = int(d["maxInputChannels"])
                if ch_in <= 0:
                    continue
                nome_lower = d["name"].lower()
                if any(v in nome_lower for v in NOMES_VIRTUAIS):
                    continue
                dev_info = d
                indice_usar = i
                break

            if dev_info is None:
                p.terminate()
                raise RuntimeError(
                    "Nenhum microfone físico encontrado. "
                    "Defina INDICE_MICROFONE no .env com o índice correto."
                )

        rate = int(dev_info["defaultSampleRate"])
        chunk = int(rate / 10)  # ~100ms de buffer
        native_ch = max(1, int(dev_info["maxInputChannels"]))
        candidatos = sorted({1, 2, native_ch})

        stream = None
        channels = 1
        for ch in candidatos:
            try:
                log.debug(
                    f"🎤 Tentando mic [{indice_usar}] {dev_info['name']} | {ch}ch | {rate}Hz"
                )
                stream = p.open(
                    format=FORMAT,
                    channels=ch,
                    rate=rate,
                    input=True,
                    input_device_index=indice_usar,
                    frames_per_buffer=chunk,
                )
                channels = ch
                log.debug(f"🎤 Mic aberto com sucesso: {ch}ch")
                break
            except OSError as exc:
                log.warning(f"⚠️  Mic falhou com {ch}ch: {exc}")

        if stream is None:
            p.terminate()
            raise RuntimeError(
                f"Não foi possível abrir o microfone '{dev_info['name']}' "
                f"(índice {indice_usar}) com nenhum dos canais testados: {candidatos}"
            )

        self._pa = p
        self._stream = stream
        self._rate = rate
        self._chunk = chunk
        self._channels = channels

    def _calibrar_ruido_interno(self, duracao_s: float = 2.0) -> float:
        """Executa a calibração do ruído de fundo usando o stream aberto."""
        self._definir_estado("calibrando")
        log.info(f"\n🎤 Calibrando microfone... (fique em silêncio por {duracao_s:.1f}s)")
        somas_rms = 0
        para_calibrar = max(1, int((self._rate / self._chunk) * duracao_s))
        lidos = 0
        for _ in range(para_calibrar):
            if self._parar.is_set():
                break
            d = self._stream.read(self._chunk, exception_on_overflow=False)
            somas_rms += _calcular_rms_int16(d)
            lidos += 1

        if lidos == 0:
            return float(self._limiar_min)

        ruido_fundo_medio = somas_rms / lidos
        limiar_volume = max(float(self._limiar_min), ruido_fundo_medio + 200.0)
        self._limiar_calibrado = limiar_volume
        self._precisa_recalibrar = False
        log.info(
            f"✅ Ruído base: {ruido_fundo_medio:.0f} | "
            f"Limiar: {limiar_volume:.0f} (mínimo: {self._limiar_min})"
        )
        return limiar_volume

    def calibrar_ruido(self, duracao_s: float = 2.0) -> float:
        """Calibra o ruído ambiente sob demanda explícita e retorna o novo limiar."""
        with self._stream_lock:
            self._garantir_stream()
            return self._calibrar_ruido_interno(duracao_s=duracao_s)

    def _descartar_buffer_acumulado(self) -> None:
        """Limpa frames antigos que ficaram no buffer do driver antes de escutar novo turno."""
        if self._stream is None:
            return
        try:
            disponivel = self._stream.get_read_available()
            while disponivel >= self._chunk and not self._parar.is_set():
                self._stream.read(self._chunk, exception_on_overflow=False)
                disponivel = self._stream.get_read_available()
        except Exception:
            pass

    def _fechar_stream_interno(self) -> None:
        """Fecha o stream e termina a instância PyAudio com segurança."""
        if self._stream is not None:
            try:
                self._stream.stop_stream()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None

    def fechar(self) -> None:
        """Encerra graciosamente a escuta atual e libera o stream PortAudio."""
        self._parar.set()
        with self._stream_lock:
            self._fechar_stream_interno()
        self._definir_estado("ocioso")

    # ── Captura e Transcrição ─────────────────────────────────────────────────

    def _escutar(self) -> str:
        """
        Grava áudio do microfone até detectar silêncio e transcreve para texto.

        Reutiliza o stream PortAudio aberto e calibra apenas na primeira execução
        (ou quando solicitado via `solicitar_recalibracao()`).
        """
        RATE_WHISPER = 16000  # sample rate exigido pelo Whisper
        LIMITE_SILENCIO = 1.5  # segundos de silêncio para cortar a gravação

        with self._stream_lock:
            self._garantir_stream()

            if self._precisa_recalibrar or self._limiar_calibrado is None:
                limiar_volume = self._calibrar_ruido_interno(duracao_s=2.0)
                if self._parar.is_set():
                    self._definir_estado("ocioso")
                    return ""
            else:
                limiar_volume = self._limiar_calibrado
                self._descartar_buffer_acumulado()

            RATE = self._rate
            CHUNK = self._chunk
            CHANNELS = self._channels

            # ── Loop de gravação ──────────────────────────────────────────────
            log.info("🎤 Aguardando sua voz...")
            self._definir_estado("mutado" if self.mutado.is_set() else "ouvindo")
            frames: list[bytes] = []
            chunks_silenciosos = 0
            max_chunks_silenciosos = int((RATE / CHUNK) * LIMITE_SILENCIO)
            max_gravacao_chunks = int((RATE / CHUNK) * 15)  # limite máximo de 15 segundos
            comecou_falar = False
            contador_espera = 0

            try:
                while True:
                    if self._parar.is_set():
                        break

                    # Se o microfone estiver mutado, aguarda sem processar fala
                    if self.mutado.is_set():
                        if self.estado_atual != "mutado":
                            self._definir_estado("mutado")
                        chunks_silenciosos = 0
                        comecou_falar = False
                        frames.clear()
                        try:
                            self._stream.read(CHUNK, exception_on_overflow=False)
                        except Exception:
                            time.sleep(0.05)
                        continue

                    if self.estado_atual == "mutado":
                        self._definir_estado("ouvindo")

                    data = self._stream.read(CHUNK, exception_on_overflow=False)

                    # Descarta frames enquanto Alice está reproduzindo áudio (previne auto-escuta)
                    if self._alice_falando and self._alice_falando.is_set():
                        chunks_silenciosos = 0
                        comecou_falar = False
                        frames.clear()
                        continue

                    rms = _calcular_rms_int16(data)

                    if not comecou_falar:
                        contador_espera += 1
                        if contador_espera % 5 == 0:
                            print(
                                f"\r[Ouvindo] Volume (RMS): {rms:04d} / "
                                f"Mínimo exigido: {limiar_volume:.0f}  ",
                                end="",
                                flush=True,
                            )
                        if rms >= limiar_volume:
                            comecou_falar = True
                            print(f"\n[!] Som detectado (RMS {rms})! Gravando agora...")
                            chunks_silenciosos = 0
                            frames.append(data)
                    else:
                        if rms < limiar_volume:
                            chunks_silenciosos += 1
                        else:
                            chunks_silenciosos = 0

                        frames.append(data)

                        # Silêncio longo → encerra
                        if chunks_silenciosos > max_chunks_silenciosos:
                            print("  -> Silêncio detectado. Processando...")
                            break

                        # Limite de 15 segundos
                        if len(frames) > max_gravacao_chunks:
                            print(
                                "  -> Fim dos 15 segundos máximos de gravação. Processando..."
                            )
                            break
            except Exception as e:
                log.error(f"Erro na gravação: {e}")
                self._fechar_stream_interno()

        # Descarta áudio parcial se parado externamente
        if self._parar.is_set() or not frames:
            self._definir_estado("ocioso")
            return ""

        # ── Transcrição ───────────────────────────────────────────────────────
        self._definir_estado("transcrevendo")
        log.debug("⏳ Transcrevendo voz...")
        try:
            raw = b"".join(frames)
            audio_data = (
                np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            )

            # Downmix multi-canal → mono
            if CHANNELS > 1:
                total_frames = (audio_data.size // CHANNELS) * CHANNELS
                audio_data = (
                    audio_data[:total_frames].reshape(-1, CHANNELS).mean(axis=1)
                )

            # Resample para 16000 Hz com filtro passa-baixa se o dispositivo usou taxa diferente
            if RATE != RATE_WHISPER and audio_data.size > 0:
                audio_data = reamostrar_audio(audio_data, RATE, RATE_WHISPER)

            with self._whisper_lock:
                segmentos, _ = self._whisper.transcribe(
                    audio_data, language="pt", beam_size=5
                )
                return "".join(s.text for s in segmentos).strip()
        finally:
            self._definir_estado("ocioso")

    # ── Roteamento de Input ───────────────────────────────────────────────────

    async def obter_pergunta(
        self,
        loop: asyncio.AbstractEventLoop,
        audio_disponivel: bool,
    ) -> tuple[str, str]:
        """
        Aguarda input de qualquer fonte: microfone, GUI ou timer proativo.

        Retorna (texto, fonte) onde fonte é 'voz', 'gui' ou 'proativo'.

        Modo chat-only (sem saída de áudio): pula o microfone completamente.
        Modo completo (com áudio): escuta microfone em paralelo com a GUI.
        """
        # ── Modo CHAT-ONLY: sem microfone ─────────────────────────────────────
        if not audio_disponivel:
            tempo_inicio = time.time()
            while True:
                await asyncio.sleep(0.1)

                if not self._fila_gui.empty():
                    texto = self._fila_gui.get_nowait()
                    if texto:
                        return texto, "gui"

                if time.time() - tempo_inicio >= self._tempo_proativo:
                    return "[PROATIVO]", "proativo"

        # ── Modo COMPLETO: microfone + GUI ────────────────────────────────────
        # Garante que o event de parada está limpo antes de iniciar nova escuta
        self._parar.clear()
        fut_mic = loop.run_in_executor(None, self._escutar)
        tempo_inicio = time.time()

        while not fut_mic.done():
            await asyncio.sleep(0.1)

            # Verifica texto digitado na GUI
            if not self._fila_gui.empty():
                texto = self._fila_gui.get_nowait()
                if texto:
                    self._parar.set()
                    if not fut_mic.done():
                        try:
                            await asyncio.wait_for(
                                asyncio.wrap_future(fut_mic), timeout=3.0
                            )
                        except (asyncio.TimeoutError, Exception):
                            pass
                    return texto, "gui"

            # Verifica timer proativo
            if time.time() - tempo_inicio >= self._tempo_proativo:
                self._parar.set()
                if not fut_mic.done():
                    try:
                        await asyncio.wait_for(
                            asyncio.wrap_future(fut_mic), timeout=3.0
                        )
                    except (asyncio.TimeoutError, Exception):
                        pass
                return "[PROATIVO]", "proativo"

        resultado = fut_mic.result()
        return (resultado or ""), "voz"