"""Logique pure de recherche texte (fold, parsing de requête, matching, snippets).

Aucune dépendance à une lib de parsing de document (pymupdf/openpyxl/docx/pptx) —
ce module ne connaît que des `str` déjà extraits par `formats.py`. C'est cette
frontière qui rend le matching testable en isolation (fixtures binaires inutiles
pour tester `fold`/`parse_query`/`match_unit`/`make_snippet`).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from .session import ToolError

# Ligatures que unicodedata.normalize("NFKD", …) ne décompose pas — ajout manuel
# pour que "sœur"/"nœud" matchent une requête "soeur"/"noeud" et réciproquement.
_LIGATURE_MAP = str.maketrans({"œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE"})


def fold(text: str) -> str:
    """Casse et accents neutralisés : lowercase + décomposition Unicode NFKD +
    suppression des diacritiques combinants + ligatures œ/æ mappées à la main."""
    text = text.translate(_LIGATURE_MAP).lower()
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


@dataclass(frozen=True)
class Query:
    terms: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.terms


def parse_query(query: str) -> Query:
    """Découpe une requête en termes AND. `"phrase exacte"` compte comme un seul
    terme (espaces internes conservés) ; le reste est découpé sur les espaces.
    Un guillemet non fermé n'est pas une erreur : le reste de la chaîne à partir
    du guillemet orphelin est traité comme des termes normaux."""
    if not query or not query.strip():
        raise ToolError("requête de recherche vide")

    terms: list[str] = []
    i = 0
    n = len(query)
    while i < n:
        c = query[i]
        if c.isspace():
            i += 1
            continue
        if c == '"':
            end = query.find('"', i + 1)
            if end == -1:
                # Guillemet non fermé : le reste devient des termes normaux.
                terms.extend(query[i + 1 :].split())
                break
            phrase = query[i + 1 : end]
            if phrase.strip():
                terms.append(phrase)
            i = end + 1
            continue
        end = i
        while end < n and not query[end].isspace():
            end += 1
        terms.append(query[i:end])
        i = end

    if not terms:
        raise ToolError("requête de recherche vide")
    return Query(terms=terms)


@dataclass(frozen=True)
class Hit:
    term: str
    offset: int
    length: int


def match_unit(raw_text: str, query: Query) -> list[Hit]:
    """AND implicite : tous les termes de `query` doivent apparaître dans
    `raw_text` (folded pour la comparaison), sinon aucun hit n'est renvoyé.
    Chaque terme est compté par **toutes** ses occurrences (pas seulement la
    première) : la liste renvoyée est le vrai décompte d'occurrences de l'unité,
    trié par offset croissant pour que les snippets sortent dans l'ordre du texte
    (plusieurs occurrences d'un même terme forment plusieurs Hit distincts).
    Les offsets renvoyés pointent dans `raw_text` non foldé (folding préserve
    la longueur caractère à caractère à l'exception des ligatures/diacritiques
    composés, donc on refolde terme par terme pour retrouver la position brute
    via une recherche sur le texte foldé, dont les offsets sont réutilisés tel
    quels — foldé et brut ont la même longueur dans l'immense majorité des cas
    FR ; les rares décalages dus à NFKD sont acceptés pour un snippet indicatif)."""
    folded_text = fold(raw_text)
    hits: list[Hit] = []
    for term in query.terms:
        folded_term = fold(term)
        if not folded_term:
            continue
        term_len = len(folded_term)
        pos = folded_text.find(folded_term)
        if pos == -1:
            # AND-gate : un terme absent invalide toute l'unité.
            return []
        # Toutes les occurrences non chevauchantes de ce terme.
        while pos != -1:
            hits.append(Hit(term=term, offset=pos, length=term_len))
            pos = folded_text.find(folded_term, pos + term_len)
    hits.sort(key=lambda h: h.offset)
    return hits


def make_snippet(raw_text: str, offset: int, length: int, radius: int = 80) -> str:
    """Fenêtre de `raw_text` centrée sur `[offset, offset+length)`, bornée par
    `radius` caractères de chaque côté, avec ellipses `…` si tronqué et espaces
    normalisés (une seule ligne, pas de retours à la ligne bruts)."""
    start = max(0, offset - radius)
    end = min(len(raw_text), offset + length + radius)
    window = raw_text[start:end]
    window = " ".join(window.split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(raw_text) else ""
    return f"{prefix}{window}{suffix}"


# ---------------------------------------------------------------------------
# Orchestration (impure côté assemblage, mais sans lib de parsing de document)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UnitResult:
    """Une unité (page/feuille/slide/membre zip) ayant au moins un hit.

    `label` doit être directement réutilisable comme selector `read` (ou `path`
    pour un membre de zip) — c'est le contrat de round-trip du brief."""

    label: str
    hit_count: int
    snippets: list[str]


def snippets_for_unit(raw_text: str, query: Query, radius: int = 80) -> list[str]:
    """Un snippet par occurrence matchée, dans l'ordre d'apparition dans le texte
    (toutes occurrences de tous les termes, cf. `match_unit`)."""
    hits = match_unit(raw_text, query)
    return [make_snippet(raw_text, h.offset, h.length, radius) for h in hits]


def render_results(kind: str, query: str, unit_results: list[UnitResult], cap: int) -> str:
    """Formate les résultats groupés par unité, plafonnés à `cap` snippets au
    total (nombre de snippets, pas de caractères — cap dédié à `search`,
    distinct de READ_CAP). Troncature signalée explicitement."""
    if not unit_results:
        return f"Aucun résultat pour {query!r} ({kind})."

    total_hits = sum(r.hit_count for r in unit_results)
    lines = [f"Recherche {query!r} — {total_hits} occurrence(s) dans {len(unit_results)} unité(s) :"]

    shown = 0
    truncated = False
    for result in unit_results:
        if shown >= cap:
            truncated = True
            break
        lines.append(f"\n--- {result.label} ({result.hit_count} occurrence(s)) ---")
        for snippet in result.snippets:
            if shown >= cap:
                truncated = True
                break
            lines.append(f"  {snippet}")
            shown += 1

    if truncated:
        remaining = total_hits - shown
        lines.append(f"\n[Tronqué à {cap} snippets — {remaining} occurrence(s) supplémentaire(s) non affichée(s)]")

    return "\n".join(lines)
