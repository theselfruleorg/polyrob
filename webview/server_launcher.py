import argparse
import logging
import uvicorn
import os
import sys
import time

def load_environment(env_file: str = "/etc/webview.env"):
    """Load environment variables from a deployment env file if present.

    SECURITY: never prints secret VALUES. The previous hand-rolled parser did
    ``print(f"  Set {key}={value}")`` for every line, leaking auth tokens / JWT
    secrets / API keys in cleartext into ``journalctl -u polyrob-webview.service``
    on every restart. Uses python-dotenv for correct quoting/escaping and logs
    only key NAMES (which are not secret).
    """
    if not os.path.exists(env_file):
        return
    try:
        from dotenv import dotenv_values, load_dotenv
        keys = [k for k in dotenv_values(env_file).keys() if k]
        load_dotenv(env_file, override=True)
    except Exception:
        # Value-safe fallback if python-dotenv is unavailable: parse without
        # ever echoing a value.
        keys = []
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    os.environ[key] = value.strip()
                    keys.append(key)
    print(f"Loaded {len(keys)} env var(s) from {env_file}: {', '.join(keys)}")

def setup_logging(log_level):
    """Set up proper logging configuration."""
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    
    # Configure root logger
    logging.basicConfig(
        level=numeric_level, 
        format=log_format,
        datefmt=date_format
    )
    
    # Also create a file handler to log to a file
    logs_dir = os.path.join(os.environ.get("WEBVIEW_INSTALL_PREFIX", "/opt/rob"), "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, "webview.log")
    
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(logging.Formatter(log_format, date_format))
    
    # Add the file handler to the root logger
    logging.getLogger('').addHandler(file_handler)
    
    # Log startup information
    logger = logging.getLogger("webview.launcher")
    logger.info(f"WebView server starting up, logging to {log_file}")
    logger.info(f"Python version: {sys.version}")
    
    return logger

def main():
    """Main entry point for WebView server."""
    # Load environment variables first
    load_environment()
    
    # Resolve bind defaults via the webgate config object: single-user (the
    # default) → 127.0.0.1:5050; multitenant → 0.0.0.0:5050. WEBGATE_HOST/PORT and
    # WEBVIEW_HOST/PORT env overrides are honored inside webgate; an explicit
    # --host/--port CLI flag still wins over all of them.
    from webview import webgate
    parser = argparse.ArgumentParser(description="Launch the POLYROB WebView server")
    parser.add_argument("--host", default=webgate.bind_host(),
                        help="Bind address")
    parser.add_argument("--port", type=int, default=webgate.bind_port(),
                        help="Port to listen on")
    parser.add_argument("--log-level", default=os.environ.get("WEBVIEW_LOG_LEVEL", "info"), 
                        help="Logging level")
    args = parser.parse_args()

    # Set up logging
    logger = setup_logging(args.log_level)
    
    # Add PYTHONPATH to sys.path if not already included
    pythonpath = os.environ.get("PYTHONPATH")
    if pythonpath and pythonpath not in sys.path:
        sys.path.insert(0, pythonpath)
        logger.info(f"Added {pythonpath} to sys.path")

    # Import the webview.server module
    try:
        from webview.server import app
        logger.info("Successfully imported webview.server app")
    except ImportError as e:
        logger.error(f"Failed to import webview.server: {e}")
        logger.error(f"Current sys.path: {sys.path}")
        sys.exit(1)

    # Log startup configuration
    logger.info(f"Starting server on {args.host}:{args.port}")
    logger.info(f"Environment: PYTHONPATH={os.environ.get('PYTHONPATH', 'not set')}")
    logger.info(f"Environment: DISPLAY={os.environ.get('DISPLAY', 'not set')}")
    
    # Run the Uvicorn server
    try:
        uvicorn.run(
            app, 
            host=args.host, 
            port=args.port, 
            log_level=args.log_level.lower(),
            access_log=True
        )
    except Exception as e:
        logger.error(f"Failed to start server: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
