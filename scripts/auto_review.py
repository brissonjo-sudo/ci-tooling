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
                               "protected_files": ["chemin/a", "src/*/textes/*"],
                               "immutable_files": ["migrations/*.sql"],
                               "forbid_patterns": [{"pattern": "regex",
                                                    "files": "src/*",
                                                    "message": "texte publie",
                                                    "blocking": false}],
                               "forbid_em_dash": true,
                               "max_diff_chars": 80000,
                               "providers": ["gemini", "mistral"],
                               "model": "modele du relecteur"
                             }

Les chemins de configuration sont des motifs fnmatch ou l'etoile traverse les
separateurs : "src/*" couvre donc aussi "src/a/b.php". Un chemin exact reste
un motif valide.

Variables d'environnement :

  GITHUB_TOKEN        token du workflow, permission pull-requests: write
  GITHUB_REPOSITORY   owner/repo
  PR_NUMBER           numero de la PR
  GEMINI_API_KEY      cle API Google AI Studio, facultative
  MISTRAL_API_KEY     cle API Mistral, facultative
  REVIEW_MODEL        modele du relecteur ; prioritaire sur la cle "model"
  CONFIG_DIR          racine du depot appelant, defaut le repertoire courant
  DRY_RUN=1           affiche le commentaire sans le poster

Sans aucune cle, le script sort en succes sans rien publier : un depot equipe
mais pas encore configure ne doit pas afficher d'echec. Avec une cle, la
relecture est publiee telle quelle et le commentaire signale l'absence de
verification. Avec deux, la verification croisee a lieu.

Aucune dependance hors bibliotheque standard.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
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
# Les modeles de raisonnement consomment leur budget de jetons avant
# d'ecrire la reponse : trop bas, ils rendent un message sans contenu.
REVIEW_MAX_TOKENS = 4000
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


class PatternRule:
    """Un motif interdit, tel que declare dans forbid_patterns."""

    def __init__(self, regex: re.Pattern, files: str, message: str,
                 blocking: bool):
        self.regex = regex
        self.files = files
        self.message = message
        self.blocking = blocking


def build_pattern_rules(raw: Any) -> list[PatternRule]:
    """Compile les motifs interdits. Une entree illisible est ignoree.

    Une regex invalide ne doit pas emporter la relecture : elle est signalee
    dans les logs et le reste des controles continue, comme pour un JSON
    invalide.
    """
    rules: list[PatternRule] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            print(f"forbid_patterns : entree ignoree, objet attendu ({item!r})",
                  file=sys.stderr)
            continue
        pattern = str(item.get("pattern") or "")
        if not pattern:
            print("forbid_patterns : entree ignoree, cle 'pattern' absente",
                  file=sys.stderr)
            continue
        try:
            regex = re.compile(pattern)
        except re.error as e:
            print(f"forbid_patterns : motif ignore, regex invalide "
                  f"({pattern!r}) : {e}", file=sys.stderr)
            continue
        rules.append(PatternRule(
            regex=regex,
            files=str(item.get("files") or "*"),
            message=str(item.get("message") or "").strip()
            or f"Motif interdit `{pattern}`",
            blocking=bool(item.get("blocking", False)),
        ))
    return rules


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

        self.protected_files: list[str] = [str(p) for p in
                                           (data.get("protected_files") or [])]
        self.immutable_files: list[str] = [str(p) for p in
                                           (data.get("immutable_files") or [])]
        self.forbid_patterns: list[PatternRule] = build_pattern_rules(
            data.get("forbid_patterns"))
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


def get_pr_files(repo: str, pr: int, token: str) -> dict[str, str]:
    """Fichiers de la PR, chemin vers statut (added, modified, removed...).

    Le statut sert au controle des fichiers immuables ; partout ailleurs seule
    la liste des chemins compte, et un dictionnaire s'y prete aussi bien.
    """
    files: dict[str, str] = {}
    page = 1
    while True:
        url = (f"https://api.github.com/repos/{repo}/pulls/{pr}/files"
               f"?per_page=100&page={page}")
        batch = json.loads(http_ok("GET", url, gh_headers(token)))
        for f in batch:
            files[f["filename"]] = str(f.get("status") or "")
        if len(batch) < 100 or page >= 30:
            return files
        page += 1


def get_commit_date(repo: str, sha: str, token: str) -> str:
    """Date de validation du commit, pour dater la confirmation attendue."""
    url = f"https://api.github.com/repos/{repo}/commits/{sha}"
    data = json.loads(http_ok("GET", url, gh_headers(token)))
    committer = (data.get("commit") or {}).get("committer") or {}
    return str(committer.get("date") or "")


def get_comments(repo: str, pr: int, token: str) -> list[dict]:
    url = (f"https://api.github.com/repos/{repo}/issues/{pr}"
           "/comments?per_page=100")
    payload = json.loads(http_ok("GET", url, gh_headers(token)))
    return payload if isinstance(payload, list) else []


def upsert_comment(repo: str, pr: int, token: str, body: str,
                   existing: list[dict]) -> str:
    """Met a jour le commentaire portant MARKER, sinon en cree un."""
    base = f"https://api.github.com/repos/{repo}/issues/{pr}/comments"
    for c in existing:
        if MARKER in (c.get("body") or ""):
            http_ok("PATCH",
                    f"https://api.github.com/repos/{repo}/issues/comments/{c['id']}",
                    gh_headers(token), {"body": body})
            return "mis a jour"
    http_ok("POST", base, gh_headers(token), {"body": body})
    return "cree"


# --- Controles objectifs -----------------------------------------------------


def added_lines(diff: str) -> Iterable[tuple[str, str, bool]]:
    """Parcourt les lignes ajoutees : (fichier, contenu, dans un bloc de code).

    Les delimiteurs de bloc de code sont suivis sur les lignes ajoutees et sur
    les lignes de contexte, pas sur les lignes supprimees. Le suivi reste
    indicatif : un bloc ouvert avant le premier hunk n'est pas vu.
    """
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
        if line[0] == "+":
            yield current, content, in_code


def count_added_dashes(diff: str) -> dict[str, int]:
    """Compte les cadratins ajoutes dans les .md, hors blocs de code."""
    counts: dict[str, int] = {}
    for path, content, in_code in added_lines(diff):
        if in_code or not path.endswith(".md"):
            continue
        n = content.count(DASH)
        if n:
            counts[path] = counts.get(path, 0) + n
    return counts


def path_matches(path: str, patterns: Iterable[str]) -> bool:
    """Vrai si le chemin correspond a l'un des motifs fnmatch.

    L'etoile de fnmatch traverse les separateurs : "src/*" couvre aussi
    "src/a/b.php". Un chemin exact reste un motif valide.
    """
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def protected_touched(files: Iterable[str],
                      protected: Iterable[str]) -> list[str]:
    return sorted(f for f in files if path_matches(f, protected))


def immutable_touched(statuses: dict[str, str],
                      patterns: Iterable[str]) -> list[str]:
    """Fichiers immuables modifies plutot qu'ajoutes.

    Une migration deja poussee est suivie par son numero, pas par son contenu :
    la modifier apres coup passe inapercu jusqu'a ce qu'une autre branche
    fusionne la version d'origine.
    """
    return sorted(f for f, status in statuses.items()
                  if status == "modified" and path_matches(f, patterns))


def scan_patterns(diff: str, rules: list[PatternRule]
                  ) -> list[tuple[PatternRule, dict[str, int]]]:
    """Compte les occurrences de chaque motif dans les lignes ajoutees."""
    hits: list[tuple[PatternRule, dict[str, int]]] = [(r, {}) for r in rules]
    for path, content, _ in added_lines(diff):
        for rule, counts in hits:
            if not fnmatch.fnmatch(path, rule.files):
                continue
            n = len(rule.regex.findall(content))
            if n:
                counts[path] = counts.get(path, 0) + n
    return hits


def format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{f} ({n})" for f, n in sorted(counts.items()))


def build_checks(cfg: Config, diff: str,
                 statuses: dict[str, str]) -> tuple[str, bool, str]:
    """Retourne (texte publie, un controle bloque-t-il, texte pour le modele).

    Seuls les fichiers proteges sont transmis au modele. Comptages et motifs
    restent hors de sa vue : le lui montrer l'amenait a en reparler a tort, y
    compris en contredisant le chiffre affiche deux lignes plus haut.
    """
    published = []
    for_model = []
    blocking = False

    if cfg.forbid_em_dash:
        dashes = count_added_dashes(diff)
        if dashes:
            published.append(f"- Cadratins ajoutes hors code : "
                             f"**{sum(dashes.values())}**. "
                             + format_counts(dashes))
        else:
            published.append("- Cadratins ajoutes hors code : aucun.")

    clean = 0
    for rule, counts in scan_patterns(diff, cfg.forbid_patterns):
        if not counts:
            clean += 1
            continue
        blocking = blocking or rule.blocking
        published.append(f"- {rule.message} : **{sum(counts.values())}** "
                         f"occurrence(s). " + format_counts(counts))
    if clean:
        published.append(f"- Motifs interdits sans occurrence : **{clean}** "
                         f"sur **{len(cfg.forbid_patterns)}**.")

    if cfg.immutable_files:
        touched = immutable_touched(statuses, cfg.immutable_files)
        if touched:
            blocking = True
            published.append("- Fichiers immuables modifies : "
                             + ", ".join(f"`{f}`" for f in touched)
                             + ". Un fichier deja versionne est modifie au "
                               "lieu d'en ajouter un nouveau.")
        else:
            published.append("- Fichiers immuables : aucun modifie.")

    if cfg.protected_files:
        touched = protected_touched(statuses, cfg.protected_files)
        if touched:
            blocking = True
            line = ("- Fichiers proteges modifies : "
                    + ", ".join(f"`{f}`" for f in touched) + ".")
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
                 max_tokens: int = REVIEW_MAX_TOKENS) -> tuple[dict | None, str]:
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
                # Un 200 ne garantit pas un contenu : un modele de raisonnement
                # qui epuise son budget de jetons, ou dont la reponse est
                # filtree, renvoie un message sans cle "content".
                try:
                    choice = (json.loads(body).get("choices") or [{}])[0]
                except (ValueError, IndexError, AttributeError, TypeError):
                    choice = {}
                content = (choice.get("message") or {}).get("content") or ""
                parsed = extract_json(content)
                if parsed is not None:
                    return parsed, ""
                reason = choice.get("finish_reason") or "non precisee"
                last = (f"{model}: reponse inexploitable, JSON attendu "
                        f"(finish_reason={reason})")
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
            max_tokens: int = REVIEW_MAX_TOKENS) -> tuple[dict | None, str, list[str]]:
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


def parse_iso(value: str) -> datetime | None:
    """Lit un horodatage GitHub. Le Z final n'est accepte qu'a partir de 3.11."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def find_confirmation(comments: list[dict], author: str,
                      head_date: str) -> dict | None:
    """Cherche la confirmation de l'auteur pour le commit courant.

    Un controle bloquant demande a l'auteur de confirmer que la modification
    est intentionnelle. Sans cette lecture, la demande n'avait aucun moyen
    d'aboutir : le verdict restait BLOQUE meme apres une reponse detaillee.

    Est retenu le dernier commentaire de l'auteur de la PR, hors commentaire
    de l'outil lui-meme. Il ne vaut que s'il est posterieur au commit relu :
    une confirmation anterieure porte sur un etat qui n'est plus celui de la
    PR, et doit etre renouvelee.
    """
    head = parse_iso(head_date)
    best: tuple[datetime, dict] | None = None
    for c in comments:
        if (c.get("user") or {}).get("login") != author:
            continue
        if MARKER in (c.get("body") or ""):
            continue
        when = parse_iso(c.get("created_at"))
        if when is not None and (best is None or when > best[0]):
            best = (when, c)
    if best is None:
        return None
    when, comment = best
    return {"login": author, "date": when,
            "url": str(comment.get("html_url") or ""),
            "valide": head is None or when > head}


def confirmation_line(confirmation: dict | None) -> str:
    """Phrase publiee sous le verdict quand un controle bloquant s'applique."""
    if confirmation is None:
        return ("Un controle bloquant s'est declenche : la fusion attend une "
                "confirmation de l'auteur, par un commentaire poste sur cette "
                "pull request apres le dernier push.")
    quand = confirmation["date"].strftime("%d/%m/%Y a %H:%M UTC")
    if not confirmation["valide"]:
        return (f"Un controle bloquant s'est declenche. Le dernier commentaire "
                f"de @{confirmation['login']} date du {quand}, avant le dernier "
                f"push : la confirmation porte sur un etat qui n'est plus celui "
                f"de la pull request et doit etre renouvelee.")
    return (f"Controle bloquant leve : @{confirmation['login']} a confirme par "
            f"un commentaire du {quand}, posterieur au dernier push.")


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


# Etats possibles de la verification croisee, tels qu'affiches en pied de
# commentaire. Les distinguer evite de faire passer "il n'y avait rien a
# verifier" pour "la verification n'a pas eu lieu".
VERIFY_STATES = {
    "rien": "aucun constat a verifier",
    "solo": "sans verification croisee, un seul fournisseur configure",
    "echec": "verification croisee indisponible, constats publies tels quels",
}


def footer(reviewer: str, verifier: str, verify_state: str = "") -> str:
    who = f"Relu par `{reviewer}`" if reviewer else "Relecture indisponible"
    if verifier:
        who += f", verifie par `{verifier}`."
    else:
        who += ", " + VERIFY_STATES.get(verify_state, "sans verification croisee") + "."
    return (f"_{who} Genere par "
            "[brissonjo-sudo/ci-tooling](https://github.com/brissonjo-sudo/ci-tooling). "
            "Ce commentaire est mis a jour a chaque push._")


def build_comment(pr: dict, files: list[str], checks: str, blocking: bool,
                  review: dict | None, findings: list[dict],
                  rejected: list[dict], dropped: int, reviewer: str,
                  verifier: str, error: str | None,
                  verify_state: str = "",
                  confirmation: dict | None = None) -> str:
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
                  "", f"```\n{error}\n```", "", "---",
                  footer(reviewer, verifier, verify_state)]
        return "\n".join(parts)

    leve = bool(confirmation and confirmation["valide"])
    parts += ["### Verdict",
              f"**{compute_verdict(findings, blocking and not leve)}**"]
    summary = str(review.get("resume") or "").strip()
    if summary:
        parts.append(summary)
    if blocking:
        parts.append(confirmation_line(confirmation))
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

    parts += ["---", footer(reviewer, verifier, verify_state)]
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
                           "error": None, "verify_state": "solo"}
    if not files:
        out["review"] = {"resume": "La PR ne modifie aucun fichier.",
                         "points_forts": [], "constats": [], "question": ""}
        out["reviewer"] = "aucun appel necessaire"
        return out

    user = build_user_message(cfg, pr, files, diff, checks_for_model)
    errors: list[str] = []
    for provider in providers:
        print(f"Relecture par {provider.name}...")
        try:
            review, reviewer, errs = provider.run(cfg.review_prompt(), user,
                                                  requested)
        except Exception as e:  # noqa: BLE001
            # Un fournisseur qui casse ne doit pas emporter le job : le
            # commentaire reste publie, avec les controles deterministes.
            errors.append(f"{provider.name}: {type(e).__name__}: {e}")
            print(f"  {errors[-1]}", file=sys.stderr)
            continue
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
    if not others:
        out["verify_state"] = "solo"
    elif not findings:
        # Rien a verifier n'est pas la meme chose que pas de verificateur.
        out["verify_state"] = "rien"
    if findings and others:
        checker = others[0]
        print(f"Verification par {checker.name}...")
        try:
            payload, verifier, _ = checker.run(
                VERIFY_PROMPT,
                f"Constats a verifier :\n{numbered_findings(findings)}\n\n"
                f"Diff :\n```diff\n{diff[:cfg.max_diff_chars]}\n```")
        except Exception as e:  # noqa: BLE001
            payload, verifier = None, ""
            print(f"  {type(e).__name__}: {e}", file=sys.stderr)
        if payload is not None:
            out["findings"], out["rejected"] = apply_verdicts(findings, payload)
            out["verifier"] = verifier
            out["verify_state"] = "fait"
            print(f"{len(out['findings'])} constat(s) confirme(s), "
                  f"{len(out['rejected'])} rejete(s)")
        else:
            out["verify_state"] = "echec"
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
        # Absence de cle : le depot est equipe mais pas encore configure.
        # C'est un cas normal, pas une erreur : le job sort en succes sans
        # rien publier, et la relecture demarrera d'elle-meme le jour ou une
        # cle sera ajoutee. Echouer ici afficherait une croix rouge sur toutes
        # les PR d'un depot qui n'a rien demande.
        needed = " ou ".join(PROVIDERS[p]["env"] for p in cfg.provider_order)
        print(f"Aucune cle de fournisseur ({needed}) : relecture ignoree. "
              "Ajouter l'un de ces secrets dans les reglages du depot pour "
              "l'activer.")
        return 0
    print("Fournisseurs disponibles : " + ", ".join(p.name for p in providers))

    pr = get_pr(repo, pr_number, token)
    statuses = get_pr_files(repo, pr_number, token)
    files = list(statuses)
    diff = get_pr_diff(repo, pr_number, token)
    print(f"PR #{pr_number} sur {repo} : {len(files)} fichier(s), "
          f"diff de {len(diff)} caracteres")

    checks, blocking, checks_for_model = build_checks(cfg, diff, statuses)

    comments = get_comments(repo, pr_number, token)
    confirmation = None
    if blocking:
        # La confirmation demandee doit pouvoir aboutir : sans cette lecture,
        # le verdict restait BLOQUE meme apres une reponse de l'auteur.
        head_sha = str(pr.get("head", {}).get("sha") or "")
        author = str((pr.get("user") or {}).get("login") or "")
        confirmation = find_confirmation(
            comments, author, get_commit_date(repo, head_sha, token))
        if confirmation is None:
            print(f"Controle bloquant, aucune confirmation de @{author}")
        else:
            etat = "valide" if confirmation["valide"] else "anterieure au push"
            print(f"Controle bloquant, confirmation de @{author} : {etat}")
    state = review_pull_request(cfg, providers, pr, files, diff,
                                checks_for_model, requested)

    comment = build_comment(pr, files, checks, blocking, state["review"],
                            state["findings"], state["rejected"],
                            state["dropped"], state["reviewer"],
                            state["verifier"], state["error"],
                            state["verify_state"], confirmation)
    print("=" * 60)
    print(comment)
    print("=" * 60)

    if dry_run:
        print("DRY_RUN=1 : commentaire non poste.")
    else:
        print("Commentaire",
              upsert_comment(repo, pr_number, token, comment, comments))

    return 1 if state["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
