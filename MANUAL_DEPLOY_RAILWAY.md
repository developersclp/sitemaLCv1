# 🚀 Manual Oficial de Deploy no Railway — Sistema LC v1 (Locar & Inlog)

Este guia contém o passo a passo completo para colocar o **Sistema LC v1** no ar pelo **Railway**, com **banco de dados próprio persistente (SQLite + Volume)**, **chaves da API Inlog seguras**, **autenticação do Painel Central** e **Links Exclusivos Assinados (`/v/<token>`)**.

---

## 1. Arquivos de Deploy Já Preparados no Projeto

O sistema já está estruturado com todos os manifestos exigidos pelo Railway (tanto na raiz quanto dentro da pasta `sitemaLCv1`):

| Arquivo | Função no Railway |
| :--- | :--- |
| `requirements.txt` | Instala `geopandas`, `pyogrio`, `shapely`, `pandas` e `pyproj`. |
| `nixpacks.toml` | Configura Python 3.12 e bibliotecas nativas GIS (`gdal`, `geos`, `proj`, `gcc`). |
| `Procfile` | Define o processo web padrão (`python app.py` / `python sitemaLCv1/app.py`). |
| `railway.json` | Define builder `NIXPACKS`, política de restart automático e healthcheck `/api/status`. |
| `.env.example` | Modelo oficial de todas as variáveis de ambiente de produção. |
| `database.py` | Suporta `DATABASE_PATH` e `RAILWAY_VOLUME_MOUNT_PATH` com cópia automática de seed. |

---

## 2. Subindo o Projeto para o GitHub

> ⚠️ **Importante sobre o banco `locar.db`:**  
> O arquivo local `locar.db` costuma ultrapassar **100 MB** (limite padrão de push do GitHub). Por segurança e compatibilidade, `sitemaLCv1/.gitignore` já ignora `.env` e `locar.db*`.  
> Todas as malhas viárias (`.gpkg`) e rotas planejadas (`.gpkg`) estão abaixo de 65 MB e serão enviadas normalmente para o GitHub — na primeira inicialização no Railway, o sistema reconstrói automaticamente toda a base do banco a partir dos `.gpkg` dentro do Volume persistente.

1. Crie um repositório **Privado** no GitHub (ex: `locar-sistema-lc`).
2. Entre na pasta oficial do projeto (`cd sitemaLCv1`) para enviar somente os arquivos de produção (sem arquivos legados da pasta pai), inicialize e envie o código:
   ```bash
   cd sitemaLCv1
   git init
   git add .
   git commit -m "Deploy inicial Sistema LC v1 pronto para Railway"
   git branch -M main
   git remote add origin https://github.com/SEU_USUARIO/locar-sistema-lc.git
   git push -u origin main
   ```

---

## 3. Criando o Projeto e Serviço no Railway

1. Acesse [railway.app](https://railway.app) e faça login.
2. Clique em **New Project** -> **Deploy from GitHub repo** e selecione `locar-sistema-lc`.
3. Se você enviou a pasta inteira `sistemaLC`:
   - Vá em **Settings** -> **Source** -> **Root Directory** e informe **`sitemaLCv1`** *(recomendado, pois os caminhos de malhas e rotas ficam diretos na raiz do container)*.
4. Aguarde o build inicial no painel **Deployments** (pode levar ~2 minutos na primeira vez por causa dos pacotes geoespaciais).

---

## 4. Configurando o Banco de Dados Próprio (Volume Persistente)

Sem um **Volume**, o sistema perderia as edições feitas no QGIS Web e o cache de auditorias/GPS a cada novo deploy.

1. No serviço criado no Railway, clique com o botão direito na tela de arquitetura (ou aperte `Ctrl + K` -> **Add Volume**) e adicione um **Volume** ao serviço.
2. Em **Mount Path**, configure exatamente:
   ```
   /data
   ```
3. Como definido em `database.py`, o banco passará a gravar em `/data/locar.db` (e se um dia você subir seu `locar.db` via Railway CLI/SFTP para `/data/locar.db`, ele será preservado entre todos os deploys).

> 💡 **Dica de Recursos:**  
> Como o cruzamento espacial carrega malhas `.gpkg` completas em RAM, recomenda-se manter o serviço no plano **Hobby** ou **Pro** com pelo menos **2 GB a 4 GB de RAM**.

---

## 5. Configurando as Variáveis de Ambiente e Chaves (Variables)

Na aba **Variables** do seu serviço no Railway, adicione as variáveis abaixo (use **Raw Editor** para colar tudo de uma vez e preencher suas senhas):

```env
# API OFICIAL INLOG
INLOG_BASE_URL=https://locar.inlog.com.br/IntegrationColeta
INLOG_TOKEN_URL=https://locar.inlog.com.br/autenticacao-services/connect/token
INLOG_CLIENT_ID=Locar.Client.Api.Coleta
INLOG_CLIENT_SECRET=COLOQUE_AQUI_SEU_CLIENT_SECRET_OFICIAL_INLOG
INLOG_SERVER_ID=Inlog.Integration.Coleta.Api

# SEGURANÇA DO PAINEL E DOS LINKS ASSINADOS
SECRET_KEY=COLOQUE_UM_SEGREDO_FORTE_ALEATORIO_COM_32_OU_MAIS_CARACTERES
ADMIN_PASSWORD=DEFINA_A_SENHA_DE_ACESSO_DO_SEU_SETOR
REQUIRE_ADMIN_LOGIN=true

# BANCO DE DADOS NO VOLUME PERSISTENTE
DATABASE_PATH=/data/locar.db

# CONFIGURAÇÃO DO SERVIDOR E MALHAS
HOST=0.0.0.0
MALHA_TIPO=TRANSITAVEIS

# REGRAS DE AUDITORIA ESPACIAL
GPS_BUFFER_METROS=20.0
VELOCIDADE_MIN_COLETA=2.0
VELOCIDADE_MAX_COLETA=25.0
DWELL_TIME_MIN_SEGUNDOS=180
```

> ⚠️ **Atenção:**
> - **Não** defina `PORT` manualmente no Railway — o próprio Railway injeta `PORT` automaticamente e o `app.py` lê `os.getenv("PORT", 8080)`.
> - Defina `REQUIRE_ADMIN_LOGIN=true` em produção para garantir que a URL principal (`https://seu-dominio.up.railway.app/`) sempre peça a senha do seu setor (`ADMIN_PASSWORD`).
> - Copie o valor real de `INLOG_CLIENT_SECRET` do seu arquivo local `sitemaLCv1/.env` (não incluído no Git).

---

## 6. Gerando Domínio Público e Testando

1. Vá em **Settings** -> **Networking** -> **Public Networking** -> clique em **Generate Domain** (ou conecte um domínio próprio da Locar, ex: `monitoramento.locar.com.br`).
2. Acesse `https://seu-dominio.up.railway.app/api/status`:
   - Deve retornar JSON com `"inlog_configurado": true` e `"status_inlog": "ONLINE"`.
3. Acesse `https://seu-dominio.up.railway.app/`:
   - Com `REQUIRE_ADMIN_LOGIN=true`, aparecerá a tela **Painel Central Operacional Locar** pedindo sua `ADMIN_PASSWORD`.
   - Ao entrar, você terá acesso completo a todas as cidades, Edição GIS, Central Inlog e geração de links.

---

## 7. Como Funciona a Segurança dos Links Compartilhados (`/v/<token>`)

Quando você clica em **🔗 Link** no painel:

1. O servidor gera uma URL assinada criptograficamente com HMAC-SHA256 no formato:
   ```
   https://seu-dominio.up.railway.app/v/eyJjaWRhZGUiOi...
   ```
2. **O que o destinatário vê ao abrir o link:**
   - Apenas a **Logo LOCAR**, o nome do projeto/cidade/data no topo e o botão **⬇ Baixar CSV**.
   - **Sem** barra superior, **sem** Edição GIS, **sem** Central Inlog, **sem** troca de cidades e **sem** Camadas Inlog em Tempo Real.
   - Mantém os **4 cards de métricas** (incluindo `% de Coleta Manual`), a legenda com **Coleta Manual** no mapa e os filtros **Por coleta**, **Turno (Diurno / Noturno)**, **Por setor** e **Carros**.
3. **Blindagem contra adulteração de URL:**
   - Se quem recebeu o link tentar mudar o token na URL, o servidor bloqueia com **403 Link Inválido**.
   - Se tentar apagar o `/v/...` da URL para acessar a raiz `/` e ver o Painel Central, o navegador possui trava `HttpOnly` (`lc_viewer_lock`) e o servidor **bloqueia e redireciona imediatamente para `/login`**, exigindo `ADMIN_PASSWORD`.
   - Tentativas diretas às APIs de escrita, logs ou troca de cidade são recusadas no backend com **HTTP 403**.

---

## 8. Verificando Logs e Saúde no Railway

- No painel do serviço -> aba **Deploy Logs** e **HTTP Logs**, você acompanhará:
  - `[DB] Banco inicial copiado para volume persistente: /data/locar.db` (caso exista seed)
  - `[MALHA] <cidade>: projeto construído com X trechos...`
  - `Servidor multi-thread ativo em: http://0.0.0.0:<PORT>`
  - `Banco SQLite ativo: /data/locar.db`
- **Backup do Banco:** Você pode usar o botão **📥 Exportar GeoJSON** dentro do modo **Edição GIS** para exportar suas edições de malha sempre que desejar, ou realizar snapshot direto do Volume no menu **Volumes -> Backups** do Railway.
