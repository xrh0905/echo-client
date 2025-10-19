"""Command-line entry point for echo-client."""
from __future__ import annotations

import asyncio

from rich.console import Console

from .server import EchoServer


def main() -> None:
    """Run the interactive echo client helper."""
    console = Console()
    server = EchoServer(console)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        console.print("[yellow]服务器已停止[/yellow]")


__all__ = ["main"]
