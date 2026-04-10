FROM python:3.12-slim

WORKDIR /app

# Installation des dépendances
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copie du script principal
COPY bridge.py .

# Commande par défaut
CMD ["python", "bridge.py"]
