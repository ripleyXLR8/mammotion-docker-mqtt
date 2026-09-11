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

1. Copier le fichier dans les templates personnalisés de Jeedom, **pour chaque
   version que tu utilises** (`dashboard` = bureau, `mobile` = app Jeedom
   officielle) :
   ```
   data/customTemplates/dashboard/cmd.info.string.mammotionCamera.html
   data/customTemplates/mobile/cmd.info.string.mammotionCamera.html
   ```
   (propriétaire `www-data`, soit `chown 33:33` depuis l'hôte Docker).
   Note : **JeedomConnect** n'utilise pas ces templates (système de widgets
   propre) — voir plus bas.
2. Sur l'équipement de la tondeuse, créer une commande **info / string**
   (ex. « Caméra FPV »), la rendre visible.
3. Dans l'onglet *Affichage* de cette commande, choisir le widget
   **mammotionCamera** (Dashboard et Mobile).
4. Renseigner la **valeur** de la commande avec l'URL du pont, p. ex.
   `http://192.168.100.170:8189` — le widget la lit via `data-camurl`.
   À défaut, il utilise l'URL de repli codée en dur dans le fichier (à adapter).

## Jeedom en HTTPS : éviter le blocage *mixed-content*

Une page HTTPS **refuse** une `iframe` en HTTP : si tu ouvres Jeedom via une URL
HTTPS (domaine derrière un reverse proxy), pointer la tuile vers
`http://<hôte>:8189` échoue silencieusement. Sers alors le pont en HTTPS, le plus
simple étant de le republier **sous un sous-chemin du domaine Jeedom** :

1. Pont : variable `BASE_PATH=/mammocam` (le lecteur, `/tokens` et `/keepalive`
   sont alors servis sous ce préfixe).
2. Reverse proxy (ex. Nginx Proxy Manager, sur l'hôte du domaine Jeedom) :
   ajouter une *custom location* `/mammocam` → `http://<hôte-du-pont>:8189`
   (sans réécriture : le pont attend déjà le préfixe).
3. Valeur de la commande caméra = `https://<domaine-jeedom>/mammocam`.

La tuile charge alors une `iframe` **de même origine** que le tableau de bord :
ni *mixed-content*, ni souci de CSP `frame-src`.

## Notes

- Sans HTTPS (Jeedom ouvert en `http://<ip>`), l'iframe HTTP directe vers
  `http://<hôte>:8189` fonctionne telle quelle.
- Le flux n'est demandé qu'au clic ; tant que la tuile n'est pas ouverte, aucune
  session caméra n'est consommée.
- Le bouton **Plein écran** ouvre le lecteur autonome dans un nouvel onglet.

## JeedomConnect

JeedomConnect a son propre moteur de widgets et **ignore** `customTemplates/`.
Pour y afficher la caméra, deux options :

- ouvrir directement `https://<domaine-jeedom>/mammocam/` dans le navigateur du
  téléphone (le lecteur autonome, déjà fonctionnel) ;
- configurer dans JeedomConnect un widget capable d'afficher une page web
  (webview / iframe / lien) pointant vers cette même URL.

## Widget carte GPS (`cmd.info.string.mammotionMap.html`)

Affiche la position de la tondeuse sur une carte **Leaflet + OpenStreetMap**
(aucune clef d'API). Même montage que la caméra : le pont sert la page `/map`
(et `/position` en JSON), le widget l'affiche en iframe (auto-chargée, pas de
contrainte de session). Installer comme la caméra (fichier dans
`customTemplates/dashboard` et `/mobile`, commande info/string, valeur =
`https://<domaine>/mammocam/map`, template `mammotionMap`).

Position : `location.device` de la tondeuse est en **degrés absolus** dès qu'il y
a un fix RTK (sinon ~0 → repli sur la base) ; `location.RTK` (la base) est en
**radians**, converti en degrés et affiché comme repère « Base RTK ». `/position`
renvoie `{mower, base, mower_src, raw}`. Si le device ne fournit pas de position
absolue, renseigne `GARDEN_ANCHOR=lat,lon` (voir README principal).

**Zones de tonte** : le pont synchronise la carte de la tondeuse au démarrage
(`start_map_sync`) et sert le GeoJSON des aires/obstacles/chemins sur `/zones`
(chaque `properties` porte le style Leaflet). La page carte les dessine
(`L.geoJSON`) et cadre la vue sur leur emprise. Les aires apparaissent en vert.

**Fond satellite + recalage** : sélecteur OSM/Satellite (Esri, sans clef). Le RTK
étant centimétrique en relatif mais à quelques mètres près en absolu, un bouton
**« Recaler la zone »** fait apparaître une poignée à glisser sur la vraie base ;
« Enregistrer » envoie `POST /offset`, mémorisé côté serveur en **MQTT retenu**
(`mammotion/config/map_offset`) — donc persistant et appliqué partout (tuile, tous
appareils). Sans fix RTK (tondeuse en veille), la dernière position/base valides
sont mises en cache pour ne pas vider la carte ; la **base RTK** (qui ne bouge pas) est en plus **persistée** en MQTT retenu (`mammotion/config/rtk_base`), si bien que le repère base et les zones sont disponibles dès le démarrage du pont, sans attendre un fix.
