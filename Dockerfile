FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# `mcp_proxy` (le paquet local packagé par hatchling, cf. pyproject.toml) doit
# être présent AVANT `uv sync --frozen` : sinon uv installe l'environnement sans
# lui, puis le reconstruit et re-résout au premier `uv run` — au runtime, donc
# avec accès réseau requis à chaque démarrage du conteneur.
COPY pyproject.toml uv.lock ./
COPY mcp_proxy ./mcp_proxy
RUN uv sync --frozen --no-dev

# Le reste : upstreams "inprocess" (bench, weather, web, brave...) chargés dans
# le process du proxy, pas de subprocess — dev_auth_server.py n'est donc pas
# nécessaire (--with-dev-auth n'est pas utilisé ici).
COPY servers ./servers
COPY config.json ./config.json
# Copie du dist/ de MIAOU, servie sous /app/ si config.json porte
# "miaou_dist": "miaou_dist". Le dossier est versionné vide (.gitkeep) : l'image
# se construit sans copie préalable, et le proxy ne sert alors rien.
COPY miaou_dist ./miaou_dist

EXPOSE 8765

# --no-sync : le venv a déjà été synchronisé au build ; refaire une résolution
# à chaque démarrage exigerait un accès réseau (dépendances dev incluses).
CMD ["uv", "run", "--no-sync", "mcp_proxy"]
