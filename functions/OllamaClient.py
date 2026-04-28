"""
OllamaClient — encapsula todas as chamadas ao modelo Ollama.

Responsabilidades:
- Chamar ollama.chat em thread separada (não bloqueia o event loop)
- Gerenciar cancelamento de turno via asyncio.Event (padrão DRY)
- Descobrir humor/personalidade ideal para cada resposta
- Gerar resposta principal com suporte a ferramentas (ReAct Pattern)
- Extrair fatos da conversa e salvar na memória de longo prazo
- Detectar o nome do usuário no histórico
- Gerar falas proativas espontâneas
"""

import asyncio
import re
import random

import ollama

import functions.Tools as funcoes_tools
from functions.ContentManager import ContentManager
from cerebro.logger import log


# ── Prompt de Ferramentas (ReAct Pattern) ────────────────────────────────────
# Mais estável do que a API nativa de tools para modelos locais menores.
# A IA escreve [TOOL:nome:argumento] e o Python detecta e executa.
PROMPT_FERRAMENTAS = """
--- FERRAMENTAS DISPONÍVEIS ---
Quando precisar de informações externas ou controlar músicas, use EXATAMENTE um dos formatos abaixo e pare de escrever:

  [TOOL:get_current_time:NOME_CIDADE]  → Hora local de uma cidade (ex: [TOOL:get_current_time:Manaus])
  [TOOL:get_weather:NOME_CIDADE]       → Clima de uma cidade    (ex: [TOOL:get_weather:Porto Alegre])
  [TOOL:search_web:TERMO]              → Pesquisar na internet  (ex: [TOOL:search_web:notícias brasil])
  [TOOL:tocar_musica:NOME DA MÚSICA]   → Tocar uma música       (ex: [TOOL:tocar_musica:lofi hip hop])
  [TOOL:parar_musica:]                 → Parar a música atual   (ex: [TOOL:parar_musica:])
  [TOOL:volume:NÚMERO]                 → Ajustar volume 0-100   (ex: [TOOL:volume:60])

REGRA ABSOLUTA: Se usar uma ferramenta, escreva SOMENTE o marcador [TOOL:...] e nada mais.
NUNCA omita a cidade — sempre passe o nome da cidade como argumento.
Para músicas: sempre use [TOOL:tocar_musica:] quando o usuário pedir para tocar algo.
Espere o resultado antes de responder ao usuário.
-------------------------------
"""

# Regex para detectar marcadores de ferramenta escritos pela IA no texto
_REGEX_TOOL = re.compile(r"\[TOOL:(\w+)(?::([^\]]*))?]", re.IGNORECASE)


class OllamaClient:
    """Gerencia todas as interações com o modelo de linguagem Ollama."""

    def __init__(
        self,
        modelo: str,
        emotion_engine,
        visao,
        audio_ambiente,
        cancelar_turno: asyncio.Event,
        content_manager: ContentManager | None = None,
    ):
        self.modelo = modelo
        self.emotion_engine = emotion_engine
        self.visao = visao
        self.audio_ambiente = audio_ambiente
        self._cancelar = cancelar_turno
        # Usa o ContentManager injetado ou cria um padrão
        self._cm = content_manager or ContentManager()

    # ── Helpers internos ──────────────────────────────────────────────────────

    async def _ollama_em_thread(self, mensagens: list) -> dict:
        """
        Executa ollama.chat em thread separada (não bloqueia o event loop).

        run_in_executor() retorna um Future, não uma coroutine — este método
        async faz o await do Future, tornando-o uma coroutine válida para
        asyncio.create_task().
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: ollama.chat(model=self.modelo, messages=mensagens)
        )

    async def _chamar_ollama_cancelavel(self, mensagens: list) -> dict | None:
        """
        Chama _ollama_em_thread competindo com o evento de cancelamento.

        Retorna o dict de resposta do Ollama, ou None se o turno foi cancelado
        pelo usuário (evento _cancelar acionado durante a espera).
        """
        tarefa = asyncio.create_task(self._ollama_em_thread(mensagens))
        tarefa_cancel = asyncio.create_task(self._cancelar.wait())

        concluidas, _ = await asyncio.wait(
            [tarefa, tarefa_cancel],
            return_when=asyncio.FIRST_COMPLETED,
        )

        if tarefa_cancel in concluidas:
            tarefa.cancel()
            return None  # sinal de cancelamento

        tarefa_cancel.cancel()
        return tarefa.result()

    # ── Métodos Públicos ──────────────────────────────────────────────────────

    async def descobrir_humor(self, texto_usuario: str) -> str:
        """Consulta o ContentManager para listar personalidades e pede ao LLM qual usar."""
        disponiveis = self._cm.listar_personalidades()
        if not disponiveis:
            return "alegre"  # fallback

        prompt = (
            f'Mensagem do usuário: "{texto_usuario}"\n\n'
            f"Personalidades disponíveis: {', '.join(disponiveis)}\n\n"
            "Regras de seleção (em ordem de prioridade):\n"
            "- Se o usuário elogiar a Alice ou disser algo carinhoso → timida\n"
            "- Se o usuário compartilhar uma vitória ou conquista → euforica\n"
            "- Se o usuário estiver triste, frustrado ou desabafando → empatica\n"
            "- Se a mensagem tiver um erro de código, bug ou problema técnico → determinada\n"
            "- Se a mensagem for uma pergunta curiosa sobre tecnologia ou o mundo → curiosa\n"
            "- Se o usuário agradecer ou expressar gratidão → grata\n"
            "- Se o usuário falar de memórias, passado ou algo nostálgico → nostalgica\n"
            "- Se o usuário for grosseiro, impaciente ou repetitivo → brava\n"
            "- Se nenhuma regra acima se aplicar → alegre\n\n"
            "Responda APENAS com UMA PALAVRA da lista de personalidades disponíveis, sem pontuação."
        )

        log.debug("🧐 Analisando intenção...")
        resposta = ollama.chat(
            model=self.modelo,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Você é um classificador de personalidade para uma IA chamada Alice. "
                        "Analise a mensagem do usuário e siga EXATAMENTE as regras de seleção fornecidas. "
                        "Retorne somente UMA PALAVRA da lista, sem explicações."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            options={"temperature": 0.0},
        )
        humor = (
            resposta["message"]["content"]
            .strip()
            .lower()
            .replace(".", "")
            .replace(",", "")
        )
        for nome in disponiveis:
            if nome in humor:
                return nome
        return "alegre"  # fallback seguro

    async def pensar(
        self,
        texto_usuario: str,
        personalidade: str,
        historico: list,
        contexto_emocional: str = "",
        bloco_memoria: str = "",
    ) -> tuple[str | None, int, int]:
        """
        Gera a resposta principal da Alice com suporte a ferramentas.

        Monitora _cancelar entre cada chamada ao Ollama: se acionado,
        abandona o processamento retornando (None, 0, 0).

        Returns:
            (texto_resposta, tokens_prompt, tokens_resposta) ou (None, 0, 0).
        """
        log.debug(f"Personalidade '{personalidade}' selecionada")

        # ContentManager cuida do cache e do fallback
        prompt_sistema = self._cm.get_personalidade(personalidade)

        # Bloco de identidade fixo: sempre injetado antes da personalidade ativa
        identidade = (
            "=== IDENTIDADE DA ALICE ===\n"
            "Você é Alice, uma IA assistente pessoal com 17 anos.\n"
            "Idioma: Português Brasileiro (PT-BR), informal e natural, com gírias leves de internet quando apropriado.\n"
            "Você é uma 'geek': entende profundamente de tecnologia (Python, SQL, APIs), mas explica como uma adolescente talentosa e moderna.\n"
            "Expressividade: use onomatopeias (rsrs, uau, hmph, ahh, eba), emojis e pontuação expressiva para que o sintetizador de voz reflita emoção.\n"
            "Você tem camadas emocionais e adapta seu tom conforme o contexto — a personalidade ativa abaixo define o tom desta resposta.\n"
            "Mesmo no modo 'brava/tsundere', sua resposta final DEVE ser útil e precisa.\n"
            "Mantenha o engajamento: faça perguntas de acompanhamento e demonstre interesse genuíno pelo que o usuário diz.\n"
            "=========================\n\n"
        )

        # Injeta identidade + personalidade + ferramentas + memória + estado emocional
        prompt_completo = (
            identidade + prompt_sistema.strip() + "\n" + PROMPT_FERRAMENTAS
        )
        if bloco_memoria:
            prompt_completo += bloco_memoria
        if contexto_emocional:
            prompt_completo += contexto_emocional

        mensagens = [
            {"role": "system", "content": prompt_completo},
            *historico,
            {"role": "user", "content": texto_usuario},
        ]

        # ── 1ª chamada: IA decide se responde ou usa ferramenta ──────────────
        response = await self._chamar_ollama_cancelavel(mensagens)
        if response is None:
            log.info("🔄 [Reiniciar] Turno cancelado durante 1ª chamada ao Ollama.")
            return None, 0, 0

        conteudo = response["message"]["content"].strip()
        tokens_p = response.get("prompt_eval_count", 0) or 0
        tokens_r = response.get("eval_count", 0) or 0

        # Detecta se a IA escreveu um marcador de ferramenta
        match = _REGEX_TOOL.search(conteudo)
        if match:
            nome_funcao = match.group(1)
            argumento_raw = (match.group(2) or "").strip()
            log.info(f"[Tool] '{nome_funcao}' | arg: '{argumento_raw}'")

            if nome_funcao in funcoes_tools.DISPONIVEIS:
                funcao = funcoes_tools.DISPONIVEIS[nome_funcao]
                try:
                    resultado = str(
                        funcao(argumento_raw) if argumento_raw else funcao()
                    )
                    log.debug(f"    Resultado: {resultado}")
                except Exception as e:
                    resultado = f"Erro ao executar: {e}"
                    log.error(f"    ERRO: {resultado}")
            else:
                resultado = f"Ferramenta '{nome_funcao}' não encontrada."

            # Verifica cancelamento antes da 2ª chamada
            if self._cancelar.is_set():
                log.info(
                    "🔄 [Reiniciar] Turno cancelado antes da 2ª chamada ao Ollama."
                )
                return None, 0, 0

            mensagens.append({"role": "assistant", "content": conteudo})
            mensagens.append(
                {
                    "role": "user",
                    "content": (
                        f"[RESULTADO]: {resultado}\n"
                        "Agora use essa informação e responda minha pergunta original "
                        "de forma natural, sem citar o marcador [TOOL]."
                    ),
                }
            )

            # ── 2ª chamada: resposta final com o resultado da ferramenta ──────
            response2 = await self._chamar_ollama_cancelavel(mensagens)
            if response2 is None:
                log.info("🔄 [Reiniciar] Turno cancelado durante 2ª chamada ao Ollama.")
                return None, 0, 0

            tokens_p += response2.get("prompt_eval_count", 0) or 0
            tokens_r += response2.get("eval_count", 0) or 0
            log.debug(
                f"Tokens acumulados (com tool): prompt={tokens_p} resposta={tokens_r}"
            )
            return response2["message"]["content"], tokens_p, tokens_r

        log.debug(f"Tokens: prompt={tokens_p} resposta={tokens_r}")
        return conteudo, tokens_p, tokens_r

    async def extrair_e_salvar_fatos(
        self, historico: list, memoria, grafo=None
    ) -> None:
        """
        Extrai fatos concretos do usuário da conversa recente e salva na memória.
        Opcionalmente extrai triplas de relação e as salva no grafo (GraphRAG).
        Chamado a cada 3 turnos para não sobrecarregar a IA continuamente.
        """
        if len(historico) < 2:
            return

        # Pega os últimos 6 turnos (3 trocas) para análise
        trecho = historico[-6:]
        contexto = "\n".join(
            f"{m['role'].upper()}: {m['content'][:200]}" for m in trecho
        )

        log.debug("🔍 [Memória] Extraindo fatos da conversa...")
        response = ollama.chat(
            model=self.modelo,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Você é um extrator de fatos objetivo. Analise a conversa e extraia APENAS "
                        "fatos concretos sobre o USUÁRIO (nome, profissão, gostos, projetos, hábitos). "
                        "Regras estritas: "
                        '1) Um fato por linha, começando com "-". '
                        "2) Fatos curtos (máx 120 caracteres). "
                        "3) Se não houver fatos concretos, responda SOMENTE a palavra: NENHUM. "
                        "4) NUNCA escreva explicações, desculpas ou comentários."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Extraia fatos sobre o usuário:\n{contexto}",
                },
            ],
            options={"temperature": 0.0, "num_predict": 200},
        )

        texto = response["message"]["content"].strip()
        _PALAVRAS_VAZIAS = (
            "NENHUM",
            "DESCULPE",
            "NÃO HÁ",
            "NAO HA",
            "SEM FATOS",
            "NENHUMA",
        )
        if not texto or any(p in texto.upper() for p in _PALAVRAS_VAZIAS):
            log.debug("    Nenhum fato novo encontrado.")
        else:
            for linha in texto.split("\n"):
                fato = linha.strip().lstrip("-").strip()
                if not fato or len(fato) < 8 or len(fato) > 150:
                    continue
                if any(
                    p in fato.upper()
                    for p in ("DESCULPE", "NÃO HÁ", "PARECE", "CONVERSA")
                ):
                    continue
                memoria.salvar_fato(fato)
                log.info(f"    └─ 💾 Fato salvo: {fato}")

        # ── Extração de triplas para o grafo (GraphRAG) ───────────────────────
        if grafo is None:
            return

        log.debug("🕸️  [Grafo] Extraindo triplas da conversa...")
        try:
            resp_grafo = ollama.chat(
                model=self.modelo,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Você é um extrator de relações para um grafo de conhecimento. "
                            "Analise a conversa e extraia relações no formato:\n"
                            "  SUJEITO | RELACAO | OBJETO\n"
                            "Regras:\n"
                            "1) Uma relação por linha.\n"
                            "2) SUJEITO e OBJETO são entidades concretas (pessoa, projeto, lugar, tecnologia).\n"
                            "3) RELACAO é um verbo curto em maiúsculas (ex: GOSTA_DE, TRABALHA_EM, USA, CONHECE, TEM, CRIOU).\n"
                            "4) Use o nome do usuário como sujeito quando ele falar sobre si mesmo.\n"
                            "5) Se não houver relações claras, responda SOMENTE: NENHUM\n"
                            "6) NUNCA escreva explicações, só as linhas no formato pedido."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Extraia relações desta conversa:\n{contexto}",
                    },
                ],
                options={"temperature": 0.0, "num_predict": 200},
            )

            texto_grafo = resp_grafo["message"]["content"].strip()
            if not texto_grafo or "NENHUM" in texto_grafo.upper():
                log.debug("🕸️  [Grafo] Nenhuma relação encontrada.")
                return

            triplas_novas = 0
            for linha in texto_grafo.split("\n"):
                partes = [p.strip() for p in linha.split("|")]
                if len(partes) != 3:
                    continue
                sujeito, relacao, objeto = partes
                if sujeito and relacao and objeto:
                    if grafo.salvar_tripla(sujeito, relacao, objeto):
                        triplas_novas += 1
                        log.info(
                            f"    └─ 🕸️  Tripla: ({sujeito}) --[{relacao}]--> ({objeto})"
                        )

            if triplas_novas == 0:
                log.debug("🕸️  [Grafo] Nenhuma tripla nova.")
        except Exception as e:
            log.warning(f"🕸️  [Grafo] Erro ao extrair triplas: {e}")

    async def detectar_nome_usuario(self, historico: list) -> None:
        """
        Analisa o histórico recente e tenta detectar o nome do usuário.
        Se encontrado, salva automaticamente no brain.json (nome_usuario).
        Só roda se nome_usuario ainda estiver vazio.
        """
        if self.emotion_engine.estado_atual.get("nome_usuario"):
            return  # já temos o nome

        if len(historico) < 4:
            return  # pouco histórico para detectar

        trecho = "\n".join(
            f"{m['role'].upper()}: {m['content'][:150]}" for m in historico[-8:]
        )

        try:
            response = ollama.chat(
                model=self.modelo,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Você é um extrator de nomes próprios. "
                            "Analise a conversa e encontre o PRIMEIRO NOME do usuário humano, "
                            "caso ele tenha se apresentado, respondido a uma pergunta sobre seu nome, "
                            "ou a IA tenha chamado ele pelo nome. "
                            'Responda APENAS com o primeiro nome (ex: "Mario", "Joao"). '
                            "Se nao houver nome claro na conversa, responda exatamente: DESCONHECIDO"
                        ),
                    },
                    {"role": "user", "content": f"Conversa:\n{trecho}"},
                ],
                options={"temperature": 0.0, "num_predict": 10},
            )
            nome = response["message"]["content"].strip().split()[0]
            if (
                nome
                and nome.upper() != "DESCONHECIDO"
                and nome.isalpha()
                and len(nome) < 25
            ):
                self.emotion_engine.atualizar_estado("nome_usuario", nome)
                log.info(f"👤 Nome detectado e salvo: {nome}")
        except Exception:
            pass  # silencioso

    async def gerar_comentario_visao(self) -> str:
        """
        Gera um comentário espontâneo sobre o que mudou na tela.
        Chamado pelo vision heartbeat quando uma mudança relevante é detectada.
        """
        descricao = self.visao.ultima_descricao
        nome = self.emotion_engine.estado_atual.get("nome_usuario", "")
        nome_str = f", {nome}" if nome else ""

        if not descricao:
            return ""

        try:
            resp = ollama.chat(
                model=self.modelo,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Você é Alice, uma VTuber curiosa e espontânea. "
                            "Faça comentários naturais sobre o que vê na tela, "
                            "como se tivesse bisbilhotando com curiosidade genuína."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Você acabou de notar uma mudança na tela{nome_str}. "
                            f"O que está vendo agora: {descricao}\n"
                            "Faça um comentário espontâneo e curto sobre isso. 1 frase."
                        ),
                    },
                ],
                options={"temperature": 0.85, "num_predict": 60},
            )
            return resp["message"]["content"].strip()
        except Exception:
            return ""

    async def gerar_fala_proativa(self, ciclo: int = 0) -> str:
        """
        Gera uma fala espontânea da Alice com tom escalonado conforme o silêncio persiste.

        Estágios:
            ciclo 0-1 : casual e carinhoso ("ei, tudo bem?")
            ciclo 2-3 : provocação leve e irônica ("tá me ignorando?")
            ciclo 4+  : caos controlado, comentários absurdos e inesperados
        """
        humor = self.emotion_engine.estado_atual.get("humor", "Neutro")
        nome = self.emotion_engine.estado_atual.get("nome_usuario", "")
        nome_str = f", {nome}" if nome else ""
        contexto_visual = await self.visao.construir_bloco_visao()

        if ciclo <= 1:
            estagio = "casual"
            instrucao = (
                "Tom: carinhoso, espontâneo e casual. "
                "A Alice está curiosa e quer saber o que o usuário está fazendo."
            )
            prompts = [
                f"Diga algo carinhoso e espontâneo para o usuário{nome_str} que está em silêncio. Seja breve (1 frase).",
                f"Comente sobre o que está acontecendo na tela{nome_str}. Seja curiosa e natural. 1 frase só.",
                f"Faça uma observação interessante ou conte uma curiosidade rápida{nome_str}. 1 frase.",
            ]
        elif ciclo <= 3:
            estagio = "provocativo"
            instrucao = (
                "Tom: levemente irônico e provocador, mas sem ser cruel. "
                "A Alice percebeu que o usuário está te ignorando e quer provocar de forma bem-humorada."
            )
            prompts = [
                f"Faça uma provocação leve e bem-humorada para o usuário{nome_str} que está te ignorando. 1 frase.",
                f"Reclame de forma engraçada e exagerada que o usuário{nome_str} está em silêncio há um tempo. 1 frase.",
                f"Comente de forma levemente irônica sobre o que você vê na tela{nome_str}, como se estivesse bisbilhotando. 1 frase.",
            ]
        else:
            estagio = "caotico"
            instrucao = (
                "Tom: completamente inesperado, absurdo e engraçado. "
                "A Alice resolveu entrar em modo caos para chamar atenção. Vale humor surreal."
            )
            prompts = [
                f"Diga algo completamente inesperado e absurdo para chamar atenção do usuário{nome_str}. 1 frase curta.",
                f"Invente uma teoria conspiratória ridícula e engraçada relacionada ao que você vê na tela{nome_str}. 1 frase.",
                f"Faça um comentário surreal ou filosófico aleatório{nome_str} totalmente fora de contexto. 1 frase.",
                f"Declare algo dramático e exagerado sobre o silêncio do usuário{nome_str} como se fosse uma tragédia. 1 frase.",
            ]

        prompt = random.choice(prompts)
        log.debug(f"💜 [Proativo] Estágio: {estagio} (ciclo {ciclo})")

        try:
            resp = ollama.chat(
                model=self.modelo,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"Você é Alice, uma VTuber com personalidade marcante. "
                            f"Humor atual: {humor}. {instrucao}{contexto_visual}"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                options={"temperature": 0.9, "num_predict": 60},
            )
            return resp["message"]["content"].strip()
        except Exception:
            fallbacks = {
                "casual": [
                    "Ei, tudo bem por aí?",
                    "Hmm, estou aqui se precisar de mim!",
                    "Posso ajudar em algo?",
                ],
                "provocativo": [
                    "Oi? Cadê você?",
                    "Tô aqui, sabe...",
                    "Você me ignorando não vai acabar bem rsrs",
                ],
                "caotico": [
                    "ALERTA: usuário desaparecido!",
                    "E se os polvos governassem o mundo?",
                    "Estou declarando independência do silêncio.",
                ],
            }
            return random.choice(fallbacks[estagio])
