FROM python:3.12-slim

# Installation de tini et nettoyage du cache apt pour réduire la taille de l'image
RUN apt-get update && \
    apt-get install -y --no-install-recommends tini && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Installation des dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copie du script principal
COPY bridge.py .

# Utilisation de tini comme point d'entrée principal (PID 1)
ENTRYPOINT ["/usr/bin/tini", "--"]

# La commande passée à tini
CMD ["python", "bridge.py"]
