from __future__ import annotations

import asyncio
import os
import time
import uuid
from dotenv import load_dotenv, set_key
import pyvts

from cerebro.logger import log

load_dotenv()

# Taxa de atualização de keep-alive e Lip Sync (~25 Hz = 40 ms; VTS expira parâmetros em 1s sem injeção)
_TAXA_KEEPALIVE_HZ = 25.0
_INTERVALO_KEEPALIVE_S = 1.0 / _TAXA_KEEPALIVE_HZ
_COOLDOWN_RECONEXAO_S = 5.0
_GANHO_LIPSYNC = 3.2  # amplificação de RMS [0..0.3+] para abertura natural de MouthOpen [0..1]

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

    Opção B — Injeção Direta de Parâmetros Live2D + Lip Sync Keep-Alive (~25 Hz):
    Injeta valores de parâmetros VTS diretamente (MouthSmile, EyeOpenLeft,
    BrowLeftY, MouthOpen) mantendo keep-alive contínuo e modulando `MouthOpen`
    em tempo real a partir do RMS do áudio do TTS.
    """

    def __init__(self):
        self._plugin_info = {
            "plugin_name": os.getenv("VTS_PLUGIN_NAME", "Alice VTuber"),
            "developer": os.getenv("VTS_DEVELOPER", "Mario Alexandre"),
            "authentication_token_path": "temp_pyvts_token.txt",
        }
        self.vts = pyvts.vts(plugin_info=self._plugin_info)
        self.conectado: bool = False
        self._emocao_atual: str = "Neutro"
        self._mouth_open: float = 0.0
        self._keepalive_task: asyncio.Task | None = None
        self._reconectando: bool = False
        self._ultima_tentativa_reconexao: float = 0.0
        self._fechado: bool = False
        self._ws_lock = asyncio.Lock()

    async def conectar(self) -> bool:
        """Conecta ao VTube Studio, autentica e inicia o loop de keep-alive/Lip Sync."""
        self._fechado = False
        log.info("🔌 Conectando ao VTube Studio na porta 8001...")
        try:
            async with self._ws_lock:
                await self.vts.connect()

                autenticado = await self._autenticar_com_token_salvo()

                if not autenticado:
                    log.warning(
                        "⏳ Token inválido ou ausente. Solicitando nova autorização..."
                    )
                    log.info("   → Clique em 'Allow' na janela do VTube Studio!")
                    self.vts.authentic_token = None
                    await self.vts.request_authenticate_token(force=True)
                    if not await self.vts.request_authenticate():
                        raise RuntimeError(
                            "Autorização negada ou token rejeitado pelo VTube Studio."
                        )

                    novo_token = self.vts.authentic_token
                    if not novo_token:
                        raise RuntimeError("Nenhum token recebido do VTube Studio.")
                    set_key(".env", "VTS_TOKEN", novo_token)
                    log.info("🔑 Novo token salvo automaticamente no .env!")

                self.conectado = True

            self._iniciar_keepalive()
            log.info(
                "✅ VTube Studio conectado! Expressões e Lip Sync (~25 Hz) ativados."
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
                try:
                    os.remove("temp_pyvts_token.txt")
                except OSError:
                    pass

    async def _autenticar_com_token_salvo(self) -> bool:
        """Tenta autenticar com o token salvo no .env. Retorna True se conseguiu."""
        token_salvo = os.getenv("VTS_TOKEN")
        if not token_salvo:
            return False
        try:
            self.vts.authentic_token = token_salvo
            if not await self.vts.request_authenticate():
                return False
            resp = await self.vts.request(self.vts.vts_request.requestHotKeyList())
            if (
                isinstance(resp, dict)
                and resp.get("messageType") != "APIError"
                and isinstance(resp.get("data"), dict)
                and "errorID" not in resp["data"]
                and "availableHotkeys" in resp["data"]
            ):
                log.info("✅ VTube Studio autenticado via token salvo.")
                return True
        except Exception:
            pass
        return False

    # ── Reconexão Segura e Keep-Alive / Lip Sync (~25 Hz) ────────────────────

    def _iniciar_keepalive(self) -> None:
        """Garante que a task assíncrona de keep-alive e Lip Sync (~25 Hz) está rodando."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = loop.create_task(self._loop_keepalive())

    async def _tentar_reconectar_seguro(self) -> bool:
        """Tenta reconectar ao VTube Studio sem bloquear o loop e apenas com token salvo."""
        if self._fechado or self._reconectando:
            return False
        agora = time.monotonic()
        if (agora - self._ultima_tentativa_reconexao) < _COOLDOWN_RECONEXAO_S:
            return False

        self._reconectando = True
        self._ultima_tentativa_reconexao = agora
        try:
            async with self._ws_lock:
                try:
                    await self.vts.close()
                except Exception:
                    pass
                self.vts = pyvts.vts(plugin_info=self._plugin_info)
                await asyncio.wait_for(self.vts.connect(), timeout=2.0)
                if await asyncio.wait_for(
                    self._autenticar_com_token_salvo(), timeout=2.0
                ):
                    self.conectado = True
                    log.info("🔌 [VTS] Reconectado ao VTube Studio com sucesso.")
                    return True
                try:
                    await self.vts.close()
                except Exception:
                    pass
                self.conectado = False
                return False
        except Exception as e:
            self.conectado = False
            log.debug(f"[VTS] Reconexão automática falhou: {e}")
            return False
        finally:
            self._reconectando = False

    def _montar_parametros_atuais(self) -> list[dict]:
        """Combina a expressão atual com a abertura bucal do Lip Sync (`MouthOpen`)."""
        base = EXPRESSOES.get(self._emocao_atual, EXPRESSOES["Neutro"])
        parametros: list[dict] = []
        for item in base:
            if item["id"] == "MouthOpen":
                valor_base = float(item["value"])
                # Usa o maior valor entre a abertura de expressão (ex: Surpresa) e o Lip Sync
                valor = (
                    max(valor_base, self._mouth_open)
                    if self._mouth_open > 0.01
                    else valor_base
                )
                parametros.append({"id": "MouthOpen", "value": round(valor, 3)})
            else:
                parametros.append({"id": item["id"], "value": float(item["value"])})
        return parametros

    async def _loop_keepalive(self) -> None:
        """Mantém os parâmetros injetados vivos (~25 Hz) e atualiza `MouthOpen` para Lip Sync."""
        while not self._fechado:
            await asyncio.sleep(_INTERVALO_KEEPALIVE_S)
            if self._fechado:
                break
            if not self.conectado:
                await self._tentar_reconectar_seguro()
                continue
            try:
                await self._injetar_parametros(self._montar_parametros_atuais())
            except Exception as e:
                log.warning(f"⚠️  [VTS] Conexão perdida no keep-alive: {e}")
                self.conectado = False

    def atualizar_lipsync_rms(self, rms: float) -> None:
        """
        Callback compatível com `TTSService.registrar_callback_rms`.
        Converte o RMS do áudio [0.0..1.0] em abertura suave de `MouthOpen` [0.0..1.0].
        """
        rms_limpo = max(0.0, float(rms))
        if rms_limpo < 0.005:
            alvo = 0.0
        else:
            alvo = min(1.0, rms_limpo * _GANHO_LIPSYNC)
        # Suavização exponencial rápida (ataque ágil, fechamento suave)
        alfa = 0.65 if alvo > self._mouth_open else 0.45
        self._mouth_open = (alfa * alvo) + ((1.0 - alfa) * self._mouth_open)
        if self._mouth_open < 0.01:
            self._mouth_open = 0.0

    async def definir_abertura_boca(self, abertura: float) -> None:
        """Define diretamente a abertura de `MouthOpen` [0.0..1.0] e injeta no VTS."""
        self._mouth_open = max(0.0, min(1.0, float(abertura)))
        if not self.conectado:
            await self._tentar_reconectar_seguro()
            if not self.conectado:
                return
        try:
            await self._injetar_parametros(self._montar_parametros_atuais())
        except Exception as e:
            log.warning(f"⚠️  [VTS] Erro ao atualizar MouthOpen: {e}")
            self.conectado = False

    # ── Injeção de Parâmetros e Expressões ────────────────────────────────────

    async def _injetar_parametros(self, lista_params: list[dict]):
        """
        Envia InjectParameterDataValues para o VTube Studio.
        Usa os nomes de parâmetros VTS (ex: 'MouthSmile', 'EyeOpenLeft', 'MouthOpen')
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

        async with self._ws_lock:
            return await self.vts.request(request_data)

    async def ativar_expressao(self, nome_emocao: str):
        """
        Ativa a expressão correspondente à emoção detectada pelo EmotionEngine.
        Em caso de falha de envio WebSocket, reseta `self.conectado = False` e tenta
        reconexão segura sem travar o loop.
        """
        self._emocao_atual = (
            nome_emocao if nome_emocao in EXPRESSOES else "Neutro"
        )

        if not self.conectado:
            if not await self._tentar_reconectar_seguro():
                return

        self._iniciar_keepalive()

        try:
            await self._injetar_parametros(self._montar_parametros_atuais())
            log.debug(f"🎭 [VTS] Expressão ativada: {self._emocao_atual}")
        except Exception as e:
            self.conectado = False
            log.warning(
                f"⚠️  [VTS] Erro ao ativar expressão '{nome_emocao}' (desconectado): {e}"
            )
            # Dispara tentativa de reconexão em background sem bloquear o turno
            try:
                asyncio.get_running_loop().create_task(
                    self._tentar_reconectar_seguro()
                )
            except RuntimeError:
                pass

    async def resetar_expressao(self):
        """Volta o avatar para a expressão Neutra e fecha a boca após a fala terminar."""
        self._mouth_open = 0.0
        await self.ativar_expressao("Neutro")

    async def fechar(self) -> None:
        """Cancela o loop de keep-alive, reseta `MouthOpen` e fecha a conexão WebSocket."""
        self._fechado = True
        self._mouth_open = 0.0
        if self._keepalive_task is not None and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
        self._keepalive_task = None

        if self.conectado:
            try:
                await self._injetar_parametros(EXPRESSOES["Neutro"])
            except Exception:
                pass

        self.conectado = False
        async with self._ws_lock:
            try:
                await self.vts.close()
            except Exception:
                pass