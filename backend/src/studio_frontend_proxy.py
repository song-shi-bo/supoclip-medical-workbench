"""Persistent socket path for the host ingress when the container IP changes."""
import asyncio
import os
from pathlib import Path

LIMIT = asyncio.Semaphore(32)


async def relay(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()


async def connection(reader, writer):
    remote = None
    try:
        async with LIMIT:
            upstream, remote = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', 3107), 5)
            tasks = [asyncio.create_task(relay(reader, remote)), asyncio.create_task(relay(upstream, writer))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (OSError, TimeoutError):
        pass
    finally:
        writer.close()
        if remote:
            remote.close()


async def main():
    path = Path('/domain/.studio-resources/frontend.sock')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(connection, path=str(path))
    os.chmod(path, 0o600)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
