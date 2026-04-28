"""
ContentManager — gerencia todas as personalidades e respostas da Alice.

Responsabilidades:
- Carregar e cachear em memória todos os arquivos de personalidade (.txt)
- Carregar e cachear todas as respostas fixas (respostas_vazias, etc.)
- Recarregar os conteúdos sob demanda (hot-reload sem reiniciar o sistema)
- Fornecer interface simples para acessar prompt de personalidade e frases aleatórias
- Listar personalidades disponíveis dinamicamente (leitura do disco)

Uso:
    from functions.ContentManager import ContentManager

    cm = ContentManager()
    prompt = cm.get_personalidade("alegre")
    frase  = cm.get_resposta_vazia()
    lista  = cm.listar_personalidades()
"""

import os
import random
from pathlib import Path

from cerebro.logger import log


class ContentManager:
    """
    Gerenciador centralizado de personalidades e respostas da Alice.

    Mantém um cache em memória de todos os arquivos .txt das pastas
    `personalidades/` e `respostas/`. O cache é populado na primeira
    chamada (lazy loading) e pode ser recarregado a qualquer momento
    via `recarregar()`.
    """

    # Nomes das pastas relativas à raiz do projeto
    PASTA_PERSONALIDADES = "personalidades"
    PASTA_RESPOSTAS = "respostas"

    # Fallbacks caso os arquivos não existam
    _FALLBACK_PERSONALIDADE = (
        "Você é uma IA que se chama Alice. Sempre responda em português."
    )
    _FALLBACK_RESPOSTA_VAZIA = "Acho que me perdi. O que você disse mesmo?"

    def __init__(self, base_dir: str | None = None):
        """
        Args:
            base_dir: Caminho absoluto da raiz do projeto.
                      Se None, usa o diretório de trabalho atual (os.getcwd()).
        """
        self._base = Path(base_dir) if base_dir else Path(os.getcwd())

        # Cache: { nome_sem_extensao: conteudo_str }
        self._personalidades: dict[str, str] = {}

        # Cache: { nome_arquivo_sem_extensao: [lista_de_frases] }
        self._respostas: dict[str, list[str]] = {}

        self._carregar_tudo()

    # ── Carregamento ──────────────────────────────────────────────────────────

    def _carregar_tudo(self) -> None:
        """Carrega (ou recarrega) todos os arquivos nos dois diretórios."""
        self._personalidades = self._carregar_dir(
            self.PASTA_PERSONALIDADES, modo="texto"
        )
        self._respostas = self._carregar_dir(self.PASTA_RESPOSTAS, modo="linhas")

        qtd_p = len(self._personalidades)
        qtd_r = sum(len(v) for v in self._respostas.values())
        log.info(
            f"📚 ContentManager: {qtd_p} personalidade(s) | "
            f"{qtd_r} resposta(s) carregada(s)"
        )

    def _carregar_dir(self, pasta: str, modo: str) -> dict:
        """
        Lê todos os .txt de um diretório e preenche o cache.

        Args:
            pasta: subdiretório relativo à base do projeto.
            modo:  'texto'  → valor = conteúdo completo (str)
                   'linhas' → valor = lista de linhas não-vazias (list[str])

        Returns:
            dict[str, str | list[str]]
        """
        caminho = self._base / pasta
        resultado: dict = {}

        if not caminho.exists():
            log.warning(
                f"📂 ContentManager: pasta '{pasta}' não encontrada em {self._base}"
            )
            return resultado

        for arquivo in sorted(caminho.glob("*.txt")):
            nome = arquivo.stem  # nome sem extensão
            try:
                texto = arquivo.read_text(encoding="utf-8")
                if modo == "linhas":
                    resultado[nome] = [
                        linha.strip() for linha in texto.splitlines() if linha.strip()
                    ]
                else:
                    resultado[nome] = texto
                log.debug(f"    └─ Carregado: {pasta}/{arquivo.name}")
            except Exception as e:
                log.error(f"📚 ContentManager: erro ao ler '{arquivo}': {e}")

        return resultado

    def recarregar(self) -> None:
        """
        Recarrega todos os arquivos do disco.
        Útil para hot-reload sem reiniciar o sistema após editar textos.
        """
        log.info("🔄 ContentManager: recarregando personalidades e respostas...")
        self._carregar_tudo()

    # ── Personalidades ────────────────────────────────────────────────────────

    def listar_personalidades(self) -> list[str]:
        """
        Retorna a lista de nomes de personalidades disponíveis.
        Relê o disco para garantir que novos arquivos sejam visíveis
        sem precisar chamar recarregar().
        """
        caminho = self._base / self.PASTA_PERSONALIDADES
        if not caminho.exists():
            return []
        return sorted(f.stem for f in caminho.glob("*.txt"))

    def get_personalidade(self, nome: str) -> str:
        """
        Retorna o conteúdo completo do arquivo de personalidade.

        Se o nome não existir no cache, tenta ler do disco antes
        de retornar o fallback padrão.

        Args:
            nome: nome da personalidade (ex: 'alegre', 'curiosa').

        Returns:
            str com o system prompt da personalidade.
        """
        nome = nome.lower().strip()

        # Cache hit
        if nome in self._personalidades:
            return self._personalidades[nome]

        # Tentativa de leitura direta (para personalidades adicionadas após init)
        caminho = self._base / self.PASTA_PERSONALIDADES / f"{nome}.txt"
        if caminho.exists():
            try:
                conteudo = caminho.read_text(encoding="utf-8")
                self._personalidades[nome] = conteudo  # atualiza cache
                log.debug(f"📚 ContentManager: '{nome}' carregado sob demanda.")
                return conteudo
            except Exception as e:
                log.error(f"📚 ContentManager: erro ao ler '{nome}.txt': {e}")

        log.warning(
            f"📚 ContentManager: personalidade '{nome}' não encontrada. Usando fallback."
        )
        return self._FALLBACK_PERSONALIDADE

    def get_todas_personalidades(self) -> dict[str, str]:
        """Retorna o cache completo {nome: conteúdo} das personalidades."""
        return dict(self._personalidades)

    # ── Respostas ─────────────────────────────────────────────────────────────

    def get_resposta_vazia(self) -> str:
        """
        Retorna uma frase aleatória da lista de respostas para input vazio.
        Equivalente ao antigo `responder_vazio()` em main.py.
        """
        return self._get_resposta_aleatoria(
            "respostas_vazias", self._FALLBACK_RESPOSTA_VAZIA
        )

    def get_resposta_aleatoria(self, arquivo: str, fallback: str = "") -> str:
        """
        Retorna uma frase aleatória de qualquer arquivo de respostas.

        Args:
            arquivo:  nome do arquivo sem extensão (ex: 'respostas_vazias').
            fallback: texto retornado se o arquivo não existir.
        """
        return self._get_resposta_aleatoria(arquivo, fallback)

    def _get_resposta_aleatoria(self, chave: str, fallback: str) -> str:
        """Implementação interna: sorteia uma linha do cache."""
        frases = self._respostas.get(chave, [])
        if frases:
            return random.choice(frases)

        # Tenta recarregar do disco caso o arquivo tenha sido criado após init
        caminho = self._base / self.PASTA_RESPOSTAS / f"{chave}.txt"
        if caminho.exists():
            try:
                linhas = [
                    linha.strip()
                    for linha in caminho.read_text(encoding="utf-8").splitlines()
                    if linha.strip()
                ]
                if linhas:
                    self._respostas[chave] = linhas  # atualiza cache
                    return random.choice(linhas)
            except Exception as e:
                log.error(f"📚 ContentManager: erro ao ler '{chave}.txt': {e}")

        return fallback

    def listar_respostas(self) -> dict[str, list[str]]:
        """Retorna o cache completo {nome_arquivo: [frases]} das respostas."""
        return dict(self._respostas)

    # ── Diagnóstico ───────────────────────────────────────────────────────────

    def status(self) -> dict:
        """
        Retorna um dict com o status atual do ContentManager.
        Útil para debug e para exibir no painel de administração da GUI.

        Exemplo de retorno:
            {
                'personalidades': {'alegre': 825, 'brava': 473, ...},
                'respostas': {'respostas_vazias': 7},
            }
        """
        return {
            "personalidades": {
                nome: len(conteudo) for nome, conteudo in self._personalidades.items()
            },
            "respostas": {
                nome: len(frases) for nome, frases in self._respostas.items()
            },
        }

    def __repr__(self) -> str:
        p = len(self._personalidades)
        r = sum(len(v) for v in self._respostas.values())
        return f"<ContentManager personalidades={p} respostas={r}>"
