"""
cerebro/summarizer.py — Sumarização periódica do histórico de conversa.

Problema que resolve:
  O histórico cresce até MAX_HISTORICO mensagens brutas. Com visão + áudio +
  memória injetados no system prompt, o contexto total fica enorme, aumentando
  latência e dispersando o foco do modelo.

Solução:
  Quando o histórico atinge o limiar configurado, o Summarizer condensa as
  mensagens mais antigas em um "situational brief" de 3-5 linhas via LLM.
  As mensagens antigas são substituídas pelo resumo; as mais recentes são mantidas
  intactas para preservar o fluxo imediato da conversa.

Formato no histórico após sumarização:
  [
    {"role": "user",      "content": "[RESUMO DA CONVERSA]\n..."},
    {"role": "assistant", "content": "Certo, tenho esse contexto."},
    # ... últimas N mensagens intactas ...
  ]

Design:
  - Chamada assíncrona (não bloqueia o loop).
  - Fail-silently: se o LLM falhar, retorna o histórico original sem truncar.
  - Loga o resumo gerado para auditoria.
"""

import asyncio
import ollama
from cerebro.logger import log

# Quantas mensagens recentes manter intactas após a sumarização
_MANTER_RECENTES = 8

# Quantos caracteres de cada mensagem usar no contexto enviado ao LLM
_MAX_CHARS_MSG = 400


class Summarizer:
    """
    Compacta o histórico de conversa quando ele fica longo demais.

    Uso:
        summarizer = Summarizer()
        historico = await summarizer.compactar_se_necessario(historico, modelo, limiar=16)
    """

    async def compactar_se_necessario(
        self,
        historico: list,
        modelo: str,
        limiar: int = 16,
    ) -> list:
        """
        Compacta o histórico se `len(historico) >= limiar`.

        Args:
            historico: Lista de dicts {"role": ..., "content": ...}.
            modelo:    Nome do modelo Ollama a usar para o resumo.
            limiar:    Número mínimo de mensagens para acionar a sumarização.

        Returns:
            Novo histórico compactado, ou o original se não atingiu o limiar
            ou se a sumarização falhou.
        """
        if len(historico) < limiar:
            return historico

        antigos = historico[:-_MANTER_RECENTES]
        recentes = historico[-_MANTER_RECENTES:]

        if not antigos:
            return historico

        log.info(
            f"📝 [Summarizer] Histórico com {len(historico)} msgs → "
            f"sumarizando {len(antigos)} antigas, mantendo {len(recentes)} recentes..."
        )

        resumo = await self._resumir(antigos, modelo)
        if not resumo:
            log.warning("📝 [Summarizer] Sumarização falhou — histórico mantido.")
            return historico

        log.info(f"📝 [Summarizer] Resumo gerado: {resumo[:120]}...")

        # Injeta o resumo como um par user/assistant para manter a alternância de roles
        bloco_resumo = [
            {
                "role": "user",
                "content": (
                    "[RESUMO DA CONVERSA ANTERIOR]\n"
                    f"{resumo}\n"
                    "[Fim do resumo — continue a conversa normalmente a partir daqui.]"
                ),
            },
            {
                "role": "assistant",
                "content": "Entendido! Tenho esse contexto em mente.",
            },
        ]

        novo_historico = bloco_resumo + recentes
        log.info(
            f"📝 [Summarizer] Histórico reduzido: {len(historico)} → {len(novo_historico)} mensagens."
        )
        return novo_historico

    async def _resumir(self, mensagens: list, modelo: str) -> str:
        """
        Envia o trecho antigo do histórico ao LLM e pede um resumo situacional.

        Retorna o texto do resumo, ou "" se falhar.
        """
        contexto = "\n".join(
            f"{m['role'].upper()}: {m['content'][:_MAX_CHARS_MSG]}" for m in mensagens
        )

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: ollama.chat(
                    model=modelo,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Você é um assistente de memória. "
                                "Sua única tarefa é criar um resumo conciso da conversa abaixo. "
                                "O resumo deve ter 3 a 5 linhas, em português brasileiro, "
                                "capturando: o tema principal discutido, decisões ou informações "
                                "importantes trocadas, e o estado emocional geral da conversa. "
                                "Escreva na terceira pessoa (ex: 'O usuário perguntou sobre...'). "
                                "NÃO inclua cumprimentos, disclaimers ou explicações — apenas o resumo."
                            ),
                        },
                        {
                            "role": "user",
                            "content": f"Resuma esta conversa:\n\n{contexto}",
                        },
                    ],
                    options={"temperature": 0.2, "num_predict": 300},
                ),
            )
            return response["message"]["content"].strip()
        except Exception as e:
            log.warning(f"📝 [Summarizer] Erro ao chamar Ollama: {e}")
            return ""
