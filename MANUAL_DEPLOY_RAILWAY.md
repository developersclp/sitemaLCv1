# 🚀 Manual Oficial de Deploy no Railway — Sistema LC v1 (Locar & Inlog)

Este guia contém o passo a passo completo e definitivo para colocar o **Sistema LC v1** no ar pelo **Railway**, com **Docker oficial**, **banco de dados próprio persistente (SQLite + Volume)**, **chaves da API Inlog seguras**, **autenticação do Painel Central** e **Links Exclusivos Assinados (`/v/<token>`)**.

---

## 1. Por Que o Erro `pip: command not found` Ocorria e Como Foi Resolvido

No Nixpacks padrão, ao customizar pacotes nativos para C/GIS (`gcc`, `gdal`, `geos`, `proj`), o pacote `python312` puro do Nixpkgs vinha sem o binário executável do `pip` configurado no ambiente `/bin/bash`, causando a falha imediata no deploy (`exit code 127`).

**A Solução Implementada:**
1. **`Dockerfile` Oficial Debian (`python:3.12-slim`):** O projeto agora utiliza Docker nativo com `pip`, `gdal-bin`, `libgdal-dev`, `libgeos-dev`, `libproj-dev` e compiladores instalados de forma 100% determinística.
2. **`railway.json` Otimizado:** Configurado com `"builder": "DOCKERFILE"`, garantindo cache de camadas e build rápido.
3. **`.dockerignore` Criado:** Bloqueia o upload de arquivos desnecessários (`.git`, `locar.db` local pesado de >600MB, `__pycache__`), tornando o envio e build extremamente leves.
4. **`nixpacks.toml` Corrigido (Fallback):** Mesmo que o Railway use Nixpacks, agora ele inclui `python312Packages.pip`, `virtualenv` e ativação automática do ambiente.

---

## 2. Arquivos de Configuração no Projeto

| Arquivo | Função no Railway |
| :--- | :--- |
| `Dockerfile` | Constrói a imagem Linux Debian com Python 3.12, GDAL, GEOS, PROJ e dependências. |
| `.dockerignore` | Evita envio de arquivos pesados locais para o container. |
| `requirements.txt` | Lista as bibliotecas Python (`geopandas`, `pyogrio`, `shapely`, `pandas`, `pyproj`, `requests`, `urllib3`). |
| `railway.json` | Define builder `DOCKERFILE`, política de restart e healthcheck em `/api/status`. |
| `nixpacks.toml` | Fallback de contingência caso o builder Nixpacks seja selecionado. |
| `Procfile` | Declara o processo web (`python app.py`). |
| `database.py` | Suporta `DATABASE_PATH` e `RAILWAY_VOLUME_MOUNT_PATH` com persistência em `/data`. |
| `.env.example` | Modelo com todas as variáveis de ambiente necessárias para produção. |

---

## 3. Passo a Passo do Deploy no Railway

### Passo 1: Enviar as Atualizações para o GitHub

No terminal do seu projeto:

```bash
git add .
git commit -m "Fix deploy: Dockerfile oficial com suporte GIS GDAL/GEOS e railway.json configurado"
git push origin main
```

*(Se o seu repositório já estiver conectado ao Railway, um novo build iniciará automaticamente!)*

---

### Passo 2: Conectar o Repositório no Railway (Se for um Novo Projeto)

1. Acesse [railway.app](https://railway.app) e faça login com seu GitHub.
2. Clique em **+ New Project** -> **Deploy from GitHub repo**.
3. Selecione o repositório `sitemaLCv1` (ou o repositório onde seu código está hospedado).
4. O Railway detectará automaticamente o `Dockerfile` e `railway.json`.

---

### Passo 3: Configurar o Volume Persistente para o Banco SQLite

Para garantir que o banco de dados `locar.db` (com edições de malha e históricos) não seja reiniciado a cada novo deploy:

1. No painel do seu serviço no Railway, clique em **+ New** (ou no menu do canvas) -> **Volume** (ou aperte `Ctrl + K` -> digite `Volume`).
2. Conecte o Volume ao seu serviço.
3. Em **Mount Path**, digite exatamente:
   ```
   /data
   ```
4. Clique em **Save**. O banco agora ficará salvo permanentemente em `/data/locar.db`.

> 💡 **Dica de Recursos:**
> Para processamento geográfico com malhas viárias e rotas completas em memória, recomenda-se configurar o plano com pelo menos **2 GB a 4 GB de RAM** (aba **Settings** -> **Deploy** -> **Resource Limits**).

---

### Passo 4: Configurar as Variáveis de Ambiente (Aba Variables)

No painel do serviço no Railway, clique na aba **Variables** -> selecione **Raw Editor** e cole as variáveis abaixo (substituindo pelos seus dados reais):

```env
# API OFICIAL INLOG
INLOG_BASE_URL=https://locar.inlog.com.br/IntegrationColeta
INLOG_TOKEN_URL=https://locar.inlog.com.br/autenticacao-services/connect/token
INLOG_CLIENT_ID=Locar.Client.Api.Coleta
INLOG_CLIENT_SECRET=COLE_AQUI_SEU_CLIENT_SECRET_INLOG
INLOG_SERVER_ID=Inlog.Integration.Coleta.Api

# SEGURANÇA DO PAINEL E LINKS ASSINADOS
SECRET_KEY=CRIE_UMA_CHAVE_SECRETA_LONGA_E_ALEATORIA_COM_32_CHARS
ADMIN_PASSWORD=SUA_SENHA_DE_ACESSO_AO_PAINEL_CENTRAL
REQUIRE_ADMIN_LOGIN=true

# PERSISTÊNCIA NO VOLUME
DATABASE_PATH=/data/locar.db

# SERVIDOR E MALHAS
HOST=0.0.0.0
MALHA_TIPO=TRANSITAVEIS

# AUDITORIA ESPACIAL
GPS_BUFFER_METROS=20.0
VELOCIDADE_MIN_COLETA=2.0
VELOCIDADE_MAX_COLETA=25.0
DWELL_TIME_MIN_SEGUNDOS=180
```

> ⚠️ **Atenção:**
> - **Não** crie a variável `PORT` manualmente. O Railway injeta `PORT` automaticamente e a aplicação escuta na porta correta (`os.getenv("PORT", 8080)`).
> - Certifique-se de preencher `INLOG_CLIENT_SECRET` com o segredo oficial da sua empresa.

---

### Passo 5: Gerar o Domínio Público

1. No serviço, vá na aba **Settings** -> role até **Networking** -> **Public Networking**.
2. Clique no botão **Generate Domain**.
3. O Railway criará uma URL HTTPS pública (exemplo: `https://sistemalcv1-production.up.railway.app`).

---

### Passo 6: Validação do Sistema no Ar

1. **Checagem de Saúde do Servidor:**
   Acesse no navegador:
   `https://seu-dominio.up.railway.app/api/status`
   - Deve responder com HTTP 200 e JSON indicando `"inlog_configurado": true`.
2. **Acesso ao Painel Central:**
   Acesse a URL raiz:
   `https://seu-dominio.up.railway.app/`
   - O sistema solicitará sua `ADMIN_PASSWORD`.
   - Ao logar, todas as cidades cadastradas, camadas em tempo real e rotas estarão operacionais.
3. **Links Compartilhados de Visualização:**
   - Ao gerar um link `/v/<token>`, ele estará ativo para clientes sem exigir login, isolado com permissão somente-leitura.
