"""Memória de longo prazo e grafo — SEMPRE em arquivos temporários, nunca nos .db de produção."""

import gc
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from cerebro import graph_memory, memoria, sonho

_DB_PROD_MEMORIA = os.path.abspath(os.path.join(os.path.dirname(memoria.__file__), "memoria.db"))
_DB_PROD_GRAFO = os.path.abspath(os.path.join(os.path.dirname(graph_memory.__file__), "grafo.db"))


class MemoriaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = os.path.join(self.tmp.name, "memoria_teste.db")
        p = patch.object(memoria, "CAMINHO_DB", self.db)
        p.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(p.stop)
        self.addCleanup(gc.collect)
        self.mem = memoria.MemoriaLongaPrazo()

    def test_nao_toca_no_db_de_producao(self):
        self.assertNotEqual(os.path.abspath(memoria.CAMINHO_DB), _DB_PROD_MEMORIA)
        self.assertTrue(os.path.exists(self.db))

    def test_uuid5_deterministico_e_normalizado(self):
        a = memoria._gerar_id_fato("O usuário gosta de café.")
        b = memoria._gerar_id_fato("  o   USUÁRIO gosta de café!! ")
        self.assertEqual(a, b)
        self.assertNotEqual(a, memoria._gerar_id_fato("O usuário gosta de chá"))

    def test_deduplicacao(self):
        self.mem.salvar_fato("O usuário gosta de café.")
        self.mem.salvar_fato("o usuário   gosta de CAFÉ")
        self.mem.salvar_fato("O usuário gosta de café!")
        self.assertEqual(len(self.mem.buscar_todos_fatos()), 1)

    def test_sentinelas_e_curtos_sao_ignorados(self):
        for ruido in ["", "   ", "oi", "Nome não encontrado", "informação nao fornecida",
                      "Profissão: não mencionada", "idade nao forneceu"]:
            self.mem.salvar_fato(ruido)
        self.assertEqual(self.mem.buscar_todos_fatos(), [])

    def test_sentinela_helper(self):
        self.assertTrue(memoria._eh_fato_sentinela("não mencionado"))
        self.assertFalse(memoria._eh_fato_sentinela("Mora em Campo Grande"))

    def test_remover_e_reativar(self):
        self.mem.salvar_fato("Mora em Campo Grande")
        fato = self.mem.buscar_todos_fatos()[0]
        self.assertTrue(self.mem.remover_fato(fato["id"]))
        self.assertEqual(self.mem.buscar_todos_fatos(), [])
        self.mem.salvar_fato("Mora em Campo Grande")  # reativa
        self.assertEqual(len(self.mem.buscar_todos_fatos()), 1)

    def test_remover_inexistente(self):
        self.assertFalse(self.mem.remover_fato("nao-existe"))

    def test_limpar_retorna_total(self):
        self.mem.salvar_fato("Gosta de gatos pretos")
        self.mem.salvar_fato("Trabalha com Python")
        self.assertEqual(self.mem.limpar_memoria(), 2)
        self.assertEqual(self.mem.buscar_todos_fatos(), [])

    def test_bloco_memoria_por_contexto(self):
        self.mem.salvar_fato("Gosta de gatos pretos")
        self.mem.salvar_fato("Trabalha com Python")
        self.assertIn("gatos pretos", self.mem.construir_bloco_memoria("fale sobre gatos"))
        self.mem._ultimo_contexto = ""
        self.assertIn("Python", self.mem.construir_bloco_memoria())

    def test_bloco_vazio_sem_fatos(self):
        self.assertEqual(self.mem.construir_bloco_memoria(), "")


class ReflexaoTest(unittest.IsolatedAsyncioTestCase):
    async def test_sonho_adia_quando_so_ha_reflexoes_e_poucos_fatos(self):
        mem = MagicMock()
        mem.buscar_todos_fatos.return_value = [
            {"fato": "[Reflexão] insight antigo"}, {"fato": "fato um"}, {"fato": "fato dois"},
        ]
        s = sonho.Sonho("m", mem)
        s._gerar_insight = AsyncMock(return_value="novo")
        await s._sonhar(MagicMock())
        s._gerar_insight.assert_not_awaited()
        mem.salvar_fato.assert_not_called()

    async def test_sonho_filtra_reflexoes_do_prompt_e_salva_com_prefixo(self):
        mem = MagicMock()
        mem.buscar_todos_fatos.return_value = [
            {"fato": "[Reflexão] velho"}, {"fato": "a1"}, {"fato": "a2"}, {"fato": "a3"},
        ]
        s = sonho.Sonho("m", mem)
        s._gerar_insight = AsyncMock(return_value="novo")
        await s._sonhar(MagicMock())
        enviado = s._gerar_insight.await_args.args[0]
        self.assertNotIn("velho", enviado)
        mem.salvar_fato.assert_called_once_with("[Reflexão] novo")


class GrafoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = os.path.join(self.tmp.name, "grafo_teste.db")
        p = patch.object(graph_memory, "CAMINHO_GRAFO", self.db)
        p.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(p.stop)
        self.addCleanup(gc.collect)
        self.g = graph_memory.GraphMemory()

    def test_nao_toca_no_db_de_producao(self):
        self.assertNotEqual(os.path.abspath(graph_memory.CAMINHO_GRAFO), _DB_PROD_GRAFO)

    def test_insere_e_deduplica_case_insensitive(self):
        self.assertTrue(self.g.salvar_tripla("Marcos", "gosta_de", "Café"))
        self.assertFalse(self.g.salvar_tripla("marcos", "GOSTA_DE", "café"))
        self.assertFalse(self.g.salvar_tripla("MARCOS", "Gosta_De", "CAFÉ"))
        self.assertEqual(len(self.g.listar_todas()), 1)

    def test_relacao_normalizada_em_maiusculas(self):
        self.g.salvar_tripla("Marcos", "usa", "Python")
        self.assertEqual(self.g.listar_todas()[0]["relacao"], "USA")

    def test_vazios_rejeitados(self):
        self.assertFalse(self.g.salvar_tripla("", "USA", "x"))
        self.assertFalse(self.g.salvar_tripla("a", "  ", "x"))
        self.assertFalse(self.g.salvar_tripla("a", "USA", ""))

    def test_vizinhos_case_insensitive(self):
        self.g.salvar_tripla("Marcos", "TRABALHA_EM", "Empresa Tech")
        self.g.salvar_tripla("Alice", "CONHECE", "marcos")
        self.assertEqual(len(self.g.buscar_vizinhos("MARCOS")), 2)
        self.assertEqual(self.g.buscar_vizinhos("  "), [])

    def test_busca_por_contexto(self):
        self.g.salvar_triplas([("Marcos", "GOSTA_DE", "café"), ("Ana", "USA", "Rust"), ("x", "y")])
        r = self.g.buscar_por_contexto("o que MARCOS gosta?")
        self.assertEqual([t[0] for t in r], ["Marcos"])
        self.assertEqual(self.g.buscar_por_contexto(""), [])

    def test_salvar_triplas_conta_apenas_novas(self):
        n = self.g.salvar_triplas([("A", "R", "B"), ("a", "r", "b"), ("C", "R", "D")])
        self.assertEqual(n, 2)

    def test_bloco_e_limpar(self):
        self.assertEqual(self.g.construir_bloco_grafo("qualquer"), "")
        self.g.salvar_tripla("Marcos", "GOSTA_DE", "café")
        self.assertIn("Marcos --[GOSTA_DE]--> café", self.g.construir_bloco_grafo("marcos"))
        self.assertEqual(self.g.limpar(), 1)
        self.assertEqual(self.g.listar_todas(), [])

    def test_remover_tripla(self):
        self.g.salvar_tripla("A", "R", "B")
        self.g.remover_tripla(self.g.listar_todas()[0]["id"])
        self.assertEqual(self.g.listar_todas(), [])


if __name__ == "__main__":
    unittest.main()
