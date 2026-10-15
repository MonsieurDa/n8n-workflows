# Talent Matcher

Petite application web de sourcing : vous déposez un appel d'offres ou une description de mission, elle cherche sur LinkedIn des candidats (freelances, salariés ou les deux) qui correspondent, puis vous rend la liste classée. Vous pouvez aussi la recevoir par e-mail ou l'exporter en CSV.

## Fonctionnement

1. Vous collez l'offre dans le formulaire (ou vous déposez un fichier `.txt` / `.md`) et vous choisissez le type de profil : **Freelance**, **Salarié** ou **Indifférent**. Vous pouvez aussi préciser la localisation, la séniorité et le nombre de candidats voulus.
2. Claude (Anthropic) analyse l'offre, puis lance lui-même plusieurs recherches web limitées à `linkedin.com` pour trouver des profils publics.
3. Il note chaque profil de 0 à 100 et indique ses points forts, ce qu'il reste à vérifier et le statut repéré (freelance, salarié ou inconnu).
4. Le serveur écarte toute URL qui n'est pas réellement apparue dans les résultats de recherche (aucun profil inventé). En mode freelance, il retire aussi les profils clairement salariés.
5. La shortlist s'affiche avec des filtres (score minimum, freelances confirmés). Vous pouvez la copier, l'exporter en CSV ou la recevoir par e-mail.

L'application ne se connecte pas à votre compte LinkedIn et ne scrape pas LinkedIn. Elle s'appuie uniquement sur des résultats de recherche web publics.

## Installation

Il faut Python 3.10 ou plus récent.

```bash
cd apps/talent-matcher
python -m venv .venv && source .venv/bin/activate   # Windows : .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # puis renseignez ANTHROPIC_API_KEY (et le SMTP si vous voulez l'e-mail)
uvicorn app:app --port 8000
```

Ouvrez ensuite <http://localhost:8000>.

Une recherche prend en général entre 1 et 4 minutes. Si le serveur n'est pas démarré, la page affiche quand même un exemple pour montrer le rendu.

## Configuration (`.env`)

| Variable | Rôle |
|---|---|
| `ANTHROPIC_API_KEY` | Obligatoire. Clé API Claude, à créer sur <https://console.anthropic.com>. La recherche web doit être activée pour votre organisation (Console > Settings). |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Facultatif. Sert à envoyer la liste par e-mail. Avec Gmail, utilisez un « mot de passe d'application ». |

## Fichiers

- `matcher.py` : la logique de recherche et de notation avec Claude (modèle `claude-opus-5-5`, outil de recherche web, outil `submit_candidates` pour obtenir un résultat structuré).
- `app.py` : le serveur web (FastAPI). Il lance les recherches en arrière-plan, que la page interroge toutes les 3 secondes, et envoie l'e-mail.
- `static/index.html` : l'interface.

## Coût et limites

- Chaque recherche consomme des tokens Claude et jusqu'à 12 recherches web, soit en général quelques dizaines de centimes. Le maximum se règle avec `max_uses` dans `matcher.py`.
- Les notes reposent sur ce que montrent les résultats publics (titre, extrait). Ce sont des indications : vérifiez chaque profil avant de contacter quelqu'un.
- Les recherches en cours sont gardées en mémoire. Si vous redémarrez le serveur, elles sont perdues.
- RGPD : si vous conservez ces listes, informez les personnes contactées et limitez la durée de conservation.
