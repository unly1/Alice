-- Script de inicialização do ClickHouse para a Alice
-- Executado automaticamente quando o container é criado pela primeira vez.

-- Garante que o banco de dados existe
CREATE DATABASE IF NOT EXISTS analytics;

-- ─────────────────────────────────────────────────────────────────────────────
-- Tabela 1: Interações (histórico completo de conversas)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS analytics.interactions
(
    -- Identificadores
    session_id      String    COMMENT 'ID da sessão (gerado em main.py ao iniciar)',
    turn_id         UInt32    COMMENT 'Número do turno dentro da sessão',

    -- Quem falou e o que disse
    role            String    COMMENT '"user" ou "assistant"',
    content         String    COMMENT 'Conteúdo completo da mensagem',

    -- Contexto da Alice naquele momento
    humor           String    COMMENT 'Humor/personalidade usada naquele turno',
    model           String    COMMENT 'Modelo de IA usado (ex: llama3.1:8b)',

    -- Contagem de tokens (0 para linhas do usuário)
    tokens_prompt   UInt32    DEFAULT 0  COMMENT 'Tokens do contexto enviado ao modelo',
    tokens_resposta UInt32    DEFAULT 0  COMMENT 'Tokens gerados pelo modelo',
    tokens_total    UInt32    DEFAULT 0  COMMENT 'tokens_prompt + tokens_resposta',

    -- Timestamp
    created_at      DateTime  DEFAULT now()  COMMENT 'Data e hora da mensagem'
)
ENGINE = MergeTree()
ORDER BY (session_id, turn_id, created_at)
SETTINGS index_granularity = 8192;

-- ─────────────────────────────────────────────────────────────────────────────
-- Tabela 2: Memória de longo prazo (fatos sobre o usuário)
-- ─────────────────────────────────────────────────────────────────────────────
-- Fonte primária de fatos: substituiu o SQLite como banco principal.
-- SQLite (cerebro/memoria.db) continua como backup automático.
CREATE TABLE IF NOT EXISTS analytics.memoria
(
    id         String    COMMENT 'UUID do fato (gerado em Python)',
    fato       String    COMMENT 'Texto do fato sobre o usuário',
    criado_em  DateTime  DEFAULT now()  COMMENT 'Quando o fato foi salvo'
)
ENGINE = MergeTree()
ORDER BY criado_em
SETTINGS index_granularity = 8192;

-- ─────────────────────────────────────────────────────────────────────────────
-- Tabela 3: Logs de eventos (WARNING e acima)
-- ─────────────────────────────────────────────────────────────────────────────
-- Captura alertas e erros para análise histórica e debugging.
-- Logs de DEBUG/INFO continuam indo só para arquivo (logs/).
CREATE TABLE IF NOT EXISTS analytics.logs
(
    nivel      String    COMMENT 'Nível do log: INFO, WARNING, ERROR',
    modulo     String    COMMENT 'Módulo Python que gerou o log',
    mensagem   String    COMMENT 'Texto da mensagem de log',
    session_id String    DEFAULT ''  COMMENT 'ID da sessão Alice relacionada',
    criado_em  DateTime  DEFAULT now()  COMMENT 'Timestamp do evento'
)
ENGINE = MergeTree()
ORDER BY (nivel, criado_em)
SETTINGS index_granularity = 8192;

-- ─────────────────────────────────────────────────────────────────────────────
-- Tabela 4: Histórico do estado da Alice (antigo brain.json)
-- ─────────────────────────────────────────────────────────────────────────────
-- O estado atual fica no SQLite (cerebro/memoria.db, tabela estado).
-- Aqui cada mudança vira uma linha, para ver a evolução do humor e da amizade.
CREATE TABLE IF NOT EXISTS analytics.estado_alice
(
    humor               String    COMMENT 'Humor detectado na última resposta',
    amizade_com_usuario UInt8     COMMENT 'Nível de amizade (0 a 100)',
    total_conversas     UInt32    COMMENT 'Total de turnos já registrados',
    nome_usuario        String    COMMENT 'Nome do usuário detectado',
    ultima_interacao    String    COMMENT 'Resumo da última troca',
    session_id          String    COMMENT 'ID da sessão Alice',
    criado_em           DateTime  DEFAULT now()  COMMENT 'Quando o estado mudou'
)
ENGINE = MergeTree()
ORDER BY criado_em
SETTINGS index_granularity = 8192;
