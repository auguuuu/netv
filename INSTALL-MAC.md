# Installer neTV sur un autre Mac

Environ 15 minutes, dont la plupart passées à attendre. Tu copies-colles 3 commandes dans
le Terminal, rien d'autre.

## Avant de commencer (sur l'ancien Mac)

Envoie-toi par AirDrop le fichier **`netv-settings.tar.gz`** qui est sur le Bureau de
l'ancien Mac. Il contient tes accès IPTV, tes favoris et ton historique. Il arrive dans
**Téléchargements** sur le nouveau Mac.

> Ce fichier contient ton mot de passe IPTV : ne le mets jamais sur GitHub ni dans un mail.
> S'il n'est plus sur le Bureau, refais-le sur l'ancien Mac avec :
> `~/dev/netv/tools/export-settings.sh`

## Sur le nouveau Mac

Ouvre l'app **Terminal** (Cmd + Espace, tape « Terminal », Entrée), puis colle les commandes
une par une. Appuie sur Entrée après chacune et attends qu'elle soit finie.

**1. Installer Homebrew** (l'outil qui installe le reste). Il demande ton mot de passe du Mac
(rien ne s'affiche quand tu tapes, c'est normal), puis Entrée pour confirmer.

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

À la fin, s'il affiche « Next steps » avec des commandes `eval ...`, ferme le Terminal et
rouvre-le.

**2. Récupérer le projet depuis GitHub.** Il te demande de te connecter à GitHub : choisis
`GitHub.com`, `HTTPS`, `Yes`, puis `Login with a web browser`. Copie le code affiché,
appuie sur Entrée et valide dans le navigateur.

```bash
eval "$(/opt/homebrew/bin/brew shellenv)" && brew install gh && gh auth login && gh repo clone auguuuu/netv ~/netv -- -b custom
```

**3. Installer le serveur** avec tes réglages :

```bash
~/netv/tools/install-mac.sh --settings ~/Downloads/netv-settings.tar.gz
```

Quand il affiche `neTV is running`, c'est terminé.

## Utilisation

- Adresse : **http://localhost:8765** (mets-la en favori)
- Pas de mot de passe. Le serveur n'est accessible que depuis ce Mac.
- Il démarre tout seul quand tu allumes le Mac et redémarre tout seul s'il plante.
- Les films téléchargés vont dans **Vidéos > neTV**.

## Mises à jour

Automatiques : toutes les 30 minutes, le Mac regarde s'il y a du nouveau sur GitHub
et se met à jour tout seul. Il attend la fin d'un téléchargement ou d'une lecture en cours
avant de redémarrer, pour ne rien couper.

Pour forcer une mise à jour tout de suite :

```bash
~/netv/tools/update.sh --force
```

## En cas de problème

Redémarrer le serveur :

```bash
launchctl kickstart -k gui/$(id -u)/com.augustin.netv
```

Voir ce qui se passe (les 50 dernières lignes du journal) :

```bash
tail -50 ~/Library/Logs/netv/netv.log
```
