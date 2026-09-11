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

## Ce qui est exposé

- **Capteurs** : État (mode), Batterie (%), Progression (%), Réseau, Erreur,
  Latitude/Longitude ; **binaires** : En charge, Lame.
- **Commandes** (boutons, tondeuses seulement) : Démarrer, Pause, Retour à la
  base, Annuler, Quitter la base, Lame ON, Lame OFF.
- La **caméra FPV n'est pas exposée** : elle transite par Agora (WebRTC
  propriétaire), sans forme RTSP/snapshot exploitable par Jeedom.

## Avertissement

L'accès non officiel au cloud Mammotion est contraire à ses CGU et **peut
entraîner le bannissement du compte**. Utiliser un compte dédié limite le risque.
Usage personnel, à vos risques.
