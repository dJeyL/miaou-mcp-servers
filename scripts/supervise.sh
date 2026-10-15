#!/usr/bin/env bash
# Superviseur du proxy MCP : relance la commande tant qu'elle sort en 42 ou 43.
#
# Le proxy sort en 42 après une mise à jour appliquée depuis MIAOU (clef
# `self_update` de la config) ; relancé ici par `uv run`, il repart sur le
# nouveau code avec ses dépendances à jour. Il sort en 43 sur SIGUSR1, au lieu
# de s'exec sur place, et sur demande d'un client (miaou/lifecycle/restart).
# Tout autre code de sortie arrête la boucle et est rendu tel quel (0 : arrêt
# normal, Ctrl-C compris).
# Pose MIAOU_PROXY_SUPERVISED=1 : sans elle, le proxy refuse d'appliquer une
# mise à jour, faute de quelqu'un pour le relancer. Pose aussi
# MIAOU_PROXY_RESTART_BY_EXIT=1 : sans elle, le proxy ne sort pas en 43 et
# s'exec comme hors supervision — c'est ce qui garde en vie un proxy mis à
# jour sous un superviseur plus ancien, qui ne relance que sur 42.
#
# Bash seulement (macOS, Linux, Git Bash sous Windows). Deux usages :
#
#   Exécuté — relance la commande donnée, à l'identique :
#     scripts/supervise.sh                      # uv run mcp_proxy, depuis ce clone
#     scripts/supervise.sh uv run mcp_proxy --port 8765
#
#   Sourcé — `miaou_supervise <fonction> [args…]` appelle la fonction à
#   chaque tour : ce qu'elle calcule (proxy réseau détecté, par exemple) est
#   recalculé à chaque relance, pour un portable passé du site au télétravail
#   entre deux mises à jour.
#     launch() { detect_proxy; uv run --directory "$DIR" mcp_proxy $PROXY_ARG "$@"; }
#     . "$DIR/scripts/supervise.sh"
#     miaou_supervise launch "$@"
#
# Ce fichier ne construit jamais la ligne de lancement d'une installation :
# un fork l'appelle depuis son propre script, sans le modifier.

MIAOU_UPDATE_EXIT_CODE=42
MIAOU_RESTART_EXIT_CODE=43
MIAOU_SUPERVISE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

_miaou_rollback_hint() {
    local code="$1" state="$MIAOU_SUPERVISE_ROOT/state/self-update.json" before=""
    if [ -f "$state" ]; then
        before="$(sed -n 's/.*"before": *"\([0-9a-f]*\)".*/\1/p' "$state" | head -n 1)"
    fi
    echo "supervise: le proxy relancé après la mise à jour s'est arrêté en erreur (code $code)." >&2
    if [ -n "$before" ]; then
        echo "supervise: retour à la version d'avant : git -C '$MIAOU_SUPERVISE_ROOT' reset --hard $before" >&2
    else
        echo "supervise: version d'avant notée dans state/self-update.json (champ before)." >&2
    fi
}

miaou_supervise() {
    local launch="$1" code updated=0
    shift
    export MIAOU_PROXY_SUPERVISED=1 MIAOU_PROXY_RESTART_BY_EXIT=1
    while :; do
        "$launch" "$@"
        code=$?
        if [ "$code" -eq "$MIAOU_UPDATE_EXIT_CODE" ]; then
            updated=1
            echo "supervise: mise à jour appliquée, relance du proxy." >&2
        elif [ "$code" -eq "$MIAOU_RESTART_EXIT_CODE" ]; then
            # Le code mis à jour a démarré et tourné : un échec à la relance
            # vient d'autre chose, le retour arrière ne s'y rapporte plus.
            updated=0
            echo "supervise: redémarrage demandé (SIGUSR1 ou client MCP), relance du proxy." >&2
        else
            # Pas pour un arrêt par signal (128 + n : SIGTERM rend 143,
            # uvicorn relevant le signal après un arrêt propre).
            if [ "$updated" -eq 1 ] && [ "$code" -ne 0 ] && [ "$code" -lt 128 ]; then
                _miaou_rollback_hint "$code"
            fi
            return "$code"
        fi
    done
}

_miaou_run() {
    "$@"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    if [ "$#" -eq 0 ]; then
        set -- uv run --directory "$MIAOU_SUPERVISE_ROOT" mcp_proxy
    fi
    miaou_supervise _miaou_run "$@"
    exit $?
fi
