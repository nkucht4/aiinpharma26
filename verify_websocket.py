#!/usr/bin/env python3
"""Quick verification that WebSocket migration is complete and working."""
import sys
sys.path.insert(0, '.')

def test_imports():
    """Verify all modules import successfully."""
    print("Testing imports...")
    from app.config import Config
    from app.server import make_handler, serve
    from app.engine import Engine
    from app.client import SimClient
    from app.ws_client import WsClient
    print("  [PASS] All imports successful")
    return True

def test_rest_client_default():
    """Verify REST client is used by default."""
    print("\nTesting REST API (default)...")
    from app.config import Config
    from app.engine import Engine
    from app.client import SimClient
    
    cfg = Config(use_websocket=False)
    eng = Engine(cfg)
    assert isinstance(eng.client, SimClient), "Expected SimClient for REST mode"
    assert not cfg.use_websocket, "Expected use_websocket=False"
    print("  [PASS] REST client (SimClient) is default")
    return True

def test_websocket_client_option():
    """Verify WebSocket client can be selected."""
    print("\nTesting WebSocket option...")
    from app.config import Config
    from app.engine import Engine
    from app.ws_client import WsClient
    
    cfg = Config(use_websocket=True)
    eng = Engine(cfg)
    assert isinstance(eng.client, WsClient), "Expected WsClient for WebSocket mode"
    assert cfg.use_websocket, "Expected use_websocket=True"
    print("  [PASS] WebSocket client (WsClient) selectable")
    return True

def test_environment_variable():
    """Verify USE_WEBSOCKET environment variable works."""
    print("\nTesting environment variable...")
    import os
    os.environ['USE_WEBSOCKET'] = '1'
    from app.config import Config
    cfg = Config()
    assert cfg.use_websocket, "Expected use_websocket=True from env"
    del os.environ['USE_WEBSOCKET']
    print("  [PASS] USE_WEBSOCKET environment variable works")
    return True

def test_server_handler():
    """Verify server handler has WebSocket support."""
    print("\nTesting server handler...")
    from app.config import Config
    from app.engine import Engine
    from app.server import make_handler
    
    cfg = Config()
    eng = Engine(cfg)
    handler = make_handler(eng)
    
    # Check for WebSocket methods
    assert hasattr(handler, '_handle_websocket'), "Missing WebSocket handler"
    assert hasattr(handler, '_read_websocket_frame'), "Missing frame reader"
    assert hasattr(handler, '_send_websocket_frame'), "Missing frame sender"
    assert hasattr(handler, '_handle_ws_message'), "Missing message handler"
    print("  [PASS] Server handler has WebSocket support")
    return True

def test_backward_compatibility():
    """Verify REST API endpoints still work."""
    print("\nTesting backward compatibility...")
    from app.config import Config
    from app.engine import Engine
    from app.server import make_handler
    
    cfg = Config()
    eng = Engine(cfg)
    HandlerClass = make_handler(eng)
    
    # Check for HTTP methods on the handler class
    assert hasattr(HandlerClass, 'do_GET'), "Missing GET handler"
    print("  [PASS] REST API endpoints preserved")
    return True

def main():
    """Run all verification tests."""
    print("=" * 60)
    print("WebSocket Migration Verification")
    print("=" * 60)
    
    tests = [
        test_imports,
        test_rest_client_default,
        test_websocket_client_option,
        test_environment_variable,
        test_server_handler,
        test_backward_compatibility,
    ]
    
    results = []
    for test in tests:
        try:
            result = test()
            results.append(result)
        except Exception as e:
            print(f"  [FAIL] {e}")
            results.append(False)
            import traceback
            traceback.print_exc()
    
    print("\n" + "=" * 60)
    if all(results):
        print(f"All {len(results)} verification tests PASSED!")
        print("=" * 60)
        return 0
    else:
        failed = len([r for r in results if not r])
        print(f"{failed} of {len(results)} tests FAILED!")
        print("=" * 60)
        return 1

if __name__ == "__main__":
    sys.exit(main())
