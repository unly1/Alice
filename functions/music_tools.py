"""
functions/music_tools.py — Módulo de música da Alice.

Fluxo:
  1. Alice recebe [TOOL:tocar_musica:nome da música]
  2. yt-dlp busca a URL de áudio no YouTube (sem download)
  3. mpv abre a URL em subprocess não-bloqueante com IPC socket ativo
  4. [TOOL:parar_musica:] mata o processo mpv ativo
  5. [TOOL:volume:50] ajusta o volume via mpv IPC (0–100)

Dependências:
  pip install yt-dlp
  mpv instalado no PATH (mpv.io)
"""

import json
import os
import socket
import subprocess
import threading

from cerebro.logger import log

# Caminho do IPC socket do mpv (named pipe no Windows)
_MPV_IPC_PATH = r"\\.\pipe\mpv-alice"

# Timeout para operações IPC (segundos)
_IPC_TIMEOUT = 2.0


class MusicTools:
    """Gerencia reprodução de música via yt-dlp + mpv."""

    def __init__(self):
        self._processo: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # ── Busca ──────────────────────────────────────────────────────────────────

    def _buscar_url(self, query: str) -> str:
        """
        Usa yt-dlp para encontrar a URL de áudio do primeiro resultado do YouTube.
        Não faz download — apenas extrai a URL do stream de áudio.

        Retorna a URL do stream, ou "" se falhar.
        """
        try:
            import yt_dlp  # noqa: PLC0415

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
                    return url
                return ""

        except ImportError:
            log.warning("🎵 [Música] yt-dlp não instalado. Execute: pip install yt-dlp")
            return ""
        except Exception as e:
            log.warning(f"🎵 [Música] Erro na busca: {e}")
            return ""

    # ── Reprodução ─────────────────────────────────────────────────────────────

    def tocar_musica(self, query: str, *args, **kwargs) -> str:
        """
        Busca a música no YouTube e toca com mpv em background.

        Args:
            query: Nome da música ou artista (ex: "lofi hip hop", "Daft Punk Get Lucky")

        Returns:
            Mensagem de status para a Alice narrar.
        """
        if not query or not query.strip():
            return "Qual música você quer ouvir? Me diz o nome ou artista."

        query = query.strip()
        log.info(f"🎵 [Música] Buscando: {query}")

        url = self._buscar_url(query)
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
            return f"Tocando agora: {query}!"

        except FileNotFoundError:
            log.warning("🎵 [Música] mpv não encontrado no PATH. Instale em mpv.io.")
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
        try:
            vol = int(str(nivel).strip())
            vol = max(0, min(100, vol))
        except (ValueError, TypeError):
            return "Volume inválido. Use um número de 0 a 100."

        with self._lock:
            if not self._processo or self._processo.poll() is not None:
                return "Nenhuma música está tocando."

        enviado = self._enviar_ipc({"command": ["set_property", "volume", vol]})
        if enviado:
            log.info(f"🎵 [Música] Volume → {vol}%")
            return f"Volume ajustado para {vol}%."
        return f"Volume ajustado para {vol}% (IPC indisponível, será aplicado na próxima faixa)."

    # ── Helpers internos ───────────────────────────────────────────────────────

    def _parar_processo(self) -> bool:
        """Mata o processo mpv ativo. Retorna True se havia processo rodando."""
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
