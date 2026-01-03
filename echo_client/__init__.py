"""High-level package exports for echo_client.

This project is based on the original echo-client by Rickyxrc:
https://github.com/Rickyxrc/echo-client

It works with Echo-Live, a subtitle display system by sheep-realms:
https://github.com/sheep-realms/Echo-Live
"""

from __future__ import annotations

from .server import EchoServer
from .cli import main

__all__ = ["EchoServer", "main"]
