"""Contrato de functions/Tools.py: regexes ancoradas, confirmação em 2 etapas, bloqueios."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import functions.Tools as T


class RegexAncoradasTest(unittest.TestCase):
    def test_comandos_validos_casam(self):
        casos = [
            (T._RE_YOUTUBE, "pesquisar no youtube por gatos"),
            (T._RE_WIKIPEDIA, "buscar na wikipedia sobre python"),
            (T._RE_PARAR_MUSICA, "parar música"),
            (T._RE_VOLUME, "volume 50"),
            (T._RE_DATA_HOJE, "que dia é hoje"),
            (T._RE_PIADA, "conte uma piada"),
            (T._RE_LIXEIRA, "esvaziar lixeira"),
            (T._RE_AJUDA, "ajuda"),
        ]
        for regex, texto in casos:
            with self.subTest(texto=texto):
                self.assertIsNotNone(regex.search(texto))

    def test_ancora_evita_falso_positivo_no_meio_da_frase(self):
        casos = [
            (T._RE_LIXEIRA, "preciso esvaziar lixeira depois"),
            (T._RE_LIXEIRA, "esvaziar lixeira agora mesmo"),
            (T._RE_PIADA, "piada do dia"),
            (T._RE_PARAR_MUSICA, "eu vou parar música depois"),
            (T._RE_VOLUME, "o volume está alto demais"),
            (T._RE_AJUDA, "preciso de ajuda com o código"),
            (T._RE_TOCAR_MUSICA, "eu quero tocar violão"),
        ]
        for regex, texto in casos:
            with self.subTest(texto=texto):
                self.assertIsNone(regex.search(texto))


class ProcessCommandTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.speak = AsyncMock()

    async def test_texto_vazio_ou_conversa_livre_segue_para_llm(self):
        for texto in ["", "   ", "como está o tempo lá fora?", "apague todos os meus arquivos"]:
            with self.subTest(texto=texto):
                self.assertIsNone(await T.process_command(texto, self.speak))
        self.speak.assert_not_awaited()

    async def test_lixeira_exige_confirmacao_e_nao_esvazia_na_1a_etapa(self):
        espera = MagicMock()
        with patch.object(T, "WINSHELL_AVAILABLE", True), \
             patch.object(T, "winshell", create=True) as ws:
            r = await T.process_command("Esvaziar a lixeira", self.speak, on_waiting_input=espera)
        self.assertEqual(r, "WAIT:confirmar_lixeira")
        espera.assert_called_once_with("confirmar_lixeira")
        ws.recycle_bin.assert_not_called()

    async def test_lixeira_sem_winshell_nao_pede_confirmacao(self):
        with patch.object(T, "WINSHELL_AVAILABLE", False):
            r = await T.process_command("limpar lixeira", self.speak)
        self.assertIn("winshell", r)
        self.speak.assert_not_awaited()

    async def test_confirmacao_negativa_cancela_sem_tocar_na_lixeira(self):
        for resp in ["não", "talvez", "", "esvaziar lixeira", "sim senhor"]:
            with self.subTest(resp=resp), \
                 patch.object(T, "WINSHELL_AVAILABLE", True), \
                 patch.object(T, "winshell", create=True) as ws:
                r = await T.execute_empty_recycle_bin(resp, self.speak)
                self.assertIn("cancelada", r)
                ws.recycle_bin.assert_not_called()

    async def test_confirmacao_positiva_esvazia(self):
        for resp in ["sim", "Confirmar.", "pode esvaziar!"]:
            with self.subTest(resp=resp), \
                 patch.object(T, "WINSHELL_AVAILABLE", True), \
                 patch.object(T, "winshell", create=True) as ws:
                r = await T.execute_empty_recycle_bin(resp, self.speak)
                self.assertIn("sucesso", r)
                ws.recycle_bin.return_value.empty.assert_called_once()

    async def test_volume_sem_numero_aguarda_entrada(self):
        espera = MagicMock()
        r = await T.process_command("volume", self.speak, on_waiting_input=espera)
        self.assertEqual(r, "WAIT:volume")
        espera.assert_called_once_with("volume")

    async def test_volume_com_numero_delega_ao_player(self):
        with patch.object(T.tools.music, "volume", return_value="Volume ajustado para 40%.") as v:
            r = await T.process_command("ajustar volume 40", self.speak)
        v.assert_called_once_with(40)
        self.assertIn("40", r)

    async def test_parar_musica_tem_precedencia_sobre_tocar(self):
        with patch.object(T.tools.music, "parar_musica", return_value="Música parada!") as p, \
             patch.object(T.tools.music, "tocar_musica") as t:
            await T.process_command("parar música", self.speak)
        p.assert_called_once()
        t.assert_not_called()

    async def test_tocar_extrai_argumento(self):
        with patch.object(T.tools.music, "tocar_musica", return_value="Tocando!") as t:
            await T.process_command("tocar a música de Daft Punk", self.speak)
        t.assert_called_once_with("daft punk")

    async def test_youtube_sem_termo_aguarda(self):
        r = await T.process_command("abrir youtube", self.speak)
        self.assertEqual(r, "WAIT:youtube")

    async def test_youtube_com_termo_abre_url_codificada(self):
        with patch.object(T.webbrowser, "open") as abrir:
            await T.process_command("pesquisar no youtube por café com leite", self.speak)
        url = abrir.call_args.args[0]
        self.assertIn("search_query=caf%C3%A9%20com%20leite", url)


class ToolManagerTest(unittest.TestCase):
    def test_ferramenta_inexistente_e_bloqueada(self):
        for nome in ["rm", "os.system", "esvaziar_lixeira", ""]:
            with self.subTest(nome=nome):
                self.assertIn("não encontrada", T.tools.executar(nome, "-rf /"))

    def test_whitelist_do_contrato_react(self):
        self.assertEqual(
            set(T.DISPONIVEIS),
            {"get_current_time", "get_weather", "search_web",
             "tocar_musica", "parar_musica", "volume"},
        )

    def test_excecao_na_ferramenta_vira_mensagem(self):
        mgr = T.ToolManager()
        mgr.DISPONIVEIS["volume"] = MagicMock(side_effect=RuntimeError("boom"))
        self.assertIn("Erro ao executar 'volume'", mgr.executar("volume", "10"))

    def test_volume_invalido(self):
        self.assertIn("inválido", T.MusicTools().volume("abc"))

    def test_volume_sem_musica(self):
        self.assertIn("Nenhuma música", T.MusicTools().volume("50"))

    def test_clean_command(self):
        self.assertEqual(T._clean_command("tocar música lofi", ["tocar música", "tocar"]), "lofi")
        self.assertEqual(T._clean_command("gatos no youtube", [], [" no youtube"]), "gatos")


if __name__ == "__main__":
    unittest.main()
