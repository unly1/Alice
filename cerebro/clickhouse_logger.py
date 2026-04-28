"""
ClickhouseLogger — Persistência centralizada da Alice no ClickHouse.

Tabelas gerenciadas:
  analytics.interactions — histórico completo de conversas (user + assistant)
  analytics.memoria      — fatos de longo prazo sobre o usuário
  analytics.logs         — eventos de WARNING+ para análise histórica

Design:
  - Conexão lazy: só conecta na primeira operação.
  - Fail-silently: sem ClickHouse ativo, Alice continua normalmente.
  - Métodos síncronos para memória/logs (chamados de contexto síncrono).
  - Métodos assíncronos para interações (chamados do loop principal).
"""

import asyncio
import os
import threading
import uuid
from datetime import datetime
from cerebro.logger import log
from cerebro.embeddings import gerar_embedding

try:
    import clickhouse_connect

    _CLICKHOUSE_DISPONIVEL = True
except ImportError:
    _CLICKHOUSE_DISPONIVEL = False


class ClickhouseLogger:
    """
    Centraliza toda a comunicação com o ClickHouse da Alice.

    Responsabilidades:
      - Registrar cada turno de conversa (interactions)
      - Salvar / buscar / apagar fatos do usuário (memoria)
      - Persistir logs de WARNING+ (logs)
    """

    def __init__(self):
        self._client = None
        self._conectado = False
        self._session_id = str(uuid.uuid4())[:8]
        self._turn_id = 0
        # Serializa acesso ao client entre threads (executor + sink Loguru)
        self._lock = threading.Lock()

        self._host = os.getenv("CLICKHOUSE_HOST", "localhost")
        self._port = int(os.getenv("CLICKHOUSE_HTTP_PORT", "8123"))
        self._user = os.getenv("CLICKHOUSE_USER_APP", "alice")
        self._password = os.getenv("CLICKHOUSE_PASSWORD_APP", "alice123")
        self._database = os.getenv("CLICKHOUSE_DB_APP", "analytics")

        log.info(
            f"📊 ClickhouseLogger inicializado | Sessão: {self._session_id} | "
            f"Host: {self._host}:{self._port}"
        )

    # ── Conexão ──────────────────────────────────────────────────────────────

    def _conectar(self) -> bool:
        """Tenta estabelecer conexão com o ClickHouse. Retorna True se sucesso."""
        if not _CLICKHOUSE_DISPONIVEL:
            log.warning(
                "📊 [ClickHouse] Biblioteca 'clickhouse-connect' não instalada. "
                "Execute: pip install clickhouse-connect"
            )
            return False

        try:
            self._client = clickhouse_connect.get_client(
                host=self._host,
                port=self._port,
                username=self._user,
                password=self._password,
                database=self._database,
            )
            self._client.ping()
            self._conectado = True
            log.info("📊 [ClickHouse] ✅ Conexão estabelecida com sucesso!")
            self._migrar_tabela_memoria()
            self.reembeddar_fatos_legado()
            return True
        except Exception as e:
            log.warning(f"📊 [ClickHouse] ⚠️ Falha na conexão: {e}")
            self._conectado = False
            return False

    def _migrar_tabela_memoria(self):
        """Adiciona coluna embedding à tabela memoria se ainda não existir."""
        try:
            with self._lock:
                self._client.command(
                    "ALTER TABLE memoria ADD COLUMN IF NOT EXISTS "
                    "embedding Array(Float32) DEFAULT []"
                )
            log.debug("📊 [ClickHouse] Coluna embedding verificada/criada em memoria.")
        except Exception as e:
            log.warning(f"📊 [ClickHouse] ⚠️ Migração da tabela memoria: {e}")

    def reembeddar_fatos_legado(self):
        """Inicia re-embedding de fatos existentes sem vetor (thread daemon)."""
        t = threading.Thread(target=self._reembeddar_sync, daemon=True, name="ReEmbed")
        t.start()

    def _reembeddar_sync(self):
        """Re-embeda fatos sem embedding. Roda em background na primeira conexão."""
        try:
            with self._lock:
                rows = self._client.query(
                    "SELECT id, fato FROM memoria WHERE length(embedding) = 0 LIMIT 500"
                ).result_rows
        except Exception:
            return

        if not rows:
            return

        log.info(f"🔄 [Embed] Re-embeddando {len(rows)} fato(s) legado...")
        atualizados = 0
        for id_fato, fato in rows:
            emb = gerar_embedding(fato)
            if not emb:
                continue
            vetor_str = "[" + ",".join(str(x) for x in emb) + "]"
            try:
                with self._lock:
                    self._client.command(
                        f"ALTER TABLE memoria UPDATE embedding = {vetor_str} "
                        f"WHERE id = '{id_fato}'"
                    )
                atualizados += 1
            except Exception as e:
                log.warning(f"📊 [Embed] Erro ao atualizar fato {id_fato}: {e}")

        log.info(
            f"✅ [Embed] Re-embedding concluído: {atualizados}/{len(rows)} fatos atualizados."
        )

    def _garantir_conexao(self) -> bool:
        """Conecta de forma lazy na primeira chamada."""
        if self._conectado and self._client:
            return True
        return self._conectar()

    # ── Interações (conversas) ────────────────────────────────────────────────

    async def registrar_turno(
        self,
        pergunta: str,
        resposta: str,
        humor: str = "",
        model: str = "",
        tokens_prompt: int = 0,
        tokens_resposta: int = 0,
    ):
        """
        Salva um par pergunta/resposta no ClickHouse de forma assíncrona.
        Registra duas linhas: uma para 'user' e uma para 'assistant'.

        Args:
            tokens_prompt:   Tokens do contexto enviado ao modelo (prompt_eval_count).
            tokens_resposta: Tokens gerados na resposta (eval_count).
        """
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            self._inserir_sync,
            pergunta,
            resposta,
            humor,
            model,
            tokens_prompt,
            tokens_resposta,
        )

    def _inserir_sync(
        self,
        pergunta: str,
        resposta: str,
        humor: str,
        model: str,
        tokens_prompt: int,
        tokens_resposta: int,
    ):
        """Executa o INSERT de interação de forma síncrona (chamado via executor)."""
        if not self._garantir_conexao():
            return

        self._turn_id += 1
        agora = datetime.now()
        tokens_total = tokens_prompt + tokens_resposta

        try:
            dados = [
                # Mensagem do usuário
                [
                    self._session_id,
                    self._turn_id,
                    "user",
                    pergunta,
                    humor,
                    model,
                    0,
                    0,
                    0,
                    agora,
                ],
                # Resposta da Alice
                [
                    self._session_id,
                    self._turn_id,
                    "assistant",
                    resposta,
                    humor,
                    model,
                    tokens_prompt,
                    tokens_resposta,
                    tokens_total,
                    agora,
                ],
            ]

            with self._lock:
                self._client.insert(
                    "interactions",
                    dados,
                    column_names=[
                        "session_id",
                        "turn_id",
                        "role",
                        "content",
                        "humor",
                        "model",
                        "tokens_prompt",
                        "tokens_resposta",
                        "tokens_total",
                        "created_at",
                    ],
                )
            log.debug(
                f"📊 [ClickHouse] Turno {self._turn_id} salvo | "
                f"Sessão: {self._session_id} | "
                f"Tokens: {tokens_prompt}↑ + {tokens_resposta}↓ = {tokens_total} total"
            )
        except Exception as e:
            log.warning(f"📊 [ClickHouse] ⚠️ Falha ao salvar turno: {e}")
            self._conectado = False

    # ── Memória de longo prazo ────────────────────────────────────────────────

    def registrar_fato_sync(self, fato: str, id_fato: str):
        """INSERT de um fato na tabela analytics.memoria com embedding semântico."""
        if not self._garantir_conexao():
            return

        embedding = gerar_embedding(fato)  # [] se nomic indisponível

        try:
            with self._lock:
                self._client.insert(
                    "memoria",
                    [[id_fato, fato, embedding, datetime.now()]],
                    column_names=["id", "fato", "embedding", "criado_em"],
                )
            log.debug(
                f"📊 [ClickHouse] Fato salvo {'com embedding' if embedding else 'sem embedding'}: {fato[:60]}"
            )
        except Exception as e:
            log.warning(f"📊 [ClickHouse] ⚠️ Erro ao salvar fato: {e}")
            self._conectado = False

    def buscar_fatos_relevantes(self, contexto: str = "", limite: int = 10) -> list:
        """
        Retorna fatos relevantes ao contexto usando busca semântica (cosineDistance).

        Estratégia:
          1) Gera embedding da query via nomic-embed-text.
          2) Fatos com embedding → ordenados por cosineDistance (busca semântica).
          3) Fatos legado sem embedding → complementados via ILIKE se necessário.
          4) Se nomic indisponível → ILIKE puro (comportamento anterior).
        """
        if not self._garantir_conexao():
            return []

        try:
            embedding_query = gerar_embedding(contexto) if contexto else []

            if embedding_query:
                # ── Busca semântica ───────────────────────────────────────────
                vetor_str = "[" + ",".join(str(x) for x in embedding_query) + "]"
                query_sem = (
                    f"SELECT fato FROM memoria "
                    f"WHERE length(embedding) > 0 "
                    f"ORDER BY cosineDistance(embedding, {vetor_str}) ASC "
                    f"LIMIT {limite}"
                )
                with self._lock:
                    fatos = [r[0] for r in self._client.query(query_sem).result_rows]

                # Complementa com fatos legado (sem embedding) via ILIKE se necessário
                if len(fatos) < limite:
                    restante = limite - len(fatos)
                    fatos_set = set(fatos)
                    palavras = [
                        p.strip(".,!?;:\"'").replace("'", "").replace("%", "")
                        for p in contexto.split()
                        if len(p) >= 4
                    ][:5]
                    if palavras:
                        conds = " OR ".join(f"fato ILIKE '%{p}%'" for p in palavras)
                        query_leg = (
                            f"SELECT fato FROM memoria "
                            f"WHERE length(embedding) = 0 AND ({conds}) "
                            f"ORDER BY criado_em DESC LIMIT {restante}"
                        )
                        with self._lock:
                            for row in self._client.query(query_leg).result_rows:
                                if row[0] not in fatos_set:
                                    fatos.append(row[0])

                log.debug(
                    f"📊 [Embed] Busca semântica: {len(fatos)} fato(s) recuperado(s)"
                )
                return fatos

            else:
                # ── Fallback: ILIKE (nomic indisponível ou contexto vazio) ────
                palavras = [
                    p.strip(".,!?;:\"'").replace("'", "").replace("%", "")
                    for p in contexto.split()
                    if len(p) >= 4
                ][:5]

                if palavras and contexto:
                    conds = " OR ".join(f"fato ILIKE '%{p}%'" for p in palavras)
                    query = (
                        f"SELECT fato FROM memoria WHERE ({conds}) "
                        f"ORDER BY criado_em DESC LIMIT {limite}"
                    )
                else:
                    query = (
                        f"SELECT fato FROM memoria "
                        f"ORDER BY criado_em DESC LIMIT {limite}"
                    )

                with self._lock:
                    rows = self._client.query(query).result_rows
                return [row[0] for row in rows]

        except Exception as e:
            log.warning(f"📊 [ClickHouse] ⚠️ Erro ao buscar fatos: {e}")
            self._conectado = False
            return []

    def buscar_todos_fatos_mem(self) -> list:
        """Retorna todos os fatos com id, fato e criado_em (para gerenciamento)."""
        if not self._garantir_conexao():
            return []

        try:
            with self._lock:
                rows = self._client.query(
                    "SELECT id, fato, criado_em FROM memoria ORDER BY criado_em ASC"
                ).result_rows
            return [
                {"id": str(row[0]), "fato": row[1], "criado_em": str(row[2])[:19]}
                for row in rows
            ]
        except Exception as e:
            log.warning(f"📊 [ClickHouse] ⚠️ Erro ao listar fatos: {e}")
            self._conectado = False
            return []

    def remover_fato_mem(self, id_fato: str) -> bool:
        """Remove fisicamente um fato pelo UUID (mutação assíncrona no ClickHouse)."""
        if not self._garantir_conexao():
            return False

        try:
            with self._lock:
                self._client.command(
                    f"ALTER TABLE memoria DELETE WHERE id = '{id_fato}'"
                )
            log.info(f"🗑️  [ClickHouse] Fato removido: {id_fato}")
            return True
        except Exception as e:
            log.warning(f"📊 [ClickHouse] ⚠️ Erro ao remover fato: {e}")
            self._conectado = False
            return False

    def limpar_memorias(self) -> int:
        """Apaga todos os fatos de analytics.memoria. Retorna a quantidade apagada."""
        if not self._garantir_conexao():
            return 0

        try:
            with self._lock:
                result = self._client.query("SELECT COUNT(*) FROM memoria")
                total = result.result_rows[0][0] if result.result_rows else 0
                self._client.command("TRUNCATE TABLE memoria")
            log.info(f"📊 [ClickHouse] {total} fatos apagados da memória.")
            return int(total)
        except Exception as e:
            log.warning(f"📊 [ClickHouse] ⚠️ Erro ao limpar memória: {e}")
            self._conectado = False
            return 0

    # ── Logs de eventos ───────────────────────────────────────────────────────

    def registrar_log_sync(self, nivel: str, modulo: str, mensagem: str):
        """
        INSERT de um evento de log na tabela analytics.logs.
        Chamado diretamente pelo sink do Loguru — deve ser rápido e nunca lançar.
        """
        if not self._garantir_conexao():
            return

        try:
            with self._lock:
                self._client.insert(
                    "logs",
                    [[nivel, modulo, mensagem[:500], self._session_id, datetime.now()]],
                    column_names=[
                        "nivel",
                        "modulo",
                        "mensagem",
                        "session_id",
                        "criado_em",
                    ],
                )
        except Exception:
            # Silencioso — não queremos log de log causando loop
            self._conectado = False
