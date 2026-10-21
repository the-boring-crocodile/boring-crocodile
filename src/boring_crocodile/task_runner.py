"""MCP configuration adapter for the shared contained task runner."""
from __future__ import annotations
import argparse
import asyncio
import os
from pathlib import Path
from boring_worker_kit.task_runner import serve
from .settings import Settings

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--environment', required=True)
    args = parser.parse_args()
    if os.getuid() == 0:
        raise SystemExit("Task runners must use a fixed ordinary identity, never root")
    settings = Settings.load(args.config).environments[args.environment]
    asyncio.run(serve(settings))


if __name__ == '__main__':
    main()
