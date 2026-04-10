import asyncio
import os
import json
import logging
from pymammotion.client import MammotionClient
import aiomqtt
import betterproto2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

MAMMOTION_EMAIL = os.environ.get("MAMMOTION_EMAIL")
MAMMOTION_PASSWORD = os.environ.get("MAMMOTION_PASSWORD")
MQTT_BROKER = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USER = os.environ.get("MQTT_USER")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD")

async def main():
    if not MAMMOTION_EMAIL or not MAMMOTION_PASSWORD:
        logger.error("Les identifiants MAMMOTION_EMAIL et MAMMOTION_PASSWORD sont requis.")
        return

    # 1. Initialisation de Mammotion
    mammotion = MammotionClient()
    logger.info("Connexion au Cloud Mammotion...")
    await mammotion.login_and_initiate_cloud(MAMMOTION_EMAIL, MAMMOTION_PASSWORD)
    
    # Laisser le temps au client de récupérer les appareils et leur premier état
    await asyncio.sleep(5)
    
    devices = mammotion.device_registry.all_devices
    if not devices:
        logger.error("Aucune tondeuse trouvée sur ce compte.")
        return
        
    device_names = [d.device_name for d in devices]
    logger.info(f"Tondeuses trouvées : {device_names}")

    # 2. Connexion au broker MQTT Local
    logger.info(f"Connexion au MQTT local ({MQTT_BROKER}:{MQTT_PORT})...")
    async with aiomqtt.Client(
        hostname=MQTT_BROKER, 
        port=MQTT_PORT, 
        username=MQTT_USER, 
        password=MQTT_PASSWORD
    ) as mqtt_client:
        
        # --- NOUVEAUTÉ 1 : Publication immédiate de l'état complet au démarrage ---
        for dev_name in device_names:
            handle = mammotion.device_registry.get_by_name(dev_name)
            if handle and handle.snapshot.raw:
                try:
                    # to_json() génère tout le dictionnaire de la tondeuse d'un coup
                    full_state = handle.snapshot.raw.to_json()
                    await mqtt_client.publish(f"mammotion/{dev_name}/state", full_state, retain=True)
                    logger.info(f"État initial publié sur MQTT pour {dev_name}")
                except Exception as e:
                    logger.error(f"Erreur lors de la publication initiale pour {dev_name}: {e}")

        # Callback pour remonter les mises à jour d'état vers MQTT
        def make_state_callback(dev_name):
            async def on_message(msg):
                handle = mammotion.device_registry.get_by_name(dev_name)
                if handle and handle.snapshot.raw:
                    try:
                        # --- NOUVEAUTÉ 2 : Envoi de l'état complet à chaque MAJ ---
                        full_state = handle.snapshot.raw.to_json()
                        await mqtt_client.publish(f"mammotion/{dev_name}/state", full_state, retain=True)
                    except Exception as e:
                        logger.error(f"Erreur lors de la maj d'état pour {dev_name}: {e}")
            return on_message

        # Abonnement aux événements Mammotion pour chaque tondeuse
        callbacks = []
        for dev_name in device_names:
            handle = mammotion.device_registry.get_by_name(dev_name)
            cb = make_state_callback(dev_name)
            sub = handle.broker.subscribe_unsolicited(cb)
            callbacks.append(sub) # Garder une référence forte
            
            # S'abonner au topic de commande MQTT local pour cette tondeuse
            await mqtt_client.subscribe(f"mammotion/{dev_name}/set")

        logger.info("Prêt. En attente des événements et des commandes MQTT...")

        # 3. Écoute des commandes MQTT pour piloter la tondeuse
        async for message in mqtt_client.messages:
            topic = str(message.topic)
            try:
                payload = json.loads(message.payload.decode())
                command = payload.get("command")
                
                # Extraire le nom de l'appareil depuis le topic (ex: mammotion/Luba-XXX/set)
                dev_name = topic.split("/")[1]
                
                logger.info(f"Commande reçue pour {dev_name} : {command}")

                if command == "start":
                    await mammotion.send_command_with_args(dev_name, "start_job")
                elif command == "pause":
                    await mammotion.send_command_with_args(dev_name, "pause_execute_task")
                elif command == "dock":
                    await mammotion.send_command_with_args(dev_name, "return_to_dock")
                elif command == "cancel":
                    await mammotion.send_command_with_args(dev_name, "cancel_job")
                elif command == "blades_on":
                    await mammotion.send_command_with_args(dev_name, "start_stop_blades", start_stop=True)
                elif command == "blades_off":
                    await mammotion.send_command_with_args(dev_name, "start_stop_blades", start_stop=False)
                else:
                    logger.warning(f"Commande inconnue: {command}")

            except Exception as e:
                logger.error(f"Erreur lors du traitement du message MQTT: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arrêt du pont MQTT.")
