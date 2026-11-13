"""
ClickhouseLogger — Persistência centralizada da Alice no ClickHouse.

Tabelas gerenciadas:
  analytics.interactions — histórico completo de conversas (user + assistant)
  analytics.memoria      — fatos de longo prazo sobre o usuário
  analytics.logs         — eventos de WARNING+ para análise histórica
  analytics.estado_alice — histórico de mudanças de estado emocional

Design:
  - Conexão lazy com backoff em falhas: não trava o turno quando offline.
  - Fail-silently: sem ClickHouse ativo, Alice continua normalmente.
  - Queries 100% parametrizadas via `parameters={...}` (sem interpolação f-string).
  - Re-embedding em lote sem `ALTER TABLE UPDATE` unitário em loop.
"""

import asyncio
import os
import threading
import time
import uuid
from datetime import datetime
from cerebro.logger import log
from cerebro.embeddings import gerar_embedding

try:
    import clickhouse_connect

    _CLICKHOUSE_DISPONIVEL = True
except ImportError:
    _CLICKHOUSE_DISPONIVEL = False

# Distância de cosseno máxima aceita na busca semântica (0.0 = idêntico)
_MAX_COSINE_DISTANCE = 0.45

# Tempo mínimo (segundos) entre tentativas de reconexão quando o banco está offline
_BACKOFF_RECONEXAO_S = 30.0

# Caracteres de pontuação removidos das palavras na busca lexical
_PONTUACAO_BUSCA = ".,!?;:\"'()[]{}"


class ClickhouseLogger:
    """
    Centraliza toda a comunicação com o ClickHouse da Alice.

    Responsabilidades:
      - Registrar cada turno de conversa (interactions)
      - Salvar / buscar / apagar / sincronizar fatos do usuário (memoria)
      - Persistir logs de WARNING+ (logs)
      - Registrar snapshots de humor/amizade (estado_alice)
    """

    def __init__(self):
        self._client = None
        self._conectado = False
        self._ultima_falha_ts = 0.0
        self._session_id = str(uuid.uuid4())[:8]
        self._turn_id = 0
        # Serializa acesso ao client entre threads (executor + sink Loguru)
        self._lock = threading.Lock()
        self._tabela_estado_ok = False  # estado_alice criada sob demanda

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
            self._ultima_falha_ts = time.monotonic()
            log.warning(
                "📊 [ClickHouse] Biblioteca 'clickhouse-connect' não instalada. "
                "Execute: pip install clickhouse-connect"
            )
            return False

        try:
            with self._lock:
                self._client = clickhouse_connect.get_client(
                    host=self._host,
                    port=self._port,
                    username=self._user,
                    password=self._password,
                    database=self._database,
                )
                self._client.ping()
                self._conectado = True
                self._ultima_falha_ts = 0.0
            log.info("📊 [ClickHouse] ✅ Conexão estabelecida com sucesso!")
            self._migrar_tabela_memoria()
            self.reembeddar_fatos_legado()
            return True
        except Exception as e:
            self._conectado = False
            self._ultima_falha_ts = time.monotonic()
            log.warning(f"📊 [ClickHouse] ⚠️ Falha na conexão: {e}")
            return False

    def _marcar_desconectado(self):
        """Marca conexão como inativa e inicia janela de backoff."""
        self._conectado = False
        self._ultima_falha_ts = time.monotonic()

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
        """
        Re-embeda fatos sem embedding em lote único (INSERT + 1 DELETE parametrizado),
        eliminando mutações `ALTER TABLE UPDATE` em loop.
        """
        try:
            with self._lock:
                rows = self._client.query(
                    "SELECT id, fato, criado_em FROM memoria "
                    "WHERE length(embedding) = 0 LIMIT {limite:UInt32}",
                    parameters={"limite": 500},
                ).result_rows
        except Exception:
            return

        if not rows:
            return

        log.info(f"🔄 [Embed] Re-embeddando {len(rows)} fato(s) legado em lote...")
        lote_insert = []
        ids_atualizados = []
        for id_fato, fato, criado_em in rows:
            emb = gerar_embedding(fato)
            if not emb:
                continue
            lote_insert.append([str(id_fato), str(fato), emb, criado_em])
            ids_atualizados.append(str(id_fato))

        if not lote_insert:
            return

        try:
            with self._lock:
                self._client.insert(
                    "memoria",
                    lote_insert,
                    column_names=["id", "fato", "embedding", "criado_em"],
                )
                self._client.command(
                    "ALTER TABLE memoria DELETE "
                    "WHERE id IN {ids:Array(String)} AND length(embedding) = 0",
                    parameters={"ids": ids_atualizados},
                )
            log.info(
                f"✅ [Embed] Re-embedding em lote concluído: "
                f"{len(ids_atualizados)}/{len(rows)} fatos atualizados."
            )
        except Exception as e:
            log.warning(f"📊 [Embed] Erro ao atualizar lote de embeddings: {e}")

    def _garantir_conexao(self) -> bool:
        """Conecta de forma lazy na primeira chamada, respeitando backoff em falhas."""
        if self._conectado and self._client is not None:
            return True
        if (
            self._ultima_falha_ts > 0.0
            and (time.monotonic() - self._ultima_falha_ts) < _BACKOFF_RECONEXAO_S
        ):
            return False
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
            self._marcar_desconectado()

    # ── Memória de longo prazo ────────────────────────────────────────────────

    def registrar_estado_sync(self, estado: dict):
        """
        Guarda uma foto do estado da Alice (humor, amizade, conversas, nome)
        em analytics.estado_alice. Cada mudança vira uma linha, formando o histórico.
        """
        if not self._garantir_conexao():
            return
        try:
            with self._lock:
                if not self._tabela_estado_ok:
                    self._client.command(
                        "CREATE TABLE IF NOT EXISTS estado_alice ("
                        " humor String,"
                        " amizade_com_usuario UInt8,"
                        " total_conversas UInt32,"
                        " nome_usuario String,"
                        " ultima_interacao String,"
                        " session_id String,"
                        " criado_em DateTime DEFAULT now()"
                        ") ENGINE = MergeTree() ORDER BY criado_em"
                    )
                    self._tabela_estado_ok = True
                self._client.insert(
                    "estado_alice",
                    [
                        [
                            str(estado.get("humor", "")),
                            int(estado.get("amizade_com_usuario", 0)),
                            int(estado.get("total_conversas", 0)),
                            str(estado.get("nome_usuario", "")),
                            str(estado.get("ultima_interacao", ""))[:500],
                            self._session_id,
                            datetime.now(),
                        ]
                    ],
                    column_names=[
                        "humor",
                        "amizade_com_usuario",
                        "total_conversas",
                        "nome_usuario",
                        "ultima_interacao",
                        "session_id",
                        "criado_em",
                    ],
                )
        except Exception as e:
            log.debug(f"📊 [ClickHouse] Falha ao salvar estado: {e}")

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
            self._marcar_desconectado()

    def sincronizar_fatos_sqlite(self, fatos_sqlite: list[dict]) -> int:
        """
        Sincroniza fatos ativos do SQLite que ainda não constam em analytics.memoria.
        Retorna a quantidade de fatos inseridos no ClickHouse.
        """
        if not fatos_sqlite or not self._garantir_conexao():
            return 0

        try:
            with self._lock:
                rows = self._client.query(
                    "SELECT id, lowerUTF8(trimBoth(fato)) FROM memoria"
                ).result_rows
            ids_ch = {str(r[0]) for r in rows}
            textos_ch = {str(r[1]).rstrip(".!?;:") for r in rows}

            novos = []
            for item in fatos_sqlite:
                id_f = str(item["id"])
                fato_txt = str(item["fato"]).strip()
                norm_txt = " ".join(fato_txt.lower().split()).rstrip(".!?;:")
                if id_f in ids_ch or norm_txt in textos_ch:
                    continue
                emb = gerar_embedding(fato_txt)
                criado_str = str(item.get("criado_em", ""))
                try:
                    dt_val = datetime.fromisoformat(criado_str.replace(" ", "T"))
                except Exception:
                    dt_val = datetime.now()
                novos.append([id_f, fato_txt, emb, dt_val])
                ids_ch.add(id_f)
                textos_ch.add(norm_txt)

            if novos:
                with self._lock:
                    self._client.insert(
                        "memoria",
                        novos,
                        column_names=["id", "fato", "embedding", "criado_em"],
                    )
            return len(novos)
        except Exception as e:
            log.warning(f"📊 [ClickHouse] ⚠️ Erro ao sincronizar fatos do SQLite: {e}")
            return 0

    @staticmethod
    def _extrair_padroes_ilike(contexto: str) -> list[str]:
        """Extrai até 5 padrões `%palavra%` seguros para busca ILIKE parametrizada."""
        if not contexto:
            return []
        palavras = [
            p.strip(_PONTUACAO_BUSCA).replace("%", "").replace("_", "")
            for p in contexto.split()
        ]
        termos = [p for p in palavras if len(p) >= 4][:5]
        return [f"%{t}%" for t in termos]

    def buscar_fatos_relevantes(
        self,
        contexto: str = "",
        limite: int = 10,
        max_distancia: float = _MAX_COSINE_DISTANCE,
    ) -> list:
        """
        Retorna fatos relevantes ao contexto usando busca semântica parametrizada
        (`cosineDistance <= max_distancia`) com fallback `ilike` parametrizado.
        """
        if not self._garantir_conexao():
            return []

        limite_int = max(1, int(limite))
        padroes = self._extrair_padroes_ilike(contexto)

        try:
            embedding_query = gerar_embedding(contexto) if contexto else []

            if embedding_query:
                # ── Busca semântica parametrizada com corte de distância ──────
                query_sem = (
                    "SELECT fato, cosineDistance(embedding, {vec:Array(Float32)}) AS dist "
                    "FROM memoria "
                    "WHERE length(embedding) > 0 "
                    "AND cosineDistance(embedding, {vec:Array(Float32)}) <= {max_dist:Float64} "
                    "ORDER BY dist ASC "
                    "LIMIT {limite:UInt32}"
                )
                with self._lock:
                    rows_sem = self._client.query(
                        query_sem,
                        parameters={
                            "vec": embedding_query,
                            "max_dist": float(max_distancia),
                            "limite": limite_int,
                        },
                    ).result_rows
                fatos = [r[0] for r in rows_sem]

                # Complementa via ILIKE parametrizado (fatos sem vetor ou match lexical exato)
                if len(fatos) < limite_int and padroes:
                    restante = limite_int - len(fatos)
                    fatos_set = set(fatos)
                    query_lex = (
                        "SELECT fato FROM memoria "
                        "WHERE arrayExists(p -> ilike(fato, p), {padroes:Array(String)}) "
                        "ORDER BY criado_em DESC "
                        "LIMIT {limite:UInt32}"
                    )
                    with self._lock:
                        rows_lex = self._client.query(
                            query_lex,
                            parameters={"padroes": padroes, "limite": restante},
                        ).result_rows
                    for row in rows_lex:
                        if row[0] not in fatos_set:
                            fatos.append(row[0])
                            fatos_set.add(row[0])

                log.debug(
                    f"📊 [Embed] Busca semântica: {len(fatos)} fato(s) recuperado(s)"
                )
                return fatos

            # ── Fallback: ILIKE parametrizado ou mais recentes ───────────────
            if padroes and contexto:
                query_ilike = (
                    "SELECT fato FROM memoria "
                    "WHERE arrayExists(p -> ilike(fato, p), {padroes:Array(String)}) "
                    "ORDER BY criado_em DESC "
                    "LIMIT {limite:UInt32}"
                )
                with self._lock:
                    rows = self._client.query(
                        query_ilike,
                        parameters={"padroes": padroes, "limite": limite_int},
                    ).result_rows
                if rows:
                    return [row[0] for row in rows]

            query_rec = (
                "SELECT fato FROM memoria ORDER BY criado_em DESC LIMIT {limite:UInt32}"
            )
            with self._lock:
                rows = self._client.query(
                    query_rec, parameters={"limite": limite_int}
                ).result_rows
            return [row[0] for row in rows]

        except Exception as e:
            log.warning(f"📊 [ClickHouse] ⚠️ Erro ao buscar fatos: {e}")
            self._marcar_desconectado()
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
            self._marcar_desconectado()
            return []

    def remover_fato_mem(self, id_fato: str) -> bool:
        """Remove fisicamente um fato pelo UUID usando parâmetro tipado."""
        if not self._garantir_conexao():
            return False

        try:
            with self._lock:
                self._client.command(
                    "ALTER TABLE memoria DELETE WHERE id = {id_fato:String}",
                    parameters={"id_fato": str(id_fato)},
                )
            log.info(f"🗑️  [ClickHouse] Fato removido: {id_fato}")
            return True
        except Exception as e:
            log.warning(f"📊 [ClickHouse] ⚠️ Erro ao remover fato: {e}")
            self._marcar_desconectado()
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
            self._marcar_desconectado()
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
            self._marcar_desconectado()
