import asyncio
import sys
from pathlib import Path

# Garante raiz do projeto no path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from functions.VTS_Connector import VTSConnector

async def main():
    logger.info("Iniciando processo de autenticação com o VTube Studio...")
    connector = VTSConnector()
    sucesso = await connector.conectar()
    if sucesso:
        logger.info(">>> SUCESSO: VTube Studio conectado e novo token salvo com sucesso! <<<")
    else:
        logger.error(">>> FALHA: Nao foi possivel autenticar com o VTube Studio. <<<")
    await connector.fechar()

if __name__ == "__main__":
    asyncio.run(main())
