# Echo Client - Copilot Instructions

## Project Overview

Echo Client is a command-line console tool designed for the Echo-live/OBS workflow, specifically for silent VTubers and content creators who need to send subtitles in batches. It provides:

- A local WebSocket server that listens for Echo-live broadcast connections
- An interactive CLI with Rich-based colorful terminal output
- Support for rich text formatting with Markdown and fast formatting shortcuts
- Typewriting effects with automatic pause insertion
- Configurable message decorations (quotes, parentheses, username brackets)
- Programmable message suffix support (e.g., auto-append "喵")
- Interrupt protection (Ctrl+C guard)
- Batch script execution via `/source` command
- Cross-platform packaging support with PyInstaller

## Key Technologies

- **Language**: Python 3.9+
- **WebSocket Server**: `websockets` library (asyncio-based)
- **CLI Framework**: `rich` for terminal UI
- **Chinese Processing**: `jieba` for word segmentation, `pypinyin` for pinyin conversion
- **Markdown Parsing**: `markdown-it-py`
- **Configuration**: PyYAML for YAML-based config files
- **Package Management**: Poetry
- **Code Quality**: pylint, black, isort, pre-commit hooks

## Project Structure

```
echo-client/
├── echo_client/           # Main package
│   ├── __init__.py       # Package initialization
│   ├── cli.py            # CLI entry point (main function)
│   ├── server.py         # EchoServer class - WebSocket server and console orchestration
│   ├── commands.py       # Command parsing and catalog system
│   ├── config.py         # Configuration loading/saving (config.yaml)
│   └── message.py        # Message parsing, rendering, typewriting, and formatting
├── main.py               # Compatibility entry point
├── pyproject.toml        # Poetry dependencies and project metadata
├── poetry.lock           # Locked dependencies
├── .pylintrc             # Pylint configuration
├── .pre-commit-config.yaml # Pre-commit hooks configuration
├── echo-client.spec      # PyInstaller spec for building executables
└── message_sample.txt    # Example script demonstrating all features
```

## Echo-live WebSocket Protocol

Echo Client implements a WebSocket server that Echo-live connects to as a client. This section provides comprehensive documentation about the protocol.

### Overview

The WebSocket protocol enables bidirectional communication between Echo Client (server) and Echo-live (client). Echo Client sends message data and control commands, while Echo-live sends status updates and events back.

### Complete Protocol Documentation References

#### Message Format Documentation
- **Base Message Format**: https://echo-live-doc.pages.dev/message/base/
- **Start Paragraph**: https://echo-live-doc.pages.dev/message/start-par/ - Controls paragraph-level display
- **Style Formatting**: https://echo-live-doc.pages.dev/message/style/ - Text styling (bold, italic, color, size, etc.)
- **Pause Events**: https://echo-live-doc.pages.dev/message/pause/ - Insert timed pauses in message display
- **General Events**: https://echo-live-doc.pages.dev/message/event/ - Event system for typewriting and animations
- **Paragraph Management**: https://echo-live-doc.pages.dev/message/paragraph/ - Multi-paragraph message handling

#### Broadcast API Documentation
- **Broadcast Overview**: https://echo-live-doc.pages.dev/dev/broadcast/ - Main broadcast protocol page
- **API Reference**: https://echo-live-doc.pages.dev/dev/broadcast/api/ - Complete API listing

#### Broadcast API Actions (Server → Client)

**Connection & Lifecycle:**
- **hello**: https://echo-live-doc.pages.dev/dev/broadcast/api/hello/ - Initial handshake from Echo-live client
- **ping**: https://echo-live-doc.pages.dev/dev/broadcast/api/ping/ - Heartbeat mechanism for connection monitoring
- **websocket_heartbeat**: https://echo-live-doc.pages.dev/dev/broadcast/api/websocket_heartbeat/ - Alternative heartbeat format
- **close**: https://echo-live-doc.pages.dev/dev/broadcast/api/close/ - Graceful connection closure
- **websocket_close**: https://echo-live-doc.pages.dev/dev/broadcast/api/websocket_close/ - WebSocket-level close event
- **shutdown**: https://echo-live-doc.pages.dev/dev/broadcast/api/shutdown/ - Server shutdown notification

**Display State Management:**
- **page_visible**: https://echo-live-doc.pages.dev/dev/broadcast/api/page_visible/ - Page becomes visible (tab focused)
- **page_hidden**: https://echo-live-doc.pages.dev/dev/broadcast/api/page_hidden/ - Page becomes hidden (tab unfocused)
- **live_display_update**: https://echo-live-doc.pages.dev/dev/broadcast/api/live_display_update/ - Display state changed
- **set_live_display**: https://echo-live-doc.pages.dev/dev/broadcast/api/set_live_display/ - Set display visibility

**Message Control:**
- **echo_next**: https://echo-live-doc.pages.dev/dev/broadcast/api/echo_next/ - Skip to next message
- **echo_printing**: https://echo-live-doc.pages.dev/dev/broadcast/api/echo_printing/ - Typewriting progress update
- **echo_state_update**: https://echo-live-doc.pages.dev/dev/broadcast/api/echo_state_update/ - Message state changed
- **history_clear**: https://echo-live-doc.pages.dev/dev/broadcast/api/history_clear/ - Clear message history

**Error Handling:**
- **error**: https://echo-live-doc.pages.dev/dev/broadcast/api/error/ - General error response
- **error_unknown**: https://echo-live-doc.pages.dev/dev/broadcast/api/error_unknown/ - Unknown action error

### Message Structure

Messages sent to Echo-live follow this general structure:
```json
{
  "action": "send",
  "data": {
    "username": "Someone",
    "messages": [...],  // Array of message segments with formatting
    "events": [...]     // Optional events like typewriting or pause
  }
}
```

### Core Message Components

#### 1. Message Segments (`messages` array)
Each message consists of segments with text and optional styling:
```json
{
  "text": "Hello",
  "style": {
    "color": "#66ccff",
    "bold": true,
    "italic": false,
    "size": "middle"
  }
}
```

Supported style properties:
- **color**: Hex color code or named color
- **bold**: Boolean for bold text
- **italic**: Boolean for italic text
- **underline**: Boolean for underlined text
- **strike**: Boolean for strikethrough
- **size**: String - "extra-small", "small", "middle", "large", "extra-large"
- **className**: String - CSS class name (optionally prefixed with "echo-text-")

#### 2. Events Array
Events control display timing and animations:

**Pause Event:**
```json
{
  "name": "pause",
  "duration": 500  // milliseconds
}
```

**Typewriting Event:**
```json
{
  "name": "echo",
  "text": "你好",
  "data": "ni3 hao3",  // pinyin or zhuyin representation
  "speed": 10  // ms per character
}
```

#### 3. Paragraph Control
Use `start-par` property to control paragraph display:
```json
{
  "action": "send",
  "data": {
    "username": "Someone",
    "messages": [...],
    "start-par": false  // Continue in same paragraph vs. new paragraph
  }
}
```

### Client-to-Server Messages

Echo-live clients send these actions to the server:

1. **hello** - Initial connection with client info
2. **ping** - Heartbeat to maintain connection
3. **page_visible/page_hidden** - Tab visibility changes
4. **echo_state_update** - Message display state changes
5. **echo_printing** - Typewriting progress updates
6. **live_display_update** - Display visibility changes
7. **close** - Graceful disconnect notification

### Server Response Pattern

For most client messages, the server should:
1. Update internal state (client tracking, heartbeat counts, visibility)
2. Optionally respond with acknowledgment (not required for heartbeats)
3. Log events to console for debugging

### Implementation in Echo Client

The `EchoServer` class handles the protocol:
- `_handle_client()` - Main WebSocket handler
- Tracks client state in multiple dicts (IDs, names, types, visibility, heartbeat counts)
- Sends messages via `_broadcast_to_websocket()`
- Processes incoming events and updates console display

### Protocol Best Practices

1. **Always send valid JSON** - Echo-live will reject malformed messages
2. **Include username** - Required field for all "send" actions
3. **Validate message structure** - Use the parsing functions in `message.py`
4. **Handle disconnects gracefully** - Clean up client state on disconnect
5. **Monitor heartbeats** - Track ping messages to detect dead connections
6. **Respect display visibility** - Don't send messages when page is hidden (optional)
7. **Use typewriting carefully** - Requires proper text segmentation for Chinese

## Development Workflow

### Setup
```bash
poetry install          # Install all dependencies
poetry run echo-client  # Run the application
```

### Linting and Code Quality
```bash
poetry run pylint echo_client/        # Run pylint
poetry run black echo_client/         # Format code (via pre-commit)
poetry run isort echo_client/         # Sort imports (via pre-commit)
pre-commit run --all-files            # Run all pre-commit hooks
```

### Building Executables
```bash
pip install pyinstaller
pyinstaller echo-client.spec          # Creates dist/echo-client.exe
```

## Code Style and Conventions

### Python Style
- Follow PEP 8 with max line length of 150 characters (see `.pylintrc`)
- Use type hints (`from __future__ import annotations`)
- Docstrings for modules (present) but not strictly enforced for all functions
- Pylint score target: 9.5+ out of 10

### Naming Conventions
- Classes: PascalCase (e.g., `EchoServer`, `CommandCatalog`)
- Functions/Methods: snake_case (e.g., `load_config`, `parse_message`)
- Constants: UPPER_SNAKE_CASE (e.g., `PING_PAYLOAD`, `DEFAULT_PRINT_SPEED`)
- Private methods: prefix with `_` (e.g., `_cmd_help`)

### Async/Await Patterns
- Use `asyncio` for all WebSocket and I/O operations
- Server runs in `asyncio.run(server.run())`
- WebSocket handlers are async coroutines
- Console I/O uses `asyncio.to_thread()` for blocking operations

## Configuration System

The application uses YAML-based configuration stored in `config.yaml`:

### Key Configuration Fields
- **Server Settings**: `host` (default: 127.0.0.1), `port` (default: 3000)
- **Display Settings**: `username`, `username_brackets`
- **Typewriting**: `typewriting`, `typewriting_scheme` (pinyin/zhuyin), `print_speed`
- **Auto Features**: `autopause`, `auto_quotes`, `auto_parentheses`, `auto_suffix`
- **Control**: `inhibit_ctrl_c`, `command_prefix` (default: `/`)

Configuration is hot-reloaded on each message send, allowing runtime modifications.

## Message Formatting System

Echo Client supports two overlay formatting systems:

### 1. Markdown Syntax
- `**text**` or `__text__` → Bold
- `*text*` or `_text_` → Italic
- `` `code` `` → Code style

### 2. Fast Formatting (@ prefix)
- Style: `@b` (bold), `@i` (italic), `@u` (underline), `@s` (strikethrough)
- Color: `@[#66ccff]` or `@[color-name]`
- Size: `@+` (larger), `@-` (smaller) - can stack
- Reset: `@r` (restore default)
- Emoji: `@{emoji-id}`
- CSS Class: `@<classname>` (adds `echo-text-` prefix) or `@<:classname>` (raw)
- Literal: `\@` → `@`

Both systems can be combined in the same message.

## Command System

Commands use a pluggable catalog system defined in `commands.py`:

### Command Structure
- Commands start with `command_prefix` (default: `/`)
- Support aliases (e.g., `/h`, `/?` for `/help`)
- Can have arguments and usage hints
- Toggle commands show current state
- Input starting with `//` sends literal `/` text

### Adding New Commands
1. Add command method to `EchoServer` (prefix with `_cmd_`)
2. Define `CommandSpec` in `build_command_specs()`
3. Register aliases and argument hints
4. Update status display if it's a toggle command

## Testing

Currently, the project does not have automated unit tests. Testing is done manually by:
1. Running the application with `poetry run echo-client`
2. Executing commands interactively
3. Using `/source message_sample.txt` for comprehensive feature testing
4. Connecting with Echo-live in OBS to test WebSocket protocol

## Common Pitfalls and Gotchas

1. **Async Context**: All WebSocket operations must be async. Use `asyncio.to_thread()` for blocking console I/O.
2. **Config Reloading**: Config is reloaded on each message send, not on server start. Changes take effect immediately.
3. **Message Escaping**: Double `//` at start escapes to send literal `/` text. The `@` character uses `\@` for escaping.
4. **Client Tracking**: Server maintains multiple dicts for client state (IDs, names, types, visibility, heartbeat counts).
5. **Signal Handling**: When `inhibit_ctrl_c` is enabled, SIGINT is ignored. Use `/quit` or disable with `/nocc`.
6. **Typewriting**: Requires jieba word segmentation for Chinese text, then converts to pinyin/zhuyin per character.

## Dependencies and Security

- Keep Poetry dependencies updated via `poetry update`
- Major dependencies:
  - `websockets ^12.0` - WebSocket server
  - `rich ^13.7.0` - Terminal UI
  - `pypinyin ^0.50.0` - Pinyin conversion
  - `pyyaml ^6.0.1` - Config parsing
  - `jieba ^0.42.1` - Chinese segmentation
  - `markdown-it-py ^3.0.0` - Markdown parsing
  - `aiohttp ^3.9.0` - Async HTTP (if needed)

## Internationalization

The project is primarily designed for Chinese-speaking users:
- UI messages and console output are in Chinese
- Documentation (README.md) is in Chinese
- Supports both Simplified Chinese input and processing
- Typewriting supports pinyin (拼音) and zhuyin (注音/Bopomofo) schemes

## Future Development Considerations

When extending the project:
- Maintain backward compatibility with config.yaml format
- Ensure new commands follow the established pattern
- Keep the WebSocket protocol aligned with Echo-live specifications
- Test with actual Echo-live instances in OBS
- Update `message_sample.txt` with examples of new features
- Consider performance impact of message parsing (it's per-message)
