"""
cerebro/sonho.py — Reflexão offline da Alice ("Sonho").

Durante períodos de inatividade prolongada, a Alice "sonha":
  1. Lê todos os fatos da memória de longo prazo.
  2. Pede ao LLM um insight novo sobre o usuário — algo que ainda não foi dito.
  3. Salva esse insight como um novo fato na memória.
  4. Exibe o sonho na GUI (sem falar em voz alta).

Comportamento:
  - Só aciona após TEMPO_SONHO segundos sem nenhuma interação real.
  - Após sonhar, aguarda TEMPO_SONHO novamente antes do próximo sonho.
  - Fail-silently: se o LLM falhar ou não houver fatos suficientes, não faz nada.
  - Não bloqueia o loop principal — roda como task asyncio em background.
"""

import asyncio
import ollama
from cerebro.logger import log

# Mínimo de fatos necessários para o sonho ser útil
_MIN_FATOS = 3

# Máximo de fatos enviados ao LLM (evita tokens excessivos)
_MAX_FATOS = 30

# Máximo de tokens para o sonho gerado
_MAX_TOKENS_SONHO = 120


class Sonho:
    """
    Gerencia a reflexão offline ("sonho") da Alice.

    Uso:
        sonho = Sonho(modelo="llama3", memoria=memoria_longa)
        asyncio.create_task(sonho.loop(inatividade_fn, fila_saida_gui))
    """

    def __init__(self, modelo: str, memoria):
        self.modelo = modelo
        self.memoria = memoria

    async def loop(
        self,
        inatividade_fn,  # callable() → float: segundos desde última interação
        fila_saida_gui,  # queue.Queue para enviar texto à GUI
        tempo_sonho: int = 300,  # segundos de inatividade para acionar
    ):
        """
        Loop em background. Monitora inatividade e aciona o sonho quando adequado.

        Args:
            inatividade_fn:  Função (síncrona) que retorna segundos desde a última
                             interação real do usuário. Fornecida pelo main.py.
            fila_saida_gui:  Fila de saída para exibir o sonho na janela de chat.
            tempo_sonho:     Tempo mínimo de inatividade (segundos) para acionar.
        """
        log.info(f"🌙 [Sonho] Loop iniciado | Inatividade mínima: {tempo_sonho}s")

        # Aguarda estabilização inicial antes do primeiro ciclo
        await asyncio.sleep(tempo_sonho)

        while True:
            inativo_ha = inatividade_fn()

            if inativo_ha >= tempo_sonho:
                log.info(
                    f"🌙 [Sonho] {inativo_ha:.0f}s de inatividade — iniciando reflexão..."
                )
                await self._sonhar(fila_saida_gui)
                # Após sonhar, aguarda o período completo antes de tentar novamente
                await asyncio.sleep(tempo_sonho)
            else:
                # Verifica novamente em 30s (polling leve)
                await asyncio.sleep(30)

    async def _sonhar(self, fila_saida_gui):
        """Gera e persiste um insight sobre o usuário a partir da memória."""
        fatos = self.memoria.buscar_todos_fatos()

        if len(fatos) < _MIN_FATOS:
            log.debug(f"🌙 [Sonho] Poucos fatos ({len(fatos)}) — sonho adiado.")
            return

        # Seleciona os fatos mais recentes para não ultrapassar o limite
        fatos_recentes = fatos[-_MAX_FATOS:]
        lista_fatos = "\n".join(
            f"- {f['fato']}" if isinstance(f, dict) else f"- {f}"
            for f in fatos_recentes
        )

        insight = await self._gerar_insight(lista_fatos)
        if not insight:
            return

        log.info(f"🌙 [Sonho] Insight gerado: {insight}")

        # Salva o insight como novo fato na memória
        self.memoria.salvar_fato(f"[Reflexão] {insight}")

        # Exibe na GUI sem falar em voz alta
        fila_saida_gui.put(f"🌙 *{insight}*")

    async def _gerar_insight(self, lista_fatos: str) -> str:
        """
        Pede ao LLM um insight novo sobre o usuário a partir dos fatos conhecidos.
        Roda em executor para não bloquear o event loop.
        """

        def _chamar_ollama():
            return ollama.chat(
                model=self.modelo,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Você é Alice, uma assistente pessoal íntima e observadora. "
                            "Com base nos fatos que você sabe sobre o usuário, gere UMA reflexão "
                            "ou insight novo e genuíno — algo que você ainda não disse, uma conexão "
                            "entre fatos, um padrão que você notou, ou uma pergunta curiosa que "
                            "surgiu enquanto você 'pensava' durante o silêncio. "
                            "Escreva em primeira pessoa, de forma natural e calorosa, como se fosse "
                            "um pensamento espontâneo. Máximo de 2 frases. "
                            "NÃO comece com 'Eu pensei' ou 'Enquanto você estava ausente'. "
                            "Escreva diretamente o insight."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"O que você sabe sobre o usuário:\n{lista_fatos}\n\n"
                            "Gere um insight ou reflexão nova sobre essa pessoa."
                        ),
                    },
                ],
                options={"temperature": 0.85, "num_predict": _MAX_TOKENS_SONHO},
            )

        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(None, _chamar_ollama)
            texto = response["message"]["content"].strip()
            return texto if texto else ""
        except Exception as e:
            log.warning(f"🌙 [Sonho] Erro ao gerar insight: {e}")
            return ""
