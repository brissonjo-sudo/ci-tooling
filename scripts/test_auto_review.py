#!/usr/bin/env python3
"""Tests de scripts/auto_review.py, sans acces reseau.

Usage : python3 scripts/test_auto_review.py
Sortie : une ligne par groupe, code de retour 0 si tout passe.
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
+Ligne avec un tiret long {a.DASH} ici
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

FILES = ["docs/note.md", "src/x.py"]


def check(label: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"{label} : {detail}")


# --- Controles objectifs -----------------------------------------------------

def test_dashes() -> None:
    check("comptage", a.count_added_dashes(DIFF) == {"docs/note.md": 3},
          a.count_added_dashes(DIFF))

    d = f"+++ b/x.md\n@@ -1,3 +1,4 @@\n {FENCE}\n+code {a.DASH} ici\n {FENCE}\n+prose {a.DASH} la\n"
    check("bloc en contexte", a.count_added_dashes(d) == {"x.md": 1},
          a.count_added_dashes(d))

    d = f"+++ b/y.md\n@@ -1,2 +1,2 @@\n-{FENCE}\n+prose {a.DASH} un\n-{FENCE}\n+prose {a.DASH} deux\n"
    check("ligne supprimee", a.count_added_dashes(d) == {"y.md": 2},
          a.count_added_dashes(d))

    check("diff vide", a.count_added_dashes("") == {})
    print("controles objectifs OK")


# --- Lecture du JSON ---------------------------------------------------------

def test_extract_json() -> None:
    check("objet nu", a.extract_json('{"a": 1}') == {"a": 1})
    check("bloc de code",
          a.extract_json('```json\n{"a": 1}\n```') == {"a": 1})
    check("texte autour",
          a.extract_json('Voici :\n{"a": 1}\nvoila') == {"a": 1})
    check("tableau refuse", a.extract_json("[1, 2]") is None)
    check("vide refuse", a.extract_json("") is None)
    check("illisible refuse", a.extract_json("pas du json") is None)
    print("lecture du JSON OK")


# --- Constats ----------------------------------------------------------------

def test_findings() -> None:
    raw = [
        {"fichier": "docs/note.md", "ligne": 3, "gravite": "grave",
         "probleme": "fuite de secret", "preuve": "+token = 'abc'"},
        {"fichier": "./src/x.py", "gravite": "n'importe quoi",
         "probleme": "gravite inconnue"},
        {"fichier": "fichier/absent.py", "probleme": "hors PR"},
        {"fichier": "docs/note.md", "probleme": ""},
        "pas un objet",
    ]
    with contextlib.redirect_stdout(io.StringIO()):
        kept, dropped = a.clean_findings(raw, FILES)
    check("deux retenus", len(kept) == 2, kept)
    check("trois ecartes", dropped == 3, dropped)
    check("chemin normalise", kept[1]["fichier"] == "src/x.py", kept[1])
    check("gravite corrigee", kept[1]["gravite"] == "moyen", kept[1])
    check("ligne absente", kept[1]["ligne"] is None, kept[1])
    check("constats non liste", a.clean_findings(None, FILES) == ([], 0))

    # Le verdict decoule des constats, il n'est pas demande au modele.
    check("approuve", a.compute_verdict([], False) == "APPROUVE")
    check("a corriger",
          a.compute_verdict([{"gravite": "moyen"}], False) == "A CORRIGER")
    check("bloque par gravite",
          a.compute_verdict([{"gravite": "grave"}], False) == "BLOQUE")
    check("bloque par fichier protege",
          a.compute_verdict([], True) == "BLOQUE")
    print("constats et verdict OK")


def test_verdicts() -> None:
    findings = [{"fichier": "a", "probleme": "un"},
                {"fichier": "b", "probleme": "deux"},
                {"fichier": "c", "probleme": "trois"}]
    payload = {"verdicts": [
        {"index": 1, "confirme": True, "raison": "vu"},
        {"index": 2, "confirme": False, "raison": "preuve absente"},
    ]}
    confirmed, rejected = a.apply_verdicts(findings, payload)
    # Le troisieme n'a pas de verdict : il est conserve par prudence.
    check("deux confirmes", [f["probleme"] for f in confirmed] == ["un", "trois"],
          confirmed)
    check("un rejete", [f["probleme"] for f in rejected] == ["deux"], rejected)
    check("raison conservee", rejected[0]["raison"] == "preuve absente", rejected)

    confirmed, rejected = a.apply_verdicts(findings, {})
    check("sans verdicts, tout conserve", len(confirmed) == 3 and not rejected)
    print("verification des constats OK")


# --- Configuration -----------------------------------------------------------

def test_config() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cfg = a.Config(root)
        check("ordre par defaut",
              cfg.provider_order == a.DEFAULT_PROVIDER_ORDER, cfg.provider_order)
        check("aucun modele impose", cfg.model == "")
        checks, blocking, for_model = a.build_checks(cfg, DIFF, FILES)
        check("aucun controle", "Aucun controle objectif" in checks, checks)
        check("rien pour le modele", for_model == "- Aucun.", for_model)
        check("non bloquant", blocking is False)

        (root / ".github").mkdir()
        (root / a.CONFIG_JSON).write_text(json.dumps({
            "protected_files": ["data/ref.csv"],
            "forbid_em_dash": True,
            "max_diff_chars": 5000,
            "providers": ["mistral", "inconnu"],
            "model": "gemini-3.8-flash",
        }), encoding="utf-8")
        (root / a.CONFIG_RULES).write_text("Ne jamais toucher aux ancres.",
                                           encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            cfg = a.Config(root)
        check("fournisseur inconnu ecarte", cfg.provider_order == ["mistral"],
              cfg.provider_order)
        check("modele lu", cfg.model == "gemini-3.8-flash", cfg.model)
        check("taille lue", cfg.max_diff_chars == 5000, cfg.max_diff_chars)
        prompt = cfg.review_prompt()
        # La regle des cadratins n'est volontairement pas transmise au modele.
        check("cadratin absent du prompt", "cadratin" not in prompt.lower(), prompt)
        check("regles dans le prompt", "ancres" in prompt)

        checks, blocking, for_model = a.build_checks(
            cfg, DIFF, FILES + ["data/ref.csv"])
        check("cadratins comptes", "**3**" in checks, checks)
        check("protege signale", "data/ref.csv" in checks, checks)
        check("bloquant", blocking is True)
        check("cadratins caches au modele", "adratin" not in for_model, for_model)

        (root / a.CONFIG_JSON).write_text("{ pas du json", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            cfg = a.Config(root)
        check("json invalide tolere",
              cfg.provider_order == a.DEFAULT_PROVIDER_ORDER, cfg.provider_order)
        check("regles quand meme lues", "ancres" in cfg.rules)
    print("configuration OK")


# --- Fournisseurs ------------------------------------------------------------

def test_providers() -> None:
    os.environ.pop("GEMINI_API_KEY", None)
    os.environ["MISTRAL_API_KEY"] = "k-mistral"
    found = a.build_providers(["gemini", "mistral"])
    check("un seul fournisseur", [p.name for p in found] == ["mistral"], found)

    os.environ["GEMINI_API_KEY"] = "k-gemini"
    found = a.build_providers(["gemini", "mistral"])
    check("ordre respecte", [p.name for p in found] == ["gemini", "mistral"])
    check("url gemini",
          found[0].base.endswith("/v1beta/openai"), found[0].base)

    p = a.Provider("gemini", "k")
    p.available_models = lambda: ["gemini-2.5-flash", "models/autre"]
    with contextlib.redirect_stdout(io.StringIO()):
        cands = p.candidates("gemini-3.8-flash")
    check("demande en tete", cands[0] == "gemini-3.8-flash", cands)
    check("replis filtres", cands[1:] == ["gemini-2.5-flash"], cands)

    p.available_models = lambda: []
    with contextlib.redirect_stdout(io.StringIO()):
        cands = p.candidates()
    check("sans liste, tous les replis", cands == p.models, cands)
    print("fournisseurs OK")


# --- Parcours complet, reseau simule -----------------------------------------

REVIEW_OK = {
    "resume": "PR propre.",
    "points_forts": ["tests", "documentation"],
    "constats": [
        {"fichier": "src/x.py", "ligne": 1, "gravite": "moyen",
         "probleme": "constat reel", "preuve": "+py"},
        {"fichier": "src/x.py", "gravite": "mineur",
         "probleme": "constat invente", "preuve": "+rien"},
        {"fichier": "hors/pr.py", "probleme": "fichier absent"},
    ],
    "question": "Pourquoi ce choix ?",
}
VERIFY_OK = {"verdicts": [
    {"index": 1, "confirme": True, "raison": "ligne presente"},
    {"index": 2, "confirme": False, "raison": "preuve introuvable"},
]}


def make_transport(calls: list, gemini, mistral):
    def transport(method, url, headers, data=None, timeout=60):
        model = (data or {}).get("model", "")
        calls.append((method, url, model))
        if url.endswith("/models"):
            if "googleapis" in url:
                return 200, json.dumps({"data": [{"id": "gemini-3.8-flash"}]}), {}
            return 200, json.dumps({"data": [{"id": "mistral-medium-latest"}]}), {}
        if "chat/completions" in url:
            check("format json demande",
                  data.get("response_format") == {"type": "json_object"}, data)
            sent = "\n".join(m["content"] for m in data["messages"])
            check("comptage des cadratins non transmis",
                  "Cadratins ajoutes" not in sent, sent[:200])
            handler = gemini if "googleapis" in url else mistral
            return handler(data, sent)
        if url.endswith("/pulls/42") and headers["Accept"].endswith("diff"):
            return 200, DIFF, {}
        if url.endswith("/pulls/42"):
            return 200, json.dumps({"title": "Test", "body": None, "additions": 5,
                                    "deletions": 1,
                                    "head": {"sha": "abcdef1234"}}), {}
        if "/pulls/42/files" in url:
            return 200, json.dumps([{"filename": f} for f in FILES]), {}
        if url.endswith("/issues/42/comments?per_page=100"):
            return 200, json.dumps([{"id": 2, "body": a.MARKER + " ancien"}]), {}
        if url.endswith("/issues/comments/2") and method == "PATCH":
            check("marqueur conserve", a.MARKER in data["body"])
            return 200, "{}", {}
        raise AssertionError(f"appel inattendu : {method} {url}")
    return transport


def run_main(tmp: str) -> tuple[int, str]:
    os.environ.update(GITHUB_TOKEN="t", GITHUB_REPOSITORY="o/r", PR_NUMBER="42",
                      CONFIG_DIR=tmp, REVIEW_MODEL="", MISTRAL_MODEL="")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        rc = a.main()
    return rc, buf.getvalue()


def json_reply(payload):
    return lambda data, sent: (200, json.dumps({"choices": [{"message": {
        "content": json.dumps(payload)}}]}), {})


def test_end_to_end() -> None:
    original_http, original_sleep = a.http, a.time.sleep
    slept: list = []
    a.time.sleep = slept.append
    os.environ["GEMINI_API_KEY"] = "k-gemini"
    os.environ["MISTRAL_API_KEY"] = "k-mistral"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".github").mkdir()
            (Path(tmp) / a.CONFIG_JSON).write_text(
                json.dumps({"forbid_em_dash": True}), encoding="utf-8")

            # 1. Gemini relit, Mistral verifie et rejette un constat invente.
            calls: list = []
            a.http = make_transport(calls, json_reply(REVIEW_OK),
                                    json_reply(VERIFY_OK))
            os.environ.pop("DRY_RUN", None)
            rc, out = run_main(tmp)
            check("code retour", rc == 0, rc)
            check("relecteur gemini", "gemini/gemini-3.8-flash" in out, out[:400])
            check("verificateur mistral", "mistral/mistral-medium" in out, out[:400])
            check("constat reel publie", "constat reel" in out)
            check("constat invente ecarte",
                  "constat invente" in out and "preuve introuvable" in out)
            check("verdict calcule", "**A CORRIGER**" in out, out[:600])
            check("trois ecartes annonces",
                  "Constats ecartes avant publication : 2" in out, out)
            check("commentaire mis a jour",
                  any(m == "PATCH" for m, _, _ in calls))
            print("relecture puis verification OK")

            # 2. Le verificateur confirme tout : le verdict suit les constats.
            a.http = make_transport([], json_reply(REVIEW_OK), json_reply(
                {"verdicts": [{"index": 1, "confirme": True},
                              {"index": 2, "confirme": True}]}))
            rc, out = run_main(tmp)
            check("deux constats publies",
                  "constat reel" in out and "constat invente" in out)
            print("verificateur permissif OK")

            # 3. Un seul fournisseur : pas de verification, c'est annonce.
            os.environ.pop("MISTRAL_API_KEY")
            a.http = make_transport([], json_reply(REVIEW_OK), None)
            rc, out = run_main(tmp)
            check("code retour", rc == 0, rc)
            check("absence de verification annoncee",
                  "sans verification croisee" in out, out[-500:])
            os.environ["MISTRAL_API_KEY"] = "k-mistral"
            print("fournisseur unique OK")

            # 4. Gemini tombe en 429, Mistral prend la relecture.
            def busy(data, sent):
                return 429, '{"message":"quota"}', {"retry-after": "2"}
            slept.clear()
            a.http = make_transport([], busy, json_reply(REVIEW_OK))
            rc, out = run_main(tmp)
            check("code retour", rc == 0, rc)
            check("bascule sur mistral", "mistral/mistral-medium" in out, out[:400])
            check("retry-after respecte", slept and set(slept) == {2}, slept)
            print("bascule de fournisseur OK")

            # 5. Reponse illisible partout : commentaire d'indisponibilite.
            def garbage(data, sent):
                return 200, json.dumps({"choices": [{"message": {
                    "content": "desole, pas de JSON"}}]}), {}
            a.http = make_transport([], garbage, garbage)
            rc, out = run_main(tmp)
            check("code retour echec", rc == 1, rc)
            check("indisponible annonce", "INDISPONIBLE" in out)
            check("controles publies malgre l'echec", "**3**" in out)
            print("relecture illisible OK")

            # 6. Reponse 200 sans cle "content" : le cas qui faisait planter le
            #    job. Un modele de raisonnement a bout de jetons repond ainsi.
            def no_content(data, sent):
                return 200, json.dumps({"choices": [{
                    "finish_reason": "length", "message": {"role": "assistant"}}]}), {}
            a.http = make_transport([], no_content, json_reply(REVIEW_OK))
            rc, out = run_main(tmp)
            check("pas de plantage", rc == 0, rc)
            check("bascule sur le suivant", "mistral/" in out, out[:400])
            print("reponse sans contenu OK")

            # 6 bis. Corps illisible ou structure inattendue : meme exigence.
            for body in ("pas du json", '{"choices": []}', '{"autre": 1}'):
                a.http = make_transport(
                    [], lambda d, s, b=body: (200, b, {}), json_reply(REVIEW_OK))
                rc, out = run_main(tmp)
                check(f"corps {body[:15]} tolere", rc == 0, (body, rc))
            print("corps de reponse inattendus OK")

            # 6 ter. Un fournisseur qui leve une exception n'emporte pas le job.
            def boom(data, sent):
                raise RuntimeError("panne fournisseur")
            a.http = make_transport([], boom, json_reply(REVIEW_OK))
            rc, out = run_main(tmp)
            check("exception absorbee", rc == 0, rc)
            check("relais pris", "mistral/" in out, out[:400])
            print("exception fournisseur OK")

            # 7. DRY_RUN : rien n'est publie.
            calls = []
            a.http = make_transport(calls, json_reply(REVIEW_OK),
                                    json_reply(VERIFY_OK))
            os.environ["DRY_RUN"] = "1"
            rc, out = run_main(tmp)
            os.environ.pop("DRY_RUN")
            check("code retour", rc == 0, rc)
            check("rien publie",
                  not any(m in ("POST", "PATCH") and "comments" in u
                          for m, u, _ in calls), calls)
            print("mode DRY_RUN OK")

            # 8. Aucune cle : succes silencieux, aucun appel, rien publie.
            os.environ.pop("GEMINI_API_KEY")
            os.environ.pop("MISTRAL_API_KEY")
            calls = []
            a.http = make_transport(calls, None, None)
            rc, out = run_main(tmp)
            check("succes silencieux", rc == 0, rc)
            check("aucun appel", calls == [], calls)
            check("message explicite", "relecture ignoree" in out, out)
            print("aucune cle OK")
    finally:
        a.http, a.time.sleep = original_http, original_sleep
        os.environ["GEMINI_API_KEY"] = "k-gemini"
        os.environ["MISTRAL_API_KEY"] = "k-mistral"


def main() -> int:
    test_dashes()
    test_extract_json()
    test_findings()
    test_verdicts()
    test_config()
    test_providers()
    test_end_to_end()
    print("\nTous les tests passent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
