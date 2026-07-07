"""Point d'entrée `python -m mcp_web` / `uv run --directory servers python -m mcp_web`."""
from . import server

if __name__ == "__main__":
    server.main()
