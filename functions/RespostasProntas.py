"""
RespostasProntas — respostas fixas com áudio já gerado, para perguntas frequentes.

Responsabilidades:
- Ler respostas_prontas.json (gatilhos + texto de cada resposta)
- Reconhecer quando a pergunta do usuário é uma dessas perguntas
- Gerar o áudio de cada resposta uma vez e guardar em audio/prontas/
  (só regenera quando o texto ou a voz mudam)
"""

import asyncio
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path

import numpy as np
import soundfile as sf

from cerebro.logger import log

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
CAMINHO_JSON = str(_PROJECT_ROOT / "respostas_prontas.json")
PASTA_AUDIO = str(_PROJECT_ROOT / "audio" / "prontas")
_SAMPLE_RATE = 24000

# Palavras ignoradas no começo da frase ("Oi Alice, se apresente")
_PREFIXOS = {"alice", "oi", "ola", "ei", "hey", "e", "ai", "entao"}
# Quantas palavras a mais que o gatilho a frase pode ter ("por favor", "pra mim")
_FOLGA_PALAVRAS = 2


def _normalizar(texto: str) -> str:
    """Minúsculas, sem acento, sem pontuação e sem 'Alice'/'oi' no começo."""
    texto = unicodedata.normalize("NFKD", texto.lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    palavras = re.sub(r"[^a-z0-9 ]+", " ", texto).split()
    while palavras and palavras[0] in _PREFIXOS:
        palavras.pop(0)
    return " ".join(palavras)


def _ler_wav(wav: str) -> np.ndarray:
    """Lê arquivo WAV do disco em float32."""
    audio, _ = sf.read(wav, dtype="float32")
    return audio


def _salvar_wav_e_marca(wav: str, audio: np.ndarray, marca: str, assinatura: str) -> None:
    """Grava arquivo WAV e sua assinatura SHA-1 no disco."""
    sf.write(wav, audio, _SAMPLE_RATE)
    with open(marca, "w", encoding="utf-8") as f:
        f.write(assinatura)


class RespostasProntas:
    """Carrega, reconhece e toca as respostas prontas da Alice."""

    def __init__(self, caminho: str | Path = CAMINHO_JSON):
        caminho_path = Path(caminho)
        if not caminho_path.is_absolute():
            caminho_path = _PROJECT_ROOT / caminho_path
        self._caminho = str(caminho_path)
        self._respostas: list[dict] = []
        self._audios: dict[str, np.ndarray] = {}
        self.recarregar()

    def recarregar(self) -> None:
        """Lê o JSON de respostas prontas (arquivo ausente = nenhuma resposta)."""
        try:
            with open(self._caminho, encoding="utf-8") as f:
                dados = json.load(f)
            self._respostas = [
                {**r, "_gatilhos": [_normalizar(g) for g in r["gatilhos"]]}
                for r in dados.get("respostas", [])
                if r.get("id") and r.get("texto") and r.get("gatilhos")
            ]
            log.info(f"⚡ Respostas prontas carregadas: {len(self._respostas)}")
        except FileNotFoundError:
            self._respostas = []
        except Exception as e:
            log.warning(f"⚡ Erro ao ler {self._caminho}: {e}")
            self._respostas = []

    def buscar(self, pergunta: str) -> dict | None:
        """Retorna a resposta pronta que bate com a pergunta, ou None."""
        frase = _normalizar(pergunta)
        if not frase:
            return None
        n_palavras = len(frase.split())
        for resposta in self._respostas:
            for gatilho in resposta["_gatilhos"]:
                if not gatilho:
                    continue
                cabe = n_palavras <= len(gatilho.split()) + _FOLGA_PALAVRAS
                if cabe and re.search(rf"\b{re.escape(gatilho)}\b", frase):
                    return resposta
        return None

    # ── Áudio ────────────────────────────────────────────────────────────────

    @staticmethod
    def _assinatura(texto: str, instruct: str) -> str:
        return hashlib.sha1(f"{instruct}\n{texto}".encode("utf-8")).hexdigest()

    async def preparar(self, tts) -> None:
        """
        Gera (ou carrega do disco) o áudio de cada resposta pronta.
        Só chama o TTS para respostas novas ou com texto/voz alterados.
        Usa asyncio.to_thread para I/O de áudio sem bloquear o event loop.
        """
        if not self._respostas:
            return
        os.makedirs(PASTA_AUDIO, exist_ok=True)
        for resposta in self._respostas:
            rid = resposta["id"]
            wav = os.path.join(PASTA_AUDIO, f"{rid}.wav")
            marca = os.path.join(PASTA_AUDIO, f"{rid}.txt")
            assinatura = self._assinatura(resposta["texto"], tts.instruct)
            try:
                with open(marca, encoding="utf-8") as f:
                    em_dia = f.read().strip() == assinatura
            except FileNotFoundError:
                em_dia = False

            try:
                if em_dia and os.path.exists(wav):
                    audio = await asyncio.to_thread(_ler_wav, wav)
                else:
                    log.info(f"⚡ Gerando áudio da resposta pronta '{rid}'...")
                    audio = await tts.gerar_audio_completo(resposta["texto"])
                    if audio is None:
                        continue
                    await asyncio.to_thread(
                        _salvar_wav_e_marca, wav, audio, marca, assinatura
                    )
                self._audios[rid] = audio
            except Exception as e:
                log.warning(f"⚡ Falha no áudio da resposta pronta '{rid}': {e}")

    async def falar(self, resposta: dict, tts) -> None:
        """Toca o áudio pronto; se ainda não existir, gera na hora como uma fala normal."""
        audio = self._audios.get(resposta["id"])
        if audio is not None:
            await tts.tocar_audio(audio)
        else:
            await tts.falar(resposta["texto"])