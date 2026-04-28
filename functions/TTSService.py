"""
TTSService — serviço de síntese de voz e reprodução de áudio.

Responsabilidades:
- Verificar (e cachear) a disponibilidade de saída de áudio via pygame
- Gerar áudio via OmniVoice (TTS local offline, k2-fsa)
- Reproduzir o áudio e monitorar o evento de cancelamento de turno
"""

import asyncio
import os
import re
import threading
import time

import pygame
import soundfile as sf

from cerebro.logger import log

# Regex compilado uma vez — remove emojis/símbolos Unicode antes de enviar ao TTS
# Cobre: emoticons, pictogramas, bandeiras, dingbats, sequencias ZWJ e variantes gráficas
_EMOJI_RE = re.compile(
    "["
    "\U0001f600-\U0001f64f"  # emoticons
    "\U0001f300-\U0001f5ff"  # símbolos e pictogramas
    "\U0001f680-\U0001f6ff"  # transporte e mapa
    "\U0001f700-\U0001f77f"  # alquimia
    "\U0001f780-\U0001f7ff"  # geom. estendidos
    "\U0001f800-\U0001f8ff"  # setas suplementares
    "\U0001f900-\U0001f9ff"  # símbolos suplementares e pictogramas
    "\U0001fa00-\U0001fa6f"  # símbolos de xadrez
    "\U0001fa70-\U0001faff"  # símbolos e pictogramas estendidos
    "\U00002702-\U000027b0"  # dingbats
    "\U000024c2-\U0001f251"  # caracteres enclosed
    "\U0001f1e0-\U0001f1ff"  # bandeiras (letras regionais)
    "\U00002500-\U00002bef"  # símbolos diversos (box drawing, etc.)
    "\U00010000-\U0010ffff"  # plano suplementar completo
    "\u200d"  # zero-width joiner
    "\ufe0f"  # variante de apresentação gráfica
    "]+",
    flags=re.UNICODE,
)


class TTSService:
    """Gerencia síntese de voz (TTS) e reprodução de áudio da Alice via OmniVoice."""

    def __init__(
        self,
        instruct: str,
        arquivo_audio: str,
        cancelar_turno: asyncio.Event,
        dispositivo_saida: str | None = None,
        alice_falando: threading.Event | None = None,
    ):
        self.instruct = instruct
        self.arquivo_audio = arquivo_audio
        self._cancelar = cancelar_turno
        self._dispositivo_saida = dispositivo_saida
        self._alice_falando = alice_falando
        self._disponivel: bool | None = None  # lazy-initialized na primeira chamada
        self._ultimo_tts: float = 0.0  # timestamp da última chamada TTS
        self._tts_delay_min: float = 0.3  # segundos mínimos entre chamadas TTS
        self._modelo = None  # lazy-loaded na primeira fala para não atrasar o startup
        if dispositivo_saida:
            log.info(
                f"🎙️  TTSService (OmniVoice) | Instruct: {instruct} | Saída: {dispositivo_saida}"
            )
        else:
            log.info(f"🎙️  TTSService (OmniVoice) | Instruct: {instruct}")

    # ── Carregamento do Modelo ────────────────────────────────────────────────

    def _carregar_modelo(self):
        """Carrega o modelo OmniVoice na primeira chamada (lazy init)."""
        if self._modelo is None:
            log.info("🔄 Carregando modelo OmniVoice... (primeira fala, pode demorar)")
            from omnivoice import OmniVoice

            self._modelo = OmniVoice.from_pretrained(
                "k2-fsa/OmniVoice", device_map="cuda:0"
            )
            log.info("✅ Modelo OmniVoice carregado.")
        return self._modelo

    async def pre_carregar(self) -> None:
        """Carrega o modelo OmniVoice antecipadamente no startup para evitar atraso na primeira fala."""
        await asyncio.get_event_loop().run_in_executor(None, self._carregar_modelo)

    # ── Verificação de Hardware ───────────────────────────────────────────────

    def _testar_audio(self) -> bool:
        """Verifica se o pygame consegue inicializar o mixer de saída de áudio."""
        if self._dispositivo_saida:
            os.environ["SDL_AUDIODEVICENAME"] = self._dispositivo_saida
        try:
            pygame.mixer.init()
            pygame.mixer.quit()
            return True
        except Exception as e:
            log.warning(
                f"🔇 Saída de áudio indisponível ({e}). Alice responderá apenas pelo chat."
            )
            return False

    @property
    def disponivel(self) -> bool:
        """Retorna (e cacheia) se o dispositivo de saída de áudio está disponível."""
        if self._disponivel is None:
            self._disponivel = self._testar_audio()
        return self._disponivel

    # ── TTS + Reprodução ─────────────────────────────────────────────────────

    async def falar(self, texto_ia: str) -> None:
        """
        Transforma texto em áudio via OmniVoice e reproduz, respeitando cancelamento.

        Se não houver dispositivo de saída de áudio disponível (ex: WASAPI sem
        endpoint), pula o TTS silenciosamente — a resposta já está visível no chat.
        Quando o áudio estiver disponível, reproduz normalmente e para imediatamente
        se o evento de cancelamento for acionado (usuário clicou em Reiniciar).
        """
        # Remove marcação markdown: **negrito**, *itálico*, # títulos, `código`
        texto_limpo = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", texto_ia)
        texto_limpo = re.sub(r"#{1,6}\s*", "", texto_limpo)
        texto_limpo = re.sub(r"`[^`]*`", "", texto_limpo)

        # Filtra ações de roleplay entre asteriscos que sobraram (ex: *riso*)
        texto_limpo = re.sub(r"\*[^*]*\*", "", texto_limpo).strip()

        # Remove emojis — TTS pode vocalizar ou falhar com caracteres especiais
        texto_limpo = _EMOJI_RE.sub("", texto_limpo).strip()
        # Colapsa espaços múltiplos deixados pela remoção
        texto_limpo = re.sub(r" {2,}", " ", texto_limpo)

        if not texto_limpo:
            return

        # Verifica disponibilidade (inicializa na primeira chamada)
        if not self.disponivel:
            log.debug(
                "🔇 [TTS ignorado] Sem saída de áudio. Resposta já exibida no chat."
            )
            return

        # Aborta cedo se o turno já foi cancelado antes de gerar o áudio
        if self._cancelar.is_set():
            return

        # Rate limiting: garante intervalo mínimo entre chamadas ao TTS
        espera = self._tts_delay_min - (time.monotonic() - self._ultimo_tts)
        if espera > 0:
            await asyncio.sleep(espera)

        log.debug(f"🎙️ Gerando voz OmniVoice... [instruct={self.instruct}]")

        # Carrega o modelo uma única vez (fora do loop de retry)
        try:
            modelo = await asyncio.get_event_loop().run_in_executor(
                None, self._carregar_modelo
            )
        except Exception as e:
            log.warning(f"🔇 Falha ao carregar modelo OmniVoice: {e}. Pulando fala.")
            return

        # Retry com backoff exponencial (3 tentativas: 1s, 2s, 4s)
        for tentativa in range(3):
            try:
                resultado = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: modelo.generate(text=texto_limpo, instruct=self.instruct),
                )
                # OmniVoice retorna uma lista de numpy arrays; sample rate fixo em 24000 Hz
                sf.write(self.arquivo_audio, resultado[0], 24000)
                break
            except Exception as e:
                if tentativa == 2:
                    log.warning(f"🔇 TTS falhou após 3 tentativas: {e}. Pulando fala.")
                    return
                await asyncio.sleep(2**tentativa)

        self._ultimo_tts = time.monotonic()

        try:
            if self._alice_falando:
                self._alice_falando.set()
            pygame.mixer.init()
            pygame.mixer.music.load(self.arquivo_audio)
            pygame.mixer.music.play()

            while pygame.mixer.music.get_busy():
                # Para o áudio imediatamente se o Reiniciar for pressionado
                if self._cancelar.is_set():
                    pygame.mixer.music.stop()
                    break
                await asyncio.sleep(0.1)

            pygame.mixer.quit()
        except Exception as e:
            # Áudio falhou em tempo de execução — marca como indisponível e continua
            self._disponivel = False
            log.warning(
                f"🔇 Erro ao reproduzir áudio: {e}. Voltando ao modo somente chat."
            )
        finally:
            if self._alice_falando:
                self._alice_falando.clear()
