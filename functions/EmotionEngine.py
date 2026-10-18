import json
import os
import re
import sqlite3
import threading
from cerebro.logger import log

# Estado persistente da Alice: tabela `estado` no mesmo SQLite da memória.
CAMINHO_DB = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "cerebro", "memoria.db"
)
# Arquivo antigo: lido uma única vez para migrar e depois renomeado para .migrado
CAMINHO_BRAIN = os.path.join(os.path.dirname(__file__), "brain.json")

# Estado padrão caso ainda não exista nada salvo.
ESTADO_PADRAO = {
    "humor": "Neutro",
    "amizade_com_usuario": 50,
    "ultima_interacao": "",
    "total_conversas": 0,
    "nome_usuario": "",
}


class EmotionEngine:
    """
    Motor emocional da Alice.

    Responsável por:
    - Ler e persistir o estado emocional no banco (SQLite, tabela `estado`)
    - Registrar o histórico do estado no ClickHouse (analytics.estado_alice), se conectado
    - Analisar as respostas da IA e detectar emoções via palavras-chave (Tagger)
    - Construir contexto emocional para injetar no system prompt
    - Incrementar o nível de amizade com o usuário ao longo das interações
    """

    def __init__(self):
        self._ch = None  # ClickhouseLogger, ligado depois via conectar_clickhouse()
        self._criar_tabela()
        self._migrar_brain_json()
        self.estado_atual = self.ler_estado()

    # ── Banco ─────────────────────────────────────────────────────────────────

    def _criar_tabela(self):
        """Cria a tabela chave/valor do estado se não existir."""
        with sqlite3.connect(CAMINHO_DB) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS estado ("
                " chave TEXT PRIMARY KEY,"
                " valor TEXT NOT NULL,"
                " atualizado_em TEXT DEFAULT CURRENT_TIMESTAMP)"
            )

    def _migrar_brain_json(self):
        """Na primeira execução, copia o brain.json para o banco e renomeia o arquivo."""
        if not os.path.exists(CAMINHO_BRAIN):
            return
        with sqlite3.connect(CAMINHO_DB) as conn:
            ja_tem = conn.execute("SELECT COUNT(*) FROM estado").fetchone()[0]
        if ja_tem:
            return
        try:
            with open(CAMINHO_BRAIN, "r", encoding="utf-8") as f:
                dados = json.load(f)
            self._gravar(dados)
            os.replace(CAMINHO_BRAIN, CAMINHO_BRAIN + ".migrado")
            log.info(
                "🧠 brain.json migrado para o banco (cerebro/memoria.db, tabela estado).")
        except Exception as e:
            log.warning(f"🧠 Falha ao migrar brain.json para o banco: {e}")

    def _gravar(self, estado: dict):
        """Grava todas as chaves do estado no SQLite."""
        with sqlite3.connect(CAMINHO_DB) as conn:
            conn.executemany(
                "INSERT INTO estado (chave, valor, atualizado_em)"
                " VALUES (?, ?, CURRENT_TIMESTAMP)"
                " ON CONFLICT(chave) DO UPDATE SET"
                " valor = excluded.valor, atualizado_em = excluded.atualizado_em",
                [(k, json.dumps(v, ensure_ascii=False))
                 for k, v in estado.items()],
            )

    def conectar_clickhouse(self, ch):
        """Liga o ClickHouse para guardar o histórico de cada mudança de estado."""
        self._ch = ch

    def ler_estado(self) -> dict:
        """Carrega o estado do banco ou retorna o estado padrão se não existir."""
        dados = dict(ESTADO_PADRAO)
        try:
            with sqlite3.connect(CAMINHO_DB) as conn:
                for chave, valor in conn.execute("SELECT chave, valor FROM estado"):
                    dados[chave] = json.loads(valor)
        except Exception as e:
            log.warning(f"🧠 Falha ao ler estado do banco: {e}")
        return dados

    def salvar(self):
        """Persiste o estado atual no SQLite e manda uma cópia para o ClickHouse."""
        try:
            self._gravar(self.estado_atual)
        except Exception as e:
            log.warning(f"🧠 Falha ao salvar estado no banco: {e}")
        if self._ch is not None:
            # Em thread para não atrasar o turno; falhas são silenciosas no logger
            threading.Thread(
                target=self._ch.registrar_estado_sync,
                args=(dict(self.estado_atual),),
                daemon=True,
            ).start()

    def atualizar_estado(self, chave: str, valor):
        """Atualiza um campo do estado e salva imediatamente."""
        self.estado_atual[chave] = valor
        self.salvar()

    def registrar_turno(self, resumo_interacao: str):
        """
        Chamado ao fim de cada troca de conversa.
        Atualiza a última interação, incrementa o total de conversas
        e ajusta o nível de amizade sutilmente (+1 por turno, máx 100).
        """
        self.estado_atual["ultima_interacao"] = resumo_interacao
        self.estado_atual["total_conversas"] = (
            self.estado_atual.get("total_conversas", 0) + 1
        )
        amizade_atual = self.estado_atual.get("amizade_com_usuario", 50)
        self.estado_atual["amizade_com_usuario"] = min(100, amizade_atual + 1)
        self.salvar()

    def analisar_tag_emocao(self, texto_resposta_ia: str) -> str:
        """
        Sistema de 'Tagger' — detecta a emoção dominante no texto da IA.
        Atualiza o humor interno e retorna a tag de expressão para o VTube Studio.
        """
        texto_lower = texto_resposta_ia.lower()

        # Mapeamento expandido para as novas personalidades v1.2 (Tagger)
        if re.search(
            r"\b(meu deus|incrível|não acredito|pulando|conseguiu|uau|eita|nossa|uhuuu|yay|venceu)\b",
            texto_lower,
        ):
            emocao = "Surpresa"  # Euforia / Surpresa
        elif re.search(
            r"\b(sem jeito|bobinha|bochechas|vergonha|shy-mode|bobona|bobinha|ai para|elogio)\b",
            texto_lower,
        ):
            # Timidez (na Alice Hiyori, MouthSmile inclui blush rosado)
            emocao = "Riso"
        elif re.search(
            r"\b(foco|conseguir|mãos à obra|potencial|meta|organizar|realização|desafios)\b",
            texto_lower,
        ):
            emocao = "Neutro"  # Determinação
        elif re.search(
            r"\b(grata|presente|obrigada|carinho|conexão|quecinho|parceira|trocar energia)\b",
            texto_lower,
        ):
            emocao = "Riso"  # Gratidão / Afeto
        elif re.search(
            r"\b(ei\.\.\.|respirar|passando por|silêncio|desabafar|estou aqui|triste|poxa|lamento|sozinho|descansar)\b",
            texto_lower,
        ):
            emocao = "Tristeza"  # Empatia / Solidariedade
        elif re.search(
            r"\b(nostalgia|lembrança|reflexão|passado|suave|memória|olhar para trás|sentimentos profundos)\b",
            texto_lower,
        ):
            emocao = "Neutro"  # Nostalgia
        elif re.search(
            r"\b(curiosidade|curiosa|fascinada|aprender|explica|conta|descobrir|olhos brilhando)\b",
            texto_lower,
        ):
            # Curiosidade (expressão de interesse/surpresa)
            emocao = "Surpresa"
        elif re.search(
            r"\b(hahaha|kkk|rsrs|engraçado|risos|que delícia|adoro|amo)\b", texto_lower
        ):
            emocao = "Riso"  # Riso padrão
        elif re.search(
            r"\b(irritada|brava|grr|ugh|hmph|que raiva|não aguento)\b", texto_lower
        ):
            emocao = "Irritada"  # Raiva padrão
        else:
            emocao = "Neutro"

        # Atualiza o humor salvo no banco com a emoção detectada
        self.estado_atual["humor"] = emocao
        self.salvar()

        log.debug(
            f"🎨 [Emoção] {emocao} | Amizade: {self.estado_atual['amizade_com_usuario']}/100"
        )
        return emocao

    def construir_prompt_contexto(self) -> str:
        """
        Gera um bloco de contexto emocional para injetar no system prompt da Alice.
        Faz ela responder de forma consistente com seu estado interno persistido.
        """
        humor = self.estado_atual.get("humor", "Neutro")
        amizade = self.estado_atual.get("amizade_com_usuario", 50)
        total = self.estado_atual.get("total_conversas", 0)
        nome = self.estado_atual.get("nome_usuario", "")

        nome_str = f" Você sabe que o nome do usuário é '{nome}'." if nome else ""
        amizade_str = (
            "muito próxima"
            if amizade > 75
            else ("amigável" if amizade > 40 else "ainda conhecendo")
        )

        return (
            f"\n[Estado Interno da Alice]\n"
            f"- Humor atual: {humor}\n"
            f"- Relação com o usuário: {amizade_str} (nível {amizade}/100)\n"
            f"- Total de interações já tivemos: {total}.{nome_str}\n"
            f"Deixe esse estado guiar sutilmente o tom das suas respostas.\n"
        )
