"""
interface/janela.py — Janela de chat da Alice (customtkinter dark theme)

Roda em uma thread daemon separada, sem bloquear o loop asyncio principal.
Comunicação com o loop principal via duas queues:
  - fila_entrada: texto digitado pelo usuário → processado pela Alice
  - fila_saida: resposta da Alice → exibida na janela
"""

import os
import queue
import threading
import customtkinter as ctk
from datetime import datetime
from tkinter import messagebox
from dotenv import set_key

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
COR_TEXTO = "#e8e8f0"
COR_TEXTO_DIM = "#888899"
COR_DESTAQUE = "#6644bb"
COR_STATUS_BG = "#0a0a1f"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class JanelaAlice:
    """
    Janela de chat com CustomTkinter.
    Thread-safe: recebe mensagens via fila_saida e envia via fila_entrada.
    """

    def __init__(
        self,
        fila_entrada: queue.Queue,
        fila_saida: queue.Queue,
        emotion_engine_ref,  # Referência ao EmotionEngine global
        memoria_ref=None,  # Referência à MemoriaLongaPrazo
        fila_reiniciar: queue.Queue | None = None,  # Sinaliza reinício de sessão
    ):
        self.fila_entrada = fila_entrada
        self.fila_saida = fila_saida
        self.emotion_engine = emotion_engine_ref
        self.memoria = memoria_ref
        self.fila_reiniciar = fila_reiniciar
        self.root: ctk.CTk | None = None
        self._janela_memoria: ctk.CTkToplevel | None = None

    # ─── Ciclo de vida ────────────────────────────────────────────────────────

    def iniciar(self):
        """Ponto de entrada da thread da janela."""
        self.root = ctk.CTk()
        self.root.title("🌸 Alice — Interface de Chat")
        self.root.geometry("860x640")
        self.root.minsize(600, 400)
        self.root.configure(fg_color=COR_FUNDO)
        self.root.protocol("WM_DELETE_WINDOW", self._ao_fechar)

        self._construir_interface()
        self._ciclo_status()
        self._ciclo_saida()

        self.root.mainloop()

    def _ao_fechar(self):
        """Encerra o programa inteiro ao fechar a janela."""
        if messagebox.askyesno(
            "Encerrar Alice",
            "Deseja realmente encerrar a Alice?",
            icon="question",
        ):
            os._exit(0)

    # ─── Construção da UI ─────────────────────────────────────────────────────

    def _construir_interface(self):
        """Monta todos os widgets da janela."""

        # ── Título / Header ──────────────────────────────────────────────────
        frame_header = ctk.CTkFrame(
            self.root, fg_color=COR_STATUS_BG, height=50, corner_radius=0
        )
        frame_header.pack(fill="x")

        ctk.CTkLabel(
            frame_header,
            text="  🌸  Alice  •  Assistente Local",
            font=ctk.CTkFont("Segoe UI", 15, weight="bold"),
            text_color=COR_TEXTO,
        ).pack(side="left", padx=16, pady=10)

        # Botão Encerrar Alice
        ctk.CTkButton(
            frame_header,
            text="⏻ Encerrar",
            command=self._ao_fechar,
            fg_color="#3a0a0a",
            hover_color="#7a1010",
            border_color="#cc2222",
            border_width=1,
            font=ctk.CTkFont("Segoe UI", 12),
            height=30,
            width=110,
        ).pack(side="right", padx=(4, 8), pady=10)

        # Botão de Gerenciar Memória
        ctk.CTkButton(
            frame_header,
            text="🧠 Memória",
            command=self._abrir_gerenciador_memoria,
            fg_color="#1a1a3e",
            hover_color=COR_DESTAQUE,
            border_color="#4444aa",
            border_width=1,
            font=ctk.CTkFont("Segoe UI", 12),
            height=30,
            width=110,
        ).pack(side="right", padx=(0, 4), pady=10)

        # Botão Reiniciar Conversa
        ctk.CTkButton(
            frame_header,
            text="🔄 Reiniciar",
            command=self._reiniciar,
            fg_color="#0d2a1a",
            hover_color="#1a5a32",
            border_color="#22aa55",
            border_width=1,
            font=ctk.CTkFont("Segoe UI", 12),
            height=30,
            width=110,
        ).pack(side="right", padx=(0, 4), pady=10)

        # ── Status bar ───────────────────────────────────────────────────────
        frame_status = ctk.CTkFrame(
            self.root, fg_color=COR_PAINEL, height=38, corner_radius=0
        )
        frame_status.pack(fill="x")

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
            font=ctk.CTkFont("Segoe UI", 13),
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
            font=ctk.CTkFont("Segoe UI", 13, weight="bold"),
            height=40,
            width=110,
        )
        self.btn_enviar.pack(side="right", padx=(0, 12), pady=10)

    # ─── Aba de Áudio ─────────────────────────────────────────────────────────

    def _construir_aba_audio(self, parent):
        """Monta a aba de seleção de dispositivos de áudio."""
        self._audio_vars: dict = {}  # chave → StringVar

        # Enumera dispositivos
        entradas, saidas = self._listar_dispositivos_audio()

        nomes_entradas = [label for label, _ in entradas]
        nomes_saidas = [label for label, _ in saidas]

        # Valores atuais do .env
        mic1_idx = os.getenv("INDICE_MICROFONE", "")
        mic2_idx = os.getenv("INDICE_MICROFONE_2", "")
        saida1_nm = os.getenv("DISPOSITIVO_SAIDA_AUDIO", "")
        saida2_nm = os.getenv("DISPOSITIVO_SAIDA_AUDIO_2", "")

        def _match_entrada(idx_str):
            for label, idx in entradas:
                if idx is not None and str(idx) == idx_str:
                    return label
            return nomes_entradas[0]

        def _match_saida(nome_str):
            for label, nome in saidas:
                if nome and nome_str and nome_str.lower() in nome.lower():
                    return label
            return nomes_saidas[0]

        # ── Layout ────────────────────────────────────────────────────────────
        scroll = ctk.CTkScrollableFrame(
            parent, fg_color=COR_FUNDO, scrollbar_button_color=COR_DESTAQUE
        )
        scroll.pack(fill="both", expand=True, padx=0, pady=0)

        def secao(texto):
            ctk.CTkLabel(
                scroll,
                text=texto,
                font=ctk.CTkFont("Segoe UI", 13, weight="bold"),
                text_color=COR_DESTAQUE,
                anchor="w",
            ).pack(fill="x", padx=20, pady=(18, 4))
            ctk.CTkFrame(
                scroll, fg_color=COR_ALICE_BORDA, height=1, corner_radius=0
            ).pack(fill="x", padx=20, pady=(0, 8))

        def linha(label_txt, var_key, opcoes, valor_inicial):
            frame = ctk.CTkFrame(scroll, fg_color="transparent")
            frame.pack(fill="x", padx=20, pady=5)

            ctk.CTkLabel(
                frame,
                text=label_txt,
                width=180,
                anchor="w",
                font=ctk.CTkFont("Segoe UI", 12),
                text_color=COR_TEXTO,
            ).pack(side="left")

            var = ctk.StringVar(value=valor_inicial)
            self._audio_vars[var_key] = var

            menu = ctk.CTkOptionMenu(
                frame,
                variable=var,
                values=opcoes,
                fg_color=COR_ALICE,
                button_color=COR_DESTAQUE,
                button_hover_color="#8866ee",
                text_color=COR_TEXTO,
                dropdown_fg_color=COR_PAINEL,
                dropdown_text_color=COR_TEXTO,
                dropdown_hover_color=COR_DESTAQUE,
                font=ctk.CTkFont("Segoe UI", 12),
                width=400,
            )
            menu.pack(side="left", padx=(8, 0))

        # Entradas
        secao("🎤  Entradas de Áudio  (Microfone)")
        linha(
            "Entrada 1  (principal)", "mic1", nomes_entradas, _match_entrada(mic1_idx)
        )
        linha(
            "Entrada 2  (secundária)", "mic2", nomes_entradas, _match_entrada(mic2_idx)
        )

        # Saídas
        secao("🔊  Saídas de Áudio  (TTS / Voz da Alice)")
        linha("Saída 1  (principal)", "saida1", nomes_saidas, _match_saida(saida1_nm))
        linha("Saída 2  (secundária)", "saida2", nomes_saidas, _match_saida(saida2_nm))

        # Botão salvar + status
        frame_footer = ctk.CTkFrame(scroll, fg_color="transparent")
        frame_footer.pack(fill="x", padx=20, pady=(20, 8))

        self._lbl_audio_status = ctk.CTkLabel(
            frame_footer,
            text="",
            font=ctk.CTkFont("Segoe UI", 11),
            text_color=COR_TEXTO_DIM,
        )
        self._lbl_audio_status.pack(side="left")

        ctk.CTkButton(
            frame_footer,
            text="💾  Salvar configuração",
            command=lambda: self._salvar_config_audio(entradas, saidas),
            fg_color=COR_DESTAQUE,
            hover_color="#8866ee",
            font=ctk.CTkFont("Segoe UI", 13, weight="bold"),
            height=38,
            width=210,
        ).pack(side="right")

        ctk.CTkLabel(
            scroll,
            text="⚠️  Reinicie a Alice para que as alterações tenham efeito.",
            font=ctk.CTkFont("Segoe UI", 10),
            text_color=COR_TEXTO_DIM,
        ).pack(pady=(0, 16))

    def _listar_dispositivos_audio(self):
        """Retorna (entradas, saidas) como listas de (label, valor)."""
        try:
            import pyaudiowpatch as pyaudio

            pa = pyaudio.PyAudio()
            entradas = [("— Nenhum —", None)]
            saidas = [("— Nenhum —", None)]
            for i in range(pa.get_device_count()):
                d = pa.get_device_info_by_index(i)
                nome = d["name"]
                if d["maxInputChannels"] > 0:
                    entradas.append((f"[{i}]  {nome}", i))
                if d["maxOutputChannels"] > 0:
                    saidas.append((nome, nome))
            pa.terminate()
        except Exception:
            entradas = [("— Erro ao listar dispositivos —", None)]
            saidas = [("— Erro ao listar dispositivos —", None)]
        return entradas, saidas

    def _salvar_config_audio(self, entradas, saidas):
        """Salva a seleção de dispositivos no .env."""

        def _idx_de(label):
            for lbl, idx in entradas:
                if lbl == label:
                    return str(idx) if idx is not None else ""
            return ""

        def _nome_de(label):
            for lbl, nome in saidas:
                if lbl == label:
                    return nome or ""
            return ""

        mic1 = _idx_de(self._audio_vars["mic1"].get())
        mic2 = _idx_de(self._audio_vars["mic2"].get())
        saida1 = _nome_de(self._audio_vars["saida1"].get())
        saida2 = _nome_de(self._audio_vars["saida2"].get())

        set_key(_ENV_PATH, "INDICE_MICROFONE", mic1)
        set_key(_ENV_PATH, "INDICE_MICROFONE_2", mic2)
        set_key(_ENV_PATH, "DISPOSITIVO_SAIDA_AUDIO", saida1)
        set_key(_ENV_PATH, "DISPOSITIVO_SAIDA_AUDIO_2", saida2)

        self._lbl_audio_status.configure(
            text="✅  Salvo! Reinicie a Alice para aplicar.",
            text_color="#44cc88",
        )

    def _status_label(self, parent, texto: str) -> ctk.CTkLabel:
        lbl = ctk.CTkLabel(
            parent,
            text=texto,
            font=ctk.CTkFont("Segoe UI", 11),
            text_color=COR_TEXTO_DIM,
        )
        lbl.pack(side="left", padx=14, pady=6)
        return lbl

    # ─── Reiniciar Conversa ───────────────────────────────────────────────────

    def _reiniciar(self):
        """Limpa o chat na GUI e sinaliza ao loop principal para zerar o histórico."""
        confirm = messagebox.askyesno(
            "Reiniciar Conversa",
            "Deseja reiniciar a conversa?\n\nO histórico desta sessão será apagado.",
            icon="question",
        )
        if not confirm:
            return

        # Limpa os balões da área de chat
        for widget in self.chat_area.winfo_children():
            widget.destroy()

        # Sinaliza ao loop asyncio para limpar HISTORICO_SESSAO
        if self.fila_reiniciar is not None:
            self.fila_reiniciar.put(True)

        # Mensagem de boas-vindas após reinício
        self._inserir_bubble("Conversa reiniciada! Como posso te ajudar? 🌸", "Alice")

    # ─── Gerenciador de Memória ───────────────────────────────────────────────

    def _abrir_gerenciador_memoria(self):
        """Abre (ou traz ao foco) a janela de gerenciamento de memória."""
        # Evita abrir duas janelas
        if self._janela_memoria and self._janela_memoria.winfo_exists():
            self._janela_memoria.focus()
            return

        if not self.memoria:
            messagebox.showinfo("Memória", "Módulo de memória não disponível.")
            return

        win = ctk.CTkToplevel(self.root)
        win.title("🧠 Gerenciador de Memória da Alice")
        win.geometry("680x520")
        win.configure(fg_color=COR_FUNDO)
        win.grab_set()  # modal — foca nesta janela
        self._janela_memoria = win

        # ── Header ────────────────────────────────────────────────────────────
        frame_top = ctk.CTkFrame(win, fg_color=COR_STATUS_BG, corner_radius=0)
        frame_top.pack(fill="x")
        self._lbl_mem_contagem = ctk.CTkLabel(
            frame_top,
            text="",
            font=ctk.CTkFont("Segoe UI", 13, weight="bold"),
            text_color=COR_TEXTO,
        )
        self._lbl_mem_contagem.pack(side="left", padx=16, pady=10)

        # ── Lista de fatos (scrollable) ───────────────────────────────────────
        frame_lista = ctk.CTkScrollableFrame(
            win,
            fg_color=COR_PAINEL,
            scrollbar_button_color=COR_DESTAQUE,
        )
        frame_lista.pack(fill="both", expand=True, padx=12, pady=(8, 4))

        # id_fato → checkbox
        self._checkboxes: dict[int, ctk.CTkCheckBox] = {}
        self._vars: dict[int, ctk.BooleanVar] = {}  # id_fato → var
        self._frame_lista = frame_lista

        self._carregar_fatos_gui()

        # ── Footer com botões ─────────────────────────────────────────────────
        frame_footer = ctk.CTkFrame(win, fg_color=COR_PAINEL, corner_radius=0)
        frame_footer.pack(fill="x", pady=(0, 0))

        ctk.CTkButton(
            frame_footer,
            text="☑ Todos",
            command=self._selecionar_todos,
            fg_color="#1a1a3e",
            hover_color="#2a2a5e",
            border_color="#4444aa",
            border_width=1,
            font=ctk.CTkFont("Segoe UI", 12),
            width=110,
        ).pack(side="left", padx=(12, 4), pady=10)

        ctk.CTkButton(
            frame_footer,
            text="☐ Nenhum",
            command=self._desselecionar_todos,
            fg_color="#1a1a3e",
            hover_color="#2a2a5e",
            border_color="#4444aa",
            border_width=1,
            font=ctk.CTkFont("Segoe UI", 12),
            width=110,
        ).pack(side="left", padx=4, pady=10)

        ctk.CTkButton(
            frame_footer,
            text="🗑️ Apagar selecionados",
            command=self._apagar_selecionados,
            fg_color="#3a1a1a",
            hover_color="#6a1a1a",
            border_color="#aa4444",
            border_width=1,
            font=ctk.CTkFont("Segoe UI", 12, weight="bold"),
            width=170,
        ).pack(side="right", padx=(4, 4), pady=10)

        ctk.CTkButton(
            frame_footer,
            text="💣 Limpar tudo",
            command=self._limpar_tudo,
            fg_color="#4a0a0a",
            hover_color="#7a1010",
            border_color="#cc2222",
            border_width=1,
            font=ctk.CTkFont("Segoe UI", 12, weight="bold"),
            width=130,
        ).pack(side="right", padx=(4, 12), pady=10)

    def _carregar_fatos_gui(self):
        """Popula (ou recarrega) a lista de fatos com checkboxes."""
        # Limpa widgets existentes
        for widget in self._frame_lista.winfo_children():
            widget.destroy()
        self._checkboxes.clear()
        self._vars.clear()

        fatos = self.memoria.buscar_todos_fatos()
        self._lbl_mem_contagem.configure(
            text=f"  🧠  Memória da Alice   ({len(fatos)} fatos)"
        )

        if not fatos:
            ctk.CTkLabel(
                self._frame_lista,
                text="Nenhum fato salvo ainda.",
                text_color=COR_TEXTO_DIM,
                font=ctk.CTkFont("Segoe UI", 12),
            ).pack(pady=20)
            return

        for f in fatos:
            var = ctk.BooleanVar(value=False)
            self._vars[f["id"]] = var

            data = f["criado_em"][:10]
            texto = f"[{data}]  {f['fato']}"
            if len(texto) > 80:
                texto = texto[:77] + "..."

            cb = ctk.CTkCheckBox(
                self._frame_lista,
                text=texto,
                variable=var,
                text_color=COR_TEXTO,
                font=ctk.CTkFont("Segoe UI", 11),
                fg_color=COR_DESTAQUE,
                hover_color="#8866ee",
                checkmark_color="#ffffff",
            )
            cb.pack(anchor="w", padx=10, pady=2)
            self._checkboxes[f["id"]] = cb

    def _selecionar_todos(self):
        for var in self._vars.values():
            var.set(True)

    def _desselecionar_todos(self):
        for var in self._vars.values():
            var.set(False)

    def _apagar_selecionados(self):
        ids = [id_ for id_, var in self._vars.items() if var.get()]
        if not ids:
            messagebox.showinfo(
                "Memória", "Nenhum fato selecionado.", parent=self._janela_memoria
            )
            return
        confirm = messagebox.askyesno(
            "Confirmar",
            f"Apagar {len(ids)} fato(s) selecionado(s)?",
            parent=self._janela_memoria,
        )
        if confirm:
            for id_ in ids:
                self.memoria.remover_fato(id_)
            self._carregar_fatos_gui()

    def _limpar_tudo(self):
        confirm = messagebox.askyesno(
            "⚠️ Limpar Tudo",
            "Isso vai apagar TODA a memória da Alice.\n\nTem certeza?",
            icon="warning",
            parent=self._janela_memoria,
        )
        if confirm:
            total = self.memoria.limpar_memoria()
            messagebox.showinfo(
                "Memória",
                f"✅ {total} fatos apagados. Alice começa do zero.",
                parent=self._janela_memoria,
            )
            self._carregar_fatos_gui()

    # ─── Chat bubbles ─────────────────────────────────────────────────────────

    def _adicionar_mensagem(self, texto: str, autor: str):
        """Adiciona um balão de mensagem thread-safe via after()."""
        if self.root is None:
            return
        self.root.after(0, self._inserir_bubble, texto, autor)

    def _inserir_bubble(self, texto: str, autor: str):
        is_alice = autor == "Alice"

        # Container externo para alinhar
        wrapper = ctk.CTkFrame(self.chat_area, fg_color="transparent")
        wrapper.pack(fill="x", pady=2)

        # Lado do balão
        side = "left" if is_alice else "right"
        cor_fundo = COR_ALICE if is_alice else COR_USER
        cor_borda = COR_ALICE_BORDA if is_alice else COR_USER_BORDA
        autor_txt = "🌸 Alice" if is_alice else "👤 Você"

        bubble = ctk.CTkFrame(
            wrapper,
            fg_color=cor_fundo,
            border_color=cor_borda,
            border_width=1,
            corner_radius=14,
        )
        bubble.pack(side=side, padx=(8 if is_alice else 80, 80 if is_alice else 8))

        # Autor + hora
        hora = datetime.now().strftime("%H:%M")
        ctk.CTkLabel(
            bubble,
            text=f"{autor_txt}  {hora}",
            font=ctk.CTkFont("Segoe UI", 9),
            text_color=COR_TEXTO_DIM,
        ).pack(anchor="w", padx=12, pady=(6, 0))

        # Texto
        ctk.CTkLabel(
            bubble,
            text=texto,
            font=ctk.CTkFont("Segoe UI", 12),
            text_color=COR_TEXTO,
            wraplength=460,
            justify="left",
        ).pack(anchor="w", padx=12, pady=(2, 8))

        # Auto-scroll para o fim
        self.root.after(50, self._scroll_fim)

    def _scroll_fim(self):
        self.chat_area._parent_canvas.yview_moveto(1.0)

    # ─── Enviar mensagem ──────────────────────────────────────────────────────

    def _enviar(self, event=None):
        texto = self.entry.get().strip()
        if not texto:
            return
        self.entry.delete(0, "end")
        self._inserir_bubble(texto, "Você")
        self.fila_entrada.put(texto)
        # Feedback visual — desabilita input enquanto Alice processa
        self.entry.configure(state="disabled")
        self.btn_enviar.configure(state="disabled", text="⏳ Aguardando...")

    def habilitar_input(self):
        """Chamado após a Alice responder (via root.after thread-safe)."""
        if self.root:
            self.root.after(0, self._restaurar_input)

    def _restaurar_input(self):
        self.entry.configure(state="normal")
        self.btn_enviar.configure(state="normal", text="Enviar ➤")
        self.entry.focus()

    # ─── Polling loops ────────────────────────────────────────────────────────

    def _ciclo_saida(self):
        """Verifica a fila de mensagens da Alice a cada 100ms."""
        try:
            while True:
                msg = self.fila_saida.get_nowait()
                self._inserir_bubble(msg, "Alice")
                self.habilitar_input()
        except queue.Empty:
            pass
        if self.root:
            self.root.after(100, self._ciclo_saida)

    def _ciclo_status(self):
        """Atualiza o status bar com dados do brain.json a cada 2s."""
        try:
            estado = self.emotion_engine.ler_estado()
            nome = estado.get("nome_usuario") or "—"
            humor = estado.get("humor", "Neutro")
            amizade = estado.get("amizade_com_usuario", 0)
            turnos = estado.get("total_conversas", 0)

            self.lbl_nome.configure(text=f"👤  {nome}")
            self.lbl_humor.configure(text=f"🎭  {humor}")
            self.lbl_amizade.configure(text=f"💙  {amizade}/100")
            self.lbl_turnos.configure(text=f"💬  {turnos} conversas")
        except Exception:
            pass

        if self.root:
            self.root.after(2000, self._ciclo_status)


# ─── Ponto de entrada ─────────────────────────────────────────────────────────


def iniciar_janela(
    fila_entrada: queue.Queue,
    fila_saida: queue.Queue,
    emotion_engine_ref,
    memoria_ref=None,
    fila_reiniciar: queue.Queue | None = None,
) -> JanelaAlice:
    """
    Inicia a janela em uma thread daemon separada.
    Retorna a instância para que o loop principal possa chamar habilitar_input().
    """
    janela = JanelaAlice(
        fila_entrada, fila_saida, emotion_engine_ref, memoria_ref, fila_reiniciar
    )
    t = threading.Thread(target=janela.iniciar, daemon=True, name="AliceGUI")
    t.start()
    return janela
