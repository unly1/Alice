"""OllamaClient: heurística de humor sem chamadas externas e contrato ReAct [TOOL:nome:arg]."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import functions.OllamaClient as OC


def _cliente():
    return OC.OllamaClient("modelo-teste", MagicMock(), MagicMock(), asyncio.Event())


def _resp(texto, p=1, r=1):
    return {"message": {"content": texto}, "prompt_eval_count": p, "eval_count": r}


class HumorLocalTest(unittest.IsolatedAsyncioTestCase):
    CASOS = {
        "você é muito fofa": "timida",
        "consegui! deu certo": "euforica",
        "estou muito triste hoje": "empatica",
        "deu um erro no meu código": "determinada",
        "muito obrigado pela ajuda": "grata",
        "tenho saudade da infância": "nostalgica",
        "você é lenta e inútil": "brava",
        "como funciona um algoritmo": "curiosa",
        "bom dia": "alegre",
        "": "alegre",
    }

    async def test_mapeamento_sem_nenhuma_chamada_externa(self):
        c = _cliente()
        with patch.object(OC.ollama, "chat") as chat, \
             patch.object(c, "_ollama_em_thread", new=AsyncMock()) as em_thread:
            for texto, esperado in self.CASOS.items():
                with self.subTest(texto=texto):
                    self.assertEqual(await c.descobrir_humor(texto), esperado)
            chat.assert_not_called()
            em_thread.assert_not_awaited()

    async def test_pergunta_generica_vira_curiosa(self):
        self.assertEqual(await _cliente().descobrir_humor("que horas são?"), "curiosa")

    async def test_prioridade_timida_antes_de_curiosa(self):
        self.assertEqual(await _cliente().descobrir_humor("você é linda, como funciona isso?"), "timida")

    async def test_resultado_sempre_e_personalidade_existente(self):
        c = _cliente()
        existentes = set(c._cm.listar_personalidades())
        for texto in self.CASOS:
            self.assertIn(await c.descobrir_humor(texto), existentes)

    async def test_sem_personalidades_cai_em_alegre(self):
        c = _cliente()
        c._cm = MagicMock()
        c._cm.listar_personalidades.return_value = []
        self.assertEqual(await c.descobrir_humor("qualquer coisa"), "alegre")

    async def test_modo_llm_com_falha_volta_para_heuristica(self):
        c = _cliente()
        with patch.object(c, "_ollama_em_thread", new=AsyncMock(side_effect=RuntimeError("off"))):
            self.assertEqual(await c.descobrir_humor("estou triste", usar_llm=True), "empatica")


class RegexToolTest(unittest.TestCase):
    def test_extrai_nome_e_argumento(self):
        m = OC._REGEX_TOOL.search("[TOOL:get_weather:Porto Alegre]")
        self.assertEqual((m.group(1), m.group(2)), ("get_weather", "Porto Alegre"))

    def test_argumento_vazio_ou_ausente(self):
        self.assertEqual(OC._REGEX_TOOL.search("[TOOL:parar_musica:]").group(2), "")
        self.assertIsNone(OC._REGEX_TOOL.search("[TOOL:parar_musica]").group(2))

    def test_case_insensitive_e_no_meio_do_texto(self):
        m = OC._REGEX_TOOL.search("claro! [tool:volume:60] pronto")
        self.assertEqual((m.group(1), m.group(2)), ("volume", "60"))

    def test_nao_casa_formatos_invalidos(self):
        for txt in ["TOOL:volume:60", "[TOOL::x]", "[TOOL volume:60]", "(TOOL:volume:60)"]:
            with self.subTest(txt=txt):
                self.assertIsNone(OC._REGEX_TOOL.search(txt))

    def test_prompt_documenta_todas_as_ferramentas_disponiveis(self):
        from functions.Tools import DISPONIVEIS
        for nome in DISPONIVEIS:
            self.assertIn(f"[TOOL:{nome}:", OC.PROMPT_FERRAMENTAS)


class PensarReActTest(unittest.IsolatedAsyncioTestCase):
    async def test_sem_tool_devolve_texto_e_tokens(self):
        c = _cliente()
        c._chamar_ollama_cancelavel = AsyncMock(return_value=_resp("Oi!", 10, 3))
        self.assertEqual(await c.pensar("oi", "alegre", []), ("Oi!", 10, 3))
        self.assertEqual(c._chamar_ollama_cancelavel.await_count, 1)

    async def test_tool_valida_executa_e_remove_marcador(self):
        c = _cliente()
        c._chamar_ollama_cancelavel = AsyncMock(
            side_effect=[_resp("[TOOL:get_current_time:Manaus]", 5, 2), _resp("São 10h em Manaus.", 7, 4)]
        )
        fake = MagicMock(return_value="10:00")
        with patch.dict(OC.funcoes_tools.DISPONIVEIS, {"get_current_time": fake}):
            texto, tp, tr = await c.pensar("que horas em Manaus?", "alegre", [])
        fake.assert_called_once_with("Manaus")
        self.assertEqual(texto, "São 10h em Manaus.")
        self.assertEqual((tp, tr), (12, 6))
        pos = c._chamar_ollama_cancelavel.await_args_list[1].args[0]
        self.assertIn("[RESULTADO]: 10:00", pos[-1]["content"])

    async def test_ferramenta_inexistente_informa_erro_ao_modelo(self):
        c = _cliente()
        c._chamar_ollama_cancelavel = AsyncMock(
            side_effect=[_resp("[TOOL:apagar_tudo:/]"), _resp("Não posso.")]
        )
        texto, _, _ = await c.pensar("x", "alegre", [])
        self.assertEqual(texto, "Não posso.")
        pos = c._chamar_ollama_cancelavel.await_args_list[1].args[0]
        self.assertIn("'apagar_tudo' não encontrada", pos[-1]["content"])

    async def test_loop_limitado_e_marcador_residual_removido(self):
        c = _cliente()
        c._chamar_ollama_cancelavel = AsyncMock(return_value=_resp("[TOOL:parar_musica:]"))
        fake = MagicMock(return_value="ok")
        with patch.dict(OC.funcoes_tools.DISPONIVEIS, {"parar_musica": fake}):
            texto, _, _ = await c.pensar("x", "alegre", [])
        self.assertEqual(c._chamar_ollama_cancelavel.await_count, 1 + OC._MAX_ITERACOES_TOOL)
        self.assertEqual(fake.call_count, OC._MAX_ITERACOES_TOOL)
        self.assertNotIn("[TOOL", texto)

    async def test_excecao_na_ferramenta_nao_derruba_turno(self):
        c = _cliente()
        c._chamar_ollama_cancelavel = AsyncMock(side_effect=[_resp("[TOOL:volume:5]"), _resp("Falhou, desculpa.")])
        with patch.dict(OC.funcoes_tools.DISPONIVEIS, {"volume": MagicMock(side_effect=RuntimeError("x"))}):
            texto, _, _ = await c.pensar("x", "alegre", [])
        self.assertEqual(texto, "Falhou, desculpa.")

    async def test_cancelamento_retorna_none(self):
        c = _cliente()
        c._chamar_ollama_cancelavel = AsyncMock(return_value=None)
        self.assertEqual(await c.pensar("x", "alegre", []), (None, 0, 0))


if __name__ == "__main__":
    unittest.main()
