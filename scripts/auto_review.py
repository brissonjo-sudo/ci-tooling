#!/usr/bin/env python3
"""Relecture automatique des pull requests par Mistral.

Outillage partage : ce script vit dans brissonjo-sudo/ci-tooling et est appele
par le workflow reutilisable .github/workflows/auto-review.yml. Chaque depot
qui l'utilise n'a qu'un fichier de quelques lignes a ecrire.

Deroulement :

1. lecture de la configuration du depot appelant (facultative) ;
2. recuperation du diff et des metadonnees de la PR via l'API GitHub ;
3. controles objectifs : fichiers proteges, tirets cadratins ajoutes ;
4. envoi du diff a Mistral pour la relecture proprement dite ;
5. publication d'un commentaire unique, mis a jour a chaque push.

Configuration du depot appelant, toutes les cles etant facultatives :

  .github/auto-review.md     regles du projet, ajoutees telles quelles au
                             prompt ; c'est le fichier a ecrire en premier
  .github/auto-review.json   {
                               "rules": "regles en ligne, alternative au .md",
                               "rules_file": "chemin d'un autre fichier",
                               "protected_files": ["chemin/a", "chemin/b"],
                               "forbid_em_dash": true,
                               "max_diff_chars": 80000,
                               "model": "mistral-medium-latest"
                             }

Variables d'environnement :

  GITHUB_TOKEN        token du workflow, permission pull-requests: write
  GITHUB_REPOSITORY   owner/repo
  PR_NUMBER           numero de la PR
  MISTRAL_API_KEY     cle API Mistral, secret du depot
  MISTRAL_MODEL       modele demande ; prioritaire sur la cle "model"
  CONFIG_DIR          racine du depot appelant, defaut le repertoire courant
  DRY_RUN=1           affiche le commentaire sans le poster

Aucune dependance hors bibliotheque standard.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

# --- Constantes --------------------------------------------------------------

MARKER = "<!-- auto-review:mistral -->"

# Constats sur un plan gratuit Mistral, releves en conditions reelles :
# mistral-large repond 403 "not available in your subscription tier" ;
# mistral-medium et mistral-small plafonnent a 20 000 tokens par minute et
# renvoient 429 des qu'un diff depasse quelques milliers de lignes ;
# ministral-14b repond et relit correctement.
# Le modele demande est toujours tente en premier, puis les replis ci-dessous,
# du plus capable au plus modeste. Un 403 ou 404 passe au suivant sans attente.
DEFAULT_MODEL = "mistral-medium-latest"
FALLBACK_MODELS = ["ministral-14b-latest", "mistral-small-latest",
                   "ministral-8b-latest"]

MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_MODELS_URL = "https://api.mistral.ai/v1/models"
RETRY_DELAYS = (5, 20)
DEFAULT_MAX_DIFF_CHARS = 80_000
DASH = "\u2014"  # tiret cadratin, en echappement pour rester repérable

CONFIG_JSON = ".github/auto-review.json"
CONFIG_RULES = ".github/auto-review.md"

BASE_PROMPT = """Tu relis une pull request. Tu es rigoureux, concis et utile.

Cherche en priorite, dans cet ordre :
1. les bugs et regressions : cas limites, erreurs de logique, valeurs nulles,
   indices hors bornes, appels reseau sans gestion d'erreur ;
2. les problemes de securite : secret en clair, entree non validee, elevation
   de privileges, dependance non epinglee ;
3. les incoherences entre le code, les tests et la documentation modifies ;
4. la lisibilite et la duplication, seulement si le reste est propre.

Ne signale que ce que le diff montre. N'invente pas de ligne, de fonction ou
de fichier absent du diff. Si tu n'es pas sur, formule-le comme une question
plutot que comme un probleme.

Le message qui suit contient une section "Controles automatiques deja
effectues". Ces controles sont deterministes et font autorite. Ne les refais
pas, ne les contredis jamais, et ne fonde aucun probleme sur un comptage que
tu aurais fait toi-meme et qui les contredirait. Si un controle annonce zero
occurrence, il y en a zero.

Reponds en francais, en Markdown, en 350 mots maximum, avec exactement cette
structure :

### Verdict
Un seul mot parmi : APPROUVE, A CORRIGER, BLOQUE. Puis une phrase de
justification.

### Points forts
Deux ou trois puces maximum.

### Problemes
Une puce par probleme, avec le fichier et si possible la ligne. Classe du plus
grave au moins grave. Ecris "Aucun" s'il n'y en a pas.

### Question a l'auteur
Une seule question, ou "Aucune".

Ne commente pas le style d'ecriture sauf s'il contredit une regle du projet.
Ne propose pas de refonte hors du perimetre de la PR."""


# --- Configuration du depot appelant -----------------------------------------


class Config:
    """Reglages du depot appelant, tous facultatifs."""

    def __init__(self, root: Path):
        self.root = root
        data: dict[str, Any] = {}
        path = root / CONFIG_JSON
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("l'objet racine doit etre un dictionnaire")
                print(f"Configuration lue dans {CONFIG_JSON}")
            except Exception as e:  # noqa: BLE001
                print(f"{CONFIG_JSON} ignore : {e}", file=sys.stderr)
                data = {}

        self.protected_files: set[str] = set(data.get("protected_files") or [])
        self.forbid_em_dash: bool = bool(data.get("forbid_em_dash", False))
        self.model: str = str(data.get("model") or DEFAULT_MODEL)
        try:
            self.max_diff_chars = max(1000, int(data.get("max_diff_chars")
                                                or DEFAULT_MAX_DIFF_CHARS))
        except (TypeError, ValueError):
            self.max_diff_chars = DEFAULT_MAX_DIFF_CHARS
        self.rules: str = self._read_rules(data)

    def _read_rules(self, data: dict[str, Any]) -> str:
        inline = (data.get("rules") or "").strip()
        name = data.get("rules_file") or CONFIG_RULES
        text = ""
        candidate = self.root / str(name)
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8").strip()
            print(f"Regles du projet lues dans {name}")
        parts = [p for p in (inline, text) if p]
        return "\n\n".join(parts)

    def system_prompt(self) -> str:
        # forbid_em_dash n'est volontairement pas transmis au modele : le
        # comptage est deterministe et se suffit a lui-meme. Informer le modele
        # de la regle le poussait a signaler des occurrences inexistantes, y
        # compris en contradiction avec le comptage affiche a cote.
        blocks = [BASE_PROMPT]
        if self.rules:
            blocks.append("Regles propres a ce depot, elles priment sur les "
                          "consignes generales :\n\n" + self.rules)
        return "\n\n".join(blocks)


# --- HTTP --------------------------------------------------------------------


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def http(method: str, url: str, headers: dict, data: dict | None = None,
         timeout: int = 60) -> tuple[int, str, dict]:
    """Retourne (statut, corps, en-tetes en minuscules)."""
    payload = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=payload, method=method, headers=headers)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), \
                {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), \
            {k.lower(): v for k, v in e.headers.items()}


def http_ok(method: str, url: str, headers: dict, data: dict | None = None,
            timeout: int = 60) -> str:
    status, body, _ = http(method, url, headers, data, timeout)
    if status >= 300:
        raise HttpError(status, body)
    return body


# --- GitHub ------------------------------------------------------------------


def gh_headers(token: str, accept: str = "application/vnd.github+json") -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "auto-review",
    }


def get_pr(repo: str, pr: int, token: str) -> dict:
    url = f"https://api.github.com/repos/{repo}/pulls/{pr}"
    return json.loads(http_ok("GET", url, gh_headers(token)))


def get_pr_diff(repo: str, pr: int, token: str) -> str:
    url = f"https://api.github.com/repos/{repo}/pulls/{pr}"
    return http_ok("GET", url, gh_headers(token, "application/vnd.github.v3.diff"))


def get_pr_files(repo: str, pr: int, token: str) -> list[str]:
    files: list[str] = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repo}/pulls/{pr}/files?per_page=100&page={page}"
        batch = json.loads(http_ok("GET", url, gh_headers(token)))
        files.extend(f["filename"] for f in batch)
        if len(batch) < 100 or page >= 30:
            return files
        page += 1


def upsert_comment(repo: str, pr: int, token: str, body: str) -> str:
    """Met a jour le commentaire portant MARKER, sinon en cree un."""
    base = f"https://api.github.com/repos/{repo}/issues/{pr}/comments"
    existing = json.loads(http_ok("GET", base + "?per_page=100", gh_headers(token)))
    for c in existing:
        if MARKER in (c.get("body") or ""):
            http_ok("PATCH", f"https://api.github.com/repos/{repo}/issues/comments/{c['id']}",
                    gh_headers(token), {"body": body})
            return "mis a jour"
    http_ok("POST", base, gh_headers(token), {"body": body})
    return "cree"


# --- Controles objectifs -----------------------------------------------------


def count_added_dashes(diff: str) -> dict[str, int]:
    """Compte les cadratins dans les lignes ajoutees des fichiers .md.

    Les delimiteurs de bloc de code sont suivis sur les lignes ajoutees et sur
    les lignes de contexte, pas sur les lignes supprimees. Le compte reste
    indicatif : un bloc ouvert avant le premier hunk n'est pas vu.
    """
    counts: dict[str, int] = {}
    current = None
    in_code = False
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[4:].strip()
            current = current[2:] if current.startswith("b/") else current
            in_code = False
            continue
        if line.startswith("---") or line.startswith("diff --git") or line.startswith("@@"):
            continue
        if current is None or line[:1] not in ("+", " "):
            continue
        content = line[1:]
        if content.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or line[0] != "+" or not current.endswith(".md"):
            continue
        n = content.count(DASH)
        if n:
            counts[current] = counts.get(current, 0) + n
    return counts


def protected_touched(files: Iterable[str], protected: set[str]) -> list[str]:
    return sorted(f for f in files if f in protected)


def build_checks(cfg: Config, diff: str,
                 files: list[str]) -> tuple[str, bool, str]:
    """Retourne (texte publie, un controle bloque-t-il, texte pour le modele).

    Le comptage des cadratins n'est pas transmis au modele : c'est un controle
    deterministe, et le lui montrer l'amenait a en reparler a tort.
    """
    published = []
    for_model = []
    blocking = False

    if cfg.forbid_em_dash:
        dashes = count_added_dashes(diff)
        if dashes:
            total = sum(dashes.values())
            detail = ", ".join(f"{f} ({n})" for f, n in sorted(dashes.items()))
            published.append(f"- Cadratins ajoutes hors code : **{total}**. {detail}")
        else:
            published.append("- Cadratins ajoutes hors code : aucun.")

    if cfg.protected_files:
        touched = protected_touched(files, cfg.protected_files)
        if touched:
            blocking = True
            line = ("- Fichiers proteges modifies : "
                    + ", ".join(f"`{f}`" for f in touched)
                    + ". L'auteur confirme le caractere intentionnel par "
                      "un commentaire sur la PR.")
        else:
            line = "- Fichiers proteges : aucun touche."
        published.append(line)
        for_model.append(line)

    if not published:
        published.append("- Aucun controle objectif configure pour ce depot.")
    if not for_model:
        for_model.append("- Aucun.")
    return "\n".join(published), blocking, "\n".join(for_model)


# --- Mistral -----------------------------------------------------------------


def list_models(api_key: str) -> list[str]:
    """Identifiants des modeles accessibles a la cle, ou [] si indisponible."""
    try:
        status, body, _ = http("GET", MISTRAL_MODELS_URL,
                               {"Authorization": f"Bearer {api_key}"}, timeout=30)
        if status < 300:
            return sorted(m["id"] for m in json.loads(body).get("data", []))
    except Exception:  # noqa: BLE001
        pass
    return []


def candidate_models(requested: str, available: list[str]) -> list[str]:
    """Modele demande, toujours tente, puis les replis que la cle voit.

    GET /v1/models ne reflete pas forcement les modeles reellement autorises :
    un modele absent de la liste peut repondre, et inversement. Le modele
    demande est donc essaye quoi qu'il arrive ; un 403 ou 404 le fait passer
    sans attente.
    """
    fallbacks = [m for m in FALLBACK_MODELS if m != requested]
    if available:
        fallbacks = [m for m in fallbacks if m in available]
    return [requested] + fallbacks


def build_user_message(cfg: Config, pr: dict, files: list[str],
                       diff: str, checks: str) -> str:
    truncated = len(diff) > cfg.max_diff_chars
    suffix = f" (tronque a {cfg.max_diff_chars} caracteres)" if truncated else ""
    return (
        f"Titre de la PR : {pr.get('title', '')}\n\n"
        f"Description :\n{pr.get('body') or '(vide)'}\n\n"
        f"Fichiers modifies ({len(files)}) :\n" + "\n".join(f"- {f}" for f in files) +
        f"\n\nControles automatiques deja effectues :\n{checks}\n\n"
        f"Diff{suffix} :\n```diff\n{diff[:cfg.max_diff_chars]}\n```"
    )


def call_model(api_key: str, model: str, system: str,
               user: str) -> tuple[str | None, str]:
    """Interroge un modele avec retries. Retourne (reponse, derniere erreur)."""
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 1200,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    last = ""
    for attempt in range(len(RETRY_DELAYS) + 1):
        status, body, resp_headers = http("POST", MISTRAL_URL, headers, payload,
                                          timeout=120)
        if status < 300:
            return json.loads(body)["choices"][0]["message"]["content"].strip(), ""
        last = f"{model}: HTTP {status}: {body[:200]}"
        print(f"  {last}", file=sys.stderr)
        if status not in (429, 500, 502, 503, 504) or attempt == len(RETRY_DELAYS):
            break
        delay = RETRY_DELAYS[attempt]
        retry_after = resp_headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            delay = min(max(int(retry_after), 1), 90)
        print(f"  nouvelle tentative dans {delay} s", file=sys.stderr)
        time.sleep(delay)
    return None, last


def ask_mistral(api_key: str, model: str, cfg: Config, pr: dict,
                files: list[str], diff: str, checks: str) -> tuple[str, str]:
    """Retourne (relecture, modele utilise). Leve RuntimeError si tout echoue."""
    system = cfg.system_prompt()
    user = build_user_message(cfg, pr, files, diff, checks)
    available = list_models(api_key)
    print("Modeles visibles par la cle : "
          + (", ".join(available) if available else "(liste indisponible)"))
    errors = []
    for candidate in candidate_models(model, available):
        print(f"Modele {candidate}...")
        review, err = call_model(api_key, candidate, system, user)
        if review:
            return review, candidate
        errors.append(err)
    detail = "\n".join(errors)
    if available:
        detail += "\n\nModeles visibles par la cle : " + ", ".join(available[:20])
    else:
        detail += "\n\nImpossible de lister les modeles visibles par la cle."
    raise RuntimeError(detail)


# --- Commentaire -------------------------------------------------------------


def build_comment(pr: dict, files: list[str], checks: str, blocking: bool,
                  review: str | None, model: str, error: str | None) -> str:
    head = pr.get("head", {}).get("sha", "")[:7]
    parts = [
        MARKER,
        f"## Relecture automatique ({model})",
        f"Commit `{head}`, {len(files)} fichier(s), "
        f"+{pr.get('additions', 0)}/-{pr.get('deletions', 0)} lignes.",
        "",
        "### Controles objectifs",
        checks,
    ]
    if blocking:
        parts += ["", "**Un controle objectif bloque la fusion tant que "
                      "l'auteur ne confirme pas.**"]
    parts += ["", "---", ""]
    if review:
        parts.append(review)
    else:
        parts += ["### Verdict",
                  "INDISPONIBLE. Aucun modele Mistral n'a repondu.",
                  "Verifier le plan et les limites par modele sur la page "
                  "Limits de la console Mistral.",
                  "", f"```\n{error}\n```"]
    parts += ["", "---",
              "_Relecture generee par "
              "[brissonjo-sudo/ci-tooling](https://github.com/brissonjo-sudo/ci-tooling). "
              "Ce commentaire est mis a jour a chaque push._"]
    return "\n".join(parts)


# --- Main --------------------------------------------------------------------


def env(name: str, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name) or default
    if required and not value:
        print(f"Variable {name} manquante.", file=sys.stderr)
        sys.exit(2)
    return value


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # logs Actions dans l'ordre
    token = env("GITHUB_TOKEN")
    repo = env("GITHUB_REPOSITORY")
    pr_number = int(env("PR_NUMBER"))
    api_key = env("MISTRAL_API_KEY")
    dry_run = os.environ.get("DRY_RUN") == "1"

    cfg = Config(Path(env("CONFIG_DIR", required=False, default=".")))
    model = env("MISTRAL_MODEL", required=False, default=cfg.model)

    print(f"PR #{pr_number} sur {repo}, modele demande {model}")
    pr = get_pr(repo, pr_number, token)
    files = get_pr_files(repo, pr_number, token)
    diff = get_pr_diff(repo, pr_number, token)
    print(f"{len(files)} fichier(s), diff de {len(diff)} caracteres")

    checks, blocking, checks_for_model = build_checks(cfg, diff, files)

    review: str | None = None
    error: str | None = None
    used = model
    if not files:
        review = "### Verdict\nAPPROUVE. La PR ne modifie aucun fichier."
    else:
        try:
            review, used = ask_mistral(api_key, model, cfg, pr, files, diff,
                                       checks_for_model)
        except Exception as e:  # noqa: BLE001
            error = str(e)
            print(f"Echec Mistral : {error}", file=sys.stderr)

    comment = build_comment(pr, files, checks, blocking, review, used, error)
    print("=" * 60)
    print(comment)
    print("=" * 60)

    if dry_run:
        print("DRY_RUN=1 : commentaire non poste.")
    else:
        print("Commentaire", upsert_comment(repo, pr_number, token, comment))

    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
