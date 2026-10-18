"""
RespostasProntas — respostas fixas com áudio já gerado, para perguntas frequentes.

Responsabilidades:
- Ler respostas_prontas.json (gatilhos + texto de cada resposta)
- Reconhecer quando a pergunta do usuário é uma dessas perguntas
- Gerar o áudio de cada resposta uma vez e guardar em audio/prontas/
  (só regenera quando o texto ou a voz mudam)
"""

import hashlib
import json
import os
import re
import unicodedata

import numpy as np
import soundfile as sf

from cerebro.logger import log

CAMINHO_JSON = "respostas_prontas.json"
PASTA_AUDIO = os.path.join("audio", "prontas")
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


class RespostasProntas:
    """Carrega, reconhece e toca as respostas prontas da Alice."""

    def __init__(self, caminho: str = CAMINHO_JSON):
        self._caminho = caminho
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
                    audio, _ = sf.read(wav, dtype="float32")
                else:
                    log.info(f"⚡ Gerando áudio da resposta pronta '{rid}'...")
                    audio = await tts.gerar_audio_completo(resposta["texto"])
                    if audio is None:
                        continue
                    sf.write(wav, audio, _SAMPLE_RATE)
                    with open(marca, "w", encoding="utf-8") as f:
                        f.write(assinatura)
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
