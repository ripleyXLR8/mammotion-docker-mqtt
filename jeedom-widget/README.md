# Widget Jeedom — caméra FPV dans une tuile

`cmd.info.string.mammotionCamera.html` est un **widget de commande Jeedom** qui
affiche le flux caméra FPV de la tondeuse directement dans une tuile du tableau
de bord, sans quitter Jeedom.

Il ne décode rien lui-même : au clic sur la vignette il ouvre, dans une `iframe`,
la page lecteur servie par le pont (`http://<hôte>:8189/`, SDK Agora Web). Le
navigateur reste le pair WebRTC, exactement comme l'app officielle. Un bouton
**Fermer** retire l'`iframe` et **libère la session** (le cloud Mammotion
n'autorise qu'une session à la fois — voir le README principal).

## Installation

1. Copier le fichier dans le dossier des templates personnalisés de Jeedom :
   ```
   data/customTemplates/dashboard/cmd.info.string.mammotionCamera.html
   ```
   (propriétaire `www-data`, soit `chown 33:33` depuis l'hôte Docker).
2. Sur l'équipement de la tondeuse, créer une commande **info / string**
   (ex. « Caméra FPV »), la rendre visible.
3. Dans l'onglet *Affichage* de cette commande, choisir le widget
   **mammotionCamera** (Dashboard et Mobile).
4. Renseigner la **valeur** de la commande avec l'URL du pont, p. ex.
   `http://192.168.100.170:8189` — le widget la lit via `data-camurl`.
   À défaut, il utilise l'URL de repli codée en dur dans le fichier (à adapter).

## Notes

- L'iframe pointe vers un port HTTP du pont (8189 par défaut). Si Jeedom est en
  HTTPS, servez aussi le pont en HTTPS (ou via un reverse proxy) pour éviter le
  blocage *mixed-content*.
- Le flux n'est demandé qu'au clic ; tant que la tuile n'est pas ouverte, aucune
  session caméra n'est consommée.
- Le bouton **Plein écran** ouvre le lecteur autonome dans un nouvel onglet.
