"""
Configura o ClickHouse da Alice pela primeira vez (ou numa máquina nova).

O que faz:
1. Cria o usuário e a senha do ClickHouse no .env, se ainda não existirem
   (pergunta; Enter aceita o usuário "alice" e uma senha gerada na hora)
2. Confere se o Docker Desktop está instalado e abre ele se estiver fechado
3. Sobe o container `clickhouse` pelo cerebro/clickhouse/docker-compose.yaml,
   que cria esse usuário na primeira vez
4. Cria as tabelas do init.sql (pode rodar de novo sem problema)
5. Confere se o Python da Alice consegue conectar (clickhouse-connect)

Uso (na pasta da Alice, com o .venv ativo):
    python tools/configurar_clickhouse.py

Numa máquina formatada, depois deste script rode:
    python tools/restaurar_banco.py
"""

import os
import secrets
import sys

import _clickhouse_docker as ch
from _clickhouse_docker import (
    ENV,
    criar_tabelas,
    falhar,
    garantir_docker,
    passo,
    subir_clickhouse,
)

CHAVES = ("CLICKHOUSE_USER_APP", "CLICKHOUSE_PASSWORD_APP", "CLICKHOUSE_DB_APP")


def _ler_env() -> dict:
    valores = {}
    if os.path.exists(ENV):
        with open(ENV, encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if linha and not linha.startswith("#") and "=" in linha:
                    chave, valor = linha.split("=", 1)
                    valores[chave.strip()] = valor.strip().strip('"').strip("'")
    return valores


def _perguntar(texto: str, padrao: str) -> str:
    if not sys.stdin.isatty():
        return padrao
    resposta = input(f"  {texto} [{padrao}]: ").strip()
    return resposta or padrao


def garantir_credenciais() -> None:
    """Cria usuário/senha do ClickHouse no .env na primeira vez."""
    passo("Conferindo usuário e senha do ClickHouse no .env...")
    env = _ler_env()
    if all(env.get(k) for k in CHAVES[:2]):
        ch.definir_credenciais(
            env["CLICKHOUSE_USER_APP"],
            env["CLICKHOUSE_PASSWORD_APP"],
            env.get("CLICKHOUSE_DB_APP") or "analytics",
        )
        print(f"  ✓ Usando o usuário '{ch.USUARIO}' que já está no .env.")
        return

    if ch.volume_existe():
        # O ClickHouse só cria o usuário quando o volume é novo. Se já existe,
        # ele foi criado com o padrão antigo do docker-compose (alice/alice123).
        usuario, senha = "alice", "alice123"
        print(
            "  O ClickHouse já tem dados nesta máquina, então mantenho o usuário "
            "que ele já usa."
        )
    else:
        print("  Primeira configuração: vamos criar o usuário do ClickHouse.")
        print("  (Aperte Enter para aceitar o valor entre colchetes.)")
        usuario = _perguntar("Usuário", "alice")
        senha = _perguntar("Senha", secrets.token_urlsafe(12))

    novas = {
        "CLICKHOUSE_HOST": "localhost",
        "CLICKHOUSE_HTTP_PORT": "8123",
        "CLICKHOUSE_USER_APP": usuario,
        "CLICKHOUSE_PASSWORD_APP": senha,
        "CLICKHOUSE_DB_APP": "analytics",
    }
    faltando = {k: v for k, v in novas.items() if not env.get(k)}
    with open(ENV, "a", encoding="utf-8") as f:
        f.write("\n# ClickHouse (criado por tools/configurar_clickhouse.py)\n")
        for chave, valor in faltando.items():
            f.write(f"{chave}={valor}\n")
    ch.definir_credenciais(usuario, senha, "analytics")
    print(f"  ✓ Usuário '{usuario}' e senha salvos no .env ({ENV}).")
    print("    Guarde essa senha: ela também vai junto em cada backup do .env.")


def conferir_login() -> None:
    """Confere se o usuário do .env consegue entrar no ClickHouse."""
    try:
        ch.sql_texto("SELECT 1")
    except RuntimeError as e:
        falhar(
            f"O ClickHouse recusou o usuário '{ch.USUARIO}' do .env.\n"
            "  Isso acontece quando o volume de dados foi criado com outro usuário/senha.\n"
            "  Coloque no .env o usuário e a senha antigos, ou, se não houver nada "
            "importante no ClickHouse,\n"
            "  apague o volume (docker compose -f cerebro/clickhouse/docker-compose.yaml "
            "down -v) e rode este script de novo.\n"
            f"  Detalhe: {e}"
        )


def testar_conexao_python() -> None:
    passo("Testando a conexão pelo Python (clickhouse-connect)...")
    try:
        import clickhouse_connect
    except ImportError:
        print(
            "  ⚠ clickhouse-connect não instalado. Rode: pip install -r requirements.txt"
        )
        return
    try:
        cliente = clickhouse_connect.get_client(
            host="localhost",
            port=8123,
            username=ch.USUARIO,
            password=ch.SENHA,
            database=ch.BANCO,
        )
        cliente.ping()
        print("  ✓ A Alice vai conseguir conectar no ClickHouse.")
    except Exception as e:
        print(f"  ⚠ Falha ao conectar pelo Python: {e}")


def main() -> None:
    garantir_docker()
    garantir_credenciais()
    subir_clickhouse()
    conferir_login()
    criar_tabelas()
    testar_conexao_python()
    print("\n✅ ClickHouse configurado. Já pode abrir a Alice.")


if __name__ == "__main__":
    main()
