"""
Restaura um backup feito pelo tools/backup_banco.py.

Uso (na pasta da Alice, com o .venv ativo e a Alice fechada):
    python tools/restaurar_banco.py                       # backup mais recente de D:\\Backups\\Alice
    python tools/restaurar_banco.py D:\\Backups\\Alice\\alice_2026-10-18_190000
    python tools/restaurar_banco.py --substituir          # apaga o que já existe nas tabelas antes

Sem --substituir, nada que já existe é mexido: tabelas do ClickHouse com dados e
SQLite que já existem são pulados. Com --substituir, as tabelas são esvaziadas antes
e os SQLite atuais são renomeados para .antes-restauracao (não são apagados).
"""

import argparse
import json
import os
import shutil

from _clickhouse_docker import (
    BACKUP_PADRAO,
    RAIZ,
    SQLITE_DBS,
    criar_tabelas,
    falhar,
    garantir_docker,
    passo,
    sql,
    sql_texto,
    subir_clickhouse,
)


def achar_backup(caminho: str) -> str:
    """Aceita a pasta de um backup ou a pasta raiz (pega o mais recente)."""
    if os.path.exists(os.path.join(caminho, "manifesto.json")):
        return caminho
    if not os.path.isdir(caminho):
        falhar(f"Pasta não encontrada: {caminho}")
    backups = sorted(
        p for p in os.listdir(caminho)
        if os.path.exists(os.path.join(caminho, p, "manifesto.json"))
    )
    if not backups:
        falhar(f"Nenhum backup encontrado em {caminho}")
    return os.path.join(caminho, backups[-1])


def restaurar_clickhouse(origem: str, substituir: bool) -> None:
    pasta = os.path.join(origem, "clickhouse")
    if not os.path.isdir(pasta):
        print("  (backup sem ClickHouse)")
        return
    for arquivo in sorted(os.listdir(pasta)):
        if not arquivo.endswith(".native"):
            continue
        tabela = arquivo[: -len(".native")]
        with open(os.path.join(pasta, f"{tabela}.sql"), encoding="utf-8") as f:
            criar = f.read().replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
        sql(criar)

        existentes = int(sql_texto(f"SELECT count() FROM {tabela}"))
        if existentes and not substituir:
            print(f"  ⏭ {tabela}: já tem {existentes} linhas, pulando (use --substituir)")
            continue
        if existentes:
            sql(f"TRUNCATE TABLE {tabela}")

        with open(os.path.join(pasta, arquivo), "rb") as f:
            sql(f"INSERT INTO {tabela} FORMAT Native", entrada=f.read())
        linhas = int(sql_texto(f"SELECT count() FROM {tabela}"))
        print(f"  ✓ {tabela}: {linhas} linhas")


def restaurar_sqlite(origem: str, substituir: bool) -> None:
    pasta = os.path.join(origem, "sqlite")
    for relativo in SQLITE_DBS:
        copia = os.path.join(pasta, os.path.basename(relativo))
        if not os.path.exists(copia):
            continue
        alvo = os.path.join(RAIZ, relativo)
        if os.path.exists(alvo):
            if not substituir:
                print(f"  ⏭ {relativo}: já existe, pulando (use --substituir)")
                continue
            os.replace(alvo, alvo + ".antes-restauracao")
        shutil.copy2(copia, alvo)
        print(f"  ✓ {relativo}")


def _chaves_env(caminho: str) -> set[str]:
    with open(caminho, encoding="utf-8") as f:
        return {
            linha.split("=", 1)[0].strip()
            for linha in f
            if "=" in linha and not linha.lstrip().startswith("#")
        }


def restaurar_env(env_backup: str, env_atual: str) -> None:
    """
    Sem .env: copia o do backup. Com .env (ex: criado pelo configurar_clickhouse):
    só acrescenta as configurações que faltam, mantendo usuário/senha atuais do
    ClickHouse, que são os que o container desta máquina usa.
    """
    if not os.path.exists(env_backup):
        return
    if not os.path.exists(env_atual):
        shutil.copy2(env_backup, env_atual)
        print("\n  ✓ .env restaurado")
        return
    existentes = _chaves_env(env_atual)
    with open(env_backup, encoding="utf-8") as f:
        faltando = [
            linha.rstrip("\n")
            for linha in f
            if "=" in linha
            and not linha.lstrip().startswith("#")
            and linha.split("=", 1)[0].strip() not in existentes
        ]
    if faltando:
        with open(env_atual, "a", encoding="utf-8") as f:
            f.write("\n# Restaurado do backup (tools/restaurar_banco.py)\n")
            f.write("\n".join(faltando) + "\n")
        print(f"\n  ✓ .env: {len(faltando)} configuração(ões) restaurada(s) do backup")


def main() -> None:
    parser = argparse.ArgumentParser(description="Restaura os bancos da Alice")
    parser.add_argument("pasta", nargs="?", default=BACKUP_PADRAO)
    parser.add_argument(
        "--substituir", action="store_true",
        help="substitui os dados atuais (ClickHouse e SQLite) pelos do backup",
    )
    args = parser.parse_args()

    origem = achar_backup(args.pasta)
    with open(os.path.join(origem, "manifesto.json"), encoding="utf-8") as f:
        manifesto = json.load(f)
    print(f"Restaurando o backup de {manifesto['criado_em']}: {origem}")

    garantir_docker()
    subir_clickhouse()
    criar_tabelas()

    passo("Restaurando o ClickHouse...")
    restaurar_clickhouse(origem, args.substituir)

    passo("Restaurando os SQLite...")
    restaurar_sqlite(origem, args.substituir)

    restaurar_env(os.path.join(origem, ".env"), os.path.join(RAIZ, ".env"))

    print("\n✅ Restauração concluída. Já pode abrir a Alice.")


if __name__ == "__main__":
    main()
