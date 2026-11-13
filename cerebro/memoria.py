"""
cerebro/memoria.py — Memória de longo prazo da Alice.

Fonte primária:  ClickHouse (analytics.memoria)
Backup silencioso: SQLite (cerebro/memoria.db)

A interface pública é idêntica à versão anterior — nenhum outro arquivo
precisa ser alterado para usar esta classe.

Melhorias de integridade e concorrência:
  - `_usar_ch()` aciona `_garantir_conexao()` (com backoff em falha) e sincroniza
    fatos pendentes do SQLite para o ClickHouse ao conectar.
  - Deduplicação por UUIDv5 determinístico sobre o texto normalizado + checagem
    de conteúdo ativo para bloquear duplicatas e marcadores sentinela.
  - SQLite configurado com `WAL`, `synchronous=NORMAL`, `busy_timeout=5000`,
    `threading.Lock` e índice `idx_fatos_ativo`.
"""

import datetime
import os
import sqlite3
import threading
import uuid
from cerebro.logger import log

# SQLite de backup — sempre criado, funciona mesmo sem Docker
CAMINHO_DB = os.path.join(os.path.dirname(__file__), "memoria.db")

_PONTUACAO_BUSCA = ".,!?;:\"'()"

# Marcadores sentinela de ausência de informação gerados pelo extrator LLM
_TERMOS_SENTINELA = (
    "não encontrado",
    "não encontrada",
    "nao encontrado",
    "nao encontrada",
    "não fornecido",
    "não fornecida",
    "nao fornecido",
    "nao fornecida",
    "não mencionado",
    "não mencionada",
    "não mencionados",
    "não mencionadas",
    "nao mencionado",
    "nao mencionada",
    "não forneceu",
    "nao forneceu",
)


def _normalizar_fato(fato: str) -> str:
    """Normaliza o texto de um fato (minúsculas, espaços simples e sem pontuação final)."""
    limpo = " ".join(fato.strip().lower().split())
    return limpo.rstrip(".!?;:")


def _eh_fato_sentinela(fato: str) -> bool:
    """Retorna True se o texto for vazio, curto demais ou contiver ruído sentinela."""
    norm = _normalizar_fato(fato)
    if not norm or len(norm) < 6:
        return True
    return any(termo in norm for termo in _TERMOS_SENTINELA)


def _gerar_id_fato(fato: str) -> str:
    """Gera UUIDv5 determinístico a partir do texto normalizado do fato."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, _normalizar_fato(fato)))


class MemoriaLongaPrazo:
    """
    Memória de longo prazo com dupla persistência (ClickHouse + fallback SQLite).

    Args:
        ch: instância de ClickhouseLogger (opcional).
            Se None ou desconectado, opera apenas em modo SQLite.
    """

    def __init__(self, ch=None):
        self._ch = ch
        self._lock = threading.Lock()
        self._ch_sincronizado = False
        self._ultimo_contexto: str = ""  # última pergunta do usuário (para busca)
        self._criar_tabela_sqlite()

        fonte = "ClickHouse" if self._usar_ch() else "SQLite (fallback)"
        total = len(self.buscar_todos_fatos())
        log.info(
            f"💾 Memória carregada | {total} fatos sobre o usuário | Fonte: {fonte}"
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _conectar_sqlite(self) -> sqlite3.Connection:
        """Abre conexão SQLite com WAL, synchronous=NORMAL e busy_timeout=5000ms."""
        conn = sqlite3.connect(CAMINHO_DB, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _usar_ch(self) -> bool:
        """
        True se ClickHouse está disponível e conectado.
        Aciona `_garantir_conexao()` (com backoff em falha) e sincroniza fatos
        pendentes do SQLite para o ClickHouse ao estabelecer conexão.
        """
        if self._ch is None:
            return False

        if hasattr(self._ch, "_garantir_conexao"):
            conectado = bool(self._ch._garantir_conexao())
        else:
            conectado = bool(getattr(self._ch, "_conectado", False))

        if conectado and not self._ch_sincronizado:
            self._sincronizar_sqlite_para_ch()
        elif not conectado:
            self._ch_sincronizado = False

        return conectado

    def _sincronizar_sqlite_para_ch(self):
        """Sincroniza fatos ativos do SQLite que ainda não estão no ClickHouse."""
        self._ch_sincronizado = True
        if not hasattr(self._ch, "sincronizar_fatos_sqlite"):
            return
        try:
            fatos_sqlite = self._sqlite_buscar_todos()
            if fatos_sqlite:
                inseridos = self._ch.sincronizar_fatos_sqlite(fatos_sqlite)
                if inseridos > 0:
                    log.info(
                        f"💾 [Sync] {inseridos} fato(s) pendente(s) sincronizado(s) "
                        f"do SQLite para o ClickHouse."
                    )
        except Exception as e:
            log.warning(f"💾 [Sync] Erro ao sincronizar SQLite → ClickHouse: {e}")

    # ── SQLite (backup) ───────────────────────────────────────────────────────

    def _criar_tabela_sqlite(self):
        """Cria a tabela de backup e índice no SQLite se não existirem."""
        with self._lock:
            with self._conectar_sqlite() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS fatos (
                        id        TEXT PRIMARY KEY,
                        fato      TEXT NOT NULL,
                        ativo     INTEGER DEFAULT 1,
                        criado_em TEXT NOT NULL
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_fatos_ativo "
                    "ON fatos(ativo, criado_em)"
                )
                conn.commit()

    def _sqlite_ja_existe_ativo(self, id_fato: str, fato_norm: str) -> bool:
        """Verifica se o ID ou o texto normalizado já existe ativo no SQLite."""
        try:
            with self._lock:
                with self._conectar_sqlite() as conn:
                    row = conn.execute(
                        "SELECT 1 FROM fatos WHERE ativo=1 AND id=? LIMIT 1",
                        (id_fato,),
                    ).fetchone()
                    if row:
                        return True
                    rows = conn.execute(
                        "SELECT fato FROM fatos WHERE ativo=1"
                    ).fetchall()
            return any(_normalizar_fato(r[0]) == fato_norm for r in rows)
        except Exception:
            return False

    def _sqlite_salvar(self, fato: str, id_fato: str) -> bool:
        """Salva ou reativa fato no SQLite. Retorna True se inseriu/reativou."""
        agora = datetime.datetime.now().isoformat()
        try:
            with self._lock:
                with self._conectar_sqlite() as conn:
                    conn.execute(
                        "INSERT INTO fatos (id, fato, ativo, criado_em) "
                        "VALUES (?, ?, 1, ?) "
                        "ON CONFLICT(id) DO UPDATE SET "
                        "fato = excluded.fato, ativo = 1, criado_em = excluded.criado_em",
                        (id_fato, fato, agora),
                    )
                    conn.commit()
            return True
        except Exception as e:
            log.warning(f"💾 [SQLite] Erro ao salvar fato: {e}")
            return False

    def _sqlite_buscar_relevantes(self, limite: int, contexto: str) -> list:
        try:
            with self._lock:
                with self._conectar_sqlite() as conn:
                    if contexto:
                        palavras = [
                            f"%{p.strip(_PONTUACAO_BUSCA)}%"
                            for p in contexto.split()
                            if len(p.strip(_PONTUACAO_BUSCA)) >= 4
                        ][:5]
                        if palavras:
                            conds = " OR ".join(["fato LIKE ?"] * len(palavras))
                            rows = conn.execute(
                                f"SELECT fato FROM fatos WHERE ativo=1 AND ({conds}) "
                                f"ORDER BY rowid DESC LIMIT ?",
                                (*palavras, limite),
                            ).fetchall()
                            if rows:
                                return [r[0] for r in rows]
                    rows = conn.execute(
                        "SELECT fato FROM fatos WHERE ativo=1 ORDER BY rowid DESC LIMIT ?",
                        (limite,),
                    ).fetchall()
                    return [r[0] for r in rows]
        except Exception:
            return []

    def _sqlite_buscar_todos(self) -> list:
        try:
            with self._lock:
                with self._conectar_sqlite() as conn:
                    rows = conn.execute(
                        "SELECT id, fato, criado_em FROM fatos "
                        "WHERE ativo=1 ORDER BY rowid ASC"
                    ).fetchall()
                    return [
                        {"id": r[0], "fato": r[1], "criado_em": r[2]} for r in rows
                    ]
        except Exception:
            return []

    def _sqlite_remover(self, id_fato: str) -> bool:
        try:
            with self._lock:
                with self._conectar_sqlite() as conn:
                    cur = conn.execute(
                        "UPDATE fatos SET ativo=0 WHERE id=?", (id_fato,)
                    )
                    conn.commit()
                    return cur.rowcount > 0
        except Exception:
            return False

    def _sqlite_limpar(self) -> int:
        try:
            with self._lock:
                with self._conectar_sqlite() as conn:
                    total = conn.execute(
                        "SELECT COUNT(*) FROM fatos WHERE ativo=1"
                    ).fetchone()[0]
                    conn.execute("UPDATE fatos SET ativo=0 WHERE ativo=1")
                    conn.commit()
                    return total
        except Exception:
            return 0

    # ── Interface pública ─────────────────────────────────────────────────────

    def salvar_fato(self, fato: str):
        """
        Salva um fato sobre o usuário com deduplicação determinística (UUIDv5)
        e bloqueio de marcadores sentinela.
        Grava no ClickHouse (se disponível) E no SQLite (sempre).
        """
        if not fato:
            return
        fato = fato.strip()
        if _eh_fato_sentinela(fato):
            log.debug(f"💾 Fato ignorado (curto ou sentinela): {fato}")
            return

        id_fato = _gerar_id_fato(fato)
        fato_norm = _normalizar_fato(fato)

        if self._sqlite_ja_existe_ativo(id_fato, fato_norm):
            log.debug(f"💾 Fato duplicado ignorado: {fato}")
            return

        # ClickHouse (principal)
        if self._usar_ch():
            try:
                self._ch.registrar_fato_sync(fato, id_fato)
            except Exception as e:
                log.warning(f"💾 [CH] Erro ao salvar fato: {e}")

        # SQLite (backup — sempre salva)
        self._sqlite_salvar(fato, id_fato)
        log.debug(f"💾 Fato salvo: {fato}")

    def buscar_fatos_recentes(self, limite: int = 10) -> list:
        """
        Retorna fatos relevantes ao último contexto disponível.
        Usa ClickHouse se disponível, cai no SQLite caso contrário.
        """
        if self._usar_ch():
            try:
                return self._ch.buscar_fatos_relevantes(
                    self._ultimo_contexto, limite
                )
            except Exception:
                pass  # fallback silencioso
        return self._sqlite_buscar_relevantes(limite, self._ultimo_contexto)

    def buscar_todos_fatos(self) -> list:
        """Todos os fatos ativos com id, fato e criado_em (para gerenciamento)."""
        if self._usar_ch():
            try:
                return self._ch.buscar_todos_fatos_mem()
            except Exception:
                pass
        return self._sqlite_buscar_todos()

    def remover_fato(self, id_fato) -> bool:
        """Remove um fato pelo ID. Opera no ClickHouse e no SQLite."""
        id_str = str(id_fato)
        removido = False

        if self._usar_ch():
            try:
                removido = self._ch.remover_fato_mem(id_str)
            except Exception:
                pass

        # Remove também do SQLite (pode existir lá como backup)
        removido_sq = self._sqlite_remover(id_str)
        removido = removido or removido_sq

        if removido:
            log.info(f"🗑️  Fato removido: {id_str}")
        return removido

    def limpar_memoria(self) -> int:
        """Apaga todos os fatos. Opera no ClickHouse e no SQLite."""
        total = 0

        if self._usar_ch():
            try:
                total = self._ch.limpar_memorias()
            except Exception:
                pass

        sqlite_total = self._sqlite_limpar()
        if total == 0:
            total = sqlite_total

        log.warning(f"🗑️  Memória limpa — {total} fatos apagados.")
        return total

    def construir_bloco_memoria(self, contexto: str = "") -> str:
        """
        Gera o bloco de texto para injetar no system prompt da Alice.

        Com contexto (pergunta atual): busca fatos RELEVANTES.
        Sem contexto: retorna os 10 fatos mais recentes.

        Retorna string vazia se não houver fatos.
        """
        if contexto:
            self._ultimo_contexto = contexto

        fatos = self.buscar_fatos_recentes(10)
        if not fatos:
            return ""

        # Ordem cronológica (mais antigo primeiro) para leitura natural
        fatos_str = "\n".join(f"  - {fato}" for fato in reversed(fatos))
        return (
            "\n[O que Alice já sabe sobre o usuário — Memória Permanente]\n"
            f"{fatos_str}\n"
            "Use essas informações para personalizar suas respostas naturalmente.\n"
        )
