"""Quick verification script for /osc command implementation."""
from __future__ import annotations

from rich.console import Console

from echo_client.server import EchoServer
from echo_client.commands import build_command_specs

def main() -> None:
    console = Console()
    srv = EchoServer(console)
    specs = build_command_specs(srv)
    spec_map = {s.name: s for s in specs}

    osc_spec = spec_map.get("osc")
    if osc_spec is None:
        print("ERROR: /osc command not found!")
        return

    print(f"osc spec: {osc_spec.name}")
    print(f"  description: {osc_spec.description}")
    print(f"  min_args: {osc_spec.min_args}, max_args: {osc_spec.max_args}")

    if osc_spec.status_getter:
        print(f"  status: {osc_spec.status_getter(srv)}")

    # Test _cmd_osc directly
    print("\n--- Testing _cmd_osc ---")

    # Default state
    print(f"  Default osc_enabled: {srv.config.get('osc_enabled')}")
    print(f"  Default osc_address: {srv.config.get('osc_address')}")

    # Test /osc on
    srv._cmd_osc(["on"])
    print(f"  After 'on': enabled={srv.config.get('osc_enabled')}, address={srv.config.get('osc_address')}")

    # Test /osc off
    srv._cmd_osc(["off"])
    print(f"  After 'off': enabled={srv.config.get('osc_enabled')}")

    # Test /osc <custom>
    srv._cmd_osc(["192.168.1.1:9999"])
    print(f"  After custom: enabled={srv.config.get('osc_enabled')}, address={srv.config.get('osc_address')}")

    # Test /osc (no args)
    srv._cmd_osc([])
    print(f"  After empty: enabled={srv.config.get('osc_enabled')}")

    # Test invalid address
    srv._cmd_osc(["not-a-valid-address"])
    print(f"  After invalid: enabled={srv.config.get('osc_enabled')}, address={srv.config.get('osc_address')}")

    print("\n--- Testing _validate_osc_address ---")
    print(f"  '127.0.0.1:9000': {srv._validate_osc_address('127.0.0.1:9000')}")
    print(f"  'localhost:9000': {srv._validate_osc_address('localhost:9000')}")
    print(f"  '1.2.3.4:65535': {srv._validate_osc_address('1.2.3.4:65535')}")
    print(f"  'bad': {srv._validate_osc_address('bad')}")
    print(f"  ':9000': {srv._validate_osc_address(':9000')}")
    print(f"  '127.0.0.1:': {srv._validate_osc_address('127.0.0.1:')}")
    print(f"  '127.0.0.1:99999': {srv._validate_osc_address('127.0.0.1:99999')}")

    print("\nAll tests passed!")


if __name__ == "__main__":
    main()
