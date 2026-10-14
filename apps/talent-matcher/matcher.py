"""Recherche de candidats LinkedIn correspondant à une offre, avec Claude.

Claude lit l'offre, lance lui-même des recherches web limitées à linkedin.com,
puis renvoie une shortlist notée via l'outil `submit_candidates`.
Seuls les profils réellement vus dans les résultats de recherche sont conservés.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

import anthropic

MODEL = "claude-opus-5-5"
MAX_TURNS = 8

ContractType = Literal["freelance", "employee", "any"]

CONTRACT_LABELS = {
    "freelance": "freelance / indépendant uniquement",
    "employee": "salarié (CDI / CDD)",
    "any": "indifférent (freelance ou salarié)",
}

SYSTEM_PROMPT = """Tu es un sourceur de talents expérimenté. On te confie une offre d'emploi ou une description de mission, et tu dois trouver de vraies personnes sur LinkedIn qui y correspondent.

Méthode :
1. Identifie les critères clés : intitulés de poste équivalents (français et anglais), compétences indispensables, compétences appréciées, séniorité, localisation.
2. Lance plusieurs recherches web ciblées sur les profils LinkedIn publics (pages linkedin.com/in/). Varie les intitulés et les combinaisons de compétences. Si l'on cherche des freelances, ajoute des termes comme freelance, indépendant, consultant indépendant ou TJM.
3. Ne retiens que des personnes (pas d'entreprises, d'offres d'emploi ni de posts). Pour chacune, appuie-toi uniquement sur ce que montrent les résultats : n'invente ni expérience, ni compétence, ni URL. Recopie l'URL du profil exactement telle qu'elle apparaît dans les résultats.
4. Note chaque candidat de 0 à 100 : 90+ correspond sur le poste, les compétences clés et la localisation ; 60-89 bon potentiel ; 30-59 partiel ; en dessous, ne le retiens pas.
5. freelance_signal vaut "yes" si le profil se présente comme freelance / indépendant / consultant indépendant, "no" s'il occupe clairement un poste salarié, sinon "unknown".

Quand tes recherches sont terminées, appelle l'outil submit_candidates une seule fois avec la shortlist triée du meilleur au moins bon. Rédige les textes en français."""

SUBMIT_TOOL = {
    "name": "submit_candidates",
    "description": "Enregistre la shortlist finale des candidats trouvés et l'analyse de l'offre. À appeler une seule fois, à la fin.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["offer_summary", "must_have", "nice_to_have", "seniority", "queries_used", "candidates"],
        "properties": {
            "offer_summary": {"type": "string", "description": "Le profil recherché en deux phrases."},
            "must_have": {"type": "array", "items": {"type": "string"}},
            "nice_to_have": {"type": "array", "items": {"type": "string"}},
            "seniority": {"type": "string"},
            "queries_used": {"type": "array", "items": {"type": "string"}},
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "headline", "linkedin_url", "location", "freelance_signal",
                                 "seniority", "score", "strengths", "gaps", "summary"],
                    "properties": {
                        "name": {"type": "string"},
                        "headline": {"type": "string"},
                        "linkedin_url": {"type": "string"},
                        "location": {"type": "string"},
                        "freelance_signal": {"type": "string", "enum": ["yes", "no", "unknown"]},
                        "seniority": {"type": "string"},
                        "score": {"type": "integer"},
                        "strengths": {"type": "array", "items": {"type": "string"}},
                        "gaps": {"type": "array", "items": {"type": "string"}},
                        "summary": {"type": "string"},
                    },
                },
            },
        },
    },
}

PROFILE_RE = re.compile(r"linkedin\.com/in/([^/?#\s]+)", re.IGNORECASE)


class MatchError(Exception):
    """Erreur présentable à l'utilisateur."""


@dataclass
class SearchRequest:
    description: str
    title: str = ""
    contract_type: ContractType = "any"
    location: str = ""
    remote: bool = False
    seniority: str = ""
    max_candidates: int = 15


@dataclass
class SearchResult:
    offer: dict[str, Any]
    criteria: dict[str, Any]
    queries: list[str]
    stats: dict[str, int]
    candidates: list[dict[str, Any]] = field(default_factory=list)


def _profile_slug(url: str) -> str | None:
    m = PROFILE_RE.search(url or "")
    return m.group(1).rstrip("/").lower() if m else None


def _user_prompt(req: SearchRequest) -> str:
    return (
        f"Type de profil recherché : {CONTRACT_LABELS[req.contract_type]}\n"
        f"Localisation : {req.location or 'non précisée'}{' (télétravail possible)' if req.remote else ''}\n"
        f"Séniorité souhaitée : {req.seniority or 'non précisée'}\n"
        f"Intitulé : {req.title or 'non précisé'}\n"
        f"Nombre de candidats souhaités : jusqu'à {req.max_candidates}\n\n"
        f"<offre>\n{req.description}\n</offre>"
    )


def find_candidates(
    req: SearchRequest,
    client: anthropic.Anthropic | None = None,
    on_progress: Callable[[str], None] = lambda _msg: None,
) -> SearchResult:
    if client is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise MatchError("Clé API Anthropic manquante : renseignez ANTHROPIC_API_KEY dans le fichier .env.")
        client = anthropic.Anthropic()
    tools = [
        {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": 12,
            "allowed_domains": ["linkedin.com"],
        },
        SUBMIT_TOOL,
    ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": _user_prompt(req)}]
    seen_slugs: set[str] = set()
    searches = 0
    submission: dict[str, Any] | None = None
    reminded = False

    on_progress("Analyse de l'offre et recherche des profils LinkedIn")
    for _ in range(MAX_TURNS):
        try:
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
                output_config={"effort": "medium"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise MatchError("Clé API Anthropic invalide : vérifiez ANTHROPIC_API_KEY.") from e
        except anthropic.RateLimitError as e:
            raise MatchError("Limite de requêtes Anthropic atteinte, réessayez dans une minute.") from e
        except anthropic.APIStatusError as e:
            raise MatchError(f"Erreur de l'API Anthropic ({e.status_code}) : {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise MatchError("Impossible de joindre l'API Anthropic (réseau).") from e

        if response.stop_reason == "refusal":
            raise MatchError("Claude a refusé de traiter cette demande.")

        tool_uses = []
        for block in response.content:
            if block.type == "server_tool_use" and block.name == "web_search":
                searches += 1
            elif block.type == "web_search_tool_result" and isinstance(block.content, list):
                for r in block.content:
                    slug = _profile_slug(getattr(r, "url", ""))
                    if slug:
                        seen_slugs.add(slug)
            elif block.type == "tool_use":
                tool_uses.append(block)
        on_progress(f"{searches} recherche(s) effectuée(s), {len(seen_slugs)} profil(s) repéré(s)")

        submit = next((b for b in tool_uses if b.name == "submit_candidates"), None)
        if submit is not None:
            submission = submit.input
            break

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue  # la recherche web reprend automatiquement
        if tool_uses:
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                 "content": "Outil inconnu. Utilise web_search puis submit_candidates."} for b in tool_uses
            ]})
            continue
        if reminded:
            break
        reminded = True
        messages.append({"role": "user", "content": "Appelle maintenant submit_candidates avec la shortlist (éventuellement vide)."})

    if submission is None:
        raise MatchError("La recherche n'a pas abouti. Réessayez ou reformulez l'offre.")

    on_progress("Vérification et classement des candidats")
    kept: dict[str, dict[str, Any]] = {}
    for c in submission.get("candidates", []):
        slug = _profile_slug(c.get("linkedin_url", ""))
        # On écarte toute URL qui n'est pas apparue dans les résultats de recherche.
        if not slug or slug not in seen_slugs or slug in kept:
            continue
        score = max(0, min(100, int(c.get("score", 0))))
        if score < 30:
            continue
        if req.contract_type == "freelance" and c.get("freelance_signal") == "no":
            continue
        kept[slug] = {
            "name": c.get("name", "").strip(),
            "headline": c.get("headline", "").strip(),
            "url": f"https://www.linkedin.com/in/{slug}",
            "score": score,
            "freelance": c.get("freelance_signal", "unknown"),
            "location": c.get("location", ""),
            "seniority": c.get("seniority", ""),
            "strengths": c.get("strengths", []),
            "gaps": c.get("gaps", []),
            "summary": c.get("summary", ""),
        }

    candidates = sorted(kept.values(), key=lambda c: c["score"], reverse=True)[: req.max_candidates]
    return SearchResult(
        offer={
            "title": req.title,
            "contractType": req.contract_type,
            "location": req.location,
            "summary": submission.get("offer_summary", ""),
        },
        criteria={
            "mustHave": submission.get("must_have", []),
            "niceToHave": submission.get("nice_to_have", []),
            "seniority": submission.get("seniority", ""),
        },
        queries=submission.get("queries_used", []),
        stats={"queries": searches, "profilesFound": len(seen_slugs), "returned": len(candidates)},
        candidates=candidates,
    )
