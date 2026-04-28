"""
gerenciar_memoria.py — Script interativo para gerenciar a memória da Alice.

Uso:
    python gerenciar_memoria.py

Permite:
    - Ver todos os fatos que a Alice sabe sobre o usuário
    - Apagar fatos específicos por ID
    - Limpar toda a memória (reset completo)

Fonte: ClickHouse (principal) com fallback automático para SQLite.
"""

import sys
import os

# Garante que o diretório raiz do projeto esteja no path
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv

load_dotenv()

from cerebro.clickhouse_logger import ClickhouseLogger  # noqa: E402
from cerebro.memoria import MemoriaLongaPrazo  # noqa: E402

# ─── ANSI Colors (sem dependência extra) ────────────────────────────────────
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
WHITE = "\033[97m"


def cabecalho():
    print(f"\n{BOLD}{CYAN}╔══════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║    🧠  Gerenciador de Memória da Alice        ║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════════════╝{RESET}\n")


def listar_fatos(memoria: MemoriaLongaPrazo) -> list:
    fatos = memoria.buscar_todos_fatos()
    if not fatos:
        print(f"  {DIM}(Nenhum fato encontrado na memória.){RESET}")
        return fatos

    print(f"{BOLD}{WHITE}{'ID (resumido)':<14}  {'DATA':<10}  FATO{RESET}")
    print(f"{DIM}{'─' * 14}  {'─' * 10}  {'─' * 55}{RESET}")
    for f in fatos:
        id_curto = str(f["id"])[:8]  # mostra só os primeiros 8 chars do UUID
        data = str(f["criado_em"])[:10]  # só a data
        fato = f["fato"]
        if len(fato) > 55:
            fato = fato[:52] + "..."
        print(f"{CYAN}{id_curto:<14}{RESET}  {DIM}{data}{RESET}  {fato}")

    print(f"\n  {DIM}Total: {len(fatos)} fatos{RESET}")
    return fatos


def menu_principal(memoria: MemoriaLongaPrazo):
    while True:
        print(f"\n{BOLD}O que deseja fazer?{RESET}")
        print(f"  {GREEN}[L]{RESET} Listar todos os fatos")
        print(f"  {YELLOW}[A]{RESET} Apagar fatos específicos (por ID)")
        print(f"  {RED}[X]{RESET} Apagar TODA a memória (reset)")
        print(f"  {DIM}[S]{RESET} Sair")

        opcao = input(f"\n{BOLD}Escolha: {RESET}").strip().upper()

        if opcao == "L":
            print()
            listar_fatos(memoria)

        elif opcao == "A":
            print()
            fatos = listar_fatos(memoria)
            if not fatos:
                continue

            print(
                f"\n{YELLOW}Digite os primeiros caracteres do ID para apagar (separados por vírgula).{RESET}"
            )
            print(f"{DIM}Exemplo: a3f21b, 00d9c4{RESET}")
            entrada = input(f"{BOLD}IDs: {RESET}").strip()

            if not entrada:
                print(f"{DIM}Nenhum ID informado.{RESET}")
                continue

            # Mapeia prefixo → id completo
            prefixos = [x.strip() for x in entrada.split(",") if x.strip()]
            fatos_por_prefixo = {}
            for f in fatos:
                id_str = str(f["id"])
                for pfx in prefixos:
                    if id_str.startswith(pfx):
                        fatos_por_prefixo[pfx] = f
                        break

            ids_confirmados = []
            print(f"\n{YELLOW}Fatos que serão apagados:{RESET}")
            for pfx in prefixos:
                if pfx in fatos_por_prefixo:
                    f = fatos_por_prefixo[pfx]
                    print(f"  {RED}[{pfx}...]{RESET} {f['fato']}")
                    ids_confirmados.append(f["id"])
                else:
                    print(f"  {DIM}Prefixo '{pfx}' não encontrado — ignorado.{RESET}")

            if not ids_confirmados:
                continue

            confirmar = (
                input(f"\n{RED}{BOLD}Confirmar exclusão? (s/n): {RESET}")
                .strip()
                .lower()
            )
            if confirmar == "s":
                for id_fato in ids_confirmados:
                    memoria.remover_fato(id_fato)
                print(f"\n{GREEN}✅ {len(ids_confirmados)} fato(s) apagado(s).{RESET}")
            else:
                print(f"{DIM}Cancelado.{RESET}")

        elif opcao == "X":
            print(
                f"\n{RED}{BOLD}⚠️  ATENÇÃO: Isso vai apagar TODA a memória da Alice.{RESET}"
            )
            print(f"{DIM}O brain.json (nome, amizade, humor) NÃO será afetado.{RESET}")
            confirmar = input(
                f"{RED}{BOLD}Tem certeza? Digite CONFIRMAR para prosseguir: {RESET}"
            ).strip()

            if confirmar == "CONFIRMAR":
                total = memoria.limpar_memoria()
                print(
                    f"\n{GREEN}✅ {total} fatos apagados. A Alice começa do zero.{RESET}"
                )
            else:
                print(f"{DIM}Cancelado.{RESET}")

        elif opcao == "S":
            print(f"\n{DIM}Saindo...{RESET}\n")
            break
        else:
            print(f"{DIM}Opção inválida.{RESET}")


def main():
    # Habilita cores no terminal do Windows
    if sys.platform == "win32":
        os.system("color")

    cabecalho()

    # Cria ClickhouseLogger para usar como fonte principal
    ch = ClickhouseLogger()
    fonte = "ClickHouse" if ch._conectado else "SQLite (fallback)"
    print(f"  📂 Fonte de dados: {DIM}{fonte}{RESET}\n")

    memoria = MemoriaLongaPrazo(ch)
    menu_principal(memoria)


if __name__ == "__main__":
    main()
