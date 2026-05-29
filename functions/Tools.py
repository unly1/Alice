"""
Tools.py — ferramentas externas da Alice e processamento de comandos de voz, organizados em classes e funções.

Estrutura:
  MusicTools     — reprodutor de música via yt-dlp + mpv (IPC local)
  LocationTools  — hora local e clima (usando fuso IANA e wttr.in)
  WebTools       — pesquisa na internet (DuckDuckGo + Wikipedia fallback)
  ToolManager    — gerenciador central; expõe DISPONIVEIS e executar()

Uso:
    from functions.Tools import tools, process_command

    # Processamento de comandos locais
    resposta = await process_command("que horas são?", speak_func)
"""

import datetime
import json
import os
import random
import re
import socket
import subprocess
import threading
import urllib.parse
import urllib.request
import webbrowser
import zoneinfo

# Garante que os dados de fuso IANA estão disponíveis no Windows.
try:
    import tzdata  # noqa: F401
except ImportError:
    pass

# Importações opcionais (com fallback gracioso)
try:
    import winshell
    WINSHELL_AVAILABLE = True
except ImportError:
    WINSHELL_AVAILABLE = False


# Caminhos e Constantes para MPV IPC (Música)
_MPV_IPC_PATH = r"\\.\pipe\mpv-alice"
_IPC_TIMEOUT = 2.0


# ── Banco de Piadas em Português ─────────────────────────────────────────────
JOKES_PT = [
    "Por que o desenvolvedor faliu? Porque ele usou todo o seu cache.",
    "O que o programador Java disse para o café? Você me deixa com sono e agitado ao mesmo tempo, mas eu não vivo sem você.",
    "O que o Git disse para o desenvolvedor? Commit com amor, push com pressa.",
    "Quantos programadores são necessários para trocar uma lâmpada? Nenhum, isso é um problema de hardware.",
    "Por que o computador foi ao médico? Porque ele estava com vírus.",
    "Qual é o lugar favorito do programador para passar as férias? A memória ROM.",
    "O que é um loop infinito? É quando você compra fiado e diz que vai pagar amanhã.",
    "Por que a linguagem C é tão educada? Porque ela tem classe... Ah não, desculpe, C não tem classe, quem tem classe é C++!",
    "Como o programador se despede no chat? Até logo, ou melhor, CTRL+C CTRL+V!"
]


# ── Classe: MusicTools ────────────────────────────────────────────────────────

class MusicTools:
    """Gerencia reprodução de música via yt-dlp + mpv."""

    def __init__(self):
        self._processo: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def _buscar_url(self, query: str) -> tuple[str, str]:
        """
        Usa yt-dlp para encontrar a URL de áudio do primeiro resultado do YouTube.
        Não faz download — apenas extrai a URL do stream de áudio.

        Retorna a URL do stream e o título, ou ("", "") se falhar.
        """
        try:
            import yt_dlp  # noqa: PLC0415
            from cerebro.logger import log

            ydl_opts = {
                "format": "bestaudio/best",
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "extract_flat": False,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)
                if info and "entries" in info and info["entries"]:
                    entrada = info["entries"][0]
                    url = entrada.get("url") or entrada.get("webpage_url", "")
                    titulo = entrada.get("title", query)
                    log.info(f"🎵 [Música] Encontrado: {titulo}")
                    return url, titulo
                return "", ""

        except ImportError:
            from cerebro.logger import log
            log.warning(
                "🎵 [Música] yt-dlp não instalado. Execute: pip install yt-dlp")
            return "", ""
        except Exception as e:
            from cerebro.logger import log
            log.warning(f"🎵 [Música] Erro na busca: {e}")
            return "", ""

    def tocar_musica(self, query: str, *args, **kwargs) -> str:
        """
        Busca a música no YouTube e toca com mpv em background.

        Args:
            query: Nome da música ou artista (ex: "lofi hip hop", "Daft Punk Get Lucky")

        Returns:
            Mensagem de status para a Alice narrar.
        """
        from cerebro.logger import log
        if not query or not query.strip():
            return "Qual música você quer ouvir? Me diz o nome ou artista."

        query = query.strip()
        log.info(f"🎵 [Música] Buscando: {query}")

        url, titulo = self._buscar_url(query)
        if not url:
            return f"Não consegui encontrar '{query}' no YouTube agora."

        # Para qualquer música tocando antes de iniciar nova
        self._parar_processo()

        try:
            cmd = [
                "mpv",
                "--no-video",
                "--volume=70",
                f"--input-ipc-server={_MPV_IPC_PATH}",
                "--really-quiet",
                url,
            ]

            with self._lock:
                self._processo = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )

            log.info(f"🎵 [Música] Tocando (PID {self._processo.pid})")
            return f"Tocando agora: {titulo}!"

        except FileNotFoundError:
            log.warning(
                "🎵 [Música] mpv não encontrado no PATH. Instale em mpv.io.")
            return "mpv não está instalado. Preciso do mpv para tocar música."
        except Exception as e:
            log.warning(f"🎵 [Música] Erro ao iniciar mpv: {e}")
            return f"Não consegui tocar a música: {e}"

    def parar_musica(self, *args, **kwargs) -> str:
        """
        Para a reprodução atual.

        Returns:
            Mensagem de status.
        """
        from cerebro.logger import log
        parou = self._parar_processo()
        if parou:
            log.info("🎵 [Música] Reprodução encerrada.")
            return "Música parada!"
        return "Nenhuma música está tocando no momento."

    def volume(self, nivel: str | int, *args, **kwargs) -> str:
        """
        Ajusta o volume do mpv via IPC (0–100).

        Args:
            nivel: Número de 0 a 100 (ex: "50", "80")

        Returns:
            Mensagem de status.
        """
        from cerebro.logger import log
        try:
            vol = int(str(nivel).strip())
            vol = max(0, min(100, vol))
        except (ValueError, TypeError):
            return "Volume inválido. Use um número de 0 a 100."

        with self._lock:
            if not self._processo or self._processo.poll() is not None:
                return "Nenhuma música está tocando."

        enviado = self._enviar_ipc(
            {"command": ["set_property", "volume", vol]})
        if enviado:
            log.info(f"🎵 [Música] Volume → {vol}%")
            return f"Volume ajustado para {vol}%."
        return f"Volume ajustado para {vol}% (IPC indisponível, será aplicado na próxima faixa)."

    def _parar_processo(self) -> bool:
        """Mata o processo mpv ativo. Retorna True se havia processo rodando."""
        from cerebro.logger import log
        with self._lock:
            if self._processo is None:
                return False
            if self._processo.poll() is not None:
                # Processo já terminou sozinho
                self._processo = None
                return False
            try:
                self._processo.terminate()
                try:
                    self._processo.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._processo.kill()
            except Exception as e:
                log.warning(f"🎵 [Música] Erro ao parar mpv: {e}")
            finally:
                self._processo = None
        return True

    def _enviar_ipc(self, comando: dict) -> bool:
        """
        Envia um comando JSON ao mpv via named pipe (Windows) ou socket (Unix).
        Retorna True se a comunicação foi bem-sucedida.
        """
        try:
            payload = json.dumps(comando) + "\n"
            if os.name == "nt":
                # Windows: named pipe
                import win32file  # pywin32

                handle = win32file.CreateFile(
                    _MPV_IPC_PATH,
                    win32file.GENERIC_WRITE,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    0,
                    None,
                )
                win32file.WriteFile(handle, payload.encode())
                win32file.CloseHandle(handle)
            else:
                # Linux/macOS: Unix socket
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(_IPC_TIMEOUT)
                    s.connect(_MPV_IPC_PATH)
                    s.sendall(payload.encode())
            return True
        except Exception:
            return False

    @property
    def tocando(self) -> bool:
        """Retorna True se há uma música tocando agora."""
        with self._lock:
            return self._processo is not None and self._processo.poll() is None


# ── Classe: LocationTools ─────────────────────────────────────────────────────

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

    _FUSO_DEFAULT: str = "America/Campo_Grande"

    @staticmethod
    def _normalizar(cidade: str) -> str:
        """Reduz a string a minúsculas sem sufixos de estado."""
        cidade = cidade.lower().strip()
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

    def detectar_cidade_no_texto(self, texto: str) -> str | None:
        """
        Procura o nome de uma cidade conhecida dentro do texto do usuário.
        """
        texto_norm = self._normalizar(texto)
        for cidade in sorted(self._FUSO_CIDADES, key=len, reverse=True):
            if cidade in texto_norm:
                return cidade.title()
        return None

    def get_current_time(self, cidade: str | None = None, *args, **kwargs) -> str:
        """
        Retorna a hora local atual para a cidade informada.
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


# ── Classe: WebTools ──────────────────────────────────────────────────────────

class WebTools:
    """Ferramentas de pesquisa na internet."""

    def search_web(self, query: str, *args, **kwargs) -> str:
        """
        Pesquisa na internet usando DuckDuckGo e Wikipedia como fallback.
        """
        # ── 1. DuckDuckGo ─────────────────────────────────────────────────────
        try:
            from ddgs import DDGS

            with DDGS() as ddgs:
                resultados = list(
                    ddgs.text(query, region="br-pt", max_results=3))

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
            from cerebro.logger import log
            log.warning(f"[search_web] DuckDuckGo falhou: {e}")

        # ── 2. Wikipedia (fallback) ────────────────────────────────────────────
        try:
            import wikipediaapi

            wiki = wikipediaapi.Wikipedia(
                language="pt", user_agent="Alice-VTuber/1.0")
            pagina = wiki.page(query)
            if pagina.exists():
                resumo = pagina.summary
                if len(resumo) > 500:
                    resumo = resumo[:500].rsplit(" ", 1)[0] + "..."
                return f"[Wikipedia] {pagina.title}:\n{resumo}"
            return f"Nao encontrei informacoes sobre '{query}' nem no DuckDuckGo nem na Wikipedia."

        except Exception as e:
            from cerebro.logger import log
            log.warning(f"[search_web] Wikipedia falhou: {e}")

        return (
            f"Não foi possível pesquisar '{query}' agora. Tente novamente mais tarde."
        )


# ── Utilitários de Limpeza de Comando ────────────────────────────────────────

def _clean_command(text: str, prefixes: list, suffixes: list = None) -> str:
    """Remove prefixos e sufixos comuns do comando para isolar o argumento real."""
    text_clean = text.strip()

    # Remove prefixos em ordem de tamanho (do maior para o menor)
    for prefix in sorted(prefixes, key=len, reverse=True):
        if text_clean.lower().startswith(prefix.lower()):
            text_clean = text_clean[len(prefix):].strip()
            break

    # Remove sufixos
    if suffixes:
        for suffix in sorted(suffixes, key=len, reverse=True):
            if text_clean.lower().endswith(suffix.lower()):
                text_clean = text_clean[:-len(suffix)].strip()
                break

    return text_clean


# ── Executores e Processador de Comandos Locais (Async) ───────────────────────

async def execute_youtube_search(keyword: str, speak_func) -> str:
    """Abre o YouTube com a pesquisa fornecida."""
    import urllib.parse
    url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(keyword)}"
    webbrowser.open(url)
    msg = f"Buscando '{keyword}' no YouTube."
    await speak_func(msg)
    return f"🎥 {msg}"


async def execute_wikipedia_search(query: str, speak_func) -> str:
    """Busca um resumo na Wikipedia usando wikipedia-api."""
    try:
        import wikipediaapi
        wiki = wikipediaapi.Wikipedia(
            language="pt", user_agent="Alice-VTuber/1.0")
        pagina = wiki.page(query)
        if pagina.exists():
            resumo = pagina.summary
            sentences = resumo.split(".")
            resumo_curto = ". ".join(sentences[:3]).strip()
            if not resumo_curto.endswith("."):
                resumo_curto += "."
            intro = f"Segundo a Wikipedia sobre '{query}': "
            await speak_func(intro + resumo_curto)
            return f"📚 {resumo_curto}"
        else:
            msg = f"Não encontrei nada sobre '{query}' na Wikipedia."
            await speak_func(msg)
            return f"📚 {msg}"
    except Exception as e:
        msg = "Não consegui buscar na Wikipedia agora."
        await speak_func(msg)
        return f"📚 Erro: {e}"


async def process_command(text: str, speak_func, on_waiting_input=None) -> str | None:
    """
    Analisa 'text' e executa a ação correspondente localmente se corresponder a um comando.

    Retorna:
        str  – mensagem para exibir no chat se o comando foi executado.
        None – se nenhum comando foi correspondido (o fluxo segue para a LLM).
        "EXIT" – sinaliza que o assistente deve encerrar.
        "WAIT:<context>" – aguarda entrada do usuário para 'context'.
    """
    text = text.lower().strip()
    if not text:
        return None

    # Prefixo/Sufixo para buscas
    youtube_prefixes = [
        "pesquisar no youtube por", "pesquisar no youtube",
        "pesquisa no youtube por", "pesquisa no youtube",
        "buscar no youtube por", "buscar no youtube",
        "busca no youtube por", "busca no youtube",
        "procurar no youtube por", "procurar no youtube",
        "abrir o youtube e pesquisar por", "abrir o youtube e pesquisar",
        "abrir youtube e pesquisar por", "abrir youtube e pesquisar",
        "youtube por", "youtube"
    ]
    youtube_suffixes = [" no youtube", " no youtu", " pelo youtube"]

    wikipedia_prefixes = [
        "pesquisar na wikipedia sobre", "pesquisar na wikipedia por", "pesquisar na wikipedia",
        "pesquisa na wikipedia sobre", "pesquisa na wikipedia",
        "buscar na wikipedia sobre", "buscar na wikipedia",
        "busca na wikipedia sobre", "busca na wikipedia",
        "o que é", "o que e", "quem foi", "quem é",
        "definir", "definição de", "definicao de",
        "wikipedia sobre", "wikipedia"
    ]
    wikipedia_suffixes = [" na wikipedia", " pela wikipedia"]

    music_prefixes = [
        "tocar a música", "tocar a musica", "tocar música", "tocar musica",
        "tocar a música de", "tocar a musica de", "tocar música de", "tocar musica de",
        "reproduzir a música", "reproduzir a musica", "reproduzir música", "reproduzir musica",
        "toque a música de", "toque a musica de", "toque música de", "toque musica de",
        "toque a música", "toque a musica", "toque música", "toque musica",
        "tocar", "toque", "reproduzir"
    ]
    music_suffixes = [" no youtube", " no youtu"]

    # ── 1. YouTube ──────────────────────────────────────────────────────────
    if any(k in text for k in ("youtube", "youtu")):
        query = _clean_command(text, youtube_prefixes, youtube_suffixes)
        query_check = query.replace("youtube", "").replace("youtu", "").strip()
        if query_check:
            return await execute_youtube_search(query_check, speak_func)
        else:
            await speak_func("O que você quer pesquisar no YouTube?")
            if on_waiting_input:
                on_waiting_input("youtube")
            return "WAIT:youtube"

    # ── 2. Wikipedia ────────────────────────────────────────────────────────
    if any(k in text for k in ("wikipedia", "o que é", "o que e", "quem foi", "quem é", "definir")):
        query = _clean_command(text, wikipedia_prefixes, wikipedia_suffixes)
        query_check = query.replace("wikipedia", "").strip()
        if query_check:
            return await execute_wikipedia_search(query_check, speak_func)
        else:
            await speak_func("O que você quer pesquisar na Wikipedia?")
            if on_waiting_input:
                on_waiting_input("wikipedia")
            return "WAIT:wikipedia"

    # ── 3. Música (Tocar) ───────────────────────────────────────────────────
    if any(text.startswith(p) for p in music_prefixes):
        query = _clean_command(text, music_prefixes, music_suffixes)
        if query:
            msg = tools.music.tocar_musica(query)
            await speak_func(msg)
            return f"🎵 {msg}"
        else:
            await speak_func("Qual música você quer ouvir? Me diz o nome ou o artista.")
            if on_waiting_input:
                on_waiting_input("tocar_musica")
            return "WAIT:tocar_musica"

    # ── 4. Parar Música ─────────────────────────────────────────────────────
    if any(k in text for k in ("parar música", "parar musica", "para a música", "para a musica", "desligar música", "para música", "para musica")):
        msg = tools.music.parar_musica()
        await speak_func(msg)
        return f"🎵 {msg}"

    # ── 5. Volume da Música ─────────────────────────────────────────────────
    if "volume" in text:
        numeros = re.findall(r"\d+", text)
        if numeros:
            vol = int(numeros[0])
            msg = tools.music.volume(vol)
            await speak_func(msg)
            return f"🔊 {msg}"
        else:
            await speak_func("Qual o nível do volume de zero a cem?")
            if on_waiting_input:
                on_waiting_input("volume")
            return "WAIT:volume"

    # ── 6. Clima / Tempo ────────────────────────────────────────────────────
    if any(k in text for k in ("tempo", "clima", "temperatura", "previsão", "previsao", "chover", "chovendo")):
        cidade = tools.location.detectar_cidade_no_texto(text)
        resposta = tools.location.get_weather(cidade)
        await speak_func(resposta)
        return f"🌤️ {resposta}"

    # ── 7. Horas / Data ─────────────────────────────────────────────────────
    if any(k in text for k in ("que horas", "horas são", "hora é", "que hora", "horário", "horario")):
        cidade = tools.location.detectar_cidade_no_texto(text)
        resposta = tools.location.get_current_time(cidade)
        msg = f"Agora são {resposta}." if cidade else f"São {resposta}."
        await speak_func(msg)
        return f"🕐 {msg}"

    if any(k in text for k in ("que dia", "data de hoje", "dia é hoje", "qual a data")):
        _DIAS = {
            "Monday": "segunda-feira", "Tuesday": "terça-feira",
            "Wednesday": "quarta-feira", "Thursday": "quinta-feira",
            "Friday": "sexta-feira", "Saturday": "sábado", "Sunday": "domingo",
        }
        now = datetime.datetime.now()
        dia_semana = _DIAS.get(now.strftime("%A"), now.strftime("%A"))
        data = now.strftime("%d/%m/%Y")
        msg = f"Hoje é {dia_semana}, {data}."
        await speak_func(msg)
        return f"📅 {msg}"

    # ── 8. Piada ────────────────────────────────────────────────────────────
    if any(k in text for k in ("piada", "me divirta", "engraçado")):
        joke = random.choice(JOKES_PT)
        await speak_func(joke)
        return f"🤣 {joke}"

    # ── 9. Esvaziar Lixeira ─────────────────────────────────────────────────
    if any(k in text for k in ("esvaziar lixeira", "limpar lixeira", "lixeira")):
        if WINSHELL_AVAILABLE:
            try:
                winshell.recycle_bin().empty(confirm=False, show_progress=False, sound=True)
                msg = "Lixeira esvaziada com sucesso!"
                await speak_func(msg)
                return f"🗑️ {msg}"
            except Exception as e:
                err_msg = f"Erro ao esvaziar lixeira: {e}"
                await speak_func("Não consegui esvaziar a lixeira no momento.")
                return f"❌ {err_msg}"
        else:
            return "❌ winshell não disponível. Execute: pip install winshell pywin32"

    # ── 10. Ajuda / Comandos ────────────────────────────────────────────────
    if any(k in text for k in ("ajuda", "help", "o que você faz", "comandos")):
        msg = (
            "Comandos locais que entendo: "
            "tocar música [nome], volume [0-100], parar música, "
            "pesquisar no YouTube [busca], pesquisar na Wikipedia [busca], "
            "clima [cidade], que horas são, piada, e esvaziar lixeira."
        )
        await speak_func(msg)
        return f"ℹ️ {msg}"

    # Se não corresponder a nenhum comando, retorna None para seguir para a LLM
    return None


# ── Classe: ToolManager ───────────────────────────────────────────────────────

class ToolManager:
    """
    Gerenciador central de todas as ferramentas da Alice.
    """

    def __init__(self):
        self.location = LocationTools()
        self.web = WebTools()
        self.music = MusicTools()

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
        """
        if nome not in self.DISPONIVEIS:
            return f"Ferramenta '{nome}' não encontrada."
        funcao = self.DISPONIVEIS[nome]
        try:
            return str(funcao(argumento) if argumento else funcao())
        except Exception as e:
            return f"Erro ao executar '{nome}': {e}"


# ── Singleton Global ──────────────────────────────────────────────────────────
tools = ToolManager()

# ── Aliases de Retrocompatibilidade ───────────────────────────────────────────
DISPONIVEIS = tools.DISPONIVEIS
get_current_time = tools.location.get_current_time
get_weather = tools.location.get_weather
search_web = tools.web.search_web
