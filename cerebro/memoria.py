"""
cerebro/memoria.py — Memória de longo prazo da Alice.

Fonte primária:  ClickHouse (analytics.memoria)
Backup silencioso: SQLite (cerebro/memoria.db)

A interface pública é idêntica à versão anterior — nenhum outro arquivo
precisa ser alterado para usar esta classe.

Vantagem vs SQLite puro:
  - construir_bloco_memoria(contexto) filtra por relevância via ILIKE,
    injetando no prompt apenas os fatos relacionados à conversa atual.
    Isso reduz os tokens enviados ao LLM sem perder contexto importante.
"""

import sqlite3
import os
import uuid
import datetime
from cerebro.logger import log

# SQLite de backup — sempre criado, funciona mesmo sem Docker
CAMINHO_DB = os.path.join(os.path.dirname(__file__), "memoria.db")


class MemoriaLongaPrazo:
    """
    Memória de longo prazo com dupla persistência.

    ClickHouse (primário):
      - Escalável, permite busca por relevância (ILIKE)
      - Requer Docker com o container 'clickhouse' rodando

    SQLite (fallback silencioso):
      - Funciona sempre, mesmo sem Docker
      - Usado automaticamente se ClickHouse estiver offline
      - Salvo em: cerebro/memoria.db

    Args:
        ch: instância de ClickhouseLogger (opcional).
            Se None ou desconectado, opera apenas em modo SQLite.
    """

    def __init__(self, ch=None):
        self._ch = ch
        self._ultimo_contexto: str = ""  # última pergunta do usuário (para ILIKE)
        self._criar_tabela_sqlite()

        total = len(self.buscar_todos_fatos())
        fonte = "ClickHouse" if self._usar_ch() else "SQLite (fallback)"
        log.info(
            f"💾 Memória carregada | {total} fatos sobre o usuário | Fonte: {fonte}"
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _usar_ch(self) -> bool:
        """True se ClickHouse está disponível e conectado."""
        return self._ch is not None and self._ch._conectado

    # ── SQLite (backup) ───────────────────────────────────────────────────────

    def _criar_tabela_sqlite(self):
        """Cria a tabela de backup no SQLite se não existir."""
        with sqlite3.connect(CAMINHO_DB) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fatos (
                    id        TEXT PRIMARY KEY,
                    fato      TEXT NOT NULL,
                    ativo     INTEGER DEFAULT 1,
                    criado_em TEXT NOT NULL
                )
            """)
            conn.commit()

    def _sqlite_salvar(self, fato: str, id_fato: str):
        try:
            with sqlite3.connect(CAMINHO_DB) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO fatos (id, fato, ativo, criado_em) VALUES (?, ?, 1, ?)",
                    (id_fato, fato, datetime.datetime.now().isoformat()),
                )
                conn.commit()
        except Exception as e:
            log.warning(f"💾 [SQLite] Erro ao salvar fato: {e}")

    def _sqlite_buscar_relevantes(self, limite: int, contexto: str) -> list:
        try:
            with sqlite3.connect(CAMINHO_DB) as conn:
                if contexto:
                    palavras = [f"%{p}%" for p in contexto.split() if len(p) >= 4][:5]
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
            with sqlite3.connect(CAMINHO_DB) as conn:
                rows = conn.execute(
                    "SELECT id, fato, criado_em FROM fatos WHERE ativo=1 ORDER BY rowid ASC"
                ).fetchall()
                return [{"id": r[0], "fato": r[1], "criado_em": r[2]} for r in rows]
        except Exception:
            return []

    def _sqlite_remover(self, id_fato: str) -> bool:
        try:
            with sqlite3.connect(CAMINHO_DB) as conn:
                cur = conn.execute("UPDATE fatos SET ativo=0 WHERE id=?", (id_fato,))
                conn.commit()
                return cur.rowcount > 0
        except Exception:
            return False

    def _sqlite_limpar(self) -> int:
        try:
            with sqlite3.connect(CAMINHO_DB) as conn:
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
        Salva um fato sobre o usuário.
        Grava no ClickHouse (se disponível) E no SQLite (sempre).
        """
        fato = fato.strip()
        if not fato or len(fato) < 6:
            return

        id_fato = str(uuid.uuid4())

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
                return self._ch.buscar_fatos_relevantes(self._ultimo_contexto, limite)
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
        self._sqlite_remover(id_str)

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

        Com contexto (pergunta atual): busca fatos RELEVANTES via ILIKE —
        injeta no prompt apenas o que tem relação com a conversa atual.
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
