"""WHISPER_LOCK compartilhado e cálculo de RMS (numpy) do TTSService."""

import asyncio
import threading
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from cerebro import audio_ambiente
from functions import MicListener, TTSService


class WhisperLockTest(unittest.TestCase):
    def test_e_um_lock_de_threading(self):
        self.assertIsInstance(audio_ambiente.WHISPER_LOCK, type(threading.Lock()))

    def test_mesma_instancia_entre_mic_e_ambiente(self):
        self.assertIs(MicListener.WHISPER_LOCK, audio_ambiente.WHISPER_LOCK)

    def test_exclusao_mutua_e_liberacao(self):
        lock = audio_ambiente.WHISPER_LOCK
        self.assertTrue(lock.acquire(blocking=False))
        try:
            self.assertFalse(lock.acquire(blocking=False))
        finally:
            lock.release()
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_serializa_threads_concorrentes(self):
        simultaneas, maximo, guarda = 0, 0, threading.Lock()

        def trabalho():
            nonlocal simultaneas, maximo
            with audio_ambiente.WHISPER_LOCK:
                with guarda:
                    simultaneas += 1
                    maximo = max(maximo, simultaneas)
                threading.Event().wait(0.01)
                with guarda:
                    simultaneas -= 1

        ts = [threading.Thread(target=trabalho) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        self.assertEqual(maximo, 1)

    def test_modelo_recebe_o_lock_compartilhado(self):
        # Ambos os módulos usam setattr(modelo, "_alice_lock", WHISPER_LOCK) via getattr com default.
        modelo = MagicMock(spec=[])
        lock = getattr(modelo, "_alice_lock", audio_ambiente.WHISPER_LOCK)
        self.assertIs(lock, audio_ambiente.WHISPER_LOCK)


class _Canal:
    def __init__(self, ciclos):
        self.ciclos = ciclos

    def get_busy(self):
        self.ciclos -= 1
        return self.ciclos >= 0

    def stop(self):
        self.ciclos = 0


class RmsTTSTest(unittest.IsolatedAsyncioTestCase):
    def _tts(self):
        return TTSService.TTSService("inst", "x.wav", asyncio.Event())

    async def _tocar(self, audio, ciclos=1):
        tts = self._tts()
        valores = []
        tts.registrar_callback_rms(valores.append)
        tts._garantir_mixer = MagicMock(return_value=True)
        canal = _Canal(ciclos)
        with patch.object(TTSService.pygame.mixer, "Sound") as som, \
             patch.object(TTSService, "_PASSO_PLAYBACK_S", 0.001):
            som.return_value.play.return_value = canal
            ok = await tts._tocar_pcm(audio)
        return ok, valores

    async def test_rms_de_onda_constante(self):
        audio = np.full(24000, 0.5, dtype=np.float32)
        ok, v = await self._tocar(audio)
        self.assertTrue(ok)
        self.assertAlmostEqual(v[0], 0.5, places=3)
        self.assertEqual(v[-1], 0.0)  # reset no finally

    async def test_rms_de_silencio_e_zero(self):
        _, v = await self._tocar(np.zeros(24000, dtype=np.float32))
        self.assertTrue(all(x == 0.0 for x in v))

    async def test_rms_senoide_aproxima_amplitude_sobre_raiz_de_2(self):
        t = np.arange(24000) / 24000
        audio = (0.8 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        _, v = await self._tocar(audio)
        self.assertAlmostEqual(v[0], 0.8 / np.sqrt(2), delta=0.05)

    async def test_clipping_limita_rms_a_1(self):
        _, v = await self._tocar(np.full(24000, 9.0, dtype=np.float32))
        self.assertLessEqual(max(v), 1.0)
        self.assertAlmostEqual(v[0], 1.0, places=3)

    async def test_sem_callbacks_nao_calcula_nem_notifica(self):
        tts = self._tts()
        tts._garantir_mixer = MagicMock(return_value=True)
        with patch.object(TTSService.pygame.mixer, "Sound") as som, \
             patch.object(tts, "_notificar_rms") as notif, \
             patch.object(TTSService, "_PASSO_PLAYBACK_S", 0.001):
            som.return_value.play.return_value = _Canal(2)
            self.assertTrue(await tts._tocar_pcm(np.ones(2400, dtype=np.float32)))
        notif.assert_not_called()

    async def test_notificar_rms_clamp_e_isola_callback_com_erro(self):
        tts = self._tts()
        recebido = []

        def ruim(_):
            raise RuntimeError("falha")

        tts.registrar_callback_rms(ruim)
        tts.registrar_callback_rms(recebido.append)
        await tts._notificar_rms(7.0)
        await tts._notificar_rms(-1.0)
        self.assertEqual(recebido, [1.0, 0.0])

    async def test_callback_nao_duplica_e_aceita_corrotina(self):
        tts = self._tts()
        visto = []

        async def cb(x):
            visto.append(x)

        tts.registrar_callback_rms(cb)
        tts.registrar_callback_rms(cb)
        await tts._notificar_rms(0.25)
        self.assertEqual(visto, [0.25])

    async def test_mixer_indisponivel_retorna_false(self):
        tts = self._tts()
        tts._garantir_mixer = MagicMock(return_value=False)
        self.assertFalse(await tts._tocar_pcm(np.zeros(10, dtype=np.float32)))


if __name__ == "__main__":
    unittest.main()
