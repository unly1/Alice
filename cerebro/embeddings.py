"""
cerebro/embeddings.py — Geração de embeddings via Ollama.

Usa o modelo nomic-embed-text para transformar texto em vetores Float32.
Retorna lista vazia em caso de falha (sistema degrada para ILIKE silenciosamente).
"""

import ollama
from cerebro.logger import log

MODELO_EMBED = "nomic-embed-text"


def gerar_embedding(texto: str) -> list[float]:
    """
    Gera embedding do texto usando nomic-embed-text via Ollama.

    Retorna lista de floats (vetor) em caso de sucesso.
    Retorna [] se o modelo não estiver disponível ou ocorrer qualquer erro
    — o sistema degrada graciosamente para busca por ILIKE.
    """
    if not texto or not texto.strip():
        return []
    try:
        resp = ollama.embeddings(model=MODELO_EMBED, prompt=texto.strip())
        return resp["embedding"]
    except Exception as e:
        log.warning(f"⚠️  [Embed] Falha ao gerar embedding ({MODELO_EMBED}): {e}")
        return []
