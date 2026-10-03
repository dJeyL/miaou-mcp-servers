"""Upstream stdio LEGACY de test : faux serveur JSON-RPC qui ne parle que
`initialize` (non collecté : pas de préfixe `test_`).

Bibliothèque standard seule — un vrai serveur SDK 1.x exigerait un
téléchargement, que la suite s'interdit. Il répond à `server/discover` ce que
répond un serveur 1.x à une méthode qu'il ne connaît pas (erreur JSON-RPC de
validation), puis sert `initialize`, `tools/list` et `tools/call`.

Argument : un chemin de fichier où chaque message reçu est écrit, une ligne JSON
par message, pour que le test compare ce que le proxy a émis.
"""

import json
import sys

INSTRUCTIONS = "consigne de l'upstream legacy"

_TOOL = {
    "name": "ping",
    "description": "Répond pong.",
    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
}


def _result(message: dict) -> dict | None:
    method = message.get("method")
    if method == "initialize":
        return {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "legacy-fixture", "version": "1.0"},
            "instructions": INSTRUCTIONS,
        }
    if method == "tools/list":
        return {"tools": [_TOOL]}
    if method == "tools/call":
        text = (message.get("params") or {}).get("arguments", {}).get("text", "")
        return {"content": [{"type": "text", "text": "pong " + text}], "isError": False}
    return None


def main() -> None:
    log = open(sys.argv[1], "a", encoding="utf-8")
    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        log.write(json.dumps(message, sort_keys=True) + "\n")
        log.flush()
        if "id" not in message:
            continue  # notification
        result = _result(message)
        if result is None:
            reply = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32602, "message": "Invalid request parameters"},
            }
        else:
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
