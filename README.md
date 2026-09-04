# Outillage GitHub Actions partagé

Relecture automatique des pull requests, mutualisée pour tous les dépôts de
`brissonjo-sudo`.

À chaque ouverture ou mise à jour d'une PR, un commentaire unique est publié
puis mis à jour à chaque push. Il contient les contrôles objectifs configurés
pour le dépôt et une relecture du diff.

La relecture se fait à deux voix : **un modèle relit, un modèle d'un autre
fournisseur vérifie**. Chaque constat doit citer une ligne ajoutée par le diff,
et le vérificateur n'a qu'une question à trancher, celle de savoir si le diff
démontre ce constat. Ce qu'il rejette est replié dans un bloc dépliable plutôt
que publié comme un problème.

## Équiper un dépôt

Deux gestes, une seule fois par dépôt.

**1. Ajouter les clés.** Dans le dépôt à équiper, `Settings` → `Secrets and
variables` → `Actions` → `New repository secret`. Deux secrets possibles,
`GEMINI_API_KEY` et `MISTRAL_API_KEY`. Avec un seul, la relecture a lieu sans
vérification croisée ; avec les deux, elle est vérifiée.

Un dépôt sans aucune clé n'échoue pas : le job sort en succès sans rien
publier, et la relecture démarre d'elle-même le jour où une clé est ajoutée.
L'ordre des deux gestes est donc libre.

Le pied de chaque commentaire dit précisément ce qui s'est passé :

| Mention | Signification |
| --- | --- |
| `vérifié par ...` | Le second fournisseur a tranché chaque constat. |
| `aucun constat à vérifier` | La relecture n'a rien trouvé ; il n'y avait rien à soumettre au vérificateur. |
| `un seul fournisseur configuré` | Une seule clé est présente. |
| `vérification croisée indisponible` | Le second fournisseur n'a pas répondu ; les constats sont publiés tels quels. |

**2. Créer `.github/workflows/auto-review.yml`** avec exactement ceci :

```yaml
name: Auto Review

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

jobs:
  review:
    uses: brissonjo-sudo/ci-tooling/.github/workflows/auto-review.yml@main
    permissions:
      contents: read
      pull-requests: write
    secrets: inherit
```

Le bloc `permissions` n'est pas facultatif : un workflow appelé ne peut que
réduire les droits de l'appelant, jamais les élargir. Sans lui, le run échoue
en `startup_failure` sans message.

C'est tout. Le script vit ici, une correction profite immédiatement à tous les
dépôts équipés.

## Régler le comportement pour un dépôt

Les deux fichiers sont facultatifs et se placent dans le dépôt équipé.

### `.github/auto-review.md`

Les règles propres au projet, en texte libre, ajoutées telles quelles au prompt
de relecture. C'est le fichier qui change le plus la qualité du résultat.

```markdown
- Les références juridiques ne doivent jamais être altérées ni inventées.
- Les liens internes Markdown et les ancres doivent rester cohérents.
```

### `.github/auto-review.json`

| Clé | Défaut | Rôle |
| --- | --- | --- |
| `protected_files` | `[]` | Chemins, relatifs à la racine du dépôt, dont toute modification bloque la fusion tant que l'auteur ne confirme pas par un commentaire. |
| `forbid_em_dash` | `false` | Compte les tirets cadratins ajoutés hors blocs de code dans les `.md`. Contrôle purement déterministe : le modèle n'en est pas informé. |
| `max_diff_chars` | `80000` | Taille du diff envoyée au modèle, au-delà il est tronqué. |
| `providers` | `["gemini", "mistral"]` | Ordre des fournisseurs. Le premier qui répond relit, le suivant vérifie. |
| `model` | `""` | Modèle demandé en premier au relecteur. |
| `rules` | `""` | Règles en ligne, alternative à `auto-review.md`. |
| `rules_file` | `.github/auto-review.md` | Autre emplacement pour les règles. |

Un JSON invalide est signalé dans les logs et ignoré, la relecture continue
avec les valeurs par défaut.

## Entrées du workflow réutilisable

Toutes facultatives, à passer sous `with:` dans le dépôt appelant.

| Entrée | Défaut | Rôle |
| --- | --- | --- |
| `model` | `""` | Prioritaire sur la clé `model` de la configuration. |
| `dry-run` | `false` | Analyse sans publier de commentaire. |
| `python-version` | `"3.12"` | Version de Python du job. |
| `tooling-ref` | `"main"` | Référence de ce dépôt : branche, étiquette ou SHA de commit. |

## Figer la version utilisée

Par défaut un dépôt appelle `@main` : toute correction apportée ici lui profite
immédiatement. C'est le comportement voulu dans la plupart des cas.

Pour figer, remplacez la référence par un SHA de commit, qui est l'épinglage le
plus strict :

```yaml
jobs:
  review:
    uses: brissonjo-sudo/ci-tooling/.github/workflows/auto-review.yml@main
    permissions:
      contents: read
      pull-requests: write
    secrets: inherit
    with:
      tooling-ref: 8f3c1d2e4b5a6978c0d1e2f3a4b5c6d7e8f90123
```

La référence du `uses:` sélectionne le workflow, celle de `tooling-ref`
sélectionne le script. Épingler les deux au même SHA fige l'ensemble.

## Choix du modèle

Le modèle demandé est toujours essayé en premier, puis les replis du
fournisseur, du plus capable au plus modeste. Un `403` ou un `404` passe au
suivant sans attente, un `429` déclenche deux nouvelles tentatives en
respectant l'en-tête `Retry-After`. Si un fournisseur échoue entièrement, le
suivant prend la relecture.

Relevé en conditions réelles sur les paliers gratuits :

| Fournisseur | Constat |
| --- | --- |
| Gemini | 250 000 tokens par minute, largement au-dessus d'un diff de PR |
| Mistral | 20 000 tokens par minute sur `medium` et `small`, d'où des `429` fréquents ; `mistral-large` répond `403`, absent de l'offre gratuite |

Les deux fournisseurs exposent la même interface `/chat/completions`, ce qui
permet de les traiter avec le même code.

## Ce que la relecture ne fait pas

Le modèle se trompe. Il a déjà rendu deux fois un verdict « À CORRIGER » pour un
caractère absent d'un fichier entièrement ASCII, alors que le contrôle
déterministe affiché deux lignes plus haut dans le même commentaire annonçait
zéro occurrence. Lui interdire dans le prompt de contredire les contrôles n'a
rien changé.

Trois garde-fous en découlent, du plus fiable au moins fiable.

1. **Ce qu'un contrôle déterministe sait faire n'est pas demandé au modèle, ni
   même porté à sa connaissance.** Le comptage des cadratins n'apparaît que
   dans le commentaire publié.
2. **Un constat visant un fichier absent de la PR est écarté sans discussion.**
   Filtre déterministe, aucun modèle n'intervient.
3. **Un second fournisseur vérifie chaque constat restant.** Vérifier est plus
   simple que relire, et deux fournisseurs indépendants ne se trompent pas au
   même endroit.

Le verdict lui-même n'est plus demandé au modèle : il est calculé à partir des
constats qui survivent. Un modèle avait rendu « À CORRIGER » alors qu'il
n'énonçait qu'une question.

Ces garde-fous fonctionnent. Sur la première pull request relue par le duo,
Gemini a signalé une incohérence dans un fichier que la PR ne modifiait pas ;
Mistral l'a rejetée en constatant que la preuve citée était introuvable dans les
lignes ajoutées. Le commentaire publié annonçait « Aucun problème », le constat
écarté restant consultable dans le bloc dépliable.

Cela reste une aide à la relecture, pas une validation. Les contrôles objectifs,
eux, sont déterministes et fiables.

## Développement

```bash
python3 scripts/test_auto_review.py
```

Les tests simulent tous les appels réseau : aucune clé ni accès Internet n'est
nécessaire. Ils couvrent les contrôles objectifs, la lecture du JSON, le tri des
constats, le calcul du verdict, la configuration, le choix des fournisseurs, et
sept parcours complets : relecture puis vérification, vérificateur permissif,
fournisseur unique, bascule d'un fournisseur à l'autre sur `429`, réponse
illisible, mode `DRY_RUN` et absence totale de clé.
