import pyvts
import os
import uuid
from dotenv import load_dotenv, set_key
from cerebro.logger import log

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Mapeamento de Emoção → Parâmetros VTS Hiyori (Opção B: Injeção Direta)
#
# Fonte: hiyori.vtube.json — mapeamento VTS Input ↔ Live2D Output:
#   MouthSmile (0-1) → ParamMouthForm + ParamEyeLSmile/RSmile + ParamCheek (tudo junto!)
#   MouthOpen  (0-1) → ParamMouthOpenY
#   EyeOpenLeft/Right (0-1) → ParamEyeLOpen/ROpen (output 0-1.9 no modelo)
#   BrowLeftY/BrowRightY (0-1) → ParamBrowLY + ParamBrowLForm (input 0=brow up, 1=brow down)
# ─────────────────────────────────────────────────────────────────────────────
EXPRESSOES: dict[str, list[dict]] = {
    # 😄 Riso — sorriso largo, olhos semicerrados, blush rosado
    "Riso": [
        {"id": "MouthSmile", "value": 1.0},  # smile + eye-squint + blush (tudo junto)
        {"id": "EyeOpenLeft", "value": 0.55},  # olhos meio fechados (happy squint)
        {"id": "EyeOpenRight", "value": 0.55},
        {"id": "BrowLeftY", "value": 0.25},  # sobrancelhas levantadas (feliz)
        {"id": "BrowRightY", "value": 0.25},
        {"id": "MouthOpen", "value": 0.0},
    ],
    # 😢 Tristeza — sem sorriso, olhos caídos, sobrancelhas em arco triste
    "Tristeza": [
        {"id": "MouthSmile", "value": 0.0},  # boca virada p/ baixo + sem blush
        {"id": "EyeOpenLeft", "value": 0.55},  # olhos meio fechados (abatido)
        {"id": "EyeOpenRight", "value": 0.55},
        {"id": "BrowLeftY", "value": 0.8},  # sobrancelhas caídas (triste)
        {"id": "BrowRightY", "value": 0.8},
        {"id": "MouthOpen", "value": 0.0},
    ],
    # 😠 Irritada — sobrancelhas fortemente franzidas para baixo
    "Irritada": [
        {"id": "MouthSmile", "value": 0.15},  # boca neutra/levemente fechada
        {"id": "EyeOpenLeft", "value": 0.65},  # olhos estreitados (raiva)
        {"id": "EyeOpenRight", "value": 0.65},
        {"id": "BrowLeftY", "value": 1.0},  # sobrancelhas totalmente para baixo (raiva)
        {"id": "BrowRightY", "value": 1.0},
        {"id": "MouthOpen", "value": 0.0},
    ],
    # 😲 Surpresa — olhos arregalados, sobrancelhas levantadas, boca entreaberta
    "Surpresa": [
        {"id": "MouthSmile", "value": 0.5},  # boca neutra
        {"id": "EyeOpenLeft", "value": 1.0},  # olhos totalmente abertos (arregalados)
        {"id": "EyeOpenRight", "value": 1.0},
        {"id": "BrowLeftY", "value": 0.0},  # sobrancelhas totalmente levantadas
        {"id": "BrowRightY", "value": 0.0},
        {"id": "MouthOpen", "value": 0.35},  # boca entreaberta
    ],
    # 😐 Neutro — reseta tudo para a expressão natural de repouso
    "Neutro": [
        {"id": "MouthSmile", "value": 0.5},  # neutra
        {"id": "EyeOpenLeft", "value": 0.75},  # aberto normalmente
        {"id": "EyeOpenRight", "value": 0.75},
        {"id": "BrowLeftY", "value": 0.5},  # sobrancelha posição neutra
        {"id": "BrowRightY", "value": 0.5},
        {"id": "MouthOpen", "value": 0.0},  # boca fechada
    ],
}


class VTSConnector:
    """
    Controlador do VTube Studio via API WebSocket (pyvts).

    Opção B — Injeção Direta de Parâmetros Live2D:
    Em vez de acionar hotkeys, injeta valores de parâmetros VTS diretamente
    (MouthSmile, EyeOpenLeft, BrowLeftY etc.) que mapeiam para os parâmetros
    Live2D do modelo Hiyori via hiyori.vtube.json.

    Isso permite expressões faciais precisas e suaves sem precisar configurar
    nenhuma hotkey adicional no VTube Studio.
    """

    def __init__(self):
        plugin_info = {
            "plugin_name": os.getenv("VTS_PLUGIN_NAME", "Alice VTuber"),
            "developer": os.getenv("VTS_DEVELOPER", "Mario Alexandre"),
            "authentication_token_path": "temp_pyvts_token.txt",
        }
        self.vts = pyvts.vts(plugin_info=plugin_info)
        self.conectado = False

    async def conectar(self) -> bool:
        """Conecta ao VTube Studio e autentica. Retorna True se sucesso."""
        log.info("🔌 Conectando ao VTube Studio na porta 8001...")
        try:
            await self.vts.connect()

            autenticado = await self._autenticar_com_token_salvo()

            if not autenticado:
                log.warning(
                    "⏳ Token inválido ou ausente. Solicitando nova autorização..."
                )
                log.info("   → Clique em 'Allow' na janela do VTube Studio!")
                await self.vts.request_authenticate_token()
                await self.vts.request_authenticate()

                novo_token = self.vts.authentic_token
                set_key(".env", "VTS_TOKEN", novo_token)
                log.info("🔐 Novo token salvo automaticamente no .env!")

            self.conectado = True
            log.info(
                "✅ VTube Studio conectado! Expressões via injeção de parâmetros ativadas."
            )
            return True

        except Exception as e:
            log.error(f"❌ Erro ao conectar ao VTube Studio: {e}")
            log.warning(
                "⚠️  Verifique se o VTube Studio está aberto e a API WebSocket ativa."
            )
            self.conectado = False
            return False
        finally:
            if os.path.exists("temp_pyvts_token.txt"):
                os.remove("temp_pyvts_token.txt")

    async def _autenticar_com_token_salvo(self) -> bool:
        """Tenta autenticar com o token salvo no .env. Retorna True se conseguiu."""
        token_salvo = os.getenv("VTS_TOKEN")
        if not token_salvo:
            return False
        try:
            self.vts.authentic_token = token_salvo
            await self.vts.request_authenticate()
            resp = await self.vts.request(self.vts.vts_request.requestHotKeyList())
            if "data" in resp:
                log.info("✅ VTube Studio autenticado via token salvo.")
                return True
        except Exception:
            pass
        return False

    async def _injetar_parametros(self, lista_params: list[dict]):
        """
        Envia InjectParameterDataValues para o VTube Studio.
        Usa os nomes de parâmetros VTS (ex: 'MouthSmile', 'EyeOpenLeft')
        que mapeiam para os parâmetros Live2D do modelo Hiyori.
        """
        param_values = [
            {"id": p["id"], "value": float(p["value"]), "weight": 1.0}
            for p in lista_params
        ]

        request_data = {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": f"AliceEmote{str(uuid.uuid4())[:8]}",
            "messageType": "InjectParameterDataValues",
            "data": {
                "faceFound": False,
                "mode": "set",
                "parameterValues": param_values,
            },
        }

        return await self.vts.request(request_data)

    async def ativar_expressao(self, nome_emocao: str):
        """
        Ativa a expressão correspondente à emoção detectada pelo EmotionEngine.
        Injeta os parâmetros VTS mapeados para o rosto do modelo Hiyori.
        Não lança exceção — erros de VTS não interrompem o fluxo principal.
        """
        if not self.conectado:
            return

        parametros = EXPRESSOES.get(nome_emocao, EXPRESSOES["Neutro"])

        try:
            await self._injetar_parametros(parametros)
            log.debug(f"🎭 [VTS] Expressão ativada: {nome_emocao}")
        except Exception as e:
            log.warning(f"⚠️  [VTS] Erro ao ativar expressão '{nome_emocao}': {e}")

    async def resetar_expressao(self):
        """Volta o avatar para a expressão Neutra após a fala terminar."""
        await self.ativar_expressao("Neutro")
