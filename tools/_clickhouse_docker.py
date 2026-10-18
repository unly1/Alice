"""
Funções comuns aos scripts de ClickHouse (configurar, backup e restaurar).

Tudo passa pelo `docker` da linha de comando, então não precisa de nada além
do Docker Desktop instalado. Usuário, senha e banco vêm do .env (as mesmas
variáveis do cerebro/clickhouse_logger.py), com os mesmos valores padrão.
"""

import os
import shutil
import subprocess
import sys
import time

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(RAIZ, ".env")
COMPOSE = os.path.join(RAIZ, "cerebro", "clickhouse", "docker-compose.yaml")
INIT_SQL = os.path.join(RAIZ, "cerebro", "clickhouse", "init.sql")
CONTAINER = "clickhouse"

# Bancos SQLite da Alice (fora do git por causa do *.db no .gitignore)
SQLITE_DBS = [
    os.path.join("cerebro", "memoria.db"),
    os.path.join("cerebro", "grafo.db"),
]

try:
    from dotenv import load_dotenv

    load_dotenv(ENV)
except ImportError:
    pass

# Pasta padrão dos backups (outro disco, para sobreviver a uma formatação do C:)
BACKUP_PADRAO = os.getenv("ALICE_BACKUP_DIR", r"D:\Backups\Alice")

USUARIO = os.getenv("CLICKHOUSE_USER_APP", "alice")
SENHA = os.getenv("CLICKHOUSE_PASSWORD_APP", "alice123")
BANCO = os.getenv("CLICKHOUSE_DB_APP", "analytics")



def definir_credenciais(usuario: str, senha: str, banco: str) -> None:
    """Troca as credenciais usadas pelos comandos (depois de criar o .env)."""
    global USUARIO, SENHA, BANCO
    USUARIO, SENHA, BANCO = usuario, senha, banco


def volume_existe() -> bool:
    """True se o volume de dados do ClickHouse já foi criado antes."""
    saida = docker(
        "volume", "ls", "-q", "--filter", "name=clickhouse_data", checar=False
    ).decode().split()
    return any(v.endswith("clickhouse_data") for v in saida)


DOCKER_DESKTOP_EXE = os.path.join(
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    "Docker",
    "Docker",
    "Docker Desktop.exe",
)


def passo(msg: str) -> None:
    print(f"\n▶ {msg}")


def falhar(msg: str) -> None:
    print(f"\n✖ {msg}")
    sys.exit(1)


def docker(*args, entrada: bytes | None = None, checar: bool = True) -> bytes:
    """Roda `docker <args>` e devolve a saída em bytes (seguro para dados binários)."""
    r = subprocess.run(
        ["docker", *args],
        input=entrada,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if checar and r.returncode != 0:
        erro = r.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"docker {' '.join(args[:3])}...: {erro}")
    return r.stdout


def sql(consulta: str, entrada: bytes | None = None) -> bytes:
    """Roda uma consulta no clickhouse-client dentro do container."""
    args = ["exec"]
    if entrada is not None:
        args.append("-i")
    args += [
        CONTAINER,
        "clickhouse-client",
        "--user", USUARIO,
        "--password", SENHA,
        "--database", BANCO,
        "--query", consulta,
    ]
    return docker(*args, entrada=entrada)


def sql_texto(consulta: str) -> str:
    return sql(consulta).decode("utf-8").strip()


def garantir_docker() -> None:
    """Confere se o Docker está instalado e rodando; abre o Docker Desktop se precisar."""
    if not shutil.which("docker"):
        falhar(
            "Docker não encontrado. Instale o Docker Desktop:\n"
            "  winget install -e --id Docker.DockerDesktop\n"
            "Depois reinicie o computador, abra o Docker Desktop uma vez para aceitar "
            "os termos e rode este script de novo."
        )

    if _docker_respondendo():
        return

    if os.path.exists(DOCKER_DESKTOP_EXE):
        passo("Abrindo o Docker Desktop...")
        subprocess.Popen([DOCKER_DESKTOP_EXE])
    else:
        print("  Abra o Docker Desktop manualmente.")

    print("  Esperando o Docker ficar pronto (até 3 minutos)...")
    for _ in range(90):
        if _docker_respondendo():
            print("  ✓ Docker pronto.")
            return
        time.sleep(2)
    falhar("O Docker não respondeu. Abra o Docker Desktop e tente de novo.")


def _docker_respondendo() -> bool:
    r = subprocess.run(
        ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return r.returncode == 0


def subir_clickhouse() -> None:
    """Sobe o container pelo docker-compose e espera ficar saudável."""
    passo("Subindo o ClickHouse (docker compose up -d)...")
    compose = ["compose", "-f", COMPOSE]
    if os.path.exists(ENV):
        # Usuário e senha do container vêm do .env da Alice
        compose = ["compose", "--env-file", ENV, "-f", COMPOSE]
    docker(*compose, "up", "-d")

    print("  Esperando o ClickHouse ficar pronto (até 2 minutos)...")
    for _ in range(60):
        estado = docker(
            "inspect", "-f", "{{.State.Health.Status}}", CONTAINER, checar=False
        ).decode().strip()
        if estado == "healthy":
            print("  ✓ ClickHouse pronto.")
            return
        time.sleep(2)
    falhar(
        f"O ClickHouse não ficou pronto. Veja os logs com: docker logs {CONTAINER}"
    )


def criar_tabelas() -> None:
    """Roda o init.sql (tudo é IF NOT EXISTS) e as migrações que o código faz."""
    passo("Criando as tabelas (init.sql)...")
    with open(INIT_SQL, "rb") as f:
        docker(
            "exec", "-i", CONTAINER,
            "clickhouse-client",
            "--user", USUARIO,
            "--password", SENHA,
            "--multiquery",
            entrada=f.read(),
        )
    # Mesma migração do clickhouse_logger._migrar_tabela_memoria
    sql(
        "ALTER TABLE memoria ADD COLUMN IF NOT EXISTS "
        "embedding Array(Float32) DEFAULT []"
    )
    print(f"  ✓ Tabelas: {', '.join(listar_tabelas())}")


def listar_tabelas() -> list[str]:
    saida = sql_texto(f"SHOW TABLES FROM {BANCO}")
    return [t for t in saida.splitlines() if t]
