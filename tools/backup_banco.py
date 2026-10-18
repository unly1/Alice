"""
Faz backup dos bancos da Alice numa pasta com data e hora.

O que entra no backup:
- Todas as tabelas do ClickHouse (dados em formato Native + o CREATE TABLE)
- Os SQLite cerebro/memoria.db e cerebro/grafo.db (cópia segura, mesmo com a Alice aberta)
- O .env (não vai para o git)

Uso (na pasta da Alice, com o .venv ativo):
    python tools/backup_banco.py                  # salva em D:\\Backups\\Alice
    python tools/backup_banco.py E:\\MeusBackups   # outra pasta
    python tools/backup_banco.py --manter 10      # guarda só os 10 mais recentes

A pasta padrão também pode ser trocada com ALICE_BACKUP_DIR no .env.
"""

import argparse
import datetime
import json
import os
import shutil
import sqlite3

from _clickhouse_docker import (
    BACKUP_PADRAO,
    CONTAINER,
    RAIZ,
    SQLITE_DBS,
    falhar,
    garantir_docker,
    listar_tabelas,
    passo,
    sql,
    sql_texto,
    docker,
)

PREFIXO = "alice_"


def backup_clickhouse(destino: str, manifesto: dict) -> None:
    estado = docker(
        "inspect", "-f", "{{.State.Running}}", CONTAINER, checar=False
    ).decode().strip()
    if estado != "true":
        print(
            "  ⚠ O container do ClickHouse não está rodando; pulando o ClickHouse.\n"
            "    Rode tools/configurar_clickhouse.py para subir ele."
        )
        return

    pasta = os.path.join(destino, "clickhouse")
    os.makedirs(pasta, exist_ok=True)
    for tabela in listar_tabelas():
        if tabela.startswith(".inner"):
            continue
        criar = sql_texto(f"SHOW CREATE TABLE {tabela} FORMAT TSVRaw")
        with open(os.path.join(pasta, f"{tabela}.sql"), "w", encoding="utf-8") as f:
            f.write(criar)
        dados = sql(f"SELECT * FROM {tabela} FORMAT Native")
        with open(os.path.join(pasta, f"{tabela}.native"), "wb") as f:
            f.write(dados)
        linhas = int(sql_texto(f"SELECT count() FROM {tabela}"))
        manifesto["clickhouse"][tabela] = linhas
        print(f"  ✓ {tabela}: {linhas} linhas")


def backup_sqlite(destino: str, manifesto: dict) -> None:
    pasta = os.path.join(destino, "sqlite")
    os.makedirs(pasta, exist_ok=True)
    for relativo in SQLITE_DBS:
        origem = os.path.join(RAIZ, relativo)
        if not os.path.exists(origem):
            continue
        alvo = os.path.join(pasta, os.path.basename(relativo))
        # API de backup do SQLite: cópia consistente mesmo com a Alice gravando
        with sqlite3.connect(origem) as src, sqlite3.connect(alvo) as dst:
            src.backup(dst)
        manifesto["sqlite"].append(relativo)
        print(f"  ✓ {relativo}")


def limpar_antigos(raiz: str, manter: int) -> None:
    pastas = sorted(
        p for p in os.listdir(raiz)
        if p.startswith(PREFIXO) and os.path.isdir(os.path.join(raiz, p))
    )
    for antiga in pastas[:-manter]:
        shutil.rmtree(os.path.join(raiz, antiga), ignore_errors=True)
        print(f"  🗑 Backup antigo removido: {antiga}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup dos bancos da Alice")
    parser.add_argument("pasta", nargs="?", default=BACKUP_PADRAO)
    parser.add_argument(
        "--manter", type=int, default=30,
        help="quantos backups guardar (os mais antigos são apagados; 0 = todos)",
    )
    args = parser.parse_args()

    try:
        os.makedirs(args.pasta, exist_ok=True)
    except OSError as e:
        falhar(f"Não consegui criar a pasta de backup {args.pasta}: {e}")

    agora = datetime.datetime.now()
    destino = os.path.join(args.pasta, PREFIXO + agora.strftime("%Y-%m-%d_%H%M%S"))
    os.makedirs(destino)
    manifesto = {"criado_em": agora.isoformat(timespec="seconds"),
                 "clickhouse": {}, "sqlite": []}

    garantir_docker()
    passo("Exportando o ClickHouse...")
    backup_clickhouse(destino, manifesto)

    passo("Copiando os SQLite...")
    backup_sqlite(destino, manifesto)

    env = os.path.join(RAIZ, ".env")
    if os.path.exists(env):
        shutil.copy2(env, os.path.join(destino, ".env"))
        print("\n  ✓ .env copiado")

    with open(os.path.join(destino, "manifesto.json"), "w", encoding="utf-8") as f:
        json.dump(manifesto, f, indent=4, ensure_ascii=False)

    if args.manter > 0:
        limpar_antigos(args.pasta, args.manter)

    print(f"\n✅ Backup salvo em: {destino}")


if __name__ == "__main__":
    main()
