"""
cerebro/logger.py — Configuração centralizada do Loguru para o projeto Alice

Uso em qualquer módulo:
    from cerebro.logger import log

Níveis:
    log.debug("...")    → só no arquivo (modo verbose desativado no terminal)
    log.info("...")     → terminal + arquivo (mensagens relevantes)
    log.warning("...")  → terminal + arquivo + ClickHouse
    log.error("...")    → terminal + arquivo + ClickHouse
    log.exception("...") → igual error, mas já captura o traceback do except

Sink 4 (ClickHouse) só é ativado após chamar set_clickhouse_logger() em main.py.
"""

import sys
import os
from loguru import logger

# ─── Pasta de logs ────────────────────────────────────────────────────────────
_DIR_LOGS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(_DIR_LOGS, exist_ok=True)

# ─── Remove o sink padrão do Loguru (stderr sem formatação) ──────────────────
logger.remove()

# ─── Sink 1: Terminal colorido — apenas INFO e acima ─────────────────────────
logger.add(
    sys.stdout,
    level="INFO",
    colorize=True,
    format=("<dim>{time:HH:mm:ss}</dim> <level>{level.icon} {message}</level>"),
    backtrace=False,
    diagnose=False,
)

# ─── Sink 2: Arquivo diário completo — DEBUG e acima ─────────────────────────
logger.add(
    os.path.join(_DIR_LOGS, "alice_{time:YYYY-MM-DD}.log"),
    rotation="00:00",  # novo arquivo a meia-noite
    retention="7 days",  # apaga logs com mais de 7 dias
    level="DEBUG",
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {module}:{line} — {message}",
    backtrace=False,
    diagnose=False,
    enqueue=True,  # thread-safe para logs de threads daemon
)

# ─── Sink 3: Arquivo de erros com traceback completo ─────────────────────────
logger.add(
    os.path.join(_DIR_LOGS, "alice_errors.log"),
    rotation="10 MB",
    retention="30 days",
    level="ERROR",
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}:{function}:{line}\n{message}",
    backtrace=True,  # mostra toda a cadeia de exceção
    diagnose=True,  # mostra valores das variáveis locais
    enqueue=True,
)

# Exporta o logger configurado como `log` para uso nos módulos
log = logger


# ─── Sink 4: ClickHouse (ativado via set_clickhouse_logger) ──────────────────
# Ref ao ClickhouseLogger — preenchido em main.py após o objeto ser criado.
# Evita import circular: clickhouse_logger.py já importa `log` daqui.
_ch_log_ref = None


def set_clickhouse_logger(ch_logger):
    """
    Registra o ClickhouseLogger e ativa o sink de WARNING+ no ClickHouse.
    Deve ser chamado em main.py logo após criar clickhouse_log.

    Example::
        from cerebro.logger import set_clickhouse_logger
        set_clickhouse_logger(clickhouse_log)
    """
    global _ch_log_ref
    _ch_log_ref = ch_logger
    logger.add(
        _clickhouse_sink,
        level="WARNING",
        enqueue=True,  # thread-safe, não bloqueia o log principal
        format="{message}",
    )


def _clickhouse_sink(message):
    """Sink do Loguru → tabela analytics.logs. Nunca lança excessões."""
    if _ch_log_ref is None:
        return
    try:
        record = message.record
        _ch_log_ref.registrar_log_sync(
            nivel=record["level"].name,
            modulo=record["module"],
            mensagem=record["message"],
        )
    except Exception:
        pass  # nunca crashar o app por causa de log de log
