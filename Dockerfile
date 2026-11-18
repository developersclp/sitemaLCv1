# Imagem base oficial Python 3.12 Slim (Debian Bookworm)
FROM python:3.12-slim

# Evita geracao de bytecode .pyc e forca streaming imediato dos logs no Railway
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=America/Sao_Paulo \
    PORT=8080

WORKDIR /app

# Instala bibliotecas C essenciais para operacoes espaciais/GIS (GDAL, GEOS, PROJ), tzdata e compiladores
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    build-essential \
    gcc \
    g++ \
    gdal-bin \
    libgdal-dev \
    libgeos-dev \
    libproj-dev \
    proj-bin \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Instala as dependencias Python com cache de camada Docker
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copia todos os modulos, malhas viarias GPKG e arquivos estaticos
COPY . /app/

# Porta padrao (no Railway a porta real e fornecida dinamicamente na variavel $PORT)
EXPOSE 8080

# Inicializa o servidor Locar LC v1
CMD ["python", "app.py"]
