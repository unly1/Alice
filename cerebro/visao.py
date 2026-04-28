"""
cerebro/visao.py — Visão Computacional da Alice

Captura screenshots da tela e descreve o que está acontecendo usando
um modelo de visão (moondream, llava, qwen2-vl) via Ollama.

Se nenhum modelo de visão estiver disponível, tenta OCR com pytesseract
como fallback para extrair texto visível na tela.
"""

import time
from io import BytesIO
from cerebro.logger import log

import mss
import mss.tools
import ollama
from PIL import Image

# Modelos de visão suportados (em ordem de preferência — mais capaz primeiro)
MODELOS_VISAO = [
    "gemma4:e4b",  # ← prioridade máxima (gemma4:e4b — multimodal, leve e capaz)
    "llava:7b",
    "llava",
    "llava:13b",
    "qwen2-vl",
    "minicpm-v",
    "bakllava",
    "moondream",  # fallback leve (1.8b, limitado com terminais escuros)
    "moondream2",
]

# Resolução do screenshot para análise — MENOR = melhor para modelos leves como moondream
# 1280x720 causava resposta vazia; 800x450 funciona corretamente.
LARGURA_ANALISE = 800
ALTURA_ANALISE = 450

# Intervalo mínimo entre capturas (segundos)
INTERVALO_CAPTURA = 30


class VisaoComputacional:
    """
    Gerencia a visão computacional da Alice.

    Uso:
        visao = VisaoComputacional()
        descricao = await visao.descrever_tela()
        # → "O usuário está editando código Python em um editor..."
    """

    def __init__(self):
        self.modelo_visao: str | None = self._detectar_modelo_visao()
        self.ultima_captura: float = 0.0
        self.ultima_descricao: str = ""
        self._descricao_anterior: str = ""  # usada para detectar mudança relevante

        if self.modelo_visao:
            log.info(f"👁️  Visão ativa | Modelo: {self.modelo_visao}")
        else:
            log.warning(
                "👁️  Visão: nenhum modelo encontrado. Instale com: ollama pull gemma3:4b"
            )

    # ─── Detecção de modelo ───────────────────────────────────────────────────

    def _detectar_modelo_visao(self) -> str | None:
        """Verifica quais modelos de visão estão instalados localmente."""
        try:
            resposta = ollama.list()

            # Suporta a API nova (objeto com .models) e a antiga (dict["models"])
            if hasattr(resposta, "models"):
                nomes = [
                    getattr(m, "model", None) or getattr(m, "name", "")
                    for m in resposta.models
                ]
            else:
                nomes = [
                    m.get("name", "") or m.get("model", "")
                    for m in resposta.get("models", [])
                ]

            for candidato in MODELOS_VISAO:
                base = candidato.split(":")[0]
                for nome in nomes:
                    if nome.split(":")[0] == base:
                        # retorna o nome completo (ex: "moondream:1.8b")
                        return nome
        except Exception as e:
            log.warning(f"⚠️  [Visão] Erro ao listar modelos: {e}")
        return None

    # ─── Captura de tela ──────────────────────────────────────────────────────

    def _capturar_screenshot(self) -> tuple[Image.Image, bytes]:
        """Captura a tela principal e retorna (PIL Image, bytes PNG)."""
        with mss.mss() as sct:
            monitor = sct.monitors[1]  # monitor principal
            screenshot = sct.grab(monitor)

        img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
        img = img.resize((LARGURA_ANALISE, ALTURA_ANALISE), Image.LANCZOS)

        buf = BytesIO()
        # sem optimize=True — causa respostas corrompidas no moondream
        img.save(buf, format="PNG")
        return img, buf.getvalue()

    # ─── Descrição com modelo de visão ───────────────────────────────────────

    async def descrever_tela(self, forcar: bool = False) -> str:
        """
        Captura e descreve o que está acontecendo na tela.

        Args:
            forcar: ignora o intervalo mínimo e força nova captura.

        Returns:
            Descrição textual da tela em português.
        """
        agora = time.time()
        if not forcar and (agora - self.ultima_captura) < INTERVALO_CAPTURA:
            return self.ultima_descricao  # retorna cache

        try:
            _, img_bytes = self._capturar_screenshot()
        except Exception as e:
            return f"[Visão: erro ao capturar tela — {e}]"

        if self.modelo_visao:
            descricao = await self._descrever_com_llm(img_bytes)
        else:
            descricao = await self._descrever_sem_llm(img_bytes)

        self.ultima_captura = agora
        self._descricao_anterior = self.ultima_descricao
        self.ultima_descricao = descricao
        return descricao

    async def _descrever_com_llm(self, img_bytes: bytes) -> str:
        """
        Usa ollama.generate() com o modelo de visão para descrever a tela.
        Executado em thread para não bloquear o event loop asyncio.
        """
        import asyncio

        def _chamar_ollama():
            return ollama.generate(
                model=self.modelo_visao,
                prompt=(
                    "Descreva em português o que você vê nesta captura de tela em 1 a 2 frases. "
                    "Foque no que o usuário está fazendo: qual programa, arquivo, site ou atividade está visível."
                ),
                images=[img_bytes],
                options={"temperature": 0.1},
            )

        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(None, _chamar_ollama)

            # ollama.generate() retorna objeto com .response
            if hasattr(response, "response"):
                descricao = response.response.strip()
            else:
                descricao = str(response).strip()

            if descricao:
                log.info(
                    f"👁️  [Visão] {descricao[:120]}{'...' if len(descricao) > 120 else ''}"
                )
            else:
                log.warning("👁️  [Visão] Modelo retornou resposta vazia.")

            return descricao
        except Exception as e:
            log.error(f"⚠️  [Visão] Erro ao analisar imagem: {e}")
            return ""

    async def _descrever_sem_llm(self, img_bytes: bytes) -> str:
        """Fallback sem modelo de visão: tenta extrair texto com OCR."""
        try:
            import pytesseract

            img = Image.open(BytesIO(img_bytes))
            texto = pytesseract.image_to_string(img, lang="por+eng").strip()
            if texto:
                # Trunca para não sobrecarregar o contexto
                return f"[Texto na tela]: {texto[:300]}"
        except ImportError:
            pass
        return "[Visão: nenhum modelo de visão disponível. Instale com: ollama pull moondream]"

    # ─── Bloco de contexto para injeção no prompt ────────────────────────────

    async def construir_bloco_visao(self) -> str:
        """Retorna um bloco de contexto formatado para o system prompt da Alice."""
        if not self.ultima_descricao:
            return ""
        secs = int(time.time() - self.ultima_captura)
        return (
            f"\n--- O QUE ALICE ESTÁ VENDO NA TELA ({secs}s atrás) ---\n"
            f"{self.ultima_descricao}\n"
            "(Use essa informação para contextualizar suas respostas quando relevante.)\n"
            "---"
        )

    def houve_mudanca_relevante(self, limiar: float = 0.5) -> bool:
        """
        Compara a descrição atual com a anterior usando similaridade de Jaccard
        entre conjuntos de palavras.

        Retorna True se a tela mudou significativamente (similaridade < limiar).
        Retorna False se não há descrição anterior ou se ambas são muito curtas.
        """
        ant = self._descricao_anterior.strip()
        cur = self.ultima_descricao.strip()

        if not ant or not cur or ant == cur:
            return False

        palavras_ant = set(ant.lower().split())
        palavras_cur = set(cur.lower().split())

        if not palavras_ant or not palavras_cur:
            return False

        intersecao = palavras_ant & palavras_cur
        uniao = palavras_ant | palavras_cur
        jaccard = len(intersecao) / len(uniao)

        mudou = jaccard < limiar
        if mudou:
            log.debug(f"👁️  [Heartbeat] Mudança detectada (Jaccard={jaccard:.2f})")
        return mudou

    @property
    def ativa(self) -> bool:
        return self.modelo_visao is not None
