# Outillage GitHub Actions partagé

Relecture automatique des pull requests par Mistral, mutualisée pour tous les
dépôts de `brissonjo-sudo`.

À chaque ouverture ou mise à jour d'une PR, un commentaire unique est publié
puis mis à jour à chaque push. Il contient les contrôles objectifs configurés
pour le dépôt et une relecture du diff par un modèle Mistral.

## Équiper un dépôt

Deux gestes, une seule fois par dépôt.

**1. Ajouter le secret.** Dans le dépôt à équiper, `Settings` → `Secrets and
variables` → `Actions` → `New repository secret`, nommé `MISTRAL_API_KEY`.

**2. Créer `.github/workflows/auto-review.yml`** avec exactement ceci :

```yaml
name: Auto Review

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

jobs:
  review:
    uses: brissonjo-sudo/ci-tooling/.github/workflows/auto-review.yml@main
    secrets: inherit
```

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
| `protected_files` | `[]` | Chemins dont toute modification bloque la fusion tant que l'auteur ne confirme pas par un commentaire. |
| `forbid_em_dash` | `false` | Compte les tirets cadratins ajoutés hors blocs de code dans les `.md` et l'interdit dans le prompt. |
| `max_diff_chars` | `80000` | Taille du diff envoyée au modèle, au-delà il est tronqué. |
| `model` | `mistral-medium-latest` | Modèle demandé en premier. |
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
| `tooling-ref` | `"main"` | Référence de ce dépôt. À épingler sur un tag pour figer le comportement. |

## Choix du modèle

Le modèle demandé est toujours essayé en premier, puis les replis, du plus
capable au plus modeste : `ministral-14b-latest`, `mistral-small-latest`,
`ministral-8b-latest`. Un `403` ou un `404` passe au suivant sans attente, un
`429` déclenche deux nouvelles tentatives en respectant l'en-tête `Retry-After`.

Relevé en conditions réelles sur un plan gratuit Mistral :

| Modèle | Résultat |
| --- | --- |
| `mistral-large` | `403`, absent de l'offre |
| `mistral-medium`, `mistral-small` | `429` dès qu'un diff dépasse quelques milliers de lignes |
| `ministral-14b` | répond, relecture correcte |
| `ministral-8b` | répond, relecture plus approximative |

`mistral-medium-latest` reste le défaut : il devient utilisable dès le passage
en pay-as-you-go, sans toucher au code. Son refus coûte environ 25 secondes.

## Ce que la relecture ne fait pas

Le modèle se trompe. Il a déjà signalé des variables inutilisées qui servaient,
et demandé une chose puis son contraire d'un run à l'autre. C'est une aide à la
relecture, pas une validation. Les contrôles objectifs, eux, sont déterministes
et fiables.

## Développement

```bash
python3 scripts/test_auto_review.py
```

Les tests simulent tous les appels réseau : aucune clé ni accès Internet n'est
nécessaire. Ils couvrent les contrôles objectifs, la configuration, le choix
des modèles et quatre parcours complets, dont le repli en cascade et l'échec
total.
