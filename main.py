"""CLI entry point.

Usage: uv run python main.py --config config/config.yaml

AnyDoc is a stateless conversion service: there is no database and no migration step.
"""
import argparse
import sys

import uvicorn

from app import create_app
from src.config.config_manager import Config
from src.log import get_server_logger, setup_logger

logger = get_server_logger()


def main():
    parser = argparse.ArgumentParser(
        description="AnyDoc MCP server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", type=str, default="config/config.yaml", help="Path to the config file")
    parser.add_argument("--host", type=str, help="Override the server host")
    parser.add_argument("--port", type=int, help="Override the server port")
    parser.add_argument("--log-level", type=str,
                        choices=["critical", "error", "warning", "info", "debug", "trace"],
                        help="Override the log level")
    parser.add_argument("--transport", type=str, choices=["sse", "http"], help="Override the MCP transport")

    args = parser.parse_args()

    try:
        Config.set_config(args.config)
        config_obj = Config.get_config_model()
        if not config_obj:
            raise RuntimeError("ConfigModel not loaded.")

        # --- Logging ---
        log_conf = Config.get_logging_config()
        log_level = str(getattr(log_conf, 'level', 'INFO')).upper()
        if args.log_level:
            log_level = args.log_level.upper()

        setup_logger(
            console_level=log_level,
            file_level="DEBUG",
            log_base_dir=getattr(log_conf, 'log_dir', 'logs'),
        )
        logger = get_server_logger()

        # --- Engine availability ---
        # Say at startup what this machine has installed. A missing pandoc or soffice does
        # not stop the service; it silently removes the corresponding conversion paths at
        # runtime, and that kind of failure is hard to diagnose later.
        try:
            from src.domain.convert.registry import available_engines
            for name, ok in available_engines().items():
                (logger.info if ok else logger.warning)(
                    f"Engine {name}: {'available' if ok else 'NOT AVAILABLE - related conversion paths are disabled'}"
                )
        except Exception as e:
            logger.error(f"Engine availability check failed: {e}")

        # --- Server ---
        server_conf = Config.get_server_config()
        host = args.host or getattr(server_conf, 'host', '0.0.0.0')
        port = args.port or getattr(server_conf, 'port', 5055)
        transport = args.transport or getattr(server_conf, 'transport', 'http')

        logger.info(f"Starting server: http://{host}:{port}")
        logger.info(f"Swagger UI: http://{host}:{port}/docs")
        mcp_ep = "mcp" if transport == "http" else transport
        logger.info(f"MCP endpoint: http://{host}:{port}/{mcp_ep}")

        app_instance = create_app(config_obj, transport=transport)
        uvicorn.run(app_instance, host=host, port=port, log_level=log_level.lower())

    except FileNotFoundError as e:
        from loguru import logger as base_logger
        base_logger.error(f"Config not found: {e}")
        sys.exit(1)
    except Exception as e:
        from loguru import logger as base_logger
        base_logger.error(f"Startup failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
