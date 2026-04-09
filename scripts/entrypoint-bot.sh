#!/bin/sh
uv sync --no-dev
exec uv run --no-dev python main.py
