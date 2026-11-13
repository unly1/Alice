"""VTSConnector com a API do VTube Studio totalmente mockada (sem rede, sem .env)."""

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from functions import VTS_Connector as V


def _novo_conector():
    with patch.object(V.pyvts, "vts") as fabrica:
        vts = MagicMock()
        vts.connect = AsyncMock()
        vts.close = AsyncMock()
        vts.request_authenticate = AsyncMock(return_value=True)
        vts.request_authenticate_token = AsyncMock()
        vts.request = AsyncMock()
        vts.vts_request.requestHotKeyList.return_value = {"messageType": "HotkeysInCurrentModelRequest"}
        fabrica.return_value = vts
        c = V.VTSConnector()
    return c, vts


class AutenticacaoTest(unittest.IsolatedAsyncioTestCase):
    async def test_sem_token_salvo_retorna_false_sem_rede(self):
        c, vts = _novo_conector()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VTS_TOKEN", None)
            self.assertFalse(await c._autenticar_com_token_salvo())
        vts.request_authenticate.assert_not_awaited()

    async def test_token_revogado_authenticated_false(self):
        c, vts = _novo_conector()
        vts.request_authenticate.return_value = False
        with patch.dict(os.environ, {"VTS_TOKEN": "x"}):
            self.assertFalse(await c._autenticar_com_token_salvo())

    async def test_token_invalido_apierror(self):
        c, vts = _novo_conector()
        vts.request.return_value = {
            "messageType": "APIError",
            "data": {"errorID": 8, "message": "token"},
        }
        with patch.dict(os.environ, {"VTS_TOKEN": "x"}):
            self.assertFalse(await c._autenticar_com_token_salvo())

    async def test_resposta_sem_available_hotkeys_e_rejeitada(self):
        c, vts = _novo_conector()
        vts.request.return_value = {"messageType": "Ok", "data": {}}
        with patch.dict(os.environ, {"VTS_TOKEN": "x"}):
            self.assertFalse(await c._autenticar_com_token_salvo())

    async def test_excecao_na_rede_retorna_false(self):
        c, vts = _novo_conector()
        vts.request.side_effect = ConnectionError("ws caiu")
        with patch.dict(os.environ, {"VTS_TOKEN": "x"}):
            self.assertFalse(await c._autenticar_com_token_salvo())

    async def test_token_valido_retorna_true(self):
        c, vts = _novo_conector()
        vts.request.return_value = {
            "messageType": "HotkeysInCurrentModelResponse",
            "data": {"availableHotkeys": []},
        }
        with patch.dict(os.environ, {"VTS_TOKEN": "tok"}):
            self.assertTrue(await c._autenticar_com_token_salvo())
        self.assertEqual(vts.authentic_token, "tok")


class ConectarTest(unittest.IsolatedAsyncioTestCase):
    async def test_token_salvo_valido_nao_pede_novo_token(self):
        c, vts = _novo_conector()
        c._autenticar_com_token_salvo = AsyncMock(return_value=True)
        c._iniciar_keepalive = MagicMock()
        self.assertTrue(await c.conectar())
        vts.request_authenticate_token.assert_not_awaited()
        self.assertTrue(c.conectado)

    async def test_conectar_force_true_e_token_none_quando_invalido(self):
        c, vts = _novo_conector()
        vts.authentic_token = "velho"
        c._autenticar_com_token_salvo = AsyncMock(return_value=False)
        c._iniciar_keepalive = MagicMock()

        visto = {}

        async def pedir_token(force=False):
            visto["token_no_pedido"] = vts.authentic_token
            visto["force"] = force
            vts.authentic_token = "novo-token"

        vts.request_authenticate_token.side_effect = pedir_token
        with patch.object(V, "set_key") as set_key:
            self.assertTrue(await c.conectar())
        self.assertIsNone(visto["token_no_pedido"])
        self.assertIs(visto["force"], True)
        set_key.assert_called_once_with(".env", "VTS_TOKEN", "novo-token")

    async def test_autorizacao_negada_deixa_desconectado(self):
        c, vts = _novo_conector()
        c._autenticar_com_token_salvo = AsyncMock(return_value=False)
        vts.request_authenticate.return_value = False
        with patch.object(V, "set_key") as set_key:
            self.assertFalse(await c.conectar())
        set_key.assert_not_called()
        self.assertFalse(c.conectado)

    async def test_falha_de_rede_retorna_false(self):
        c, vts = _novo_conector()
        vts.connect.side_effect = OSError("recusado")
        self.assertFalse(await c.conectar())
        self.assertFalse(c.conectado)


class ParametrosTest(unittest.IsolatedAsyncioTestCase):
    def test_todas_as_expressoes_estao_em_0_1(self):
        for nome, params in V.EXPRESSOES.items():
            for p in params:
                with self.subTest(emocao=nome, id=p["id"]):
                    self.assertGreaterEqual(p["value"], 0.0)
                    self.assertLessEqual(p["value"], 1.0)

    def test_emocoes_esperadas(self):
        self.assertEqual(set(V.EXPRESSOES), {"Riso", "Tristeza", "Irritada", "Surpresa", "Neutro"})

    def test_lipsync_clamp_superior_e_inferior(self):
        c, _ = _novo_conector()
        for _ in range(50):
            c.atualizar_lipsync_rms(50.0)  # RMS absurdo
        self.assertLessEqual(c._mouth_open, 1.0)
        self.assertGreater(c._mouth_open, 0.9)
        for _ in range(100):
            c.atualizar_lipsync_rms(-3.0)  # negativo
        self.assertEqual(c._mouth_open, 0.0)

    def test_lipsync_silencio_fecha_boca(self):
        c, _ = _novo_conector()
        c.atualizar_lipsync_rms(0.2)
        self.assertGreater(c._mouth_open, 0.0)
        for _ in range(100):
            c.atualizar_lipsync_rms(0.001)
        self.assertEqual(c._mouth_open, 0.0)

    async def test_definir_abertura_boca_clamp(self):
        c, _ = _novo_conector()
        c.conectado = True
        c._injetar_parametros = AsyncMock()
        await c.definir_abertura_boca(7.5)
        self.assertEqual(c._mouth_open, 1.0)
        await c.definir_abertura_boca(-2)
        self.assertEqual(c._mouth_open, 0.0)

    def test_parametros_montados_em_0_1_e_lipsync_usa_maximo(self):
        c, _ = _novo_conector()
        c._emocao_atual = "Surpresa"  # MouthOpen base 0.35
        c._mouth_open = 0.1
        mouth = {p["id"]: p["value"] for p in c._montar_parametros_atuais()}["MouthOpen"]
        self.assertEqual(mouth, 0.35)
        c._mouth_open = 0.9
        mouth = {p["id"]: p["value"] for p in c._montar_parametros_atuais()}["MouthOpen"]
        self.assertEqual(mouth, 0.9)
        for p in c._montar_parametros_atuais():
            self.assertTrue(0.0 <= p["value"] <= 1.0)

    def test_emocao_desconhecida_cai_em_neutro(self):
        c, _ = _novo_conector()
        c._emocao_atual = "Inexistente"
        ids = {p["id"]: p["value"] for p in c._montar_parametros_atuais()}
        self.assertEqual(ids["MouthSmile"], 0.5)

    async def test_ativar_expressao_desconhecida_vira_neutro(self):
        c, _ = _novo_conector()
        c.conectado = True
        c._iniciar_keepalive = MagicMock()
        c._injetar_parametros = AsyncMock()
        await c.ativar_expressao("Furia")
        self.assertEqual(c._emocao_atual, "Neutro")

    async def test_falha_ao_injetar_marca_desconectado(self):
        c, _ = _novo_conector()
        c.conectado = True
        c._iniciar_keepalive = MagicMock()
        c._injetar_parametros = AsyncMock(side_effect=ConnectionError("x"))
        c._tentar_reconectar_seguro = AsyncMock(return_value=False)
        await c.ativar_expressao("Riso")
        self.assertFalse(c.conectado)

    async def test_injecao_envia_peso_e_valores_float(self):
        c, vts = _novo_conector()
        await c._injetar_parametros([{"id": "MouthOpen", "value": 1}])
        req = vts.request.await_args.args[0]
        self.assertEqual(req["messageType"], "InjectParameterDataValues")
        self.assertEqual(req["data"]["parameterValues"], [{"id": "MouthOpen", "value": 1.0, "weight": 1.0}])


if __name__ == "__main__":
    unittest.main()
