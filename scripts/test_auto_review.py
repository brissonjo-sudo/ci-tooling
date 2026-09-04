#!/usr/bin/env python3
"""Tests de scripts/auto_review.py, sans acces reseau.

Usage : python3 scripts/test_auto_review.py
Sortie : une ligne par groupe de tests, code de retour 0 si tout passe.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_review as a  # noqa: E402

FENCE = "`" * 3

DIFF = f"""diff --git a/docs/note.md b/docs/note.md
--- a/docs/note.md
+++ b/docs/note.md
@@ -1,3 +1,6 @@
+Ligne avec un cadratin {a.DASH} ici
+{FENCE}
+code {a.DASH} ignore
+{FENCE}
+Encore {a.DASH} deux {a.DASH} la
-ancienne {a.DASH} ligne supprimee
diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1 +1 @@
+py {a.DASH} pas markdown
"""


def check(label: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"{label} : {detail}")


# --- Controles objectifs -----------------------------------------------------

def test_dashes() -> None:
    check("cadratins", a.count_added_dashes(DIFF) == {"docs/note.md": 3},
          a.count_added_dashes(DIFF))

    # Bloc ouvert et ferme sur des lignes de contexte.
    d = f"+++ b/x.md\n@@ -1,3 +1,4 @@\n {FENCE}\n+code {a.DASH} ici\n {FENCE}\n+prose {a.DASH} la\n"
    check("bloc en contexte", a.count_added_dashes(d) == {"x.md": 1},
          a.count_added_dashes(d))

    # Un delimiteur sur une ligne supprimee ne bascule pas l'etat.
    d = f"+++ b/y.md\n@@ -1,2 +1,2 @@\n-{FENCE}\n+prose {a.DASH} un\n-{FENCE}\n+prose {a.DASH} deux\n"
    check("ligne supprimee", a.count_added_dashes(d) == {"y.md": 2},
          a.count_added_dashes(d))

    check("diff vide", a.count_added_dashes("") == {})
    print("controles objectifs OK")


# --- Configuration -----------------------------------------------------------

def test_config() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Depot sans configuration : les valeurs par defaut s'appliquent.
        cfg = a.Config(root)
        check("defaut modele", cfg.model == a.DEFAULT_MODEL, cfg.model)
        check("defaut proteges", cfg.protected_files == set())
        check("defaut cadratins", cfg.forbid_em_dash is False)
        check("defaut regles", cfg.rules == "")
        checks, blocking, for_model = a.build_checks(cfg, DIFF, ["docs/note.md"])
        check("aucun controle", "Aucun controle objectif" in checks, checks)
        check("rien pour le modele", for_model == "- Aucun.", for_model)
        check("non bloquant", blocking is False)
        check("prompt de base", a.BASE_PROMPT in cfg.system_prompt())

        # Configuration complete.
        (root / ".github").mkdir()
        (root / a.CONFIG_JSON).write_text(json.dumps({
            "protected_files": ["data/ref.csv"],
            "forbid_em_dash": True,
            "max_diff_chars": 5000,
            "model": "ministral-14b-latest",
        }), encoding="utf-8")
        (root / a.CONFIG_RULES).write_text("Ne jamais toucher aux ancres.",
                                           encoding="utf-8")
        cfg = a.Config(root)
        check("modele configure", cfg.model == "ministral-14b-latest", cfg.model)
        check("taille configuree", cfg.max_diff_chars == 5000, cfg.max_diff_chars)
        check("regles lues", "ancres" in cfg.rules, cfg.rules)
        prompt = cfg.system_prompt()
        # La regle des cadratins n'est volontairement pas transmise au modele.
        check("cadratin absent du prompt", "cadratin" not in prompt.lower(), prompt)
        check("regles dans le prompt", "ancres" in prompt)

        checks, blocking, for_model = a.build_checks(
            cfg, DIFF, ["docs/note.md", "data/ref.csv"])
        check("cadratins comptes", "**3**" in checks, checks)
        check("protege signale", "data/ref.csv" in checks, checks)
        check("bloquant", blocking is True)
        check("cadratins caches au modele",
              "adratin" not in for_model and "**3**" not in for_model, for_model)
        check("protege visible du modele", "data/ref.csv" in for_model, for_model)

        # JSON invalide : le script continue avec les valeurs par defaut.
        (root / a.CONFIG_JSON).write_text("{ pas du json", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            cfg = a.Config(root)
        check("json invalide tolere", cfg.model == a.DEFAULT_MODEL, cfg.model)
        check("regles quand meme lues", "ancres" in cfg.rules)

        # Racine JSON qui n'est pas un objet.
        (root / a.CONFIG_JSON).write_text("[1, 2]", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            cfg = a.Config(root)
        check("liste toleree", cfg.protected_files == set())

        # max_diff_chars aberrant : plancher applique.
        (root / a.CONFIG_JSON).write_text('{"max_diff_chars": "beaucoup"}',
                                          encoding="utf-8")
        cfg = a.Config(root)
        check("taille aberrante", cfg.max_diff_chars == a.DEFAULT_MAX_DIFF_CHARS,
              cfg.max_diff_chars)
    print("configuration OK")


# --- Choix des modeles -------------------------------------------------------

def test_candidates() -> None:
    # Le modele demande est toujours en tete, meme absent de la liste.
    check("demande absent",
          a.candidate_models("x-inconnu", ["mistral-small-latest"])
          == ["x-inconnu", "mistral-small-latest"])
    # Les replis invisibles sont ecartes.
    check("replis filtres",
          a.candidate_models("mistral-medium-latest",
                             ["mistral-medium-latest", "mistral-small-latest"])
          == ["mistral-medium-latest", "mistral-small-latest"])
    # Sans liste, tous les replis sont tentes.
    check("liste vide",
          a.candidate_models("mistral-medium-latest", [])
          == ["mistral-medium-latest"] + [m for m in a.FALLBACK_MODELS
                                          if m != "mistral-medium-latest"])
    print("choix des modeles OK")


# --- Parcours complet, reseau simule -----------------------------------------

def make_transport(calls: list, mistral):
    """Fabrique un remplacant de a.http. `mistral` gere /chat/completions."""
    def transport(method, url, headers, data=None, timeout=60):
        calls.append((method, url, (data or {}).get("model")))
        if url.endswith("/v1/models"):
            return 200, json.dumps({"data": [
                {"id": "mistral-medium-latest"},
                {"id": "ministral-14b-latest"},
                {"id": "mistral-small-latest"},
            ]}), {}
        if "chat/completions" in url:
            # Le diff de test contient le mot "cadratin" : seule la section
            # des controles ne doit pas en parler.
            sent = "\n".join(m["content"] for m in data["messages"])
            check("comptage des cadratins non transmis",
                  "Cadratins ajoutes" not in sent, sent[:300])
            check("regle des cadratins non transmise",
                  "U+2014" not in sent, sent[:300])
            return mistral(data)
        if url.endswith("/pulls/42") and headers["Accept"].endswith("diff"):
            return 200, DIFF, {}
        if url.endswith("/pulls/42"):
            return 200, json.dumps({"title": "Test", "body": None, "additions": 5,
                                    "deletions": 1,
                                    "head": {"sha": "abcdef1234"}}), {}
        if "/pulls/42/files" in url:
            return 200, json.dumps([{"filename": "docs/note.md"},
                                    {"filename": "src/x.py"}]), {}
        if url.endswith("/issues/42/comments?per_page=100"):
            return 200, json.dumps([{"id": 1, "body": "autre"},
                                    {"id": 2, "body": a.MARKER + " ancien"}]), {}
        if url.endswith("/issues/comments/2") and method == "PATCH":
            check("marqueur conserve", a.MARKER in data["body"])
            return 200, "{}", {}
        raise AssertionError(f"appel inattendu : {method} {url}")
    return transport


def run_main(tmp: str) -> tuple[int, str]:
    os.environ.update(GITHUB_TOKEN="t", GITHUB_REPOSITORY="o/r", PR_NUMBER="42",
                      MISTRAL_API_KEY="k-test", MISTRAL_MODEL="", CONFIG_DIR=tmp)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        rc = a.main()
    return rc, buf.getvalue()


def test_end_to_end() -> None:
    original_http, original_sleep = a.http, a.time.sleep
    slept: list = []
    a.time.sleep = slept.append
    try:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".github").mkdir()
            (Path(tmp) / a.CONFIG_JSON).write_text(
                json.dumps({"forbid_em_dash": True,
                            "protected_files": ["data/ref.csv"]}), encoding="utf-8")

            # 1. Le premier modele repond : commentaire existant mis a jour.
            calls: list = []
            a.http = make_transport(calls, lambda d: (
                200, json.dumps({"choices": [{"message": {
                    "content": "### Verdict\nAPPROUVE. RAS."}}]}), {}))
            os.environ.pop("DRY_RUN", None)
            rc, out = run_main(tmp)
            check("code retour", rc == 0, rc)
            check("modele en tete", "(mistral-medium-latest)" in out, out[:200])
            check("commentaire modifie",
                  any(m == "PATCH" for m, _, _ in calls))
            check("aucun doublon",
                  not any(m == "POST" and "comments" in u for m, u, _ in calls))
            print("parcours nominal OK")

            # 2. 429 puis 403 puis succes : repli en cascade.
            tried: list = []
            slept.clear()

            def flaky(d):
                tried.append(d["model"])
                if d["model"] == "mistral-medium-latest":
                    return (429, '{"message":"Rate limit exceeded"}',
                            {"retry-after": "2"})
                if d["model"] == "ministral-14b-latest":
                    return (403, '{"message":"not available in your tier"}', {})
                return 200, json.dumps({"choices": [{"message": {
                    "content": "### Verdict\nAPPROUVE. via small"}}]}), {}

            a.http = make_transport([], flaky)
            rc, out = run_main(tmp)
            check("code retour repli", rc == 0, rc)
            check("ordre des essais",
                  tried == ["mistral-medium-latest"] * 3
                  + ["ministral-14b-latest", "mistral-small-latest"], tried)
            check("retry-after respecte", slept == [2, 2], slept)
            check("modele retenu", "(mistral-small-latest)" in out, out[:200])
            print("repli en cascade OK")

            # 3. Aucun modele ne repond : commentaire d'indisponibilite, code 1.
            a.http = make_transport([], lambda d: (401, '{"message":"nope"}', {}))
            rc, out = run_main(tmp)
            check("code retour echec", rc == 1, rc)
            check("mention indisponible", "INDISPONIBLE" in out)
            check("modeles listes", "Modeles visibles" in out)
            check("controles publies malgre l'echec", "**3**" in out)
            print("echec total OK")

            # 4. DRY_RUN : rien n'est publie.
            calls = []
            a.http = make_transport(calls, lambda d: (
                200, json.dumps({"choices": [{"message": {
                    "content": "### Verdict\nAPPROUVE."}}]}), {}))
            os.environ["DRY_RUN"] = "1"
            rc, out = run_main(tmp)
            os.environ.pop("DRY_RUN")
            check("code retour dry run", rc == 0, rc)
            check("rien publie",
                  not any(m in ("POST", "PATCH") and "comments" in u
                          for m, u, _ in calls), calls)
            print("mode DRY_RUN OK")
    finally:
        a.http, a.time.sleep = original_http, original_sleep


def main() -> int:
    test_dashes()
    test_config()
    test_candidates()
    test_end_to_end()
    print("\nTous les tests passent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
