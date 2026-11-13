"""
OllamaClient — encapsula todas as chamadas ao modelo Ollama.

Responsabilidades:
- Chamar ollama.chat em thread separada (não bloqueia o event loop) com keep_alive na VRAM
- Gerenciar cancelamento de turno via asyncio.Event (padrão DRY)
- Descobrir humor/personalidade ideal via heurística local rápida (latência zero) ou LLM opcional
- Gerar resposta principal com suporte a ferramentas (ReAct Pattern até 2 iterações seguras)
- Extrair fatos da conversa e salvar na memória de longo prazo
- Detectar o nome do usuário no histórico
- Gerar falas proativas espontâneas
"""

import asyncio
import inspect
import os
import random
import re

import ollama

import functions.Tools as funcoes_tools
from cerebro.logger import log
from functions.ContentManager import ContentManager

# Tempo padrão para manter o modelo carregado na VRAM do Ollama entre chamadas
KEEP_ALIVE_PADRAO = os.getenv("OLLAMA_KEEP_ALIVE", "10m")

# Limite seguro de iterações de ferramentas por turno no loop ReAct
_MAX_ITERACOES_TOOL = 2

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

# Regras léxicas de prioridade para classificação instantânea de personalidade (latência zero)
_REGRAS_HUMOR_LOCAL: list[tuple[str, re.Pattern]] = [
    (
        "timida",
        re.compile(
            r"\b(linda|fofa|maravilhosa|perfeita|te amo|amo voc[êe]|te adoro|gosto de voc[êe]|"
            r"voc[êe] [ée] incr[íi]vel|muito fofa|meu amor|querida|princesa|melhor ia|elogio)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "euforica",
        re.compile(
            r"\b(consegui|deu certo|funcionou|passei|ganhei|venci|terminei|finalizei|"
            r"aprovad[oa]|sucesso|uhuuu|yay|eba|conquista|vit[óo]ria)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "empatica",
        re.compile(
            r"\b(triste|cansad[oa]|exaust[oa]|frustrad[oa]|desanimad[oa]|sozinh[oa]|"
            r"chorando|ansios[oa]|estressad[oa]|deprimid[oa]|chatead[oa]|desabafar|desabafo|"
            r"n[ãa]o aguento mais|t[áa] dif[íi]cil)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "determinada",
        re.compile(
            r"\b(erro|bug|exception|traceback|falha|falhou|quebrou|n[ãa]o funciona|"
            r"travando|travou|corrigir|debug|consertar|syntaxerror|typeerror|keyerror|"
            r"import|stacktrace|problema t[ée]cnico)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "grata",
        re.compile(
            r"\b(obrigad[oa]|valeu|agrade[çc]o|muito obrigad[oa]|ajudou muito|"
            r"me salvou|gratid[ãa]o|thanks|vlw)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "nostalgica",
        re.compile(
            r"\b(lembro|lembra|antigamente|saudade|saudades|passado|inf[âa]ncia|"
            r"[ée]poca|nostalgia|anos atr[áa]s|quando eu era)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "brava",
        re.compile(
            r"\b(idiota|burra|in[úu]til|cala a boca|chata|lenta|demora|"
            r"porcaria|lixo|irritante|anda logo|que saco|odeio voc[êe])\b",
            re.IGNORECASE,
        ),
    ),
    (
        "curiosa",
        re.compile(
            r"\b(como funciona|o que [ée]|por que|porque|qual a diferen[çc]a|explica|"
            r"explique|me conta|curiosidade|python|sql|api|llm|algoritmo|tecnologia|"
            r"ci[êe]ncia|universo|arquitetura)\b",
            re.IGNORECASE,
        ),
    ),
]


class OllamaClient:
    """Gerencia todas as interações com o modelo de linguagem Ollama."""

    def __init__(
        self,
        modelo: str,
        emotion_engine,
        visao,
        cancelar_turno: asyncio.Event | None = None,
        content_manager: ContentManager | None = None,
        audio_ambiente=None,
        keep_alive: str = KEEP_ALIVE_PADRAO,
    ):
        # Compatibilidade: se o 4º argumento posicional não for Event, usa cancelar_turno nomeado
        if not isinstance(cancelar_turno, asyncio.Event) and isinstance(
            content_manager, asyncio.Event
        ):
            cancelar_turno = content_manager
            content_manager = None
        self.modelo = modelo
        self.emotion_engine = emotion_engine
        self.visao = visao
        self.keep_alive = keep_alive
        self._cancelar = (
            cancelar_turno
            if isinstance(cancelar_turno, asyncio.Event)
            else asyncio.Event()
        )
        # Usa o ContentManager injetado ou cria um padrão
        self._cm = content_manager or ContentManager()
        _ = audio_ambiente  # argumento legado ignorado sem criar atributo morto

    # ── Helpers internos ──────────────────────────────────────────────────────

    async def _ollama_em_thread(
        self, mensagens: list, options: dict | None = None
    ) -> dict:
        """
        Executa ollama.chat em thread separada (não bloqueia o event loop),
        mantendo o modelo aquecido na VRAM via keep_alive.
        """
        loop = asyncio.get_running_loop()

        def _call():
            kwargs: dict = {
                "model": self.modelo,
                "messages": mensagens,
                "keep_alive": self.keep_alive,
            }
            if options is not None:
                kwargs["options"] = options
            try:
                return ollama.chat(**kwargs)
            except TypeError:
                # Compatibilidade com mocks de teste que aceitam apenas (model, messages, options)
                kwargs.pop("keep_alive", None)
                return ollama.chat(**kwargs)

        return await loop.run_in_executor(None, _call)

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

    def _classificar_humor_local(
        self, texto_usuario: str, disponiveis: list[str]
    ) -> str:
        """
        Classifica a personalidade ideal por heurística léxica local (latência zero),
        seguindo a mesma ordem de prioridade das regras de domínio da Alice.
        """
        texto = (texto_usuario or "").strip()
        if not texto:
            return "alegre" if "alegre" in disponiveis else disponiveis[0]

        disponiveis_set = {d.lower(): d for d in disponiveis}

        for candidato, padrao in _REGRAS_HUMOR_LOCAL:
            if candidato in disponiveis_set and padrao.search(texto):
                return disponiveis_set[candidato]

        if "?" in texto and "curiosa" in disponiveis_set:
            return disponiveis_set["curiosa"]

        return disponiveis_set.get("alegre", disponiveis[0])

    # ── Métodos Públicos ──────────────────────────────────────────────────────

    async def descobrir_humor(
        self, texto_usuario: str, usar_llm: bool = False
    ) -> str:
        """
        Seleciona a personalidade ideal para responder ao usuário.

        Por padrão (`usar_llm=False`), usa classificação heurística local de latência
        zero para eliminar a 2ª chamada serial ao Ollama em cada turno.
        Se `usar_llm=True`, consulta o LLM com fallback seguro para a heurística local.
        """
        disponiveis = self._cm.listar_personalidades()
        if not disponiveis:
            return "alegre"  # fallback

        humor_local = self._classificar_humor_local(texto_usuario, disponiveis)
        if not usar_llm:
            log.debug(f"🧐 [Humor Local] Personalidade selecionada: {humor_local}")
            return humor_local

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

        log.debug("🧐 Analisando intenção via LLM...")
        try:
            resposta = await self._ollama_em_thread(
                mensagens=[
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
        except Exception as e:
            log.warning(f"⚠️  [Humor] Erro ao classificar personalidade via LLM: {e}")
        return humor_local

    async def pensar(
        self,
        texto_usuario: str,
        personalidade: str,
        historico: list,
        contexto_emocional: str = "",
        bloco_memoria: str = "",
    ) -> tuple[str | None, int, int]:
        """
        Gera a resposta principal da Alice com suporte a ferramentas (até 2 iterações seguras).

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

        # Loop ReAct limitado a _MAX_ITERACOES_TOOL (proteção contra loop infinito)
        usou_tool = False
        for iteracao in range(_MAX_ITERACOES_TOOL):
            match = _REGEX_TOOL.search(conteudo)
            if not match:
                break

            usou_tool = True
            nome_funcao = match.group(1)
            argumento_raw = (match.group(2) or "").strip()
            log.info(
                f"[Tool {iteracao + 1}/{_MAX_ITERACOES_TOOL}] '{nome_funcao}' | arg: '{argumento_raw}'"
            )

            if nome_funcao in funcoes_tools.DISPONIVEIS:
                funcao = funcoes_tools.DISPONIVEIS[nome_funcao]
                try:
                    if inspect.iscoroutinefunction(funcao):
                        res = await (
                            funcao(argumento_raw) if argumento_raw else funcao()
                        )
                    else:
                        res = await (
                            asyncio.to_thread(funcao, argumento_raw)
                            if argumento_raw
                            else asyncio.to_thread(funcao)
                        )
                    resultado = str(res)
                    log.debug(f"    Resultado: {resultado}")
                except Exception as e:
                    resultado = f"Erro ao executar: {e}"
                    log.error(f"    ERRO: {resultado}")
            else:
                resultado = f"Ferramenta '{nome_funcao}' não encontrada."

            # Verifica cancelamento antes da próxima chamada ao Ollama
            if self._cancelar.is_set():
                log.info(
                    "🔄 [Reiniciar] Turno cancelado antes da chamada pós-tool ao Ollama."
                )
                return None, 0, 0

            mensagens.append({"role": "assistant", "content": conteudo})
            instrucao_pos_tool = (
                f"[RESULTADO]: {resultado}\n"
                "Agora use essa informação e responda minha pergunta original "
                "de forma natural, sem citar o marcador [TOOL]."
            )
            mensagens.append({"role": "user", "content": instrucao_pos_tool})

            response_pos = await self._chamar_ollama_cancelavel(mensagens)
            if response_pos is None:
                log.info(
                    "🔄 [Reiniciar] Turno cancelado durante chamada pós-tool ao Ollama."
                )
                return None, 0, 0

            tokens_p += response_pos.get("prompt_eval_count", 0) or 0
            tokens_r += response_pos.get("eval_count", 0) or 0
            conteudo = response_pos["message"]["content"].strip()

        if usou_tool:
            log.debug(
                f"Tokens acumulados (com tool): prompt={tokens_p} resposta={tokens_r}"
            )
        else:
            log.debug(f"Tokens: prompt={tokens_p} resposta={tokens_r}")

        texto_final = _REGEX_TOOL.sub("", conteudo).strip()
        return texto_final, tokens_p, tokens_r

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
        _PALAVRAS_VAZIAS = (
            "NENHUM",
            "DESCULPE",
            "NÃO HÁ",
            "NAO HA",
            "SEM FATOS",
            "NENHUMA",
        )
        try:
            response = await self._ollama_em_thread(
                mensagens=[
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
            fatos_salvos = 0
            if texto and texto.upper() not in _PALAVRAS_VAZIAS:
                for linha in texto.split("\n"):
                    fato = linha.strip().lstrip("-").strip()
                    if not fato or len(fato) < 8 or len(fato) > 150:
                        continue
                    fato_upper = fato.upper()
                    if any(
                        p in fato_upper
                        for p in (*_PALAVRAS_VAZIAS, "PARECE", "CONVERSA")
                    ):
                        continue
                    await asyncio.to_thread(memoria.salvar_fato, fato)
                    fatos_salvos += 1
                    log.info(f"    └─ 💾 Fato salvo: {fato}")

            if fatos_salvos == 0:
                log.debug("    Nenhum fato novo encontrado.")
        except Exception as e:
            log.warning(f"🔍 [Memória] Erro ao extrair fatos: {e}")

        # ── Extração de triplas para o grafo (GraphRAG) ───────────────────────
        if grafo is None:
            return

        log.debug("🕸️  [Grafo] Extraindo triplas da conversa...")
        try:
            resp_grafo = await self._ollama_em_thread(
                mensagens=[
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
                    salvou = await asyncio.to_thread(
                        grafo.salvar_tripla, sujeito, relacao, objeto
                    )
                    if salvou:
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
        Se encontrado, salva automaticamente no estado (nome_usuario).
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
            response = await self._ollama_em_thread(
                mensagens=[
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
            partes = response["message"]["content"].strip().split()
            if not partes:
                return
            nome = partes[0].strip(".,!?:;\"'()")
            if (
                nome
                and nome.upper() != "DESCONHECIDO"
                and nome.isalpha()
                and len(nome) < 25
            ):
                self.emotion_engine.atualizar_estado("nome_usuario", nome)
                log.info(f"👤 Nome detectado e salvo: {nome}")
        except Exception as e:
            log.debug(f"👤 [Nome] Falha silenciosa ao detectar nome: {e}")

    async def gerar_comentario_visao(self) -> str:
        """
        Gera um comentário espontâneo sobre o que mudou na tela.
        Chamado pelo vision heartbeat quando uma mudança relevante é detectada.
        """
        if not self.visao:
            return ""
        descricao = self.visao.ultima_descricao
        nome = self.emotion_engine.estado_atual.get("nome_usuario", "")
        nome_str = f", {nome}" if nome else ""

        if not descricao:
            return ""

        try:
            resp = await self._ollama_em_thread(
                mensagens=[
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
        except Exception as e:
            log.warning(f"👁️  [Visão] Erro ao gerar comentário de visão: {e}")
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
        contexto_visual = (
            await self.visao.construir_bloco_visao() if self.visao else ""
        )

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
            resp = await self._ollama_em_thread(
                mensagens=[
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
        except Exception as e:
            log.warning(f"💜 [Proativo] Erro ao gerar fala proativa ({estagio}): {e}")
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
