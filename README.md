# BUSY Bar · Agent Deck

Un petit équipage de **Clawd terracotta** et de **personnages Codex bleus** sur la matrice LED frontale de la BUSY Bar. Le Mac suit les sessions locales et envoie un bitmap de **72 × 16 pixels** toutes les secondes lorsqu'il change.

## Une vue qui suit ton équipe

**Claude et Codex travaillent tous les deux → Duo.** Les grands personnages accompagnent le nombre de sessions actives de chaque famille.

![Duo : exemple fictif](docs/duo.png)

**Une seule famille travaille → Petite équipe.** Chaque petit personnage représente une session réelle. Une session Claude inactive n'empêche pas une équipe entièrement bleue, et inversement.

![Équipe Claude : exemple fictif](docs/claude-team.png)
![Équipe Codex : exemple fictif](docs/codex-team.png)

Ces trois images sont des exemples fictifs générés par `agent-deck demo docs`. Le programme ne les utilise jamais comme données de suivi.

| Repère | État |
| --- | --- |
| Terracotta ou bleu, petites pattes animées | Session au travail |
| `!` jaune | Interaction requise, si l'événement est observable |
| `✓` vert | Réponse terminée depuis le démarrage du moniteur, non acquittée |
| Gris / `?` | Inactif ou source indisponible |
| `+N` | Sessions supplémentaires au-delà des six emplacements |

Les sessions qui demandent une interaction passent en premier. Une réponse terminée de l'autre famille reste signalée par une petite coche verte, sans empêcher la vue d'équipe de la famille qui travaille. Aucun affichage personnalisé sur l'écran arrière.

## Installation sur macOS

Prérequis : Python 3.11+, [uv](https://docs.astral.sh/uv/), Claude Code et/ou l'app Codex avec leurs données locales. [ccusage](https://ccusage.com/) 20+ est optionnel pour les tokens. La version testée est 20.0.26, avec le rapport `daily --by-agent`.

```sh
git clone git@github.com:tristanbes/busybar-agent-deck.git
cd busybar-agent-deck
uv sync --locked
uv run agent-deck install --address http://10.0.4.20 --timezone Europe/Paris
uv run agent-deck status
```

`install` ajoute un LaunchAgent macOS qui démarre à la connexion, redémarre le programme s'il s'arrête et utilise le Python de ce dépôt. **Garde le dossier du dépôt à cet emplacement.**

Il ajoute aussi des hooks de statut dans `~/.claude/settings.json`, après une sauvegarde privée. Il préserve les autres hooks, permissions et réglages. Les hooks ne rendent aucune décision à Claude : ils écrivent seulement un identifiant, un état et une date, sans réseau, et sortent avec le code 0. Pour une session Claude déjà ouverte, vérifier leur présence dans `/hooks` ; un redémarrage de cette session garantit le chargement de la nouvelle configuration. Le registre natif fournit déjà l'activité sans attendre ces hooks.

Pour utiliser uniquement les sources passives :

```sh
uv run agent-deck install --no-claude-hooks
```

Pour arrêter le programme et enlever uniquement ses hooks et son LaunchAgent :

```sh
uv run agent-deck uninstall
```

Les sauvegardes et les fichiers locaux sont conservés dans `~/Library/Application Support/BusybarAgentDeck`. Ce dossier ne fait pas partie du dépôt. La configuration `config.json` permet de préciser l'adresse, le fuseau et le chemin de `ccusage`, ainsi qu'un `token` si la connexion locale de la barre exige une clé. Aucun accès cloud BUSY n'est utilisé.

## Commandes sur la barre

| Commande | Action |
| --- | --- |
| Start | Alterner sessions / tokens |
| Molette vers + / vers − | Tokens / sessions |
| Back | Revenir aux sessions |
| OK | Acquitter les coches de fin actuellement visibles |
| Sélecteur Off ou Settings | Suspendre notre affichage |

Une attente d'interaction conserve l'affichage des sessions même si la vue tokens était sélectionnée. Aucun bouton n'accepte une permission et n'envoie une réponse à un agent.

Les mêmes commandes sont disponibles depuis le terminal :

```sh
uv run agent-deck view tokens
uv run agent-deck view sessions
uv run agent-deck ack
uv run agent-deck snapshot front.png --leds
```

`snapshot` lit le framebuffer de la vraie barre ; ce n'est pas une photo physique des LED.

## Les tokens

La vue tokens affiche les totaux du jour **par famille**, rafraîchis toutes les deux minutes, en K ou M. Elle reprend les `totalTokens` de ccusage, y compris les tokens de cache tels que cet outil les rapporte. Ce n'est ni un débit instantané, ni la taille du contexte, ni le quota d'abonnement restant. Aucun coût n'est extrapolé pour un modèle sans tarif connu. Une erreur, des données trop anciennes ou un changement de jour affichent `?`.

## Sources et limites du suivi

**Claude CLI** : `~/.claude/sessions/*.json` donne l'identité, l'état busy/idle et la durée de vie du processus. Le PID et sa date de démarrage UTC sont vérifiés, pour éliminer les processus fermés et les PID réutilisés. Les hooks `PermissionRequest`, `AskUserQuestion` et `Elicitation` précisent les attentes ; `PostToolUse` et `UserPromptSubmit` indiquent la reprise. Les sous-agents n'augmentent pas le compteur de sessions principales. Seules les sessions présentes dans le registre natif sont comptées ; les variantes de CLI qui n'y figurent pas demandent un autre adaptateur.

**Codex Mac / sessions locales** : lecture seule de `~/.codex/state_5.sqlite`, `thread_history_1.sqlite` et des événements de rollout. Le dernier tour de chaque conversation utilisateur est retenu, les conversations archivées et les sous-agents sont exclus. Les anciens tours incomplets ne sont pas comptés : un tour `inProgress` sans aucune écriture locale depuis 15 minutes est écarté. Ce seuil évite les sessions fantômes, mais peut aussi masquer un tour très long et silencieux ou une attente prolongée.

L'activité de ce chat Codex a été vérifiée localement ; les transitions de fin et de question sont couvertes par des tests sur des événements contrôlés. La détection des questions synchrones utilise les événements de rollout quand ils existent. **Toutes les demandes d'approbation de l'app Codex ne sont pas exposées dans ces fichiers : le jaune côté Codex reste une détection partielle.** Une question asynchrone ne fait pas passer à tort une session encore active en attente. Les bases et formats internes de Codex peuvent changer : une incompatibilité de lecture affiche un `?`, sans modifier les données de l'app. Les tâches orchestrées dans le cloud et les autres machines ne sont pas suivies.

À son démarrage, le moniteur ignore les anciennes réponses terminées. Il garde les nouvelles coches jusqu'à acquittement ou nouveau travail. Une erreur de source ne réutilise pas le dernier compteur connu. Les bitmaps ont un bail de 15 secondes, renouvelé au plus tard toutes les 5 secondes : si le Mac ou le programme disparaît, la barre cesse d'afficher ces données. Une application de priorité supérieure peut prendre le dessus ; le programme ne monte pas automatiquement sa priorité pour la déloger.

Le programme lit les fichiers locaux nécessaires au suivi et à ccusage, mais ne les copie pas dans le dépôt et n'envoie aucun contenu de conversation à la barre. Il lui transmet uniquement les pixels. Il ne change pas sa luminosité, son réseau ni les réglages de WARP.

## Développement et vérification

```sh
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run agent-deck run
```

Les tests couvrent notamment les deux équipes seules, la bascule Duo, l'overflow, la priorité des attentes, les sessions mortes, les tours Codex anciens, l'acquittement et les erreurs de ccusage. La CI les exécute sur Linux ; les tests ne contactent aucun appareil et ne lancent aucun modèle.

Intégration matérielle vérifiée sur BUSY Bar firmware **1.2.4**, API **27.5.0** : écriture XPM et relecture exacte du framebuffer frontal en BGR888. Les sprites sont dessinés dans le code, inspirés de Clawd et du personnage bleu choisi dans la prévisualisation ; ce projet est indépendant des produits concernés.

Références : [firmware BUSY Bar](https://github.com/busy-app/busybar-firmware), [SDK Python busylib](https://github.com/busy-app/busylib-py), [hooks Claude Code](https://code.claude.com/docs/en/hooks), [hooks Codex](https://learn.chatgpt.com/docs/hooks), [ccusage](https://ccusage.com/).
