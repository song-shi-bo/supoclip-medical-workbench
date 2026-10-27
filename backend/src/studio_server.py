"""Serve one API process through loopback TCP and the authenticated host ingress."""
import os
from pathlib import Path
import socket
import uvicorn


def main():
    path = Path('/domain/.studio-resources/api.sock')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        unix.bind(str(path))
        os.chmod(path, 0o600)
        unix.listen(128)
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp.bind(('127.0.0.1', 8000))
        tcp.listen(128)
        server = uvicorn.Server(uvicorn.Config('src.main_refactored:app', log_level='info'))
        server.run(sockets=[tcp, unix])
    finally:
        tcp.close()
        unix.close()
        path.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
