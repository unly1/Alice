"""RespostasProntas e ContentManager: caminhos independentes do cwd, intenções e fallbacks."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from functions import ContentManager as CMmod
from functions import RespostasProntas as RPmod

RAIZ = Path(__file__).resolve().parent.parent

FRASES_POR_INTENCAO = {
    "apresentacao": "Alice, se apresente",
    "capacidades": "o que você consegue fazer?",
    "saudacao_estado": "Oi Alice, tudo bem?",
    "bom_dia": "bom dia",
    "boa_tarde": "Boa tarde, Alice!",
    "boa_noite": "boa noite",
    "agradecimento": "muito obrigado",
    "despedida": "tchau",
    "confirmacao_escuta": "você está me ouvindo?",
}


class _ForaDoCwd(unittest.TestCase):
    """Muda o cwd para um diretório vazio durante o teste."""

    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._cwd)


class RespostasProntasTest(_ForaDoCwd):
    def test_caminhos_absolutos_ancorados_no_projeto(self):
        self.assertEqual(Path(RPmod.CAMINHO_JSON), RAIZ / "respostas_prontas.json")
        self.assertTrue(Path(RPmod.PASTA_AUDIO).is_absolute())
        self.assertTrue(RPmod.PASTA_AUDIO.startswith(str(RAIZ)))

    def test_carrega_json_mesmo_com_cwd_diferente(self):
        rp = RPmod.RespostasProntas()
        self.assertEqual(len(rp._respostas), 9)

    def test_caminho_relativo_resolve_pela_raiz_do_projeto(self):
        rp = RPmod.RespostasProntas("respostas_prontas.json")
        self.assertEqual(len(rp._respostas), 9)
        self.assertEqual(rp._caminho, str(RAIZ / "respostas_prontas.json"))

    def test_nove_intencoes_configuradas(self):
        rp = RPmod.RespostasProntas()
        self.assertEqual({r["id"] for r in rp._respostas}, set(FRASES_POR_INTENCAO))

    def test_detecta_cada_intencao(self):
        rp = RPmod.RespostasProntas()
        for rid, frase in FRASES_POR_INTENCAO.items():
            with self.subTest(intencao=rid):
                achada = rp.buscar(frase)
                self.assertIsNotNone(achada, frase)
                self.assertEqual(achada["id"], rid)

    def test_nao_dispara_em_frase_longa_ou_vazia(self):
        rp = RPmod.RespostasProntas()
        self.assertIsNone(rp.buscar(""))
        self.assertIsNone(rp.buscar("   ...  "))
        self.assertIsNone(rp.buscar("alice"))  # só prefixo ignorado
        self.assertIsNone(rp.buscar("tchau eu preciso que você escreva um script em python agora"))
        self.assertIsNone(rp.buscar("qual a capital da França"))

    def test_arquivo_ausente_nao_quebra(self):
        rp = RPmod.RespostasProntas(os.path.join(self._tmp.name, "nao_existe.json"))
        self.assertEqual(rp._respostas, [])
        self.assertIsNone(rp.buscar("bom dia"))

    def test_json_corrompido_nao_quebra(self):
        p = Path(self._tmp.name) / "ruim.json"
        p.write_text("{ nao e json", encoding="utf-8")
        self.assertEqual(RPmod.RespostasProntas(p)._respostas, [])

    def test_entradas_incompletas_sao_descartadas(self):
        p = Path(self._tmp.name) / "parcial.json"
        p.write_text(json.dumps({"respostas": [
            {"id": "ok", "texto": "t", "gatilhos": ["oi pessoal"]},
            {"id": "sem_texto", "gatilhos": ["x"]},
            {"id": "", "texto": "t", "gatilhos": ["y"]},
            {"id": "sem_gatilho", "texto": "t", "gatilhos": []},
        ]}), encoding="utf-8")
        rp = RPmod.RespostasProntas(p)
        self.assertEqual([r["id"] for r in rp._respostas], ["ok"])

    def test_normalizacao(self):
        self.assertEqual(RPmod._normalizar("Oi, ALICE! Você está?"), "voce esta")


class ContentManagerTest(_ForaDoCwd):
    def test_raiz_padrao_nao_depende_do_cwd(self):
        cm = CMmod.ContentManager()
        self.assertEqual(cm._base, RAIZ)
        self.assertEqual(len(cm.listar_personalidades()), 9)
        self.assertIn("respostas_vazias", cm.listar_respostas())

    def test_resposta_vazia_vem_do_arquivo(self):
        cm = CMmod.ContentManager()
        self.assertIn(cm.get_resposta_vazia(), cm.listar_respostas()["respostas_vazias"])

    def test_fallback_com_base_vazia(self):
        cm = CMmod.ContentManager(base_dir=self._tmp.name)
        self.assertEqual(cm.get_resposta_vazia(), CMmod.ContentManager._FALLBACK_RESPOSTA_VAZIA)
        self.assertEqual(cm.listar_personalidades(), [])
        self.assertEqual(cm.get_personalidade("alegre"), CMmod.ContentManager._FALLBACK_PERSONALIDADE)

    def test_fallback_com_arquivo_de_respostas_vazio(self):
        os.makedirs(os.path.join(self._tmp.name, "respostas"))
        Path(self._tmp.name, "respostas", "respostas_vazias.txt").write_text("\n  \n", encoding="utf-8")
        cm = CMmod.ContentManager(base_dir=self._tmp.name)
        self.assertEqual(cm.get_resposta_vazia(), CMmod.ContentManager._FALLBACK_RESPOSTA_VAZIA)
        self.assertEqual(cm.get_resposta_aleatoria("inexistente", "padrao"), "padrao")

    def test_hot_reload_de_arquivo_criado_depois(self):
        cm = CMmod.ContentManager(base_dir=self._tmp.name)
        os.makedirs(os.path.join(self._tmp.name, "respostas"))
        Path(self._tmp.name, "respostas", "respostas_vazias.txt").write_text("só esta\n", encoding="utf-8")
        self.assertEqual(cm.get_resposta_vazia(), "só esta")

    def test_personalidade_nome_normalizado(self):
        cm = CMmod.ContentManager()
        self.assertEqual(cm.get_personalidade("  ALEGRE "), cm.get_personalidade("alegre"))
        self.assertNotEqual(cm.get_personalidade("alegre"), CMmod.ContentManager._FALLBACK_PERSONALIDADE)

    def test_random_usa_apenas_linhas_do_cache(self):
        cm = CMmod.ContentManager(base_dir=self._tmp.name)
        cm._respostas["x"] = ["a", "b"]
        with patch.object(CMmod.random, "choice", return_value="b") as ch:
            self.assertEqual(cm.get_resposta_aleatoria("x"), "b")
        ch.assert_called_once_with(["a", "b"])


if __name__ == "__main__":
    unittest.main()
