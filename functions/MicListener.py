"""
MicListener — captura de microfone, transcrição Whisper e roteamento de input.

Responsabilidades:
- Gravar áudio do microfone até detectar silêncio (VAD simples por RMS)
- Calibrar o limiar de ruído de fundo antes de cada escuta
- Transcrever o áudio capturado com faster-whisper (CUDA)
- Aguardar input de qualquer fonte: microfone, GUI (fila) ou timer proativo
- Encerrar graciosamente quando sinalizado pelo threading.Event interno
"""

import asyncio
import queue
import time
import threading

import audioop
import numpy as np
import pyaudiowpatch as pyaudio

from cerebro.logger import log


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
    ):
        self._whisper = modelo_whisper
        self._indice_mic = indice_mic
        self._limiar_min = limiar_min
        self._fila_gui = fila_gui
        self._tempo_proativo = tempo_proativo
        self._alice_falando = alice_falando
        # Sinaliza à thread do microfone que deve parar graciosamente
        self._parar = threading.Event()

    # ── Captura e Transcrição ─────────────────────────────────────────────────

    def _escutar(self) -> str:
        """
        Grava áudio do microfone até detectar silêncio e transcreve para texto.

        Verifica _parar a cada chunk para encerrar graciosamente quando o
        input vier de outra fonte (GUI) ou o timer proativo disparar.
        """
        FORMAT = pyaudio.paInt16
        RATE_WHISPER = 16000  # sample rate exigido pelo Whisper
        LIMITE_SILENCIO = 1.5  # segundos de silêncio para cortar a gravação

        p = pyaudio.PyAudio()

        # Lista microfones disponíveis (apenas em modo debug)
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["maxInputChannels"] > 0:
                log.debug(f"🎤 Mic [{i}] {d['name']}")

        # ── Seleciona dispositivo de entrada ─────────────────────────────────
        # Se um índice foi definido no .env, usa ele diretamente.
        # Caso contrário, escolhe o primeiro microfone físico real, ignorando
        # mapeadores genéricos do Windows que não podem ser abertos diretamente.
        if self._indice_mic is not None:
            dev_info = p.get_device_info_by_index(self._indice_mic)
            indice_usar = self._indice_mic
        else:
            dev_info = None
            indice_usar = None
            # Palavras-chave de dispositivos virtuais/mapeadores a ignorar
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

        RATE = int(dev_info["defaultSampleRate"])
        CHUNK = int(RATE / 10)  # ~100ms de buffer

        # Alguns drivers reportam maxInputChannels correto mas só aceitam 1 canal
        # (PortAudio errno -9998). Testa em ordem: 1, 2, nativo.
        native_ch = max(1, int(dev_info["maxInputChannels"]))
        candidatos = sorted({1, 2, native_ch})

        stream = None
        CHANNELS = 1
        for ch in candidatos:
            try:
                log.debug(
                    f"🎤 Tentando mic [{indice_usar}] {dev_info['name']} | {ch}ch | {RATE}Hz"
                )
                stream = p.open(
                    format=FORMAT,
                    channels=ch,
                    rate=RATE,
                    input=True,
                    input_device_index=indice_usar,
                    frames_per_buffer=CHUNK,
                )
                CHANNELS = ch
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

        # ── Calibração do ruído de fundo (2 segundos) ────────────────────────
        log.info("\n🎤 Calibrando microfone... (fique em silêncio por 2s)")
        somas_rms = 0
        para_calibrar = int(RATE / CHUNK * 2)
        for _ in range(para_calibrar):
            if self._parar.is_set():
                stream.stop_stream()
                stream.close()
                p.terminate()
                return ""
            d = stream.read(CHUNK, exception_on_overflow=False)
            somas_rms += audioop.rms(d, 2)

        ruido_fundo_medio = somas_rms / para_calibrar
        # Usa o maior valor entre: (ruído base + 200) e o mínimo configurado no .env
        LIMIAR_VOLUME = max(self._limiar_min, ruido_fundo_medio + 200)
        log.info(
            f"✅ Ruído base: {ruido_fundo_medio:.0f} | "
            f"Limiar: {LIMIAR_VOLUME:.0f} (mínimo: {self._limiar_min})"
        )

        # ── Loop de gravação ──────────────────────────────────────────────────
        log.info("🎤 Aguardando sua voz...")
        frames = []
        chunks_silenciosos = 0
        max_chunks_silenciosos = int((RATE / CHUNK) * LIMITE_SILENCIO)
        max_gravacao_chunks = int((RATE / CHUNK) * 15)  # limite máximo de 15 segundos
        comecou_falar = False
        contador_espera = 0

        try:
            while True:
                if self._parar.is_set():
                    break

                data = stream.read(CHUNK, exception_on_overflow=False)

                # Descarta frames enquanto Alice está reproduzindo áudio (previne auto-escuta)
                if self._alice_falando and self._alice_falando.is_set():
                    chunks_silenciosos = 0
                    comecou_falar = False
                    frames.clear()
                    continue

                rms = audioop.rms(data, 2)

                if not comecou_falar:
                    contador_espera += 1
                    if contador_espera % 5 == 0:
                        print(
                            f"\r[Ouvindo] Volume (RMS): {rms:04d} / "
                            f"Mínimo exigido: {LIMIAR_VOLUME:.0f}  ",
                            end="",
                            flush=True,
                        )
                    if rms >= LIMIAR_VOLUME:
                        comecou_falar = True
                        print(f"\n[!] Som detectado (RMS {rms})! Gravando agora...")
                        chunks_silenciosos = 0
                        frames.append(data)
                else:
                    if rms < LIMIAR_VOLUME:
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
        finally:
            stream.stop_stream()
            stream.close()
            p.terminate()

        # Descarta áudio parcial se parado externamente
        if self._parar.is_set() or not frames:
            return ""

        # ── Transcrição ───────────────────────────────────────────────────────
        log.debug("⏳ Transcrevendo voz...")
        raw = b"".join(frames)

        # Downmix multi-canal → mono
        if CHANNELS > 1:
            raw = audioop.tomono(raw, 2, 1.0 / CHANNELS, 1.0 / CHANNELS)

        # Resample para 16000 Hz se o dispositivo usou taxa diferente
        if RATE != RATE_WHISPER:
            raw, _ = audioop.ratecv(raw, 2, 1, RATE, RATE_WHISPER, None)

        audio_data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        segmentos, _ = self._whisper.transcribe(audio_data, language="pt", beam_size=5)
        return "".join(s.text for s in segmentos).strip()

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
