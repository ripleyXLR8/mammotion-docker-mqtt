# mammotion-docker-mqtt

Pont **cloud Mammotion → MQTT Discovery** pour tondeuses robots (Luba / Yuka).
Il publie l'état et les commandes de chaque tondeuse au format « MQTT Discovery »
(dit aussi « HA Discovery »), que le plugin **Jeedom MQTT Discovery** — ou Home
Assistant — transforme tout seul en équipement.

Le dialogue passe par [PyMammotion](https://github.com/mikey0000/PyMammotion)
(login cloud, MQTT Aliyun, décodage protobuf, rafraîchissement de jeton).

## ⚠️ Compte dédié obligatoire

Le cloud Mammotion **n'autorise qu'une seule session par compte**. Si le pont
utilise ton compte principal, l'app mobile et le pont se déconnectent l'un
l'autre en boucle. La bonne pratique :

1. Créer un **second compte Mammotion** dédié (ex. `toi+bridge@…`).
2. Dans l'app (compte principal) : appareil → **Partager l'appareil** → e-mail du
   compte dédié.
3. Se connecter à l'app avec le compte dédié une fois pour **accepter le partage**.
4. Renseigner l'e-mail/mot de passe du **compte dédié** dans le pont.

## Configuration (variables d'environnement)

| Variable | Défaut | Rôle |
|---|---|---|
| `MAMMOTION_EMAIL` / `MAMMOTION_PASSWORD` | — | compte Mammotion (dédié) |
| `MQTT_BROKER` / `MQTT_PORT` | `127.0.0.1` / `1883` | broker MQTT |
| `MQTT_USER` / `MQTT_PASSWORD` | — | identifiants MQTT (optionnels) |
| `MQTT_DISCOVERY_PREFIX` | `homeassistant` | préfixe de découverte |
| `MQTT_TOPIC_PREFIX` | `mammotion` | préfixe des topics d'état/commande |
| `POLL_INTERVAL` | `60` | intervalle de republication de l'état (s) |
| `INCLUDE_RTK` | `false` | exposer aussi la base RTK (capteurs seuls) |
| `HTTP_PORT` | `8099` | port du lecteur caméra FPV (page + jetons/keepalive) |
| `BASE_PATH` | *(vide)* | sous-chemin de service derrière un reverse proxy qui **ne retire pas** le préfixe, ex. `/mammocam` → sert aussi `/mammocam/`, `/mammocam/tokens`, `/mammocam/keepalive`. Indispensable pour intégrer la tuile caméra dans un Jeedom servi en HTTPS (évite le blocage *mixed-content*) |
| `AGORA_SDK_URL` | CDN jsdelivr | URL du SDK Agora Web chargé par le lecteur |
| `GARDEN_ANCHOR` | *(vide)* | ancre géographique absolue `lat,lon` du jardin/base. Les coordonnées de la tondeuse sont **relatives à la base RTK** (offset ~1e-5°) : sans ancre absolue elles tombent près de `0,0`. Utilisé par la page carte `/map` (voir `/position`). Le pont préfère la position absolue de la base RTK si le device la fournit, sinon cette ancre. |
| `MAMMOTION_HA_VERSION` | `0.6.4` | **doit être un vrai numéro de version** : le serveur en dérive l'en-tête App-Version et refuse le login sinon (renvoyé, à tort, comme « Account or password mismatch ») |

## Ce qui est exposé

- **Capteurs** : État (mode), Batterie (%), Progression (%), Réseau, Erreur,
  Latitude/Longitude ; **binaires** : En charge, Lame.
- **Commandes** (boutons, tondeuses seulement) : Démarrer, Pause, Retour à la
  base, Annuler, Quitter la base, Lame ON, Lame OFF.
- **Caméra FPV** : le flux Agora (WebRTC propriétaire) n'a pas de forme
  RTSP/snapshot, mais le pont sert une **page lecteur** (`http://<hôte>:8189/`,
  port `HTTP_PORT` publié) où le navigateur est le pair WebRTC via le SDK Agora
  Web. Voir [`jeedom-widget/`](jeedom-widget/) pour l'afficher dans une tuile
  Jeedom. Endpoints : `/` (lecteur), `/tokens` (jetons Agora), `/keepalive`
  (`refresh_fpv`). Le cloud n'autorisant qu'une session à la fois, fermez la vue
  pour libérer la caméra.
- **Carte GPS** : le pont sert une **page carte** (`/map`, Leaflet + tuiles
  OpenStreetMap, **aucune clef d'API**) et un endpoint `/position` qui donne la
  position absolue de la tondeuse (`location.device` en degrés dès qu'il y a un
  fix RTK) et de la base RTK (`location.RTK`, en radians → degrés). Affichable
  dans une tuile Jeedom (voir [`jeedom-widget/`](jeedom-widget/)).

## Avertissement

L'accès non officiel au cloud Mammotion est contraire à ses CGU et **peut
entraîner le bannissement du compte**. Utiliser un compte dédié limite le risque.
Usage personnel, à vos risques.
