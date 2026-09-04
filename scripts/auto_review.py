#!/usr/bin/env python3
"""Relecture automatique des pull requests, relue puis verifiee.

Outillage partage : ce script vit dans brissonjo-sudo/ci-tooling et est appele
par le workflow reutilisable .github/workflows/auto-review.yml. Chaque depot
qui l'utilise n'a qu'un fichier de quelques lignes a ecrire.

Deroulement :

1. lecture de la configuration du depot appelant (facultative) ;
2. recuperation du diff et des metadonnees de la PR via l'API GitHub ;
3. controles objectifs : fichiers proteges, tirets cadratins ajoutes ;
4. un premier modele relit le diff et rend ses constats en JSON ;
5. un modele d'un autre fournisseur verifie chaque constat contre le diff ;
6. publication d'un commentaire unique, mis a jour a chaque push.

L'etape 5 existe parce qu'un relecteur seul se trompe : sur ce depot, un
modele a signale a plusieurs reprises des occurrences inexistantes. Verifier
un constat est une tache bien plus simple que relire, et deux fournisseurs
independants ne se trompent pas au meme endroit. Un filtre deterministe
s'applique avant toute verification : un constat qui vise un fichier absent
de la PR est ecarte sans discussion.

Configuration du depot appelant, toutes les cles etant facultatives :

  .github/auto-review.md     regles du projet, ajoutees telles quelles au
                             prompt ; c'est le fichier a ecrire en premier
  .github/auto-review.json   {
                               "rules": "regles en ligne, alternative au .md",
                               "rules_file": "chemin d'un autre fichier",
                               "protected_files": ["chemin/a", "chemin/b"],
                               "forbid_em_dash": true,
                               "max_diff_chars": 80000,
                               "providers": ["gemini", "mistral"],
                               "model": "modele du relecteur"
                             }

Variables d'environnement :

  GITHUB_TOKEN        token du workflow, permission pull-requests: write
  GITHUB_REPOSITORY   owner/repo
  PR_NUMBER           numero de la PR
  GEMINI_API_KEY      cle API Google AI Studio, facultative
  MISTRAL_API_KEY     cle API Mistral, facultative
  REVIEW_MODEL        modele du relecteur ; prioritaire sur la cle "model"
  CONFIG_DIR          racine du depot appelant, defaut le repertoire courant
  DRY_RUN=1           affiche le commentaire sans le poster

Au moins une cle de fournisseur est necessaire. Avec deux, la verification a
lieu ; avec une seule, la relecture est publiee telle quelle et le commentaire
le signale.

Aucune dependance hors bibliotheque standard.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

# --- Constantes --------------------------------------------------------------

MARKER = "<!-- auto-review:mistral -->"

# Deux fournisseurs exposant la meme interface : /chat/completions et /models.
# L'ordre des modeles va du plus capable au plus modeste ; un 403, un 404 ou
# une reponse illisible passe au suivant sans attente.
#
# Releve en conditions reelles sur les paliers gratuits :
#   Mistral : mistral-large repond 403, medium et small plafonnent a 20 000
#   tokens par minute et renvoient 429 des qu'un diff depasse quelques
#   milliers de lignes ; ministral-14b repond.
#   Gemini : 250 000 tokens par minute, soit douze fois plus de marge.
PROVIDERS: dict[str, dict[str, Any]] = {
    "gemini": {
        "env": "GEMINI_API_KEY",
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": ["gemini-3.8-flash", "gemini-3.5-flash-lite",
                   "gemini-2.5-flash", "gemini-2.5-flash-lite"],
    },
    "mistral": {
        "env": "MISTRAL_API_KEY",
        "base": "https://api.mistral.ai/v1",
        "models": ["mistral-medium-latest", "ministral-14b-latest",
                   "mistral-small-latest", "ministral-8b-latest"],
    },
}
DEFAULT_PROVIDER_ORDER = ["gemini", "mistral"]

RETRY_DELAYS = (5, 20)
DEFAULT_MAX_DIFF_CHARS = 80_000
DASH = "—"  # tiret cadratin, en echappement pour rester reperable

CONFIG_JSON = ".github/auto-review.json"
CONFIG_RULES = ".github/auto-review.md"

SEVERITIES = ("grave", "moyen", "mineur")

REVIEW_PROMPT = """Tu relis une pull request. Tu es rigoureux, concis et utile.

Cherche en priorite, dans cet ordre :
1. les bugs et regressions : cas limites, erreurs de logique, valeurs nulles,
   indices hors bornes, appels reseau sans gestion d'erreur ;
2. les problemes de securite : secret en clair, entree non validee, elevation
   de privileges, dependance non epinglee ;
3. les incoherences entre le code, les tests et la documentation modifies ;
4. la lisibilite et la duplication, seulement si le reste est propre.

Regles absolues :
- ne signale que ce que les lignes ajoutees par le diff montrent ;
- ne reproche rien a du code que le diff supprime ;
- chaque constat cite en preuve une ligne ajoutee, recopiee mot pour mot ;
- si tu n'es pas sur, ce n'est pas un constat, c'est ta question a l'auteur ;
- les controles automatiques fournis sont deterministes et font autorite.

Reponds uniquement par un objet JSON, sans texte autour, de cette forme :

{
  "resume": "une phrase sur l'etat general de la PR",
  "points_forts": ["deux ou trois au maximum"],
  "constats": [
    {
      "fichier": "chemin exact tel qu'il apparait dans la liste des fichiers",
      "ligne": 12,
      "gravite": "grave, moyen ou mineur",
      "probleme": "une phrase, ce qui ne va pas et pourquoi",
      "preuve": "la ligne ajoutee, recopiee mot pour mot"
    }
  ],
  "question": "une seule question a l'auteur, ou une chaine vide"
}

"gravite" vaut "grave" pour une perte de donnees ou une faille de securite,
"moyen" pour un bug avere, "mineur" pour le reste. Le tableau "constats" est
vide s'il n'y a rien a signaler. N'ecris aucun verdict : il est calcule."""

VERIFY_PROMPT = """Tu verifies le travail d'un autre relecteur. Tu ne relis pas
la pull request toi-meme et tu ne cherches aucun probleme supplementaire.

On te donne le diff et une liste numerotee de constats. Pour chacun, une seule
question : le diff fourni demontre-t-il ce constat ?

Confirme un constat seulement si tu retrouves, dans les lignes ajoutees par le
diff, ce sur quoi il repose. Rejette-le dans tous les autres cas :
- la preuve citee n'apparait pas dans les lignes ajoutees ;
- le fichier ou la ligne ne correspondent pas ;
- le constat porte sur du code supprime, ou sur du code absent du diff ;
- le constat est une question, une supposition ou une preference de style.

Dans le doute, rejette. Un constat rejete a tort sera signale par l'auteur ;
un constat confirme a tort fait perdre du temps a tout le monde.

Reponds uniquement par un objet JSON, sans texte autour :

{
  "verdicts": [
    {"index": 1, "confirme": true, "raison": "une phrase"},
    {"index": 2, "confirme": false, "raison": "une phrase"}
  ]
}

Un verdict par constat, dans l'ordre, sans en omettre aucun."""


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
        self.model: str = str(data.get("model") or "")
        order = data.get("providers") or DEFAULT_PROVIDER_ORDER
        self.provider_order: list[str] = [p for p in order if p in PROVIDERS]
        if not self.provider_order:
            self.provider_order = list(DEFAULT_PROVIDER_ORDER)
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

    def review_prompt(self) -> str:
        # forbid_em_dash n'est volontairement pas transmis au modele : le
        # comptage est deterministe et se suffit a lui-meme. Informer le modele
        # de la regle le poussait a signaler des occurrences inexistantes, y
        # compris en contradiction avec le comptage affiche a cote.
        if not self.rules:
            return REVIEW_PROMPT
        return (REVIEW_PROMPT + "\n\nRegles propres a ce depot, elles priment "
                "sur les consignes generales :\n\n" + self.rules)


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
        url = (f"https://api.github.com/repos/{repo}/pulls/{pr}/files"
               f"?per_page=100&page={page}")
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
            http_ok("PATCH",
                    f"https://api.github.com/repos/{repo}/issues/comments/{c['id']}",
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
        if (line.startswith("---") or line.startswith("diff --git")
                or line.startswith("@@")):
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


# --- Fournisseurs ------------------------------------------------------------


def extract_json(text: str) -> dict | None:
    """Lit un objet JSON, meme entoure de texte ou d'un bloc de code."""
    text = (text or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        value = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(text[start:end + 1])
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


class Provider:
    """Un fournisseur de modeles derriere une interface commune."""

    def __init__(self, name: str, api_key: str):
        spec = PROVIDERS[name]
        self.name = name
        self.api_key = api_key
        self.base = spec["base"]
        self.models: list[str] = list(spec["models"])

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json"}

    def available_models(self) -> list[str]:
        """Modeles visibles par la cle, ou [] si la liste est indisponible."""
        try:
            status, body, _ = http("GET", f"{self.base}/models", self.headers,
                                   timeout=30)
            if status < 300:
                return sorted(str(m.get("id", "")).split("/")[-1]
                              for m in json.loads(body).get("data", []))
        except Exception:  # noqa: BLE001
            pass
        return []

    def candidates(self, requested: str = "") -> list[str]:
        """Modele demande, toujours tente, puis les replis que la cle voit.

        La liste renvoyee par /models ne reflete pas toujours les modeles
        reellement autorises : un modele absent peut repondre, et inversement.
        Le modele demande est donc essaye quoi qu'il arrive.
        """
        seen = self.available_models()
        print(f"  {self.name} : "
              + (f"{len(seen)} modeles visibles" if seen else "liste indisponible"))
        fallbacks = [m for m in self.models if m != requested]
        if seen:
            fallbacks = [m for m in fallbacks if m in seen] or self.models
        return ([requested] if requested else []) + fallbacks

    def ask_json(self, model: str, system: str, user: str,
                 max_tokens: int = 1600) -> tuple[dict | None, str]:
        """Un appel avec retries. Retourne (objet JSON, derniere erreur)."""
        payload = {
            "model": model,
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        last = ""
        for attempt in range(len(RETRY_DELAYS) + 1):
            status, body, headers = http("POST", f"{self.base}/chat/completions",
                                         self.headers, payload, timeout=120)
            if status < 300:
                content = json.loads(body)["choices"][0]["message"]["content"]
                parsed = extract_json(content)
                if parsed is not None:
                    return parsed, ""
                last = f"{model}: reponse illisible, JSON attendu"
                print(f"  {last}", file=sys.stderr)
                return None, last
            last = f"{model}: HTTP {status}: {body[:200]}"
            print(f"  {last}", file=sys.stderr)
            if (status not in (429, 500, 502, 503, 504)
                    or attempt == len(RETRY_DELAYS)):
                break
            delay = RETRY_DELAYS[attempt]
            retry_after = headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                delay = min(max(int(retry_after), 1), 90)
            print(f"  nouvelle tentative dans {delay} s", file=sys.stderr)
            time.sleep(delay)
        return None, last

    def run(self, system: str, user: str, requested: str = "",
            max_tokens: int = 1600) -> tuple[dict | None, str, list[str]]:
        """Essaie les modeles jusqu'a une reponse JSON.

        Retourne (objet, identifiant du modele retenu, erreurs rencontrees).
        """
        errors = []
        for model in self.candidates(requested):
            print(f"  {self.name} / {model}...")
            parsed, err = self.ask_json(model, system, user, max_tokens)
            if parsed is not None:
                return parsed, f"{self.name}/{model}", errors
            errors.append(err)
        return None, "", errors


def build_providers(order: Iterable[str]) -> list[Provider]:
    """Fournisseurs dont la cle est presente, dans l'ordre demande."""
    found = []
    for name in order:
        key = os.environ.get(PROVIDERS[name]["env"], "").strip()
        if key:
            found.append(Provider(name, key))
    return found


# --- Constats ----------------------------------------------------------------


def clean_findings(raw: Any, files: list[str]) -> tuple[list[dict], int]:
    """Normalise les constats et ecarte ceux qui visent un fichier hors PR.

    Ce filtre est deterministe : un constat dont le fichier n'appartient pas a
    la PR ne peut pas etre fonde, quelle que soit la confiance du modele.
    """
    kept: list[dict] = []
    dropped = 0
    known = set(files)
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            dropped += 1
            continue
        path = str(item.get("fichier") or "").strip().lstrip("./")
        problem = str(item.get("probleme") or "").strip()
        if not problem:
            dropped += 1
            continue
        if path and path not in known:
            print(f"  constat ecarte, fichier hors PR : {path}")
            dropped += 1
            continue
        severity = str(item.get("gravite") or "moyen").strip().lower()
        line = item.get("ligne")
        kept.append({
            "fichier": path,
            "ligne": line if isinstance(line, int) and line > 0 else None,
            "gravite": severity if severity in SEVERITIES else "moyen",
            "probleme": problem,
            "preuve": str(item.get("preuve") or "").strip(),
        })
    return kept, dropped


def numbered_findings(findings: list[dict]) -> str:
    lines = []
    for i, f in enumerate(findings, 1):
        where = f["fichier"] or "(fichier non precise)"
        if f["ligne"]:
            where += f", ligne {f['ligne']}"
        lines.append(f"{i}. [{where}] {f['probleme']}\n   Preuve citee : "
                     f"{f['preuve'] or '(aucune)'}")
    return "\n".join(lines)


def apply_verdicts(findings: list[dict],
                   payload: dict) -> tuple[list[dict], list[dict]]:
    """Separe les constats confirmes de ceux que le verificateur rejette.

    Un constat sans verdict est conserve : l'absence de reponse du
    verificateur ne doit pas faire disparaitre un probleme peut-etre reel.
    """
    verdicts = payload.get("verdicts")
    by_index: dict[int, dict] = {}
    for v in verdicts if isinstance(verdicts, list) else []:
        if isinstance(v, dict) and isinstance(v.get("index"), int):
            by_index[v["index"]] = v
    confirmed, rejected = [], []
    for i, finding in enumerate(findings, 1):
        verdict = by_index.get(i)
        if verdict is not None and verdict.get("confirme") is False:
            rejected.append(dict(finding,
                                 raison=str(verdict.get("raison") or "").strip()))
        else:
            confirmed.append(finding)
    return confirmed, rejected


def compute_verdict(findings: list[dict], blocking: bool) -> str:
    if blocking or any(f["gravite"] == "grave" for f in findings):
        return "BLOQUE"
    return "A CORRIGER" if findings else "APPROUVE"


# --- Commentaire -------------------------------------------------------------


def render_findings(findings: list[dict]) -> list[str]:
    order = {s: i for i, s in enumerate(SEVERITIES)}
    lines = []
    for f in sorted(findings, key=lambda x: order.get(x["gravite"], 1)):
        where = f"`{f['fichier']}`" if f["fichier"] else "Fichier non precise"
        if f["ligne"]:
            where += f", ligne {f['ligne']}"
        lines.append(f"- **{f['gravite'].capitalize()}** {where} : {f['probleme']}")
    return lines


def footer(reviewer: str, verifier: str) -> str:
    who = f"Relu par `{reviewer}`" if reviewer else "Relecture indisponible"
    who += (f", verifie par `{verifier}`." if verifier
            else ", sans verification croisee.")
    return (f"_{who} Genere par "
            "[brissonjo-sudo/ci-tooling](https://github.com/brissonjo-sudo/ci-tooling). "
            "Ce commentaire est mis a jour a chaque push._")


def build_comment(pr: dict, files: list[str], checks: str, blocking: bool,
                  review: dict | None, findings: list[dict],
                  rejected: list[dict], dropped: int, reviewer: str,
                  verifier: str, error: str | None) -> str:
    head = pr.get("head", {}).get("sha", "")[:7]
    parts = [
        MARKER,
        "## Relecture automatique",
        f"Commit `{head}`, {len(files)} fichier(s), "
        f"+{pr.get('additions', 0)}/-{pr.get('deletions', 0)} lignes.",
        "",
        "### Controles objectifs",
        checks,
        "",
        "---",
        "",
    ]

    if error or review is None:
        parts += ["### Verdict",
                  "INDISPONIBLE. Aucun modele n'a rendu de relecture exploitable.",
                  "", f"```\n{error}\n```", "", "---", footer(reviewer, verifier)]
        return "\n".join(parts)

    parts += ["### Verdict", f"**{compute_verdict(findings, blocking)}**"]
    summary = str(review.get("resume") or "").strip()
    if summary:
        parts.append(summary)
    if blocking:
        parts.append("Un fichier protege est modifie : la fusion attend une "
                     "confirmation explicite de l'auteur.")
    parts.append("")

    strengths = [str(s).strip() for s in (review.get("points_forts") or [])
                 if str(s).strip()][:3]
    if strengths:
        parts += ["### Points forts"] + [f"- {s}" for s in strengths] + [""]

    parts.append("### Problemes")
    parts += render_findings(findings) if findings else ["Aucun."]
    parts.append("")

    question = str(review.get("question") or "").strip()
    parts += ["### Question a l'auteur", question or "Aucune.", ""]

    if rejected or dropped:
        parts += ["<details>",
                  f"<summary>Constats ecartes avant publication : "
                  f"{len(rejected) + dropped}</summary>", ""]
        if dropped:
            parts.append(f"- {dropped} visant un fichier absent de la PR, "
                         "ecarte automatiquement.")
        for f in rejected:
            where = f["fichier"] or "fichier non precise"
            parts.append(f"- {where} : {f['probleme']} "
                         f"({f.get('raison') or 'non demontre par le diff'})")
        parts += ["", "</details>", ""]

    parts += ["---", footer(reviewer, verifier)]
    return "\n".join(parts)


# --- Main --------------------------------------------------------------------


def env(name: str, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name) or default
    if required and not value:
        print(f"Variable {name} manquante.", file=sys.stderr)
        sys.exit(2)
    return value


def build_user_message(cfg: Config, pr: dict, files: list[str],
                       diff: str, checks: str) -> str:
    truncated = len(diff) > cfg.max_diff_chars
    suffix = f" (tronque a {cfg.max_diff_chars} caracteres)" if truncated else ""
    return (
        f"Titre de la PR : {pr.get('title', '')}\n\n"
        f"Description :\n{pr.get('body') or '(vide)'}\n\n"
        f"Fichiers modifies ({len(files)}) :\n"
        + "\n".join(f"- {f}" for f in files)
        + f"\n\nControles automatiques deja effectues :\n{checks}\n\n"
          f"Diff{suffix} :\n```diff\n{diff[:cfg.max_diff_chars]}\n```"
    )


def review_pull_request(cfg: Config, providers: list[Provider], pr: dict,
                        files: list[str], diff: str, checks_for_model: str,
                        requested: str) -> dict:
    """Relit puis fait verifier. Retourne l'etat a publier."""
    out: dict[str, Any] = {"review": None, "findings": [], "rejected": [],
                           "dropped": 0, "reviewer": "", "verifier": "",
                           "error": None}
    if not files:
        out["review"] = {"resume": "La PR ne modifie aucun fichier.",
                         "points_forts": [], "constats": [], "question": ""}
        out["reviewer"] = "aucun appel necessaire"
        return out

    user = build_user_message(cfg, pr, files, diff, checks_for_model)
    errors: list[str] = []
    for provider in providers:
        print(f"Relecture par {provider.name}...")
        review, reviewer, errs = provider.run(cfg.review_prompt(), user, requested)
        errors.extend(errs)
        if review is not None:
            out["review"], out["reviewer"] = review, reviewer
            break
    if out["review"] is None:
        out["error"] = "\n".join(errors) or "aucun fournisseur n'a repondu"
        print(f"Echec de la relecture : {out['error']}", file=sys.stderr)
        return out

    findings, dropped = clean_findings(out["review"].get("constats"), files)
    out["findings"], out["dropped"] = findings, dropped

    others = [p for p in providers
              if not str(out["reviewer"]).startswith(p.name + "/")]
    if findings and others:
        checker = others[0]
        print(f"Verification par {checker.name}...")
        payload, verifier, _ = checker.run(
            VERIFY_PROMPT,
            f"Constats a verifier :\n{numbered_findings(findings)}\n\n"
            f"Diff :\n```diff\n{diff[:cfg.max_diff_chars]}\n```")
        if payload is not None:
            out["findings"], out["rejected"] = apply_verdicts(findings, payload)
            out["verifier"] = verifier
            print(f"{len(out['findings'])} constat(s) confirme(s), "
                  f"{len(out['rejected'])} rejete(s)")
        else:
            print("Verification indisponible, constats publies tels quels",
                  file=sys.stderr)
    return out


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # logs Actions dans l'ordre
    token = env("GITHUB_TOKEN")
    repo = env("GITHUB_REPOSITORY")
    pr_number = int(env("PR_NUMBER"))
    dry_run = os.environ.get("DRY_RUN") == "1"

    cfg = Config(Path(env("CONFIG_DIR", required=False, default=".")))
    requested = env("REVIEW_MODEL", required=False,
                    default=os.environ.get("MISTRAL_MODEL") or cfg.model)

    providers = build_providers(cfg.provider_order)
    if not providers:
        needed = ", ".join(PROVIDERS[p]["env"] for p in cfg.provider_order)
        print(f"Aucune cle de fournisseur. Attendu l'une de : {needed}",
              file=sys.stderr)
        return 2
    print("Fournisseurs disponibles : " + ", ".join(p.name for p in providers))

    pr = get_pr(repo, pr_number, token)
    files = get_pr_files(repo, pr_number, token)
    diff = get_pr_diff(repo, pr_number, token)
    print(f"PR #{pr_number} sur {repo} : {len(files)} fichier(s), "
          f"diff de {len(diff)} caracteres")

    checks, blocking, checks_for_model = build_checks(cfg, diff, files)
    state = review_pull_request(cfg, providers, pr, files, diff,
                                checks_for_model, requested)

    comment = build_comment(pr, files, checks, blocking, state["review"],
                            state["findings"], state["rejected"],
                            state["dropped"], state["reviewer"],
                            state["verifier"], state["error"])
    print("=" * 60)
    print(comment)
    print("=" * 60)

    if dry_run:
        print("DRY_RUN=1 : commentaire non poste.")
    else:
        print("Commentaire", upsert_comment(repo, pr_number, token, comment))

    return 1 if state["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
