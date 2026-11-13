"""
interface/janela.py — Janela de chat da Alice (customtkinter dark theme)

Roda em uma thread daemon separada, sem bloquear o loop asyncio principal.
Comunicação 100% via filas thread-safe (nenhuma chamada direta ao Tk fora da
thread da GUI):
  - fila_entrada: texto digitado pelo usuário ou comandos de controle dict
    {"tipo": "acao", "acao": "parar_fala"|"toggle_mute"|"reiniciar"|"encerrar"}
  - fila_saida: eventos vindos do loop asyncio (dicts tipados ou strings/tuplas legadas)
    * {"tipo": "mensagem", "autor": str, "texto": str, "categoria": "normal"|"proativa"|"sonho"|"sistema"}
    * {"tipo": "estado", "estado": "calibrando"|"ouvindo"|"transcrevendo"|"pensando"|"falando"|"ocioso"|"chat_only"}
    * {"tipo": "input", "habilitado": bool}
"""

from __future__ import annotations

import os
import queue
import threading
from datetime import datetime
from tkinter import messagebox
from typing import Any

import customtkinter as ctk
from dotenv import set_key

from cerebro.logger import log

# Caminho absoluto do .env (raiz do projeto)
_ENV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
)

# ─── Paleta de cores ──────────────────────────────────────────────────────────
COR_FUNDO = "#0d0d1a"
COR_PAINEL = "#13132b"
COR_ALICE = "#1a1a3e"
COR_ALICE_BORDA = "#4444aa"
COR_USER = "#2d1a33"
COR_USER_BORDA = "#9944cc"
COR_PROATIVA = "#26163a"
COR_PROATIVA_BORDA = "#b366ff"
COR_SONHO = "#112238"
COR_SONHO_BORDA = "#4499dd"
COR_SISTEMA = "#16202a"
COR_SISTEMA_BORDA = "#3a6b88"
COR_TEXTO = "#e8e8f0"
COR_TEXTO_DIM = "#888899"
COR_DESTAQUE = "#6644bb"
COR_STATUS_BG = "#0a0a1f"

# Limite máximo de balões mantidos no DOM do CustomTkinter para evitar lentidão
MAX_BUBBLES_VISUAIS = 150

# Configuração visual dos estados em tempo real
ESTADOS_UI: dict[str, tuple[str, str, str]] = {
    "calibrando": ("🎙️ Calibrando mic...", "#e6a822", "#2b220d"),
    "ouvindo": ("🟢 Ouvindo", "#33cc77", "#0d2b1a"),
    "transcrevendo": ("✍️ Transcrevendo...", "#44aaff", "#0d1f33"),
    "pensando": ("🧠 Pensando...", "#bb66ff", "#241136"),
    "falando": ("🔊 Falando", "#ff66aa", "#331126"),
    "ocioso": ("🌙 Ocioso", "#888899", "#181828"),
    "chat_only": ("⌨️ Chat-only", "#6699cc", "#122030"),
}

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class JanelaAlice:
    """
    Janela de chat com CustomTkinter.
    100% Thread-safe: toda mutação de widgets ocorre exclusivamente na thread
    da GUI através do consumo de `fila_saida` e `_fila_ui_interna` em `_ciclo_saida`.
    """

    def __init__(
        self,
        fila_entrada: queue.Queue,
        fila_saida: queue.Queue,
        emotion_engine_ref: Any = None,
        memoria_ref: Any = None,
        fila_reiniciar: queue.Queue | None = None,
        grafo_ref: Any = None,
    ):
        self.fila_entrada = fila_entrada
        self.fila_saida = fila_saida
        self.emotion_engine = emotion_engine_ref
        self.memoria = memoria_ref
        self.fila_reiniciar = fila_reiniciar
        self.grafo = grafo_ref

        # Fila interna para callbacks de threads auxiliares (SQLite/PyAudio) e chamadas legadas
        self._fila_ui_interna: queue.Queue = queue.Queue()

        self.root: ctk.CTk | None = None
        self._encerrando: bool = False
        self._mic_mutado: bool = False
        self._estado_atual_str: str = "ocioso"

        # Controle de widgets de mensagens e redimensionamento
        self._bubbles_wrappers: list[ctk.CTkFrame] = []
        self._msg_widgets_wrap: list[ctk.CTkLabel] = []
        self._wraplength_atual: int = 500
        self._resize_after_id: str | None = None

        # Gerenciador de memória
        self._janela_memoria: ctk.CTkToplevel | None = None
        self._fatos_cache: list[dict[str, Any]] = []
        self._triplas_cache: list[dict[str, Any]] = []
        self._vars_fatos: dict[Any, ctk.BooleanVar] = {}
        self._termo_busca_mem: str = ""

        # Áudio
        self._audio_vars: dict[str, ctk.StringVar] = {}
        self._entradas_audio: list[tuple[str, Any]] = [("— Carregando... —", None)]
        self._saidas_audio: list[tuple[str, Any]] = [("— Carregando... —", None)]
        self._menus_audio: dict[str, ctk.CTkOptionMenu] = {}

    # ─── Ciclo de vida ────────────────────────────────────────────────────────

    def iniciar(self):
        """Ponto de entrada da thread da janela."""
        self.root = ctk.CTk()
        self.root.title("🌸 Alice — Interface de Chat")
        self.root.geometry("920x680")
        self.root.minsize(680, 440)
        self.root.configure(fg_color=COR_FUNDO)
        self.root.protocol("WM_DELETE_WINDOW", self._ao_fechar)

        self._inicializar_fontes()
        self._construir_interface()
        self.root.bind("<Configure>", self._ao_redimensionar_janela)

        self._ciclo_status()
        self._ciclo_saida()

        # Carrega dispositivos de áudio em background sem travar o startup da GUI
        self._recarregar_dispositivos_audio_async()

        self.root.mainloop()

    def _inicializar_fontes(self):
        """Cria instâncias reutilizáveis de CTkFont para evitar vazamento de recursos Tk."""
        self._fnt_titulo = ctk.CTkFont("Segoe UI", 15, weight="bold")
        self._fnt_btn_header = ctk.CTkFont("Segoe UI", 12)
        self._fnt_btn_bold = ctk.CTkFont("Segoe UI", 12, weight="bold")
        self._fnt_status = ctk.CTkFont("Segoe UI", 11)
        self._fnt_badge = ctk.CTkFont("Segoe UI", 11, weight="bold")
        self._fnt_entry = ctk.CTkFont("Segoe UI", 13)
        self._fnt_enviar = ctk.CTkFont("Segoe UI", 13, weight="bold")
        self._fnt_autor = ctk.CTkFont("Segoe UI", 10, weight="bold")
        self._fnt_msg = ctk.CTkFont("Segoe UI", 12)
        self._fnt_msg_italico = ctk.CTkFont("Segoe UI", 12, slant="italic")
        self._fnt_copiar = ctk.CTkFont("Segoe UI", 10)
        self._fnt_secao = ctk.CTkFont("Segoe UI", 13, weight="bold")
        self._fnt_item_mem = ctk.CTkFont("Segoe UI", 11)

    def _ao_fechar(self):
        """Envia ação de encerramento limpo ao backend e fecha a janela com fallback seguro."""
        if self._encerrando:
            return
        if not messagebox.askyesno(
            "Encerrar Alice",
            "Deseja realmente encerrar a Alice?",
            icon="question",
        ):
            return

        self._encerrando = True
        try:
            self.fila_entrada.put_nowait({"tipo": "acao", "acao": "encerrar"})
        except Exception as e:
            log.debug(f"Falha ao enfileirar acao encerrar: {e}")

        if self.fila_reiniciar is not None:
            try:
                self.fila_reiniciar.put_nowait(True)
            except Exception:
                pass

        # Fecha a janela de forma limpa na mainloop e agenda fallback caso o processo trave
        if self.root is not None:
            try:
                self.root.quit()
                self.root.destroy()
            except Exception:
                pass

        def _watchdog_saida():
            import time

            time.sleep(3.0)
            os._exit(0)

        threading.Thread(
            target=_watchdog_saida, daemon=True, name="AliceExitWatchdog"
        ).start()

    # ─── Construção da UI ─────────────────────────────────────────────────────

    def _construir_interface(self):
        """Monta todos os widgets da janela."""
        assert self.root is not None

        # ── Título / Header com Controles de Voz e Sessão ────────────────────
        frame_header = ctk.CTkFrame(
            self.root, fg_color=COR_STATUS_BG, height=52, corner_radius=0
        )
        frame_header.pack(fill="x")

        ctk.CTkLabel(
            frame_header,
            text="  🌸  Alice  •  Assistente Local",
            font=self._fnt_titulo,
            text_color=COR_TEXTO,
        ).pack(side="left", padx=14, pady=10)

        # Botão Encerrar Alice
        ctk.CTkButton(
            frame_header,
            text="⏻ Encerrar",
            command=self._ao_fechar,
            fg_color="#3a0a0a",
            hover_color="#7a1010",
            border_color="#cc2222",
            border_width=1,
            font=self._fnt_btn_header,
            height=30,
            width=96,
        ).pack(side="right", padx=(4, 10), pady=10)

        # Botão de Gerenciar Memória
        ctk.CTkButton(
            frame_header,
            text="🧠 Memória",
            command=self._abrir_gerenciador_memoria,
            fg_color="#1a1a3e",
            hover_color=COR_DESTAQUE,
            border_color="#4444aa",
            border_width=1,
            font=self._fnt_btn_header,
            height=30,
            width=98,
        ).pack(side="right", padx=3, pady=10)

        # Botão Reiniciar Conversa
        ctk.CTkButton(
            frame_header,
            text="🔄 Reiniciar",
            command=self._reiniciar,
            fg_color="#0d2a1a",
            hover_color="#1a5a32",
            border_color="#22aa55",
            border_width=1,
            font=self._fnt_btn_header,
            height=30,
            width=98,
        ).pack(side="right", padx=3, pady=10)

        # Botão Parar Fala (barge-in imediato independente de Reiniciar)
        self.btn_parar_fala = ctk.CTkButton(
            frame_header,
            text="⏹️ Parar fala",
            command=self._acao_parar_fala,
            fg_color="#331a0d",
            hover_color="#663314",
            border_color="#cc6622",
            border_width=1,
            font=self._fnt_btn_header,
            height=30,
            width=104,
        )
        self.btn_parar_fala.pack(side="right", padx=3, pady=10)

        # Botão Mute / Unmute Microfone
        self.btn_mute_mic = ctk.CTkButton(
            frame_header,
            text="🎤 Mic Ativo",
            command=self._acao_toggle_mute,
            fg_color="#162836",
            hover_color="#24465e",
            border_color="#3388bb",
            border_width=1,
            font=self._fnt_btn_header,
            height=30,
            width=106,
        )
        self.btn_mute_mic.pack(side="right", padx=3, pady=10)

        # ── Status bar (Indicador reativo de estado + Estado Emocional em RAM) ─
        frame_status = ctk.CTkFrame(
            self.root, fg_color=COR_PAINEL, height=38, corner_radius=0
        )
        frame_status.pack(fill="x")

        # Pílula de estado em tempo real
        self.lbl_estado_tempo_real = ctk.CTkLabel(
            frame_status,
            text="🌙 Ocioso",
            font=self._fnt_badge,
            text_color="#888899",
            fg_color="#181828",
            corner_radius=10,
            padx=10,
            pady=2,
        )
        self.lbl_estado_tempo_real.pack(side="left", padx=(14, 10), pady=6)

        self.lbl_nome = self._status_label(frame_status, "👤  —")
        self.lbl_humor = self._status_label(frame_status, "🎭  Neutro")
        self.lbl_amizade = self._status_label(frame_status, "💙  0/100")
        self.lbl_turnos = self._status_label(frame_status, "💬  0 conversas")

        # ── Abas principais (Chat + Áudio) ────────────────────────────────────
        self._tabview = ctk.CTkTabview(
            self.root,
            fg_color=COR_PAINEL,
            segmented_button_fg_color=COR_STATUS_BG,
            segmented_button_selected_color=COR_DESTAQUE,
            segmented_button_selected_hover_color="#8866ee",
            segmented_button_unselected_color=COR_STATUS_BG,
            segmented_button_unselected_hover_color="#1a1a3e",
            text_color=COR_TEXTO,
        )
        self._tabview.pack(fill="both", expand=True, padx=0, pady=0)

        self._tabview.add("💬  Chat")
        self._tabview.add("🎙️  Áudio")

        # ── Área de chat (dentro da aba Chat) ─────────────────────────────────
        self.chat_area = ctk.CTkScrollableFrame(
            self._tabview.tab("💬  Chat"),
            fg_color=COR_FUNDO,
            scrollbar_button_color=COR_DESTAQUE,
        )
        self.chat_area.pack(fill="both", expand=True, padx=4, pady=4)

        # ── Aba de configuração de áudio ──────────────────────────────────────
        self._construir_aba_audio(self._tabview.tab("🎙️  Áudio"))

        # ── Input area ────────────────────────────────────────────────────────
        frame_input = ctk.CTkFrame(self.root, fg_color=COR_PAINEL, corner_radius=0)
        frame_input.pack(fill="x", padx=0, pady=0)

        self.entry = ctk.CTkEntry(
            frame_input,
            placeholder_text="Digite uma mensagem (ou fale pelo mic)...",
            fg_color=COR_FUNDO,
            border_color=COR_DESTAQUE,
            text_color=COR_TEXTO,
            font=self._fnt_entry,
            height=40,
        )
        self.entry.pack(side="left", fill="x", expand=True, padx=(12, 6), pady=10)
        self.entry.bind("<Return>", self._enviar)

        self.btn_enviar = ctk.CTkButton(
            frame_input,
            text="Enviar ➤",
            command=self._enviar,
            fg_color=COR_DESTAQUE,
            hover_color="#8866ee",
            font=self._fnt_enviar,
            height=40,
            width=115,
        )
        self.btn_enviar.pack(side="right", padx=(0, 12), pady=10)

    def _status_label(self, parent, texto: str) -> ctk.CTkLabel:
        lbl = ctk.CTkLabel(
            parent,
            text=texto,
            font=self._fnt_status,
            text_color=COR_TEXTO_DIM,
        )
        lbl.pack(side="left", padx=12, pady=6)
        return lbl

    # ─── Controles de Voz e Estado em Tempo Real ──────────────────────────────

    def _atualizar_indicador_estado(self, estado: str):
        """Atualiza o indicador visual de estado em tempo real na barra superior."""
        chave = (estado or "ocioso").strip().lower()
        self._estado_atual_str = chave
        rotulo, cor_texto, cor_fundo = ESTADOS_UI.get(
            chave, (f"✨ {estado}", COR_TEXTO, COR_ALICE)
        )
        self.lbl_estado_tempo_real.configure(
            text=rotulo, text_color=cor_texto, fg_color=cor_fundo
        )

    def _acao_parar_fala(self):
        """Envia comando de barge-in para interromper imediatamente a fala da Alice."""
        self.fila_entrada.put({"tipo": "acao", "acao": "parar_fala"})
        self._atualizar_indicador_estado("ocioso")

    def _acao_toggle_mute(self):
        """Alterna o estado visual de mute do microfone e notifica o loop principal."""
        self._mic_mutado = not self._mic_mutado
        if self._mic_mutado:
            self.btn_mute_mic.configure(
                text="🔇 Mic Mutado",
                fg_color="#3a1212",
                hover_color="#5c1d1d",
                border_color="#cc4444",
            )
        else:
            self.btn_mute_mic.configure(
                text="🎤 Mic Ativo",
                fg_color="#162836",
                hover_color="#24465e",
                border_color="#3388bb",
            )
        self.fila_entrada.put({"tipo": "acao", "acao": "toggle_mute"})

    # ─── Aba de Áudio (Assíncrona) ────────────────────────────────────────────

    def _construir_aba_audio(self, parent):
        """Monta a aba de seleção de dispositivos de áudio sem travar a thread principal."""
        scroll = ctk.CTkScrollableFrame(
            parent, fg_color=COR_FUNDO, scrollbar_button_color=COR_DESTAQUE
        )
        scroll.pack(fill="both", expand=True, padx=0, pady=0)

        def secao(texto):
            ctk.CTkLabel(
                scroll,
                text=texto,
                font=self._fnt_secao,
                text_color=COR_DESTAQUE,
                anchor="w",
            ).pack(fill="x", padx=20, pady=(18, 4))
            ctk.CTkFrame(
                scroll, fg_color=COR_ALICE_BORDA, height=1, corner_radius=0
            ).pack(fill="x", padx=20, pady=(0, 8))

        def linha(label_txt: str, var_key: str):
            frame = ctk.CTkFrame(scroll, fg_color="transparent")
            frame.pack(fill="x", padx=20, pady=5)

            ctk.CTkLabel(
                frame,
                text=label_txt,
                width=180,
                anchor="w",
                font=self._fnt_btn_header,
                text_color=COR_TEXTO,
            ).pack(side="left")

            var = ctk.StringVar(value="— Carregando... —")
            self._audio_vars[var_key] = var

            menu = ctk.CTkOptionMenu(
                frame,
                variable=var,
                values=["— Carregando... —"],
                fg_color=COR_ALICE,
                button_color=COR_DESTAQUE,
                button_hover_color="#8866ee",
                text_color=COR_TEXTO,
                dropdown_fg_color=COR_PAINEL,
                dropdown_text_color=COR_TEXTO,
                dropdown_hover_color=COR_DESTAQUE,
                font=self._fnt_btn_header,
                width=420,
            )
            menu.pack(side="left", padx=(8, 0))
            self._menus_audio[var_key] = menu

        secao("🎤  Entradas de Áudio  (Microfone)")
        linha("Entrada 1  (principal)", "mic1")
        linha("Entrada 2  (secundária)", "mic2")

        secao("🔊  Saídas de Áudio  (TTS / Voz da Alice)")
        linha("Saída 1  (principal)", "saida1")
        linha("Saída 2  (secundária)", "saida2")

        frame_footer = ctk.CTkFrame(scroll, fg_color="transparent")
        frame_footer.pack(fill="x", padx=20, pady=(20, 8))

        self._lbl_audio_status = ctk.CTkLabel(
            frame_footer,
            text="Carregando dispositivos de áudio...",
            font=self._fnt_status,
            text_color=COR_TEXTO_DIM,
        )
        self._lbl_audio_status.pack(side="left")

        ctk.CTkButton(
            frame_footer,
            text="💾  Salvar configuração",
            command=self._salvar_config_audio,
            fg_color=COR_DESTAQUE,
            hover_color="#8866ee",
            font=self._fnt_enviar,
            height=38,
            width=200,
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            frame_footer,
            text="🔄 Recarregar lista",
            command=self._recarregar_dispositivos_audio_async,
            fg_color="#1a1a3e",
            hover_color="#2a2a5e",
            border_color="#4444aa",
            border_width=1,
            font=self._fnt_btn_header,
            height=38,
            width=150,
        ).pack(side="right")

        ctk.CTkLabel(
            scroll,
            text="⚠️  Reinicie a Alice para que as alterações de dispositivo tenham efeito.",
            font=self._fnt_copiar,
            text_color=COR_TEXTO_DIM,
        ).pack(pady=(0, 16))

    def _recarregar_dispositivos_audio_async(self):
        """Enumera dispositivos via pyaudiowpatch em background thread."""
        if hasattr(self, "_lbl_audio_status"):
            self._lbl_audio_status.configure(
                text="⏳ Enumerando dispositivos de áudio...", text_color=COR_TEXTO_DIM
            )

        def _worker():
            entradas, saidas = self._listar_dispositivos_audio()
            self._fila_ui_interna.put(
                {"tipo": "_audio_carregado", "entradas": entradas, "saidas": saidas}
            )

        threading.Thread(target=_worker, daemon=True, name="AliceAudioEnum").start()

    def _aplicar_dispositivos_audio_ui(
        self, entradas: list[tuple[str, Any]], saidas: list[tuple[str, Any]]
    ):
        """Atualiza os OptionMenus na thread da GUI após enumeração em background."""
        self._entradas_audio = entradas
        self._saidas_audio = saidas

        nomes_entradas = [label for label, _ in entradas] or ["— Nenhum —"]
        nomes_saidas = [label for label, _ in saidas] or ["— Nenhum —"]

        mic1_idx = os.getenv("INDICE_MICROFONE", "")
        mic2_idx = os.getenv("INDICE_MICROFONE_2", "")
        saida1_nm = os.getenv("DISPOSITIVO_SAIDA_AUDIO", "")
        saida2_nm = os.getenv("DISPOSITIVO_SAIDA_AUDIO_2", "")

        def _match_entrada(idx_str: str) -> str:
            for label, idx in entradas:
                if idx is not None and str(idx) == idx_str:
                    return label
            return nomes_entradas[0]

        def _match_saida(nome_str: str) -> str:
            for label, nome in saidas:
                if nome and nome_str and nome_str.lower() in nome.lower():
                    return label
            return nomes_saidas[0]

        for key in ("mic1", "mic2"):
            if key in self._menus_audio:
                self._menus_audio[key].configure(values=nomes_entradas)
        for key in ("saida1", "saida2"):
            if key in self._menus_audio:
                self._menus_audio[key].configure(values=nomes_saidas)

        self._audio_vars["mic1"].set(_match_entrada(mic1_idx))
        self._audio_vars["mic2"].set(_match_entrada(mic2_idx))
        self._audio_vars["saida1"].set(_match_saida(saida1_nm))
        self._audio_vars["saida2"].set(_match_saida(saida2_nm))

        self._lbl_audio_status.configure(
            text=f"Dispositivos prontos ({len(entradas) - 1} entradas, {len(saidas) - 1} saídas).",
            text_color=COR_TEXTO_DIM,
        )

    def _listar_dispositivos_audio(self):
        """Retorna (entradas, saidas) como listas de (label, valor)."""
        try:
            import pyaudiowpatch as pyaudio

            pa = pyaudio.PyAudio()
            entradas: list[tuple[str, Any]] = [("— Nenhum —", None)]
            saidas: list[tuple[str, Any]] = [("— Nenhum —", None)]
            for i in range(pa.get_device_count()):
                d = pa.get_device_info_by_index(i)
                nome = d["name"]
                if d["maxInputChannels"] > 0:
                    entradas.append((f"[{i}]  {nome}", i))
                if d["maxOutputChannels"] > 0:
                    saidas.append((nome, nome))
            pa.terminate()
        except Exception as e:
            log.warning(f"Erro ao listar dispositivos de áudio na GUI: {e}")
            entradas = [("— Erro ao listar dispositivos —", None)]
            saidas = [("— Erro ao listar dispositivos —", None)]
        return entradas, saidas

    def _salvar_config_audio(self):
        """Salva a seleção de dispositivos no .env em thread separada."""

        def _idx_de(label: str) -> str:
            for lbl, idx in self._entradas_audio:
                if lbl == label:
                    return str(idx) if idx is not None else ""
            return ""

        def _nome_de(label: str) -> str:
            for lbl, nome in self._saidas_audio:
                if lbl == label:
                    return nome or ""
            return ""

        mic1 = _idx_de(self._audio_vars["mic1"].get())
        mic2 = _idx_de(self._audio_vars["mic2"].get())
        saida1 = _nome_de(self._audio_vars["saida1"].get())
        saida2 = _nome_de(self._audio_vars["saida2"].get())

        self._lbl_audio_status.configure(
            text="⏳ Salvando configuração...", text_color=COR_TEXTO_DIM
        )

        def _worker():
            erro = None
            try:
                set_key(_ENV_PATH, "INDICE_MICROFONE", mic1)
                set_key(_ENV_PATH, "INDICE_MICROFONE_2", mic2)
                set_key(_ENV_PATH, "DISPOSITIVO_SAIDA_AUDIO", saida1)
                set_key(_ENV_PATH, "DISPOSITIVO_SAIDA_AUDIO_2", saida2)
            except Exception as e:
                erro = str(e)
            self._fila_ui_interna.put({"tipo": "_audio_salvo", "erro": erro})

        threading.Thread(target=_worker, daemon=True, name="AliceAudioSave").start()

    # ─── Reiniciar Conversa ───────────────────────────────────────────────────

    def _reiniciar(self):
        """Limpa o chat na GUI, restaura o input e sinaliza ao loop principal."""
        confirm = messagebox.askyesno(
            "Reiniciar Conversa",
            "Deseja reiniciar a conversa?\n\nO histórico desta sessão será apagado.",
            icon="question",
        )
        if not confirm:
            return

        # Limpa os balões da área de chat e listas de controle
        for widget in self.chat_area.winfo_children():
            widget.destroy()
        self._bubbles_wrappers.clear()
        self._msg_widgets_wrap.clear()

        # Restaura imediatamente o input caso estivesse aguardando resposta
        self._restaurar_input()
        self._atualizar_indicador_estado("ocioso")

        # Sinaliza via contrato novo em fila_entrada e mantém fila_reiniciar por compatibilidade
        self.fila_entrada.put({"tipo": "acao", "acao": "reiniciar"})
        if self.fila_reiniciar is not None:
            try:
                self.fila_reiniciar.put_nowait(True)
            except Exception:
                pass

    # ─── Gerenciador de Memória (Fatos + Grafo Assíncronos) ───────────────────

    def _abrir_gerenciador_memoria(self):
        """Abre (ou traz ao foco) a janela de gerenciamento de memória e grafo."""
        if self._janela_memoria and self._janela_memoria.winfo_exists():
            self._janela_memoria.focus()
            return

        if not self.memoria and not self.grafo:
            messagebox.showinfo("Memória", "Módulo de memória não disponível.")
            return

        assert self.root is not None
        win = ctk.CTkToplevel(self.root)
        win.title("🧠 Gerenciador de Memória da Alice")
        win.geometry("780x580")
        win.minsize(640, 440)
        win.configure(fg_color=COR_FUNDO)
        win.grab_set()
        self._janela_memoria = win

        # ── Header com Busca / Filtro em Tempo Real ───────────────────────────
        frame_top = ctk.CTkFrame(win, fg_color=COR_STATUS_BG, corner_radius=0)
        frame_top.pack(fill="x")

        self._lbl_mem_contagem = ctk.CTkLabel(
            frame_top,
            text="  🧠  Carregando memória...",
            font=self._fnt_secao,
            text_color=COR_TEXTO,
        )
        self._lbl_mem_contagem.pack(side="left", padx=16, pady=10)

        self._entry_busca_mem = ctk.CTkEntry(
            frame_top,
            placeholder_text="🔍 Filtrar fatos ou triplas...",
            fg_color=COR_FUNDO,
            border_color=COR_DESTAQUE,
            text_color=COR_TEXTO,
            font=self._fnt_btn_header,
            width=260,
            height=32,
        )
        self._entry_busca_mem.pack(side="right", padx=14, pady=10)
        self._entry_busca_mem.bind("<KeyRelease>", self._ao_filtrar_memoria)

        # ── Abas internas: Fatos (editável) e Grafo de Relações (somente leitura) ─
        self._tab_mem = ctk.CTkTabview(
            win,
            fg_color=COR_PAINEL,
            segmented_button_fg_color=COR_STATUS_BG,
            segmented_button_selected_color=COR_DESTAQUE,
            segmented_button_selected_hover_color="#8866ee",
            segmented_button_unselected_color=COR_STATUS_BG,
            text_color=COR_TEXTO,
        )
        self._tab_mem.pack(fill="both", expand=True, padx=12, pady=(6, 4))

        aba_fatos = self._tab_mem.add("📌 Fatos Salvos")
        aba_grafo = self._tab_mem.add("🕸️ Grafo de Relações (Somente Leitura)")

        # Lista de fatos
        self._frame_lista_fatos = ctk.CTkScrollableFrame(
            aba_fatos,
            fg_color=COR_FUNDO,
            scrollbar_button_color=COR_DESTAQUE,
        )
        self._frame_lista_fatos.pack(fill="both", expand=True, padx=4, pady=4)

        # Footer de ações (apenas na aba de fatos)
        frame_footer = ctk.CTkFrame(aba_fatos, fg_color=COR_PAINEL, corner_radius=0)
        frame_footer.pack(fill="x", pady=(4, 0))

        ctk.CTkButton(
            frame_footer,
            text="☑ Todos",
            command=self._selecionar_todos,
            fg_color="#1a1a3e",
            hover_color="#2a2a5e",
            border_color="#4444aa",
            border_width=1,
            font=self._fnt_btn_header,
            width=95,
        ).pack(side="left", padx=(8, 4), pady=8)

        ctk.CTkButton(
            frame_footer,
            text="☐ Nenhum",
            command=self._desselecionar_todos,
            fg_color="#1a1a3e",
            hover_color="#2a2a5e",
            border_color="#4444aa",
            border_width=1,
            font=self._fnt_btn_header,
            width=95,
        ).pack(side="left", padx=4, pady=8)

        ctk.CTkButton(
            frame_footer,
            text="🔄 Atualizar",
            command=self._solicitar_carga_memoria_async,
            fg_color="#1a1a3e",
            hover_color="#2a2a5e",
            border_color="#4444aa",
            border_width=1,
            font=self._fnt_btn_header,
            width=100,
        ).pack(side="left", padx=4, pady=8)

        ctk.CTkButton(
            frame_footer,
            text="💣 Limpar tudo",
            command=self._limpar_tudo,
            fg_color="#4a0a0a",
            hover_color="#7a1010",
            border_color="#cc2222",
            border_width=1,
            font=self._fnt_btn_bold,
            width=125,
        ).pack(side="right", padx=(4, 8), pady=8)

        ctk.CTkButton(
            frame_footer,
            text="🗑️ Apagar selecionados",
            command=self._apagar_selecionados,
            fg_color="#3a1a1a",
            hover_color="#6a1a1a",
            border_color="#aa4444",
            border_width=1,
            font=self._fnt_btn_bold,
            width=165,
        ).pack(side="right", padx=4, pady=8)

        # Lista de triplas do Grafo (somente leitura)
        self._frame_lista_grafo = ctk.CTkScrollableFrame(
            aba_grafo,
            fg_color=COR_FUNDO,
            scrollbar_button_color=COR_DESTAQUE,
        )
        self._frame_lista_grafo.pack(fill="both", expand=True, padx=4, pady=4)

        self._termo_busca_mem = ""
        self._solicitar_carga_memoria_async()

    def _ao_filtrar_memoria(self, _event=None):
        if not hasattr(self, "_entry_busca_mem"):
            return
        self._termo_busca_mem = self._entry_busca_mem.get().strip().lower()
        self._renderizar_memoria_filtrada()

    def _solicitar_carga_memoria_async(self):
        """Busca fatos do SQLite/ClickHouse e triplas do grafo.db em background thread."""
        if hasattr(self, "_lbl_mem_contagem") and self._lbl_mem_contagem.winfo_exists():
            self._lbl_mem_contagem.configure(text="  ⏳  Carregando registros...")

        def _worker():
            fatos: list[dict[str, Any]] = []
            triplas: list[dict[str, Any]] = []
            try:
                if self.memoria is not None:
                    fatos = self.memoria.buscar_todos_fatos() or []
            except Exception as e:
                log.warning(f"Erro ao carregar fatos na GUI: {e}")

            try:
                grafo_obj = self.grafo
                if grafo_obj is None:
                    from cerebro.graph_memory import GraphMemory

                    grafo_obj = GraphMemory()
                    self.grafo = grafo_obj
                if hasattr(grafo_obj, "listar_todas"):
                    triplas = grafo_obj.listar_todas(limite=300) or []
            except Exception as e:
                log.warning(f"Erro ao carregar triplas do grafo na GUI: {e}")

            self._fila_ui_interna.put(
                {"tipo": "_memoria_carregada", "fatos": fatos, "triplas": triplas}
            )

        threading.Thread(target=_worker, daemon=True, name="AliceMemLoad").start()

    def _renderizar_memoria_filtrada(self):
        """Popula as listas de fatos (sem truncamento de 77 chars) e de triplas na GUI."""
        if not (self._janela_memoria and self._janela_memoria.winfo_exists()):
            return

        termo = self._termo_busca_mem

        # 1. Filtra e renderiza fatos
        for widget in self._frame_lista_fatos.winfo_children():
            widget.destroy()
        self._vars_fatos.clear()

        fatos_filtrados = [
            f
            for f in self._fatos_cache
            if not termo
            or termo in str(f.get("fato", "")).lower()
            or termo in str(f.get("criado_em", "")).lower()
        ]

        # 2. Filtra e renderiza triplas do grafo
        for widget in self._frame_lista_grafo.winfo_children():
            widget.destroy()

        triplas_filtradas = [
            t
            for t in self._triplas_cache
            if not termo
            or termo in str(t.get("sujeito", "")).lower()
            or termo in str(t.get("relacao", "")).lower()
            or termo in str(t.get("objeto", "")).lower()
        ]

        self._lbl_mem_contagem.configure(
            text=(
                f"  🧠  Fatos: {len(fatos_filtrados)}/{len(self._fatos_cache)}   •   "
                f"🕸️  Triplas no Grafo: {len(triplas_filtradas)}/{len(self._triplas_cache)}"
            )
        )

        if not fatos_filtrados:
            ctk.CTkLabel(
                self._frame_lista_fatos,
                text="Nenhum fato encontrado.",
                text_color=COR_TEXTO_DIM,
                font=self._fnt_btn_header,
            ).pack(pady=20)
        else:
            for f in fatos_filtrados:
                fid = f["id"]
                var = ctk.BooleanVar(value=False)
                self._vars_fatos[fid] = var

                data = str(f.get("criado_em", ""))[:10]
                texto_completo = f"[{data}]  {f.get('fato', '')}"

                row = ctk.CTkFrame(
                    self._frame_lista_fatos,
                    fg_color=COR_PAINEL,
                    corner_radius=8,
                )
                row.pack(fill="x", padx=6, pady=3)

                cb = ctk.CTkCheckBox(
                    row,
                    text="",
                    variable=var,
                    width=24,
                    fg_color=COR_DESTAQUE,
                    hover_color="#8866ee",
                    checkmark_color="#ffffff",
                )
                cb.pack(side="left", padx=(10, 4), pady=8, anchor="n")

                # Exibe o texto completo com quebra de linha automática (sem truncar em 77 chars)
                lbl_fato = ctk.CTkLabel(
                    row,
                    text=texto_completo,
                    font=self._fnt_item_mem,
                    text_color=COR_TEXTO,
                    wraplength=620,
                    justify="left",
                    anchor="w",
                )
                lbl_fato.pack(
                    side="left", fill="x", expand=True, padx=(2, 10), pady=8
                )

        if not triplas_filtradas:
            ctk.CTkLabel(
                self._frame_lista_grafo,
                text="Nenhuma relação registrada no grafo.",
                text_color=COR_TEXTO_DIM,
                font=self._fnt_btn_header,
            ).pack(pady=20)
        else:
            for t in triplas_filtradas:
                data_t = str(t.get("criado_em", ""))[:10]
                suj = t.get("sujeito", "")
                rel = t.get("relacao", "")
                obj = t.get("objeto", "")
                linha_txt = f"[{data_t}]   {suj}   ──[{rel}]──▶   {obj}"

                card_t = ctk.CTkFrame(
                    self._frame_lista_grafo,
                    fg_color=COR_PAINEL,
                    corner_radius=8,
                )
                card_t.pack(fill="x", padx=6, pady=3)

                ctk.CTkLabel(
                    card_t,
                    text=linha_txt,
                    font=self._fnt_item_mem,
                    text_color=COR_TEXTO,
                    wraplength=650,
                    justify="left",
                    anchor="w",
                ).pack(fill="x", padx=12, pady=7)

    def _selecionar_todos(self):
        for var in self._vars_fatos.values():
            var.set(True)

    def _desselecionar_todos(self):
        for var in self._vars_fatos.values():
            var.set(False)

    def _apagar_selecionados(self):
        if not self.memoria:
            return
        ids = [id_ for id_, var in self._vars_fatos.items() if var.get()]
        if not ids:
            messagebox.showinfo(
                "Memória", "Nenhum fato selecionado.", parent=self._janela_memoria
            )
            return

        # Monta prévia segura dos registros que serão apagados
        fatos_sel = [
            str(f.get("fato", "")) for f in self._fatos_cache if f.get("id") in ids
        ]
        previa = "\n".join(f"• {txt[:90]}" for txt in fatos_sel[:5])
        if len(fatos_sel) > 5:
            previa += f"\n... (+{len(fatos_sel) - 5} outro(s))"

        confirm = messagebox.askyesno(
            "Confirmar Exclusão",
            f"Deseja apagar permanentemente {len(ids)} fato(s) selecionado(s)?\n\n{previa}",
            icon="warning",
            parent=self._janela_memoria,
        )
        if not confirm:
            return

        self._lbl_mem_contagem.configure(text="  ⏳  Removendo fatos selecionados...")

        def _worker():
            for id_ in ids:
                try:
                    self.memoria.remover_fato(id_)
                except Exception as e:
                    log.warning(f"Erro ao remover fato {id_}: {e}")
            self._solicitar_carga_memoria_async()

        threading.Thread(target=_worker, daemon=True, name="AliceMemDelete").start()

    def _limpar_tudo(self):
        if not self.memoria:
            return
        confirm = messagebox.askyesno(
            "⚠️ Limpar Toda a Memória",
            f"Isso vai apagar TODOS os {len(self._fatos_cache)} fatos salvos da Alice.\n\n"
            "Esta ação não pode ser desfeita. Tem certeza?",
            icon="warning",
            parent=self._janela_memoria,
        )
        if not confirm:
            return

        self._lbl_mem_contagem.configure(text="  ⏳  Limpando toda a memória...")

        def _worker():
            total = 0
            try:
                total = self.memoria.limpar_memoria()
            except Exception as e:
                log.warning(f"Erro ao limpar memória: {e}")
            self._fila_ui_interna.put({"tipo": "_memoria_limpa", "total": total})
            self._solicitar_carga_memoria_async()

        threading.Thread(target=_worker, daemon=True, name="AliceMemClear").start()

    # ─── Chat bubbles e Redimensionamento ─────────────────────────────────────

    def _ao_redimensionar_janela(self, event=None):
        """Ajusta dinamicamente o wraplength das mensagens com debounce no resize."""
        if self.root is None or event is None or event.widget is not self.root:
            return
        largura_alvo = max(320, min(760, int(event.width * 0.58)))
        if abs(largura_alvo - self._wraplength_atual) < 24:
            return
        self._wraplength_atual = largura_alvo
        if self._resize_after_id is not None:
            try:
                self.root.after_cancel(self._resize_after_id)
            except Exception:
                pass
        self._resize_after_id = self.root.after(90, self._aplicar_wraplength_bubbles)

    def _aplicar_wraplength_bubbles(self):
        self._resize_after_id = None
        vivos: list[ctk.CTkLabel] = []
        for lbl in self._msg_widgets_wrap:
            if lbl.winfo_exists():
                lbl.configure(wraplength=self._wraplength_atual)
                vivos.append(lbl)
        self._msg_widgets_wrap = vivos

    def _copiar_texto(self, texto: str, btn_ref: ctk.CTkButton | None = None):
        """Copia o texto da mensagem para a área de transferência do sistema."""
        if self.root is None:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(texto)
            if btn_ref is not None and btn_ref.winfo_exists():
                btn_ref.configure(text="✓ Copiado")
                self.root.after(
                    1200,
                    lambda: btn_ref.configure(text="📋 Copiar")
                    if btn_ref.winfo_exists()
                    else None,
                )
        except Exception as e:
            log.debug(f"Falha ao copiar texto para o clipboard: {e}")

    def _adicionar_mensagem(
        self, texto: str, autor: str, categoria: str = "normal"
    ):
        """Enfileira a adição de um balão de mensagem de forma 100% thread-safe."""
        self.fila_saida.put(
            {
                "tipo": "mensagem",
                "autor": autor,
                "texto": texto,
                "categoria": categoria,
            }
        )

    def _inserir_bubble(
        self, texto: str, autor: str, categoria: str = "normal"
    ):
        """Cria o balão visual na área de chat (chamado exclusivamente na thread da GUI)."""
        cat = (categoria or "normal").lower()
        autor_norm = (autor or "Alice").strip()
        is_user = autor_norm.lower() in {"você", "voce", "user", "usuário", "usuario"}

        # Poda automática se exceder o limite visual (evita degradação do Canvas Tk)
        while len(self._bubbles_wrappers) >= MAX_BUBBLES_VISUAIS:
            antigo = self._bubbles_wrappers.pop(0)
            if antigo.winfo_exists():
                antigo.destroy()

        wrapper = ctk.CTkFrame(self.chat_area, fg_color="transparent")
        wrapper.pack(fill="x", pady=3)
        self._bubbles_wrappers.append(wrapper)

        # Estilo visual por categoria e autor
        if is_user:
            side = "right"
            cor_fundo = COR_USER
            cor_borda = COR_USER_BORDA
            autor_txt = "👤 Você"
            fonte_msg = self._fnt_msg
        elif cat == "proativa":
            side = "left"
            cor_fundo = COR_PROATIVA
            cor_borda = COR_PROATIVA_BORDA
            autor_txt = "💜 Alice • Iniciativa"
            fonte_msg = self._fnt_msg
        elif cat == "sonho":
            side = "left"
            cor_fundo = COR_SONHO
            cor_borda = COR_SONHO_BORDA
            autor_txt = "🌙 Alice • Reflexão (Sonho)"
            fonte_msg = self._fnt_msg_italico
        elif cat == "sistema":
            side = "left"
            cor_fundo = COR_SISTEMA
            cor_borda = COR_SISTEMA_BORDA
            autor_txt = "⚙️ Sistema"
            fonte_msg = self._fnt_msg
        else:
            side = "left"
            cor_fundo = COR_ALICE
            cor_borda = COR_ALICE_BORDA
            autor_txt = "🌸 Alice"
            fonte_msg = self._fnt_msg

        bubble = ctk.CTkFrame(
            wrapper,
            fg_color=cor_fundo,
            border_color=cor_borda,
            border_width=1,
            corner_radius=14,
        )
        bubble.pack(
            side=side, padx=(8 if side == "left" else 72, 72 if side == "left" else 8)
        )

        # Linha de cabeçalho do balão: Autor + Hora + Botão Copiar
        header_row = ctk.CTkFrame(bubble, fg_color="transparent")
        header_row.pack(fill="x", padx=12, pady=(6, 0))

        hora = datetime.now().strftime("%H:%M")
        ctk.CTkLabel(
            header_row,
            text=f"{autor_txt}   {hora}",
            font=self._fnt_autor,
            text_color=COR_TEXTO_DIM,
        ).pack(side="left")

        btn_copiar = ctk.CTkButton(
            header_row,
            text="📋 Copiar",
            width=62,
            height=20,
            fg_color="transparent",
            hover_color=COR_PAINEL,
            text_color=COR_TEXTO_DIM,
            font=self._fnt_copiar,
        )
        btn_copiar.configure(
            command=lambda b=btn_copiar, t=texto: self._copiar_texto(t, b)
        )
        btn_copiar.pack(side="right", padx=(10, 0))

        # Corpo da mensagem com wraplength responsivo
        lbl_msg = ctk.CTkLabel(
            bubble,
            text=texto,
            font=fonte_msg,
            text_color=COR_TEXTO,
            wraplength=self._wraplength_atual,
            justify="left",
            anchor="w",
        )
        lbl_msg.pack(anchor="w", padx=12, pady=(2, 8))
        self._msg_widgets_wrap.append(lbl_msg)

        if self.root is not None:
            self.root.after(40, self._scroll_fim)

    def _scroll_fim(self):
        """Move a rolagem para o fim de forma resiliente."""
        canvas = getattr(self.chat_area, "_parent_canvas", None)
        if canvas is not None and hasattr(canvas, "yview_moveto"):
            try:
                canvas.yview_moveto(1.0)
            except Exception:
                pass

    # ─── Enviar mensagem e controle de Input ──────────────────────────────────

    def _enviar(self, _event=None):
        texto = self.entry.get().strip()
        if not texto:
            return
        self.entry.delete(0, "end")
        self._inserir_bubble(texto, "Você", categoria="normal")
        self.fila_entrada.put(texto)
        self._bloquear_input()
        self._atualizar_indicador_estado("pensando")

    def desabilitar_input(self):
        """Thread-safe: enfileira bloqueio do input sem chamar root.after de outra thread."""
        self.fila_saida.put({"tipo": "input", "habilitado": False})

    def _bloquear_input(self):
        self.entry.configure(state="disabled")
        self.btn_enviar.configure(state="disabled", text="⏳ Aguardando...")

    def habilitar_input(self):
        """Thread-safe: enfileira liberação do input sem chamar root.after de outra thread."""
        self.fila_saida.put({"tipo": "input", "habilitado": True})

    def _restaurar_input(self):
        self.entry.configure(state="normal")
        self.btn_enviar.configure(state="normal", text="Enviar ➤")
        self.entry.focus()

    # ─── Processamento de Eventos e Polling Loops ─────────────────────────────

    def _processar_item_saida(self, item: Any):
        """Interpreta tanto o contrato novo em dict quanto strings/tuplas legadas."""
        if isinstance(item, dict):
            tipo = item.get("tipo")
            if tipo == "mensagem":
                autor = str(item.get("autor") or "Alice")
                texto = str(item.get("texto") or "")
                categoria = str(item.get("categoria") or "normal").lower()
                if texto:
                    self._inserir_bubble(texto, autor, categoria=categoria)
                # Só reabilita automaticamente em respostas normais/sistema da Alice
                if autor.lower() == "alice" and categoria in {"normal", "sistema"}:
                    self._restaurar_input()
            elif tipo == "estado":
                self._atualizar_indicador_estado(str(item.get("estado") or "ocioso"))
            elif tipo == "input":
                if bool(item.get("habilitado", True)):
                    self._restaurar_input()
                else:
                    self._bloquear_input()
            return

        if isinstance(item, tuple) and len(item) >= 2:
            autor, texto = str(item[0]), str(item[1])
            cat = str(item[2]) if len(item) >= 3 else "normal"
            self._inserir_bubble(texto, autor, categoria=cat)
            if autor.lower() == "alice" and cat == "normal":
                self._restaurar_input()
            return

        # Fallback legado para strings puras
        msg = str(item)
        if msg.startswith("🎤 Você disse:"):
            texto_usr = msg.replace("🎤 Você disse:", "", 1).strip()
            self._inserir_bubble(texto_usr, "Você", categoria="normal")
            self._bloquear_input()
        elif msg.startswith("💜"):
            self._inserir_bubble(msg, "Alice", categoria="proativa")
        elif msg.startswith("🌙"):
            self._inserir_bubble(msg, "Alice", categoria="sonho")
        elif msg.startswith("🔄"):
            self._inserir_bubble(msg, "Sistema", categoria="sistema")
            self._restaurar_input()
        else:
            self._inserir_bubble(msg, "Alice", categoria="normal")
            self._restaurar_input()

    def _processar_item_interno(self, item: dict[str, Any]):
        """Processa resultados vindos das worker threads internas da GUI."""
        tipo = item.get("tipo")
        if tipo == "_audio_carregado":
            self._aplicar_dispositivos_audio_ui(
                item.get("entradas", []), item.get("saidas", [])
            )
        elif tipo == "_audio_salvo":
            erro = item.get("erro")
            if erro:
                self._lbl_audio_status.configure(
                    text=f"❌ Erro ao salvar: {erro}", text_color="#ff6666"
                )
            else:
                self._lbl_audio_status.configure(
                    text="✅  Salvo! Reinicie a Alice para aplicar.",
                    text_color="#44cc88",
                )
        elif tipo == "_memoria_carregada":
            self._fatos_cache = item.get("fatos", [])
            self._triplas_cache = item.get("triplas", [])
            self._renderizar_memoria_filtrada()
        elif tipo == "_memoria_limpa":
            total = item.get("total", 0)
            if self._janela_memoria and self._janela_memoria.winfo_exists():
                messagebox.showinfo(
                    "Memória",
                    f"✅ {total} fatos apagados. Alice começa do zero.",
                    parent=self._janela_memoria,
                )

    def _ciclo_saida(self):
        """Drena fila_saida e _fila_ui_interna a cada 80ms na thread da GUI."""
        if self._encerrando:
            return
        try:
            while True:
                item = self.fila_saida.get_nowait()
                self._processar_item_saida(item)
        except queue.Empty:
            pass

        try:
            while True:
                interno = self._fila_ui_interna.get_nowait()
                self._processar_item_interno(interno)
        except queue.Empty:
            pass

        if self.root is not None and not self._encerrando:
            self.root.after(80, self._ciclo_saida)

    def _ciclo_status(self):
        """
        Atualiza os indicadores emocionais na barra superior lendo apenas o dict
        em memória (`emotion_engine.estado_atual`), sem abrir SQLite/disco na UI thread.
        """
        if self._encerrando:
            return
        try:
            if self.emotion_engine is not None:
                estado = getattr(self.emotion_engine, "estado_atual", None)
                if not isinstance(estado, dict):
                    estado = {}
                nome = estado.get("nome_usuario") or "—"
                humor = estado.get("humor", "Neutro")
                amizade = estado.get("amizade_com_usuario", 0)
                turnos = estado.get("total_conversas", 0)

                self.lbl_nome.configure(text=f"👤  {nome}")
                self.lbl_humor.configure(text=f"🎭  {humor}")
                self.lbl_amizade.configure(text=f"💙  {amizade}/100")
                self.lbl_turnos.configure(text=f"💬  {turnos} conversas")
        except Exception as e:
            log.debug(f"Erro ao atualizar status bar: {e}")

        if self.root is not None and not self._encerrando:
            self.root.after(1500, self._ciclo_status)


# ─── Ponto de entrada ─────────────────────────────────────────────────────────


def iniciar_janela(
    fila_entrada: queue.Queue,
    fila_saida: queue.Queue,
    emotion_engine_ref: Any = None,
    memoria_ref: Any = None,
    fila_reiniciar: queue.Queue | None = None,
    grafo_ref: Any = None,
) -> JanelaAlice:
    """
    Inicia a janela em uma thread daemon separada.
    Retorna a instância de JanelaAlice.
    """
    janela = JanelaAlice(
        fila_entrada=fila_entrada,
        fila_saida=fila_saida,
        emotion_engine_ref=emotion_engine_ref,
        memoria_ref=memoria_ref,
        fila_reiniciar=fila_reiniciar,
        grafo_ref=grafo_ref,
    )
    t = threading.Thread(target=janela.iniciar, daemon=True, name="AliceGUI")
    t.start()
    return janela
