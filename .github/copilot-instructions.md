# Echo Client - Copilot Instructions

## Project Overview

Echo Client is a command-line console tool designed for the Echo-live/OBS workflow. It enables silent VTubers and content creators to efficiently control subtitle display. The application provides:

- A local WebSocket server that bridges with Echo-live in OBS
- Rich terminal interface with interactive commands
- Rich text formatting (Markdown + custom fast formatting syntax)
- Typewriting effects with pinyin/zhuyin support
- Optional WebUI server for web-based management

### Tech Stack

- **Language**: Python 3.9+
- **Core Dependencies**:
  - `websockets`: WebSocket server implementation
  - `rich`: Terminal UI and formatting
  - `pypinyin`: Chinese phonetic conversion for typewriting
  - `jieba`: Chinese text segmentation
  - `pyyaml`: Configuration file management
  - `markdown-it-py`: Markdown parsing
  - `aiohttp`: WebUI HTTP server (optional)

### Architecture

The project follows a modular structure:
- `cli.py`: Entry point and CLI initialization
- `server.py`: WebSocket server orchestration and console interaction
- `config.py`: Configuration loading/saving with YAML persistence
- `message.py`: Message parsing, formatting, and typewriting utilities
- `commands.py`: Command definitions and execution logic

## Development Setup

### Prerequisites

- Python 3.9 or higher
- Poetry for dependency management

### Installation

```bash
git clone https://github.com/xrh0905/echo-client.git
cd echo-client
poetry install
```

### Running the Application

```bash
poetry run echo-client
```

### Running Tests

Currently, the project does not have automated tests. Manual testing is performed by:
1. Running the application
2. Testing commands interactively
3. Connecting Echo-live client from OBS
4. Verifying message formatting and WebSocket communication

## Code Style and Conventions

### Python Style

- **Line Length**: Maximum 150 characters (configured in `.pylintrc`)
- **Type Hints**: Use type hints for all function signatures (from `__future__ import annotations`)
- **Docstrings**: Module-level and function-level docstrings required
- **Formatting**: Automated with `black` and `isort` via pre-commit hooks

### Linting

- **Primary Linter**: pylint (configured in `.pylintrc`)
- **Pre-commit Hooks**: 
  - check-yaml
  - trailing-whitespace
  - check-ast
  - black (code formatter)
  - isort (import sorter)
  - pylint

Run linting manually:
```bash
poetry run pylint echo_client/
```

### Code Organization

- Use `from __future__ import annotations` at the top of each module
- Organize imports: standard library → third-party → local
- Define `__all__` exports at the end of modules
- Keep functions focused and under 50 lines when possible
- Prefer composition over inheritance

### Naming Conventions

- **Modules**: lowercase with underscores (e.g., `message.py`, `config.py`)
- **Classes**: PascalCase (e.g., `EchoServer`, `CommandCatalog`)
- **Functions/Variables**: snake_case (e.g., `load_config`, `print_speed`)
- **Constants**: UPPER_SNAKE_CASE (e.g., `DEFAULT_CONFIG`, `CONFIG_FILENAME`)
- **Private**: Prefix with underscore (e.g., `_base_directory`, `_events`)

## Configuration Management

### Configuration File

- **Location**: `config.yaml` in the same directory as the executable
- **Format**: YAML with Unicode support
- **Loading**: Configuration is loaded on startup and reloaded on each message
- **Persistence**: Changes via commands are immediately saved to disk

### Default Values

All configuration keys have defaults defined in `config.py::DEFAULT_CONFIG`. When adding new configuration options:

1. Add the key to `DEFAULT_CONFIG` with a sensible default
2. Document it in `README.md` configuration table
3. Add a command to modify it if user-configurable
4. Ensure backward compatibility by using `config.get(key, default)`

### Configuration Schema

```python
{
    "command_prefix": str,          # Command prefix (default: "/")
    "username": str,                # Default username
    "host": str,                    # WebSocket host
    "port": int,                    # WebSocket port
    "typewriting": bool,            # Enable typewriting
    "typewriting_scheme": str,      # "pinyin" or "zhuyin"
    "autopause": bool,              # Auto-insert pauses
    "autopausestr": str,            # Characters triggering pauses
    "autopausetime": int,           # Pause duration multiplier
    "print_speed": int,             # Milliseconds per character
    "auto_quotes": bool,            # Auto-wrap with quotes
    "auto_parentheses": bool,       # Auto-wrap with parentheses
    "username_brackets": bool,      # Wrap username with 【】
    "inhibit_ctrl_c": bool,         # Disable Ctrl+C interrupt
    "auto_suffix": bool,            # Append custom suffix
    "auto_suffix_value": str,       # Suffix text (default: "喵")
    "enable_webui": bool,           # Enable WebUI server
    "webui_root": str,              # WebUI static files directory
    "webui_save_endpoint": str,     # WebUI save API endpoint
    "webui_websocket_path": str,    # WebUI WebSocket path prefix
}
```

## Command System

### Command Structure

Commands are defined in `commands.py` using the `CommandSpec` class:

```python
CommandSpec(
    name="command_name",
    aliases=["alias1", "alias2"],
    description="Command description",
    handler=async_handler_function,
    arg_hint="[optional] <required>",
)
```

### Adding New Commands

1. Define the command handler function in `commands.py` with signature:
   ```python
   async def cmd_name(server: EchoServer, arg: str) -> None:
       """Handler docstring."""
       # Implementation
   ```

2. Add the `CommandSpec` to the list returned by `build_command_specs()`

3. Update `README.md` command table with:
   - Command name and aliases
   - Description
   - Usage example

### Command Handler Guidelines

- Accept `server: EchoServer` and `arg: str` parameters
- Use `server.config` to access/modify configuration
- Call `server._persist_config()` after config changes
- Use `server.console.print()` for user feedback
- Handle edge cases (empty args, invalid values)
- Provide clear error messages with color coding:
  - `[green]` for success
  - `[yellow]` for warnings
  - `[red]` for errors

## Message Formatting

### Fast Formatting Syntax

The custom `@` syntax provides quick formatting:

- `@b`, `@i`, `@u`, `@s`: Bold, italic, underline, strikethrough
- `@[color]`: Set color (e.g., `@[#66ccff]`)
- `@+`, `@-`: Increase/decrease font size
- `@r`: Reset to default style
- `@{emoji}`: Insert emoji/image placeholder
- `@<class>`: Add CSS class (auto-prefixed with `echo-text-`)
- `@<:class>`: Add CSS class without prefix
- `\@`: Literal `@` character

### Markdown Support

Standard Markdown is also supported:
- `**bold**` or `__bold__`
- `*italic*` or `_italic_`
- `` `code` ``

### Message Processing Pipeline

1. Parse fast formatting (`@` syntax) → `parse_message()`
2. Parse Markdown → `render()`
3. Apply auto-quote/parentheses wrapping
4. Add typewriting data if enabled
5. Apply auto-pause markers if enabled
6. Build final message JSON

## WebSocket Communication

### Echo-live Protocol

Messages sent to Echo-live clients follow this structure:

```json
{
  "action": "echo_message_add",
  "data": {
    "username": "【Username】",
    "messages": [
      {
        "type": "text",
        "data": {
          "content": "Message content"
        }
      }
    ],
    "typewriting": {
      "mode": "pinyin",
      "delay": 30,
      "data": [{"type": "char", "data": "p'i'n'y'i'n"}]
    }
  }
}
```

### Client Connection Handling

- Each client gets a unique ID from `itertools.count()`
- Track client metadata: names, types, heartbeat counts
- Handle `hello`, `close`, and custom actions
- Broadcast ping every 30 seconds to maintain connection
- Support graceful disconnection on close action

### WebUI WebSocket

WebUI uses a multi-channel WebSocket system:
- Channels: `global`, `live`, `history`, `server`
- Messages to `global` broadcast to all channels
- Other channels broadcast only within themselves
- Message format includes `action`, `from`, `data`, `target` fields

## Async Patterns

### Event Loop Management

- Main entry is `asyncio.run(server.run())`
- Use `asyncio.create_task()` for background tasks
- Properly handle `KeyboardInterrupt` with console feedback

### Signal Handling

The application supports optional Ctrl+C protection:
- When `inhibit_ctrl_c` is enabled, SIGINT is temporarily ignored
- Original signal handler is restored when protection is disabled
- Use `_sync_sigint_guard()` to update signal state

### Concurrent Operations

- Input loop runs concurrently with WebSocket server
- Multiple clients can connect simultaneously
- Each client connection runs in its own task
- Use `asyncio.sleep(0)` to yield control in busy loops

## Packaging and Distribution

### PyInstaller

The project includes `echo-client.spec` for building standalone executables:

```bash
poetry install
pip install pyinstaller
pyinstaller echo-client.spec
```

Output: `dist/echo-client.exe` (or platform equivalent)

### Configuration in Bundled Apps

When running as a PyInstaller bundle:
- `sys.frozen` is True
- Configuration file is placed next to the executable
- Use `_base_directory()` to locate the config directory

## Common Tasks

### Adding a New Configuration Option

1. Add to `DEFAULT_CONFIG` in `config.py`
2. Add a toggle command in `commands.py`
3. Update README.md configuration table
4. Use the option in relevant code (e.g., `message.py`, `server.py`)

### Adding a New Message Format

1. Define parsing logic in `message.py`
2. Update `parse_message()` to handle the new syntax
3. Document in README.md message format section
4. Add examples to `message_sample.txt`

### Modifying WebSocket Protocol

1. Update message structure in `server.py`
2. Ensure backward compatibility when possible
3. Document protocol changes in README.md
4. Test with actual Echo-live client

## Internationalization

- The application primarily uses Chinese (Simplified) for UI messages
- String literals in console output should be in Chinese
- Code comments and docstrings should be in English
- Variable names and function names should be in English

## Security Considerations

- Configuration file may contain sensitive data (host, port)
- WebUI save endpoint writes to filesystem - validate paths
- WebSocket server binds to configurable host/port - document security implications
- No authentication mechanism - assume trusted network environment

## Performance Guidelines

- Typewriting conversion is performed for every message - keep efficient
- Configuration is reloaded on each message - optimize I/O
- WebSocket broadcasting should be async - use `asyncio.gather()` for parallel sends
- Avoid blocking operations in the main event loop

## Debugging Tips

- Use `console.print()` with Rich markup for readable debug output
- Check WebSocket connection with OBS browser source developer tools
- Verify configuration file location with startup messages
- Test typewriting output by examining the `typewriting.data` structure
- Use `/help` command to verify command registration

## Future Considerations

- Consider adding unit tests for message parsing
- Consider adding integration tests for WebSocket communication
- Consider supporting additional languages beyond Chinese
- Consider adding command history and autocomplete
- Consider migrating configuration to more structured validation (e.g., Pydantic)
