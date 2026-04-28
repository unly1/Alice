"""
Tools.py — ferramentas externas da Alice, organizadas em classes.

Estrutura:
  LocationTools  — hora local e clima (usando fuso IANA e wttr.in)
  WebTools       — pesquisa na internet (DuckDuckGo + Wikipedia fallback)
  ToolManager    — gerenciador central; expõe DISPONIVEIS e executar()

Uso:
    from functions.Tools import tools, DISPONIVEIS

    # Via ToolManager
    resultado = tools.executar("get_current_time", "Manaus")
    cidade    = tools.location.detectar_cidade_no_texto("que horas são em Recife?")

    # Backward compat (OllamaClient, main.py)
    DISPONIVEIS["get_weather"]("Porto Alegre")
"""

import datetime
import json
import logging
import re
import urllib.parse
import urllib.request
import zoneinfo

from functions.music_tools import MusicTools

# Garante que os dados de fuso IANA estão disponíveis no Windows.
try:
    import tzdata  # noqa: F401
except ImportError:
    pass


# ─────────────────────────────────────────────────────────────────────────────
class LocationTools:
    """
    Ferramentas de localização: hora local e clima.

    Atributos de classe:
        CIDADE_PADRAO — cidade usada quando o usuário não especifica nenhuma
                        (padrão: Campo Grande, MS — UTC-4)
    """

    CIDADE_PADRAO: str = "Campo Grande"

    # ── Mapeamento cidade → fuso IANA ────────────────────────────────────────
    _FUSO_CIDADES: dict[str, str] = {
        # UTC-3 (Brasília / São Paulo)
        "são paulo": "America/Sao_Paulo",
        "sao paulo": "America/Sao_Paulo",
        "rio de janeiro": "America/Sao_Paulo",
        "belo horizonte": "America/Sao_Paulo",
        "salvador": "America/Sao_Paulo",
        "fortaleza": "America/Fortaleza",
        "recife": "America/Recife",
        "brasília": "America/Sao_Paulo",
        "brasilia": "America/Sao_Paulo",
        "curitiba": "America/Sao_Paulo",
        "florianópolis": "America/Sao_Paulo",
        "florianopolis": "America/Sao_Paulo",
        "porto alegre": "America/Sao_Paulo",
        "belém": "America/Belem",
        "belem": "America/Belem",
        "natal": "America/Fortaleza",
        "maceió": "America/Maceio",
        "maceio": "America/Maceio",
        "joão pessoa": "America/Fortaleza",
        "joao pessoa": "America/Fortaleza",
        "aracaju": "America/Maceio",
        "teresina": "America/Fortaleza",
        "são luís": "America/Fortaleza",
        "sao luis": "America/Fortaleza",
        "macapá": "America/Belem",
        "macapa": "America/Belem",
        "santarém": "America/Santarem",
        "santarem": "America/Santarem",
        # UTC-4
        "porto velho": "America/Porto_Velho",
        "cuiabá": "America/Cuiaba",
        "cuiaba": "America/Cuiaba",
        "campo grande": "America/Campo_Grande",
        "manaus": "America/Manaus",
        "boa vista": "America/Boa_Vista",
        # UTC-5
        "rio branco": "America/Rio_Branco",
    }

    # Fuso default quando a cidade não é encontrada no mapa
    # (Campo Grande, MS — alinhado com CIDADE_PADRAO)
    _FUSO_DEFAULT: str = "America/Campo_Grande"

    # ── Helpers internos ──────────────────────────────────────────────────────

    @staticmethod
    def _normalizar(cidade: str) -> str:
        """Reduz a string a minúsculas sem sufixos de estado."""
        cidade = cidade.lower().strip()
        # Remove estado: "São Paulo, SP" | "Porto Alegre/RS" | "Campo Grande - MS"
        cidade = re.split(r"[,/\-]", cidade)[0].strip()
        return cidade

    def _fuso_para_cidade(self, cidade: str) -> str:
        """Resolve o nome da cidade para um fuso IANA."""
        chave = self._normalizar(cidade)
        if chave in self._FUSO_CIDADES:
            return self._FUSO_CIDADES[chave]
        # Busca parcial
        for k, v in self._FUSO_CIDADES.items():
            if k in chave or chave in k:
                return v
        return self._FUSO_DEFAULT

    # ── Detecção de cidade no texto ───────────────────────────────────────────

    def detectar_cidade_no_texto(self, texto: str) -> str | None:
        """
        Procura o nome de uma cidade conhecida dentro do texto do usuário.

        Retorna o nome formatado (title-case) se encontrado, ou None se o
        usuário não especificou nenhuma cidade — neste caso, usar CIDADE_PADRAO.

        Exemplos:
            "que horas são em recife?"  → "Recife"
            "como está o tempo?"        → None  (usa Campo Grande por padrão)
            "e em porto alegre?"        → "Porto Alegre"
        """
        texto_norm = self._normalizar(texto)
        # Verifica cidades multi-palavra primeiro (mais específicas)
        for cidade in sorted(self._FUSO_CIDADES, key=len, reverse=True):
            if cidade in texto_norm:
                return cidade.title()
        return None

    # ── Ferramentas Públicas ──────────────────────────────────────────────────

    def get_current_time(self, cidade: str | None = None, *args, **kwargs) -> str:
        """
        Retorna a hora local atual para a cidade informada.

        Se cidade for None ou vazio, usa CIDADE_PADRAO (Campo Grande).

        Args:
            cidade: Nome da cidade (ex: 'Manaus', 'Brasília').
        """
        cidade = (cidade or self.CIDADE_PADRAO).strip()

        fuso_nome = self._fuso_para_cidade(cidade)
        tz = None
        try:
            tz = zoneinfo.ZoneInfo(fuso_nome)
        except Exception:
            pass
        if tz is None:
            try:
                tz = zoneinfo.ZoneInfo(self._FUSO_DEFAULT)
            except Exception:
                pass
        if tz is None:
            # Último recurso: UTC-4 sem depender de tzdata
            tz = datetime.timezone(datetime.timedelta(hours=-4))

        agora = datetime.datetime.now(tz)
        offset = agora.strftime("%z")
        offset_fmt = f"UTC{offset[:3]}:{offset[3:]}" if offset else ""
        return agora.strftime("%H:%M de %d/%m/%Y") + (
            f" ({offset_fmt})" if offset_fmt else ""
        )

    def get_weather(self, cidade: str | None = None, *args, **kwargs) -> str:
        """
        Consulta a temperatura atual via wttr.in para a cidade informada.

        Se cidade for None ou vazio, usa CIDADE_PADRAO (Campo Grande).

        Args:
            cidade: Nome da cidade (ex: 'Porto Alegre', 'Manaus').
        """
        cidade = (cidade or self.CIDADE_PADRAO).strip()
        cidade_limpa = self._normalizar(cidade).title()
        cidade_encoded = urllib.parse.quote(cidade_limpa)
        url = f"https://wttr.in/{cidade_encoded}?format=j1&lang=pt"

        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Alice-VTuber/1.0"}
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode())

            atual = data["current_condition"][0]
            temp_c = atual["temp_C"]
            desc_pt = atual.get("lang_pt", [{}])[0].get("value") or atual.get(
                "weatherDesc", [{}]
            )[0].get("value", "")
            humidade = atual.get("humidity", "?")
            sensacao = atual.get("FeelsLikeC", temp_c)

            return (
                f"Em {cidade_limpa}: {temp_c}°C, {desc_pt}. "
                f"Sensação térmica: {sensacao}°C | Umidade: {humidade}%."
            )

        except Exception as e:
            return f"Não consegui obter o tempo para {cidade_limpa} agora. ({e})"


# ─────────────────────────────────────────────────────────────────────────────
class WebTools:
    """Ferramentas de pesquisa na internet."""

    def search_web(self, query: str, *args, **kwargs) -> str:
        """
        Pesquisa na internet usando DuckDuckGo e Wikipedia como fallback.

        Tenta primeiro o DuckDuckGo (até 3 resultados com snippet).
        Se falhar, tenta a Wikipedia em português.

        Args:
            query: Termo ou pergunta a pesquisar.
        """
        log = logging.getLogger(__name__)

        # ── 1. DuckDuckGo ─────────────────────────────────────────────────────
        try:
            from ddgs import DDGS

            with DDGS() as ddgs:
                resultados = list(ddgs.text(query, region="br-pt", max_results=3))

            if resultados:
                linhas = [f"[Busca] Resultados para '{query}':"]
                for i, r in enumerate(resultados, 1):
                    titulo = r.get("title", "").strip()
                    snippet = r.get("body", "").strip()
                    url = r.get("href", "").strip()
                    if snippet and len(snippet) > 200:
                        snippet = snippet[:200].rsplit(" ", 1)[0] + "..."
                    if titulo or snippet:
                        linhas.append(f"{i}. {titulo}")
                        if snippet:
                            linhas.append(f"   {snippet}")
                        if url:
                            linhas.append(f"   Fonte: {url}")
                return "\n".join(linhas)

        except Exception as e:
            log.warning(f"[search_web] DuckDuckGo falhou: {e}")

        # ── 2. Wikipedia (fallback) ────────────────────────────────────────────
        try:
            import wikipediaapi

            wiki = wikipediaapi.Wikipedia(language="pt", user_agent="Alice-VTuber/1.0")
            pagina = wiki.page(query)
            if pagina.exists():
                resumo = pagina.summary
                if len(resumo) > 500:
                    resumo = resumo[:500].rsplit(" ", 1)[0] + "..."
                return f"[Wikipedia] {pagina.title}:\n{resumo}"
            return f"Nao encontrei informacoes sobre '{query}' nem no DuckDuckGo nem na Wikipedia."

        except Exception as e:
            log.warning(f"[search_web] Wikipedia falhou: {e}")

        return (
            f"Não foi possível pesquisar '{query}' agora. Tente novamente mais tarde."
        )


# ─────────────────────────────────────────────────────────────────────────────
class ToolManager:
    """
    Gerenciador central de todas as ferramentas da Alice.

    Instancie uma vez e reutilize. Expõe:
      - tools.location  : LocationTools
      - tools.web       : WebTools
      - tools.DISPONIVEIS : dict nome→função (compatível com OllamaClient)
      - tools.executar()  : executa uma ferramenta pelo nome com tratamento de erro
    """

    def __init__(self):
        self.location = LocationTools()
        self.web = WebTools()
        self.music = MusicTools()

        # Mapeamento nome → função (usado pelo OllamaClient no ReAct Pattern)
        self.DISPONIVEIS: dict[str, callable] = {
            "get_current_time": self.location.get_current_time,
            "get_weather": self.location.get_weather,
            "search_web": self.web.search_web,
            "tocar_musica": self.music.tocar_musica,
            "parar_musica": self.music.parar_musica,
            "volume": self.music.volume,
        }

    def executar(self, nome: str, argumento: str = "") -> str:
        """
        Executa uma ferramenta pelo nome, com tratamento de erro embutido.

        Args:
            nome:      nome da ferramenta (ex: 'get_weather')
            argumento: argumento opcional (ex: nome da cidade ou query)

        Returns:
            Resultado em string, ou mensagem de erro amigável.
        """
        if nome not in self.DISPONIVEIS:
            return f"Ferramenta '{nome}' não encontrada."
        funcao = self.DISPONIVEIS[nome]
        try:
            return str(funcao(argumento) if argumento else funcao())
        except Exception as e:
            return f"Erro ao executar '{nome}': {e}"


# ── Singleton global ──────────────────────────────────────────────────────────
# Instância única usada por todo o sistema.
tools = ToolManager()

# ── Backward compatibility ────────────────────────────────────────────────────
# Mantém o acesso direto que OllamaClient e main.py já usam:
#   import functions.Tools as funcoes_tools
#   funcoes_tools.DISPONIVEIS["get_weather"]("Manaus")
#   funcoes_tools.get_current_time("Recife")
DISPONIVEIS = tools.DISPONIVEIS
get_current_time = tools.location.get_current_time
get_weather = tools.location.get_weather
search_web = tools.web.search_web
