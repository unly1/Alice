"""
cerebro/graph_memory.py — Memória em Grafo da Alice (GraphRAG).

Armazena relações entre entidades extraídas da conversa:
  (sujeito) --[relação]--> (objeto)

Exemplos de triplas:
  ("Marcos", "TRABALHA_EM", "empresa de tecnologia")
  ("Marcos", "GOSTA_DE", "café")
  ("Alice", "CONHECE", "Marcos")
  ("projeto X", "USA", "Python")

Diferença em relação à memória vetorial:
  - Memória vetorial: fatos isolados ("Marcos gosta de café")
  - Grafo: relações explícitas que podem ser percorridas
    → "O que Marcos usa no trabalho?" recupera vizinhos de "Marcos"

Backend: SQLite (sem nova dependência).
Design: fail-silently, thread-safe, complementa a memória existente.
"""

import os
import sqlite3
import threading
from cerebro.logger import log

CAMINHO_GRAFO = os.path.join(os.path.dirname(__file__), "grafo.db")

# Máximo de triplas retornadas por consulta de contexto
_LIMITE_TRIPLAS = 15


class GraphMemory:
    """
    Memória em grafo para Alice — armazena e recupera relações entre entidades.

    Uso:
        grafo = GraphMemory()
        grafo.salvar_tripla("Marcos", "GOSTA_DE", "café")
        bloco = grafo.construir_bloco_grafo("o que Marcos gosta?")
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._criar_tabela()
        total = self._contar_triplas()
        log.info(f"🕸️  GraphMemory carregado | {total} triplas no grafo")

    # ── Infraestrutura ────────────────────────────────────────────────────────

    def _criar_tabela(self):
        """Cria tabelas do grafo se não existirem."""
        try:
            with sqlite3.connect(CAMINHO_GRAFO) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS triplas (
                        id        INTEGER PRIMARY KEY AUTOINCREMENT,
                        sujeito   TEXT NOT NULL,
                        relacao   TEXT NOT NULL,
                        objeto    TEXT NOT NULL,
                        criado_em TEXT NOT NULL,
                        UNIQUE(sujeito, relacao, objeto)
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sujeito ON triplas(sujeito)"
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_objeto ON triplas(objeto)")
                conn.commit()
        except Exception as e:
            log.warning(f"🕸️  [Grafo] Erro ao criar tabela: {e}")

    def _contar_triplas(self) -> int:
        try:
            with sqlite3.connect(CAMINHO_GRAFO) as conn:
                return conn.execute("SELECT COUNT(*) FROM triplas").fetchone()[0]
        except Exception:
            return 0

    # ── Escrita ───────────────────────────────────────────────────────────────

    def salvar_tripla(self, sujeito: str, relacao: str, objeto: str) -> bool:
        """
        Persiste uma tripla (sujeito, relação, objeto).
        Ignora silenciosamente se a tripla já existir (UNIQUE constraint).

        Returns:
            True se foi inserida (nova), False se já existia ou houve erro.
        """
        sujeito = sujeito.strip()[:120]
        relacao = relacao.strip().upper()[:60]
        objeto = objeto.strip()[:200]

        if not sujeito or not relacao or not objeto:
            return False

        try:
            from datetime import datetime

            with self._lock:
                with sqlite3.connect(CAMINHO_GRAFO) as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO triplas (sujeito, relacao, objeto, criado_em) "
                        "VALUES (?, ?, ?, ?)",
                        (sujeito, relacao, objeto, datetime.now().isoformat()),
                    )
                    conn.commit()
                    nova = conn.execute("SELECT changes()").fetchone()[0] > 0
            if nova:
                log.debug(
                    f"🕸️  [Grafo] Nova tripla: ({sujeito}) --[{relacao}]--> ({objeto})"
                )
            return nova
        except Exception as e:
            log.warning(f"🕸️  [Grafo] Erro ao salvar tripla: {e}")
            return False

    def salvar_triplas(self, triplas: list[tuple]) -> int:
        """
        Salva múltiplas triplas de uma vez.

        Args:
            triplas: lista de (sujeito, relacao, objeto)

        Returns:
            Número de triplas novas inseridas.
        """
        salvas = 0
        for tripla in triplas:
            if len(tripla) == 3:
                if self.salvar_tripla(*tripla):
                    salvas += 1
        return salvas

    # ── Leitura ───────────────────────────────────────────────────────────────

    def buscar_vizinhos(
        self, entidade: str, limite: int = _LIMITE_TRIPLAS
    ) -> list[tuple]:
        """
        Retorna todas as triplas onde a entidade aparece como sujeito ou objeto.

        Returns:
            Lista de (sujeito, relacao, objeto).
        """
        entidade = entidade.strip()
        if not entidade:
            return []
        try:
            with sqlite3.connect(CAMINHO_GRAFO) as conn:
                rows = conn.execute(
                    "SELECT sujeito, relacao, objeto FROM triplas "
                    "WHERE sujeito LIKE ? OR objeto LIKE ? "
                    "ORDER BY id DESC LIMIT ?",
                    (f"%{entidade}%", f"%{entidade}%", limite),
                ).fetchall()
            return rows
        except Exception as e:
            log.warning(f"🕸️  [Grafo] Erro ao buscar vizinhos de '{entidade}': {e}")
            return []

    def buscar_por_contexto(
        self, contexto: str, limite: int = _LIMITE_TRIPLAS
    ) -> list[tuple]:
        """
        Busca triplas relevantes ao contexto extraindo palavras-chave.
        Combina vizinhos de todas as entidades encontradas.

        Returns:
            Lista deduplicada de (sujeito, relacao, objeto).
        """
        if not contexto:
            return []

        # Palavras com 4+ chars como candidatas a entidades/termos relevantes
        palavras = [p.strip(".,!?;:\"'()") for p in contexto.split() if len(p) >= 4]
        if not palavras:
            return []

        try:
            conds = " OR ".join(["sujeito LIKE ? OR objeto LIKE ?"] * len(palavras))
            params = []
            for p in palavras:
                params += [f"%{p}%", f"%{p}%"]
            params.append(limite)

            with sqlite3.connect(CAMINHO_GRAFO) as conn:
                rows = conn.execute(
                    f"SELECT DISTINCT sujeito, relacao, objeto FROM triplas "
                    f"WHERE {conds} ORDER BY id DESC LIMIT ?",
                    params,
                ).fetchall()
            return rows
        except Exception as e:
            log.warning(f"🕸️  [Grafo] Erro na busca por contexto: {e}")
            return []

    def listar_todas(self, limite: int = 200) -> list[dict]:
        """Retorna todas as triplas para gerenciamento/visualização."""
        try:
            with sqlite3.connect(CAMINHO_GRAFO) as conn:
                rows = conn.execute(
                    "SELECT id, sujeito, relacao, objeto, criado_em FROM triplas "
                    "ORDER BY id DESC LIMIT ?",
                    (limite,),
                ).fetchall()
            return [
                {
                    "id": r[0],
                    "sujeito": r[1],
                    "relacao": r[2],
                    "objeto": r[3],
                    "criado_em": r[4],
                }
                for r in rows
            ]
        except Exception:
            return []

    def remover_tripla(self, id_tripla: int) -> bool:
        """Remove uma tripla pelo ID."""
        try:
            with self._lock:
                with sqlite3.connect(CAMINHO_GRAFO) as conn:
                    conn.execute("DELETE FROM triplas WHERE id = ?", (id_tripla,))
                    conn.commit()
            return True
        except Exception:
            return False

    def limpar(self) -> int:
        """Remove todas as triplas. Retorna a quantidade apagada."""
        try:
            total = self._contar_triplas()
            with self._lock:
                with sqlite3.connect(CAMINHO_GRAFO) as conn:
                    conn.execute("DELETE FROM triplas")
                    conn.commit()
            log.warning(f"🕸️  [Grafo] {total} triplas apagadas.")
            return total
        except Exception:
            return 0

    # ── Bloco de contexto ─────────────────────────────────────────────────────

    def construir_bloco_grafo(self, contexto: str = "") -> str:
        """
        Gera bloco de texto para injetar no system prompt da Alice.

        Busca triplas relevantes ao contexto e formata como lista de relações.
        Retorna string vazia se não houver triplas relevantes.
        """
        triplas = self.buscar_por_contexto(contexto) if contexto else []

        # Se não encontrou nada relevante, pega as mais recentes como fallback
        if not triplas:
            try:
                with sqlite3.connect(CAMINHO_GRAFO) as conn:
                    rows = conn.execute(
                        "SELECT sujeito, relacao, objeto FROM triplas ORDER BY id DESC LIMIT 8"
                    ).fetchall()
                triplas = rows
            except Exception:
                pass

        if not triplas:
            return ""

        linhas = [f"  {s} --[{r}]--> {o}" for s, r, o in triplas]
        return (
            "\n[Relações conhecidas — Grafo de Memória]\n"
            + "\n".join(linhas)
            + "\nUse essas relações para dar respostas mais precisas e contextualizadas.\n"
        )
