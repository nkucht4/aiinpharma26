# WebSocket Migration Guide

## Overview

This project has been updated to support **WebSocket** as an alternative transport layer for simulator communication, while maintaining full backward compatibility with the existing REST API.

## Architecture Changes

### Transport Layer Options

1. **REST API (Default)** - Original HTTP-based communication
   - Uses standard HTTP GET/POST requests
   - Polling-based for data updates
   - Better for occasional data needs

2. **WebSocket (New)** - Persistent bidirectional connection
   - Real-time, event-driven updates
   - Lower latency
   - Reduced overhead for continuous monitoring
   - Automatic fallback to REST if WebSocket fails

## Enabling WebSocket

### For Simulator Communication (Engine → Manufacturing Simulator)

Set the environment variable:
```bash
USE_WEBSOCKET=1 python run.py
```

Or programmatically:
```python
from app.config import Config
cfg = Config(use_websocket=True)
```

### For Dashboard Communication (Browser → Server)

The dashboard automatically detects the server and:
1. Attempts to establish a WebSocket connection to `/ws`
2. Falls back to REST API if WebSocket is unavailable
3. No configuration needed - works transparently

## Components Modified

### Server-Side

#### `app/server.py` (Updated)
- Added WebSocket upgrade handler (`_handle_websocket`)
- WebSocket frame parsing and sending
- Message routing for commands: `state`, `control`, `lock`, `verify`, `trace`, `predictions`, `export`
- Broadcast mechanism for real-time state updates to all connected clients
- Automatic fallback support for static files and HTTP requests

#### `app/ws_client.py` (New)
- WebSocket client for simulator communication
- Connection establishment with handshake
- Frame encoding/decoding
- Automatic reconnection
- Matches `SimClient` interface for drop-in replacement

#### `app/engine.py` (Updated)
- Auto-selects client type based on `cfg.use_websocket`
- Falls back to `SimClient` if `WsClient` unavailable
- No logic changes - client swapping is transparent

#### `app/config.py` (Updated)
- New `use_websocket` config option (default: `False`)
- Environment variable: `USE_WEBSOCKET`

### Client-Side

#### `app/web/index.html` (Updated)
- Enhanced `api()` function with WebSocket support
- `connectWebSocket()` for establishing/managing connection
- Automatic fallback to REST if WebSocket fails
- Real-time state update handler

## Message Formats

### WebSocket Commands (Browser → Server)

```json
{
  "id": "unique_message_id",
  "cmd": "state|control|lock|verify|trace|predictions|export",
  "tail": 400,
  "line": "ASM-01",
  "action": "start|pause|step|settings",
  "interval": 3.0,
  "batch": 1,
  "auto_lock": false
}
```

### WebSocket Responses (Server → Browser)

```json
{
  "type": "state_update|control|lock|error",
  "id": "matching_message_id",
  "data": {},
  "error": "error message"
}
```

## Backward Compatibility

- ✅ All existing REST endpoints still work
- ✅ Dashboard works with either transport
- ✅ Engine accepts both `SimClient` and `WsClient`
- ✅ No breaking changes to configuration
- ✅ Automatic fallback ensures robustness

## Usage Examples

### Start with WebSocket to Simulator
```bash
USE_WEBSOCKET=1 python run.py
# Output: Simulator: https://manufacturingsimulator.onrender.com/simulator (WebSocket)
```

### Default REST API (unchanged)
```bash
python run.py
# Output: Simulator: https://manufacturingsimulator.onrender.com/simulator (REST API)
```

### Dashboard Access
```
Browser: http://127.0.0.1:8000
- Attempts WebSocket first (automatic)
- Falls back to REST if needed (transparent)
```

## Performance Benefits

| Metric | REST API | WebSocket |
|--------|----------|-----------|
| Connection Overhead | Per-request HTTP headers | One-time handshake |
| Latency | HTTP round-trip | Near-instant |
| Throughput | Request-response | Streaming |
| Real-time Updates | Polling interval | Event-driven |
| Server Load | Connection-per-request | Persistent connection |

## Troubleshooting

### WebSocket Connection Fails
1. Check firewall/proxy settings
2. Verify server supports WebSocket (check headers)
3. Check browser console for errors
4. System automatically falls back to REST API

### Enable Debug Logging
```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

### Verify Connection Status
- Browser DevTools → Network → WS filter
- Should show connection to `/ws`
- Frames tab shows live messages

## Migration Path

1. **Phase 1**: WebSocket available (current) - users opt-in with `USE_WEBSOCKET=1`
2. **Phase 2**: WebSocket default (future) - can be enabled with `USE_REST_API=1` to force REST
3. **Phase 3**: WebSocket-only (future) - REST API removed if WebSocket proves stable

## Implementation Details

### WebSocket Frame Format
- Standard RFC 6455 WebSocket protocol
- Text frames containing JSON messages
- Automatic masking/unmasking
- Supports fragmentation for large messages

### Connection Pooling
- Browser dashboard: Single persistent connection
- Engine: Connection per simulator line (optional)
- Automatic reconnection with exponential backoff

### Error Handling
- Invalid messages logged, connection remains open
- Server errors return `{"type": "error", "error": "message"}`
- Network failures trigger automatic fallback to REST
- Missing data defaults to empty/null values

## References

- [RFC 6455: WebSocket Protocol](https://tools.ietf.org/html/rfc6455)
- [Python WebSocket Handshake](https://docs.python.org/3/library/socket.html)
- [Browser WebSocket API](https://developer.mozilla.org/en-US/docs/Web/API/WebSocket)
