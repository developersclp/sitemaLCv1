"""
Conector Oficial Inlog API (Inlog.Integration.Coleta.Api)
Desenvolvido para sitemaLCv1 - Locar Saneamento Ambiental.

Implementação completa dos 10 endpoints documentados no manual oficial Inlog:
  POST /connect/token                -> OAuth2 client_credentials
  1. GET /api/Veiculos/TempoReal     -> Sinótico de veículos em tempo real
  2. GET /api/Setores/TempoReal      -> Sinótico de setores em tempo real
  3. GET /api/RelatorioDiario        -> Apontamentos de macros, tickets e pesagens
  4. GET /api/Veiculos               -> Cadastro completo da frota
  5. GET /api/Setores                -> Cadastro completo de setores
  6. GET /api/Motoristas             -> Cadastro de motoristas
  7. GET /api/Alarmes                -> Alarmes por período (24h)
  8. GET /api/Historico              -> Histórico GPS, hodômetro, horímetro e consumo (24h/placa)
  9. GET /api/Falha                  -> Falhas de módulo por período
 10. GET /api/Telemetria             -> Telemetria (faixa verde, RPM, velocidade, pedais)
"""

import os
import re
import json
import time
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import database as db


def carregar_env():
    """Lê variáveis de ambiente do arquivo .env local."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")


carregar_env()

MAPA_CONTRATO_PREFIXOS: Dict[str, List[str]] = {
    "jaboatao": ["JABOATAO"],
    "cabo": ["CABO"],
    "olinda": ["OLINDA"],
    "paulista": ["PAULISTA"],
    "caruaru": ["CARUARU"],
    "cuiaba": ["CUIABA"],
    "garanhuns": ["GARANHUNS"],
    "pesqueira": ["PESQUEIRA"],
    "vitoria": ["VITORIA"],
    "canela": ["CANELA"],
    "jequie": ["JEQUIE"],
    "novo hamburgo": ["NOVO HAMBURGO"],
    "varzea": ["VARZEA"],
    "seropedica": ["SEROPEDICA"],
}


def normalizar_ascii(texto: Optional[str]) -> str:
    """Remove acentos e normaliza texto para maiúsculas ASCII."""
    if not texto:
        return ""
    norm = unicodedata.normalize("NFKD", str(texto))
    sem_acentos = "".join(ch for ch in norm if not unicodedata.combining(ch))
    return sem_acentos.upper().strip()


def identificar_contrato(texto_grupo_ou_setor: Optional[str]) -> Optional[str]:
    """Identifica o contrato_id Locar a partir de gruposControle, macroSetor ou descrição."""
    norm = normalizar_ascii(texto_grupo_ou_setor)
    if not norm:
        return None

    # Analisa primeiro o prefixo antes do separador ' - ' (ex: 'CABO DE SANTO AGOSTINHO - CAIXA BROOKS')
    parte_cidade = norm.split(" - ")[0].strip()
    for contrato_id, prefixos in MAPA_CONTRATO_PREFIXOS.items():
        for pref in prefixos:
            if parte_cidade == pref or parte_cidade.startswith(pref + " "):
                return contrato_id

    # Fallback: verifica palavra inteira no texto completo
    for contrato_id, prefixos in MAPA_CONTRATO_PREFIXOS.items():
        for pref in prefixos:
            if re.search(rf"\b{re.escape(pref)}\b", norm):
                return contrato_id

    return None


def formatar_placa_hifen(placa: Optional[str]) -> str:
    """Garante o formato AAA-XXXX exigido por /api/Historico e demais endpoints Inlog."""
    if not placa:
        return ""
    limpa = re.sub(r"[^A-Z0-9]", "", str(placa).upper().strip())
    if len(limpa) == 7:
        return f"{limpa[:3]}-{limpa[3:]}"
    return str(placa).upper().strip()


def _parse_float(val: Any, default: float = 0.0) -> float:
    if val is None or val == "":
        return default
    try:
        return float(str(val).replace(",", "."))
    except (ValueError, TypeError):
        return default


class InlogService:
    def __init__(self):
        self.base_url = os.getenv("INLOG_BASE_URL", "https://locar.inlog.com.br/IntegrationColeta").rstrip("/")
        self.token_url = os.getenv(
            "INLOG_TOKEN_URL",
            "https://locar.inlog.com.br/autenticacao-services/connect/token",
        ).strip()
        self.client_id = os.getenv("INLOG_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("INLOG_CLIENT_SECRET", "").strip()
        self.server_id = os.getenv("INLOG_SERVER_ID", "Inlog.Integration.Coleta.Api").strip()

        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._token_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._mem_cache: Dict[str, Tuple[float, Any]] = {}
        self._recent_fetches: Dict[str, float] = {}
        self._setores_rt_refreshing = False

        self.session = requests.Session()
        retry_policy = Retry(
            total=2,
            backoff_factor=0.4,
            status_forcelist=[429, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=retry_policy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def is_configured(self) -> bool:
        """Verifica se credenciais reais foram informadas no .env."""
        return bool(
            self.client_id
            and self.client_secret
            and self.token_url
            and "seu_" not in self.client_id
        )

    def get_token(self, force: bool = False) -> Optional[str]:
        """Obtém ou renova token Bearer via OAuth2 client_credentials (thread-safe)."""
        if not self.is_configured():
            return "TOKEN_SIMULADO_LOCAR"

        with self._token_lock:
            if not force and self._token and time.time() < (self._token_expiry - 120):
                return self._token

            payload = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
                "scope": self.server_id,
            }
            headers = {"Content-Type": "application/x-www-form-urlencoded"}

            t0 = time.time()
            try:
                resp = self.session.post(self.token_url, data=payload, headers=headers, timeout=15)
                dur_ms = int((time.time() - t0) * 1000)
                if resp.status_code == 200:
                    data = resp.json()
                    self._token = data.get("access_token")
                    expires_in = int(data.get("expires_in", 3600))
                    self._token_expiry = time.time() + expires_in
                    db.registrar_sync_log("/connect/token", "client_credentials", 200, True, 1, dur_ms, f"TTL {expires_in}s")
                    return self._token
                else:
                    db.registrar_sync_log("/connect/token", "client_credentials", resp.status_code, False, 0, dur_ms, resp.text[:250])
                    return None
            except Exception as e:
                dur_ms = int((time.time() - t0) * 1000)
                db.registrar_sync_log("/connect/token", "client_credentials", 0, False, 0, dur_ms, str(e))
                return None

    def _headers(self, force_token: bool = False) -> Dict[str, str]:
        token = self.get_token(force=force_token)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    def _request_get(
        self, endpoint_path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 20
    ) -> Tuple[bool, Any, int, str]:
        """Executa GET autenticado na API Inlog com auto-renovação de token em HTTP 401 e log de auditoria."""
        if not self.is_configured():
            return False, None, 0, "Credenciais Inlog não configuradas"

        url = f"{self.base_url}{endpoint_path}"
        params_str = json.dumps(params or {}, ensure_ascii=False)
        t0 = time.time()

        try:
            resp = self.session.get(url, params=params, headers=self._headers(), timeout=timeout)
            if resp.status_code == 401:
                resp = self.session.get(url, params=params, headers=self._headers(force_token=True), timeout=timeout)

            dur_ms = int((time.time() - t0) * 1000)
            status_code = resp.status_code

            try:
                payload = resp.json()
            except ValueError:
                db.registrar_sync_log(endpoint_path, params_str, status_code, False, 0, dur_ms, f"Resposta não-JSON: {resp.text[:150]}")
                return False, None, status_code, "Resposta inválida (não-JSON)"

            if status_code == 200:
                if isinstance(payload, dict):
                    sucesso = bool(payload.get("success", True))
                    msg = str(payload.get("message") or "")
                    data = payload.get("data")
                else:
                    sucesso = True
                    msg = ""
                    data = payload

                qtd = len(data) if isinstance(data, list) else (1 if data is not None else 0)
                db.registrar_sync_log(endpoint_path, params_str, status_code, sucesso, qtd, dur_ms, msg)
                return sucesso, data, status_code, msg
            else:
                msg = payload.get("message", resp.text[:250]) if isinstance(payload, dict) else resp.text[:250]
                db.registrar_sync_log(endpoint_path, params_str, status_code, False, 0, dur_ms, str(msg))
                return False, None, status_code, str(msg)

        except Exception as e:
            dur_ms = int((time.time() - t0) * 1000)
            db.registrar_sync_log(endpoint_path, params_str, 0, False, 0, dur_ms, str(e))
            return False, None, 0, str(e)

    def _janela_24h(self, data_iso: str, respeitar_5min: bool = True) -> Optional[Tuple[str, str]]:
        """Calcula janela ISO8601 válida de 24h para data_iso, respeitando o limite de 5 min da Inlog."""
        hoje_str = datetime.now().strftime("%Y-%m-%d")
        if data_iso > hoje_str:
            return None

        data_inicio = f"{data_iso}T00:00:00"
        if data_iso == hoje_str and respeitar_5min:
            limite = datetime.now() - timedelta(minutes=6)
            if limite.strftime("%Y-%m-%d") != hoje_str:
                return None
            data_fim = limite.strftime("%Y-%m-%dT%H:%M:%S")
        else:
            data_fim = f"{data_iso}T23:59:59"

        return data_inicio, data_fim

    def _recent_fetch_valid(self, key: str, ttl: float = 20.0) -> bool:
        with self._cache_lock:
            ts = self._recent_fetches.get(key)
            return bool(ts and (time.time() - ts) < ttl)

    def _mark_recent_fetch(self, key: str):
        with self._cache_lock:
            self._recent_fetches[key] = time.time()

    def _resolver_contratos_veiculo(self, v: Dict[str, Any]) -> Tuple[Optional[str], List[str]]:
        """Extrai contrato_id principal e lista de contratos associados via gruposControle."""
        contratos = []
        for g in v.get("gruposControle") or []:
            cid = identificar_contrato(g.get("descricao"))
            if cid and cid not in contratos:
                contratos.append(cid)
        principal = contratos[0] if contratos else None
        return principal, contratos

    def _filtrar_operacao_veiculo(self, v: Dict[str, Any], contrato_id: Optional[str], operacao: Optional[str]) -> bool:
        """Filtra veículo pelo contrato e operação desejada."""
        grupos = [normalizar_ascii(g.get("descricao")) for g in (v.get("gruposControle") or [])]
        if contrato_id:
            grupos_cid = [g for g in grupos if identificar_contrato(g) == contrato_id]
            if not grupos_cid:
                return False
        else:
            grupos_cid = grupos

        if not operacao or operacao.upper() in ("ALL", "TODAS"):
            return True

        op_norm = normalizar_ascii(operacao)
        if op_norm == "COLETA":
            return any(
                ("DOMICILIAR" in g or "SELETIVA" in g or "30 DIAS" in g)
                for g in grupos_cid
            )

        return any(op_norm in g for g in grupos_cid)

    # --------------------------------------------------------------------------
    # ENDPOINT 1: SINÓTICO DE VEÍCULOS EM TEMPO REAL (GET /api/Veiculos/TempoReal)
    # --------------------------------------------------------------------------
    def get_veiculos_tempo_real(
        self,
        contrato_id: Optional[str] = None,
        operacao: Optional[str] = None,
        force_refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        """Retorna veículos em tempo real da Inlog (persistidos em inlog_veiculos_tempo_real)."""
        cid_norm = identificar_contrato(contrato_id) or (contrato_id.lower().strip() if contrato_id else None)
        if cid_norm in ("todas", "all", ""):
            cid_norm = None

        if not self.is_configured():
            return self._mock_veiculos(cid_norm or "jaboatao")

        cache_key = "veiculos_tempo_real_all"
        agora = time.time()
        todos = None

        if not force_refresh:
            with self._cache_lock:
                cached = self._mem_cache.get(cache_key)
                if cached and (agora - cached[0]) < 15.0:
                    todos = cached[1]

        if todos is None:
            sucesso, dados, _, _ = self._request_get("/api/Veiculos/TempoReal", timeout=20)
            if sucesso and isinstance(dados, list):
                todos = dados
                with self._cache_lock:
                    self._mem_cache[cache_key] = (time.time(), todos)
                self._persistir_veiculos_tempo_real(todos)
            else:
                todos = self._ler_veiculos_tempo_real_db()

        filtrados = []
        for v in todos:
            if self._filtrar_operacao_veiculo(v, cid_norm, operacao):
                filtrados.append(v)

        # Fallback apenas se a cidade não tiver nenhum veículo em DOMICILIAR/SELETIVA/30 DIAS (ex: Várzea)
        if not filtrados and cid_norm and operacao and normalizar_ascii(operacao) == "COLETA":
            for v in todos:
                if self._filtrar_operacao_veiculo(v, cid_norm, "TODAS"):
                    grupos = [normalizar_ascii(g.get("descricao")) for g in (v.get("gruposControle") or []) if identificar_contrato(g.get("descricao")) == cid_norm]
                    if any("ONIBUS" not in g and "OFICINA" not in g for g in grupos):
                        filtrados.append(v)

        return filtrados

    def _persistir_veiculos_tempo_real(self, veiculos: List[Dict[str, Any]]):
        registros = []
        for v in veiculos:
            placa = formatar_placa_hifen(v.get("placaVeiculo") or v.get("identificadorVeiculo"))
            if not placa:
                continue
            cid, _ = self._resolver_contratos_veiculo(v)
            registros.append((
                placa,
                str(v.get("identificadorVeiculo") or ""),
                str(v.get("tipoVeiculo") or ""),
                str(v.get("descricaoVeiculo") or ""),
                v.get("matriculaMotorista"),
                str(v.get("numeroSerieRastreador") or ""),
                str(v.get("statusConexaoAVL") or ""),
                str(v.get("statusIgnicaoVeiculo") or ""),
                str(v.get("dataHoraAtualizacao") or ""),
                _parse_float(v.get("velocidade"), 0.0),
                str(v.get("sensor1") or ""),
                str(v.get("sensor2") or ""),
                str(v.get("sensor3") or ""),
                _parse_float(v.get("latitude"), 0.0),
                _parse_float(v.get("longitude"), 0.0),
                json.dumps(v.get("operacoes") or [], ensure_ascii=False),
                json.dumps(v.get("gruposControle") or [], ensure_ascii=False),
                cid,
            ))

        if not registros:
            return
        with db._write_lock:
            conn = db.get_connection()
            try:
                with conn:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO inlog_veiculos_tempo_real (
                            placa, identificador, tipo_veiculo, descricao_veiculo, matricula_motorista,
                            numero_serie_rastreador, status_conexao_avl, status_ignicao, data_hora_atualizacao,
                            velocidade, sensor1, sensor2, sensor3, latitude, longitude,
                            operacoes_json, grupos_controle_json, contrato_id, atualizado_em
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        registros,
                    )
            finally:
                conn.close()

    def _ler_veiculos_tempo_real_db(self) -> List[Dict[str, Any]]:
        conn = db.get_connection()
        try:
            rows = conn.execute("SELECT * FROM inlog_veiculos_tempo_real ORDER BY placa").fetchall()
            out = []
            for r in rows:
                out.append({
                    "placaVeiculo": r["placa"],
                    "identificadorVeiculo": r["identificador"],
                    "tipoVeiculo": r["tipo_veiculo"],
                    "descricaoVeiculo": r["descricao_veiculo"],
                    "matriculaMotorista": r["matricula_motorista"],
                    "numeroSerieRastreador": r["numero_serie_rastreador"],
                    "statusConexaoAVL": r["status_conexao_avl"],
                    "statusIgnicaoVeiculo": r["status_ignicao"],
                    "dataHoraAtualizacao": r["data_hora_atualizacao"],
                    "velocidade": r["velocidade"],
                    "sensor1": r["sensor1"],
                    "sensor2": r["sensor2"],
                    "sensor3": r["sensor3"],
                    "latitude": r["latitude"],
                    "longitude": r["longitude"],
                    "operacoes": json.loads(r["operacoes_json"] or "[]"),
                    "gruposControle": json.loads(r["grupos_controle_json"] or "[]"),
                    "contrato_id": r["contrato_id"],
                })
            return out
        finally:
            conn.close()

    def get_veiculos_por_cidade(self, cidade_nome: str, operacao: str = "COLETA") -> List[Dict[str, Any]]:
        """Compatibilidade: resolve o contrato da cidade e consulta get_veiculos_tempo_real."""
        cid = identificar_contrato(cidade_nome) or cidade_nome.lower().strip()
        return self.get_veiculos_tempo_real(contrato_id=cid, operacao=operacao)

    def _obter_mapa_placa_contrato(self) -> Dict[str, str]:
        """Retorna mapa {PLACA_COM_HIFEN: contrato_id} usando cache local ou Veiculos/TempoReal."""
        conn = db.get_connection()
        try:
            rows = conn.execute(
                "SELECT placa, contrato_id FROM inlog_veiculos_tempo_real WHERE contrato_id IS NOT NULL"
            ).fetchall()
            if rows:
                return {r["placa"]: r["contrato_id"] for r in rows}
        finally:
            conn.close()

        veiculos = self.get_veiculos_tempo_real()
        mapa = {}
        for v in veiculos:
            placa = formatar_placa_hifen(v.get("placaVeiculo"))
            cid, _ = self._resolver_contratos_veiculo(v)
            if placa and cid:
                mapa[placa] = cid
        return mapa

    # --------------------------------------------------------------------------
    # ENDPOINT 2: SINÓTICO DE SETORES EM TEMPO REAL (GET /api/Setores/TempoReal)
    # --------------------------------------------------------------------------
    def get_setores_tempo_real(
        self, contrato_id: Optional[str] = None, force_refresh: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Consulta GET /api/Setores/TempoReal (timeout estendido de 60s).
        Usa snapshot em SQLite como cache inteligente (TTL 120s) e fallback em caso de timeout.
        """
        cid_norm = identificar_contrato(contrato_id) or (contrato_id.lower().strip() if contrato_id else None)
        if cid_norm in ("todas", "all", ""):
            cid_norm = None

        if not self.is_configured():
            return []

        usar_cache = False
        if not force_refresh:
            conn = db.get_connection()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS qtd, MAX(atualizado_em) AS ult FROM inlog_setores_tempo_real"
                ).fetchone()
                if row and row["qtd"] > 0 and row["ult"]:
                    dt_ult = datetime.strptime(row["ult"], "%Y-%m-%d %H:%M:%S")
                    # Nota: CURRENT_TIMESTAMP no SQLite é UTC
                    idade_seg = (datetime.now(timezone.utc).replace(tzinfo=None) - dt_ult).total_seconds()
                    if idade_seg < 180:
                        usar_cache = True
            except Exception:
                pass
            finally:
                conn.close()

        if not usar_cache:
            sucesso, dados, _, _ = self._request_get("/api/Setores/TempoReal", timeout=60)
            if sucesso and isinstance(dados, list):
                self._persistir_setores_tempo_real(dados)

        return self._ler_setores_tempo_real_db(cid_norm)

    def _persistir_setores_tempo_real(self, setores_rt: List[Dict[str, Any]]):
        registros = []
        for s in setores_rt:
            macro = str(s.get("macroSetor") or "").strip()
            setor = str(s.get("setor") or "").strip()
            turno = str(s.get("turno") or "").strip()
            cid = identificar_contrato(macro) or identificar_contrato(setor)
            placa = formatar_placa_hifen(s.get("placaVeiculo")) if s.get("placaVeiculo") else None
            registros.append((
                macro,
                setor,
                s.get("identificadorVeiculo"),
                placa,
                _parse_float(s.get("percentualConclusao"), 0.0),
                _parse_float(s.get("percentualOcupacao"), 0.0),
                s.get("dataHoraInicioAtividade"),
                s.get("controle"),
                turno,
                str(s.get("statusAtividade") or "").strip(),
                str(s.get("situacao") or "").strip(),
                s.get("logradouro"),
                s.get("motorista"),
                _parse_float(s.get("velocidade"), 0.0),
                s.get("statusIgnicaoVeiculo"),
                cid,
            ))

        if not registros:
            return
        with db._write_lock:
            conn = db.get_connection()
            try:
                with conn:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO inlog_setores_tempo_real (
                            macro_setor, setor, identificador_veiculo, placa_veiculo,
                            percentual_conclusao, percentual_ocupacao, data_hora_inicio,
                            controle, turno, status_atividade, situacao, logradouro,
                            motorista, velocidade, status_ignicao, contrato_id, atualizado_em
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        registros,
                    )
            finally:
                conn.close()

    def _ler_setores_tempo_real_db(self, cid_norm: Optional[str]) -> List[Dict[str, Any]]:
        conn = db.get_connection()
        try:
            if cid_norm:
                rows = conn.execute(
                    "SELECT * FROM inlog_setores_tempo_real WHERE contrato_id = ? ORDER BY macro_setor, setor",
                    (cid_norm,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM inlog_setores_tempo_real ORDER BY macro_setor, setor"
                ).fetchall()

            return [
                {
                    "macroSetor": r["macro_setor"],
                    "setor": r["setor"],
                    "identificadorVeiculo": r["identificador_veiculo"],
                    "placaVeiculo": r["placa_veiculo"],
                    "percentualConclusao": r["percentual_conclusao"],
                    "percentualOcupacao": r["percentual_ocupacao"],
                    "dataHoraInicioAtividade": r["data_hora_inicio"],
                    "controle": r["controle"],
                    "turno": r["turno"],
                    "statusAtividade": r["status_atividade"],
                    "situacao": r["situacao"],
                    "logradouro": r["logradouro"],
                    "motorista": r["motorista"],
                    "velocidade": r["velocidade"],
                    "statusIgnicaoVeiculo": r["status_ignicao"],
                    "contrato_id": r["contrato_id"],
                    "atualizado_em": r["atualizado_em"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # ENDPOINT 3: RELATÓRIOS DIÁRIOS - RD / MACROS (GET /api/RelatorioDiario)
    # --------------------------------------------------------------------------
    def get_relatorio_diario(
        self, data_iso: str, contrato_id: Optional[str] = None, force_refresh: bool = False
    ) -> List[Dict[str, Any]]:
        """Consulta apontamentos de macro, tickets e pesos de GET /api/RelatorioDiario."""
        cid_norm = identificar_contrato(contrato_id) or (contrato_id.lower().strip() if contrato_id else None)
        if cid_norm in ("todas", "all", ""):
            cid_norm = None

        if not self.is_configured():
            return []

        hoje_str = datetime.now().strftime("%Y-%m-%d")
        if data_iso > hoje_str:
            return []

        usar_cache = False
        fetch_key = f"rd:{data_iso}"
        if not force_refresh:
            if data_iso == hoje_str and self._recent_fetch_valid(fetch_key, 20.0):
                usar_cache = True
            elif data_iso < hoje_str:
                conn = db.get_connection()
                try:
                    qtd = conn.execute(
                        "SELECT COUNT(*) FROM inlog_relatorios_diarios WHERE data_iso = ?", (data_iso,)
                    ).fetchone()[0]
                    if qtd > 0:
                        usar_cache = True
                finally:
                    conn.close()

        if not usar_cache:
            params = {
                "dataInicio": f"{data_iso}T00:00:00",
                "dataFim": f"{data_iso}T23:59:59",
            }
            sucesso, dados, _, _ = self._request_get("/api/RelatorioDiario", params=params, timeout=30)
            if sucesso and isinstance(dados, list):
                self._persistir_relatorios_diarios(data_iso, dados)
                self._mark_recent_fetch(fetch_key)

        return self._ler_relatorios_diarios_db(data_iso, cid_norm)

    def _persistir_relatorios_diarios(self, data_iso: str, items: List[Dict[str, Any]]):
        registros = []
        for item in items:
            macro_setor = str(item.get("macroSetor") or item.get("macrosetor") or "").strip()
            grupo = str(item.get("grupoControle") or "").strip()
            setor = str(item.get("setor") or "").strip()
            placa = formatar_placa_hifen(item.get("placa") or item.get("veiculo"))
            controle = str(item.get("controle") or "").strip()
            macros = item.get("macros") or []

            cid = (
                identificar_contrato(grupo)
                or identificar_contrato(macro_setor)
                or identificar_contrato(setor)
            )
            peso_total = 0.0
            tickets = []
            for m in macros:
                campo = str(m.get("campo") or "").upper().strip()
                valor = str(m.get("valor") or "").strip()
                if campo == "PESO":
                    peso_total += _parse_float(valor, 0.0)
                elif campo == "TICKET" and valor:
                    tickets.append(valor)
                elif campo == "CD_REGIONAL" and not cid:
                    cid = identificar_contrato(valor)

            registros.append((
                data_iso,
                str(item.get("dataExecucao") or ""),
                grupo,
                str(item.get("operacao") or ""),
                macro_setor,
                setor,
                str(item.get("veiculo") or ""),
                placa,
                controle,
                json.dumps(macros, ensure_ascii=False),
                round(peso_total, 2),
                json.dumps(tickets, ensure_ascii=False),
                cid,
            ))

        if not registros:
            return
        with db._write_lock:
            conn = db.get_connection()
            try:
                with conn:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO inlog_relatorios_diarios (
                            data_iso, data_execucao, grupo_controle, operacao, macro_setor,
                            setor, veiculo, placa, controle, macros_json, peso_total_kg,
                            tickets_json, contrato_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        registros,
                    )
            finally:
                conn.close()

    def _ler_relatorios_diarios_db(self, data_iso: str, cid_norm: Optional[str]) -> List[Dict[str, Any]]:
        conn = db.get_connection()
        try:
            if cid_norm:
                rows = conn.execute(
                    "SELECT * FROM inlog_relatorios_diarios WHERE data_iso = ? AND contrato_id = ? ORDER BY placa, controle",
                    (data_iso, cid_norm),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM inlog_relatorios_diarios WHERE data_iso = ? ORDER BY grupo_controle, placa",
                    (data_iso,),
                ).fetchall()
            return [
                {
                    "dataExecucao": r["data_execucao"],
                    "grupoControle": r["grupo_controle"],
                    "operacao": r["operacao"],
                    "macroSetor": r["macro_setor"],
                    "setor": r["setor"],
                    "veiculo": r["veiculo"],
                    "placa": r["placa"],
                    "controle": r["controle"],
                    "macros": json.loads(r["macros_json"] or "[]"),
                    "pesoTotalKg": r["peso_total_kg"],
                    "tickets": json.loads(r["tickets_json"] or "[]"),
                    "contrato_id": r["contrato_id"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # ENDPOINT 4: CADASTRO DE VEÍCULOS (GET /api/Veiculos)
    # --------------------------------------------------------------------------
    def get_cadastro_veiculos(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Sincroniza GET /api/Veiculos com cache de 1h no SQLite."""
        if not self.is_configured():
            return []

        usar_cache = False
        if not force_refresh:
            conn = db.get_connection()
            try:
                row = conn.execute("SELECT COUNT(*) AS qtd, MAX(atualizado_em) AS ult FROM inlog_veiculos").fetchone()
                if row and row["qtd"] > 0 and row["ult"]:
                    dt_ult = datetime.strptime(row["ult"], "%Y-%m-%d %H:%M:%S")
                    if (datetime.now(timezone.utc).replace(tzinfo=None) - dt_ult).total_seconds() < 3600:
                        usar_cache = True
            except Exception:
                pass
            finally:
                conn.close()

        if not usar_cache:
            sucesso, dados, _, _ = self._request_get("/api/Veiculos", timeout=20)
            if sucesso and isinstance(dados, list):
                registros = [
                    (
                        formatar_placa_hifen(v.get("placa") or v.get("identificador")),
                        str(v.get("identificador") or ""),
                        str(v.get("tipo") or ""),
                        str(v.get("numeroSerieRastreador") or ""),
                        str(v.get("modelo") or ""),
                        str(v.get("chassi") or ""),
                        str(v.get("ano") or ""),
                        str(v.get("fabricante") or ""),
                    )
                    for v in dados
                    if v.get("placa") or v.get("identificador")
                ]
                with db._write_lock:
                    conn = db.get_connection()
                    try:
                        with conn:
                            conn.executemany(
                                """
                                INSERT OR REPLACE INTO inlog_veiculos (
                                    placa, identificador, tipo, numero_serie_rastreador,
                                    modelo, chassi, ano, fabricante, atualizado_em
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                                """,
                                registros,
                            )
                    finally:
                        conn.close()

        conn = db.get_connection()
        try:
            rows = conn.execute("SELECT * FROM inlog_veiculos ORDER BY placa").fetchall()
            return [
                {
                    "placa": r["placa"],
                    "identificador": r["identificador"],
                    "tipo": r["tipo"],
                    "numeroSerieRastreador": r["numero_serie_rastreador"],
                    "modelo": r["modelo"],
                    "chassi": r["chassi"],
                    "ano": r["ano"],
                    "fabricante": r["fabricante"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # ENDPOINT 5: CADASTRO DE SETORES (GET /api/Setores)
    # --------------------------------------------------------------------------
    def get_cadastro_setores(
        self, contrato_id: Optional[str] = None, force_refresh: bool = False
    ) -> List[Dict[str, Any]]:
        """Sincroniza GET /api/Setores (539 setores) com cache de 1h no SQLite."""
        cid_norm = identificar_contrato(contrato_id) or (contrato_id.lower().strip() if contrato_id else None)
        if cid_norm in ("todas", "all", ""):
            cid_norm = None

        if not self.is_configured():
            return []

        usar_cache = False
        if not force_refresh:
            conn = db.get_connection()
            try:
                row = conn.execute("SELECT COUNT(*) AS qtd, MAX(atualizado_em) AS ult FROM inlog_setores").fetchone()
                if row and row["qtd"] > 0 and row["ult"]:
                    dt_ult = datetime.strptime(row["ult"], "%Y-%m-%d %H:%M:%S")
                    if (datetime.now(timezone.utc).replace(tzinfo=None) - dt_ult).total_seconds() < 3600:
                        usar_cache = True
            except Exception:
                pass
            finally:
                conn.close()

        if not usar_cache:
            sucesso, dados, _, _ = self._request_get("/api/Setores", timeout=20)
            if sucesso and isinstance(dados, list):
                registros = []
                for s in dados:
                    ident = str(s.get("identificador") or "").strip()
                    if not ident:
                        continue
                    macro = str(s.get("macroSetor") or "").strip()
                    grupo = str(s.get("grupoSetor") or "").strip()
                    desc = str(s.get("descricao") or "").strip()
                    cid = identificar_contrato(macro) or identificar_contrato(grupo) or identificar_contrato(desc)
                    registros.append((
                        ident,
                        desc,
                        str(s.get("apelido") or "").strip(),
                        str(s.get("tipo") or "").strip(),
                        grupo,
                        str(s.get("operacao") or "").strip(),
                        macro,
                        cid,
                    ))
                with db._write_lock:
                    conn = db.get_connection()
                    try:
                        with conn:
                            conn.executemany(
                                """
                                INSERT OR REPLACE INTO inlog_setores (
                                    identificador, descricao, apelido, tipo, grupo_setor,
                                    operacao, macro_setor, contrato_id, atualizado_em
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                                """,
                                registros,
                            )
                    finally:
                        conn.close()

        conn = db.get_connection()
        try:
            if cid_norm:
                rows = conn.execute(
                    "SELECT * FROM inlog_setores WHERE contrato_id = ? ORDER BY apelido, descricao",
                    (cid_norm,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM inlog_setores ORDER BY macro_setor, apelido").fetchall()
            return [
                {
                    "identificador": r["identificador"],
                    "descricao": r["descricao"],
                    "apelido": r["apelido"],
                    "tipo": r["tipo"],
                    "grupoSetor": r["grupo_setor"],
                    "operacao": r["operacao"],
                    "macroSetor": r["macro_setor"],
                    "contrato_id": r["contrato_id"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def get_setores(self) -> List[Dict[str, Any]]:
        """Compatibilidade com assinatura anterior."""
        return self.get_cadastro_setores()

    # --------------------------------------------------------------------------
    # ENDPOINT 6: CADASTRO DE MOTORISTAS (GET /api/Motoristas)
    # --------------------------------------------------------------------------
    def get_cadastro_motoristas(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Sincroniza GET /api/Motoristas com cache de 1h no SQLite."""
        if not self.is_configured():
            return []

        usar_cache = False
        if not force_refresh:
            conn = db.get_connection()
            try:
                row = conn.execute("SELECT COUNT(*) AS qtd, MAX(atualizado_em) AS ult FROM inlog_motoristas").fetchone()
                if row and row["qtd"] > 0 and row["ult"]:
                    dt_ult = datetime.strptime(row["ult"], "%Y-%m-%d %H:%M:%S")
                    if (datetime.now(timezone.utc).replace(tzinfo=None) - dt_ult).total_seconds() < 3600:
                        usar_cache = True
            except Exception:
                pass
            finally:
                conn.close()

        if not usar_cache:
            sucesso, dados, _, _ = self._request_get("/api/Motoristas", timeout=20)
            if sucesso and isinstance(dados, list):
                registros = [
                    (str(m.get("matricula") or "").strip(), str(m.get("nome") or "").strip())
                    for m in dados
                    if m.get("matricula")
                ]
                with db._write_lock:
                    conn = db.get_connection()
                    try:
                        with conn:
                            conn.executemany(
                                """
                                INSERT OR REPLACE INTO inlog_motoristas (matricula, nome, atualizado_em)
                                VALUES (?, ?, CURRENT_TIMESTAMP)
                                """,
                                registros,
                            )
                    finally:
                        conn.close()

        conn = db.get_connection()
        try:
            rows = conn.execute("SELECT matricula, nome FROM inlog_motoristas ORDER BY nome").fetchall()
            return [{"matricula": r["matricula"], "nome": r["nome"]} for r in rows]
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # ENDPOINT 7: ALARMES (GET /api/Alarmes)
    # --------------------------------------------------------------------------
    def get_alarmes(
        self, data_iso: str, contrato_id: Optional[str] = None, force_refresh: bool = False
    ) -> List[Dict[str, Any]]:
        """Consulta alarmes de um período (24h, respeitando dataFim < 5 min atrás)."""
        cid_norm = identificar_contrato(contrato_id) or (contrato_id.lower().strip() if contrato_id else None)
        if cid_norm in ("todas", "all", ""):
            cid_norm = None

        if not self.is_configured():
            return []

        janela = self._janela_24h(data_iso, respeitar_5min=True)
        if not janela:
            return []

        hoje_str = datetime.now().strftime("%Y-%m-%d")
        usar_cache = False
        fetch_key = f"alarmes:{data_iso}"
        if not force_refresh:
            if data_iso == hoje_str and self._recent_fetch_valid(fetch_key, 20.0):
                usar_cache = True
            elif data_iso < hoje_str:
                conn = db.get_connection()
                try:
                    qtd = conn.execute("SELECT COUNT(*) FROM inlog_alarmes WHERE data_iso = ?", (data_iso,)).fetchone()[0]
                    if qtd > 0:
                        usar_cache = True
                finally:
                    conn.close()

        if not usar_cache:
            mapa_placas = self._obter_mapa_placa_contrato()
            params = {"dataInicio": janela[0], "dataFim": janela[1]}
            sucesso, dados, _, _ = self._request_get("/api/Alarmes", params=params, timeout=25)
            if sucesso and isinstance(dados, list):
                self._mark_recent_fetch(fetch_key)
                registros = []
                for a in dados:
                    placa = formatar_placa_hifen(a.get("placa") or a.get("identificador"))
                    cid = mapa_placas.get(placa)
                    info_raw = a.get("informacao")
                    info_str = json.dumps(info_raw, ensure_ascii=False) if isinstance(info_raw, (list, dict)) else str(info_raw or "")
                    registros.append((
                        data_iso,
                        placa,
                        str(a.get("identificador") or ""),
                        str(a.get("tipo") or ""),
                        str(a.get("descricao") or ""),
                        str(a.get("codigoAlarme") or ""),
                        str(a.get("motorista") or ""),
                        str(a.get("matriculaMotorista") or ""),
                        str(a.get("unidade") or ""),
                        str(a.get("operacao") or ""),
                        str(a.get("dataHoraInicio") or ""),
                        a.get("dataHoraFim"),
                        str(a.get("duracao") or ""),
                        _parse_float(a.get("latitude"), 0.0),
                        _parse_float(a.get("longitude"), 0.0),
                        str(a.get("status") or ""),
                        info_str,
                        cid,
                    ))
                if registros:
                    with db._write_lock:
                        conn = db.get_connection()
                        try:
                            with conn:
                                conn.executemany(
                                    """
                                    INSERT OR REPLACE INTO inlog_alarmes (
                                        data_iso, placa, identificador, tipo, descricao, codigo_alarme,
                                        motorista, matricula_motorista, unidade, operacao, data_hora_inicio,
                                        data_hora_fim, duracao, latitude, longitude, status, informacao, contrato_id
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    registros,
                                )
                        finally:
                            conn.close()

        conn = db.get_connection()
        try:
            if cid_norm:
                rows = conn.execute(
                    "SELECT * FROM inlog_alarmes WHERE data_iso = ? AND contrato_id = ? ORDER BY data_hora_inicio DESC",
                    (data_iso, cid_norm),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM inlog_alarmes WHERE data_iso = ? ORDER BY data_hora_inicio DESC",
                    (data_iso,),
                ).fetchall()
            out = []
            for r in rows:
                info_val = r["informacao"]
                if info_val and info_val.startswith("["):
                    try:
                        info_val = json.loads(info_val)
                    except Exception:
                        pass
                out.append({
                    "placa": r["placa"],
                    "identificador": r["identificador"],
                    "tipo": r["tipo"],
                    "descricao": r["descricao"],
                    "codigoAlarme": r["codigo_alarme"],
                    "motorista": r["motorista"],
                    "matriculaMotorista": r["matricula_motorista"],
                    "unidade": r["unidade"],
                    "operacao": r["operacao"],
                    "dataHoraInicio": r["data_hora_inicio"],
                    "dataHoraFim": r["data_hora_fim"],
                    "duracao": r["duracao"],
                    "latitude": r["latitude"],
                    "longitude": r["longitude"],
                    "status": r["status"],
                    "informacao": info_val,
                    "contrato_id": r["contrato_id"],
                })
            return out
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # ENDPOINT 8: HISTÓRICO GPS (GET /api/Historico)
    # --------------------------------------------------------------------------
    def get_historico_gps(
        self, placa: str, data_iso: str, force_refresh: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Busca histórico GPS real de 24h da placa na data informada.
        - Sempre envia placa com hífen (AAA-XXXX) como primária.
        - Respeita dataFim < agora - 5 min no dia atual.
        - Faz busca incremental no dia atual e cache persistente para datas passadas.
        - Nunca retorna dados simulados quando credenciais reais estão configuradas.
        """
        placa_hifen = formatar_placa_hifen(placa)
        if not placa_hifen:
            return []

        if not self.is_configured():
            return self._mock_gps(placa_hifen, data_iso)

        janela = self._janela_24h(data_iso, respeitar_5min=True)
        if not janela:
            return []

        hoje_str = datetime.now().strftime("%Y-%m-%d")

        # Verifica cache local
        conn = db.get_connection()
        try:
            row_stats = conn.execute(
                "SELECT COUNT(*) AS qtd, MAX(data_hora) AS max_dt FROM inlog_historico_gps WHERE placa = ? AND data_iso = ?",
                (placa_hifen, data_iso),
            ).fetchone()
            qtd_local = row_stats["qtd"] if row_stats else 0
            max_dt_local = row_stats["max_dt"] if row_stats else None
        finally:
            conn.close()

        # Para datas passadas já cacheadas, retorna direto do SQLite (exceto force_refresh)
        if data_iso < hoje_str and qtd_local > 0 and not force_refresh:
            return self._ler_historico_gps_db(placa_hifen, data_iso)

        # Para hoje, busca apenas incrementalmente a partir do último ponto salvo (se houver)
        data_inicio, data_fim = janela
        if data_iso == hoje_str and max_dt_local and not force_refresh:
            try:
                dt_ult = datetime.strptime(max_dt_local[:19], "%Y-%m-%dT%H:%M:%S")
                dt_prox = dt_ult + timedelta(seconds=1)
                dt_fim = datetime.strptime(data_fim, "%Y-%m-%dT%H:%M:%S")
                if dt_prox >= dt_fim:
                    return self._ler_historico_gps_db(placa_hifen, data_iso)
                data_inicio = dt_prox.strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                pass

        sucesso, dados, _, _ = self._request_get(
            "/api/Historico",
            params={"dataInicio": data_inicio, "dataFim": data_fim, "placa": placa_hifen},
            timeout=25,
        )

        # Se veio vazio com hífen e ainda não temos pontos locais, tenta sem hífen uma única vez
        if (not sucesso or not dados) and qtd_local == 0:
            placa_sem_hifen = placa_hifen.replace("-", "")
            if placa_sem_hifen != placa_hifen:
                sucesso2, dados2, _, _ = self._request_get(
                    "/api/Historico",
                    params={"dataInicio": data_inicio, "dataFim": data_fim, "placa": placa_sem_hifen},
                    timeout=25,
                )
                if sucesso2 and isinstance(dados2, list) and len(dados2) > 0:
                    dados = dados2
                    sucesso = True

        if sucesso and isinstance(dados, list) and len(dados) > 0:
            self._persistir_historico_gps(placa_hifen, data_iso, dados)

        return self._ler_historico_gps_db(placa_hifen, data_iso)

    def _persistir_historico_gps(self, placa_hifen: str, data_iso: str, pontos: List[Dict[str, Any]]):
        registros = []
        for p in pontos:
            lat = _parse_float(p.get("latitude"), 0.0)
            lon = _parse_float(p.get("longitude"), 0.0)
            dt_hr = str(p.get("dataHora") or "").strip()
            if not dt_hr or (lat == 0.0 and lon == 0.0):
                continue
            registros.append((
                placa_hifen,
                str(p.get("identificador") or placa_hifen),
                data_iso,
                dt_hr,
                lat,
                lon,
                _parse_float(p.get("velocidade"), 0.0),
                _parse_float(p.get("hodometro"), 0.0),
                _parse_float(p.get("horimetro"), 0.0),
                _parse_float(p.get("consumo"), 0.0),
                str(p.get("operacao") or ""),
                str(p.get("motorista") or ""),
                str(p.get("matriculaMotorista") or ""),
                str(p.get("unidade") or ""),
            ))

        if not registros:
            return
        with db._write_lock:
            conn = db.get_connection()
            try:
                with conn:
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO inlog_historico_gps (
                            placa, identificador, data_iso, data_hora, latitude, longitude,
                            velocidade, hodometro, horimetro, consumo, operacao,
                            motorista, matricula_motorista, unidade
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        registros,
                    )
            finally:
                conn.close()

    def _ler_historico_gps_db(self, placa_hifen: str, data_iso: str) -> List[Dict[str, Any]]:
        conn = db.get_connection()
        try:
            rows = conn.execute(
                """
                SELECT * FROM inlog_historico_gps
                WHERE placa = ? AND data_iso = ?
                ORDER BY data_hora ASC
                """,
                (placa_hifen, data_iso),
            ).fetchall()
            return [
                {
                    "dataHora": r["data_hora"],
                    "operacao": r["operacao"],
                    "identificador": r["identificador"],
                    "placa": r["placa"],
                    "motorista": r["motorista"],
                    "matriculaMotorista": r["matricula_motorista"],
                    "unidade": r["unidade"],
                    "consumo": r["consumo"],
                    "hodometro": r["hodometro"],
                    "horimetro": r["horimetro"],
                    "latitude": r["latitude"],
                    "longitude": r["longitude"],
                    "velocidade": r["velocidade"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # ENDPOINT 9: FALHAS (GET /api/Falha)
    # --------------------------------------------------------------------------
    def get_falhas(
        self, data_iso: str, contrato_id: Optional[str] = None, force_refresh: bool = False
    ) -> List[Dict[str, Any]]:
        """Consulta falhas por período (respeitando dataFim < 5 min atrás)."""
        cid_norm = identificar_contrato(contrato_id) or (contrato_id.lower().strip() if contrato_id else None)
        if cid_norm in ("todas", "all", ""):
            cid_norm = None

        if not self.is_configured():
            return []

        janela = self._janela_24h(data_iso, respeitar_5min=True)
        if not janela:
            return []

        hoje_str = datetime.now().strftime("%Y-%m-%d")
        usar_cache = False
        fetch_key = f"falhas:{data_iso}"
        if not force_refresh:
            if data_iso == hoje_str and self._recent_fetch_valid(fetch_key, 20.0):
                usar_cache = True
            elif data_iso < hoje_str:
                conn = db.get_connection()
                try:
                    qtd = conn.execute("SELECT COUNT(*) FROM inlog_falhas WHERE data_iso = ?", (data_iso,)).fetchone()[0]
                    if qtd > 0:
                        usar_cache = True
                finally:
                    conn.close()

        if not usar_cache:
            mapa_placas = self._obter_mapa_placa_contrato()
            params = {"dataInicio": janela[0], "dataFim": janela[1]}
            sucesso, dados, _, _ = self._request_get("/api/Falha", params=params, timeout=25)
            if sucesso and isinstance(dados, list):
                self._mark_recent_fetch(fetch_key)
                registros = []
                for f in dados:
                    placa = formatar_placa_hifen(f.get("placa") or f.get("identificador"))
                    cid = mapa_placas.get(placa)
                    registros.append((
                        data_iso,
                        placa,
                        str(f.get("identificador") or ""),
                        str(f.get("tipo") or ""),
                        str(f.get("descricao") or ""),
                        str(f.get("codigoFalha") or ""),
                        str(f.get("motorista") or ""),
                        str(f.get("matriculaMotorista") or ""),
                        str(f.get("unidade") or ""),
                        str(f.get("operacao") or ""),
                        str(f.get("dataHoraInicio") or ""),
                        f.get("dataHoraFim"),
                        str(f.get("duracao") or ""),
                        _parse_float(f.get("latitude"), 0.0),
                        _parse_float(f.get("longitude"), 0.0),
                        str(f.get("codigoFalhaModulo") or ""),
                        str(f.get("descricaoFalhaModulo") or ""),
                        _parse_float(f.get("hodometro"), 0.0),
                        _parse_float(f.get("horimetro"), 0.0),
                        cid,
                    ))
                if registros:
                    with db._write_lock:
                        conn = db.get_connection()
                        try:
                            with conn:
                                conn.executemany(
                                    """
                                    INSERT OR REPLACE INTO inlog_falhas (
                                        data_iso, placa, identificador, tipo, descricao, codigo_falha,
                                        motorista, matricula_motorista, unidade, operacao, data_hora_inicio,
                                        data_hora_fim, duracao, latitude, longitude, codigo_falha_modulo,
                                        descricao_falha_modulo, hodometro, horimetro, contrato_id
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    registros,
                                )
                        finally:
                            conn.close()

        conn = db.get_connection()
        try:
            if cid_norm:
                rows = conn.execute(
                    "SELECT * FROM inlog_falhas WHERE data_iso = ? AND contrato_id = ? ORDER BY data_hora_inicio DESC",
                    (data_iso, cid_norm),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM inlog_falhas WHERE data_iso = ? ORDER BY data_hora_inicio DESC",
                    (data_iso,),
                ).fetchall()
            return [
                {
                    "placa": r["placa"],
                    "identificador": r["identificador"],
                    "tipo": r["tipo"],
                    "descricao": r["descricao"],
                    "codigoFalha": r["codigo_falha"],
                    "motorista": r["motorista"],
                    "matriculaMotorista": r["matricula_motorista"],
                    "unidade": r["unidade"],
                    "operacao": r["operacao"],
                    "dataHoraInicio": r["data_hora_inicio"],
                    "dataHoraFim": r["data_hora_fim"],
                    "duracao": r["duracao"],
                    "latitude": r["latitude"],
                    "longitude": r["longitude"],
                    "codigoFalhaModulo": r["codigo_falha_modulo"],
                    "descricaoFalhaModulo": r["descricao_falha_modulo"],
                    "hodometro": r["hodometro"],
                    "horimetro": r["horimetro"],
                    "contrato_id": r["contrato_id"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # ENDPOINT 10: TELEMETRIA / FAIXA VERDE (GET /api/Telemetria)
    # --------------------------------------------------------------------------
    def get_telemetria(
        self,
        data_iso: str,
        placa: Optional[str] = None,
        contrato_id: Optional[str] = None,
        force_refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        """Consulta telemetria (faixa verde, movimento, RPM, pedais) de GET /api/Telemetria."""
        cid_norm = identificar_contrato(contrato_id) or (contrato_id.lower().strip() if contrato_id else None)
        if cid_norm in ("todas", "all", ""):
            cid_norm = None

        placa_hifen = formatar_placa_hifen(placa) if placa else None

        if not self.is_configured():
            return []

        hoje_str = datetime.now().strftime("%Y-%m-%d")
        if data_iso > hoje_str:
            return []

        usar_cache = False
        fetch_key = f"tel:{data_iso}:{placa_hifen or 'all'}"
        if not force_refresh:
            if data_iso == hoje_str and self._recent_fetch_valid(fetch_key, 20.0):
                usar_cache = True
            elif data_iso < hoje_str:
                conn = db.get_connection()
                try:
                    if placa_hifen:
                        qtd = conn.execute(
                            "SELECT COUNT(*) FROM inlog_telemetria WHERE data_iso = ? AND placa = ?",
                            (data_iso, placa_hifen),
                        ).fetchone()[0]
                    else:
                        qtd = conn.execute(
                            "SELECT COUNT(*) FROM inlog_telemetria WHERE data_iso = ?", (data_iso,)
                        ).fetchone()[0]
                    if qtd > 100 or (placa_hifen and qtd > 0):
                        usar_cache = True
                finally:
                    conn.close()

        if not usar_cache:
            params: Dict[str, Any] = {
                "dataInicio": f"{data_iso}T00:00:00",
                "dataFim": f"{data_iso}T23:59:59",
            }
            if placa_hifen:
                params["placa"] = placa_hifen

            sucesso, dados, _, _ = self._request_get("/api/Telemetria", params=params, timeout=30)
            if sucesso and isinstance(dados, list):
                self._mark_recent_fetch(fetch_key)
                campos_base = {
                    "placa",
                    "identificador",
                    "dataHora",
                    "tempoMovimento",
                    "tempoEmFaixaVerde",
                    "tempoDisponivelFaixaVerde",
                    "tempoTotalAgrupadorVeiculo",
                }
                registros = []
                for t in dados:
                    pl = formatar_placa_hifen(t.get("placa") or t.get("identificador"))
                    dt_hr = str(t.get("dataHora") or "")
                    if not pl or not dt_hr:
                        continue
                    extras = {k: v for k, v in t.items() if k not in campos_base and v is not None}
                    registros.append((
                        dt_hr[:10],
                        pl,
                        str(t.get("identificador") or pl),
                        dt_hr,
                        _parse_float(t.get("tempoMovimento"), 0.0),
                        _parse_float(t.get("tempoEmFaixaVerde"), 0.0),
                        _parse_float(t.get("tempoDisponivelFaixaVerde"), 0.0),
                        _parse_float(t.get("tempoTotalAgrupadorVeiculo"), 0.0),
                        json.dumps(extras, ensure_ascii=False),
                    ))
                if registros:
                    with db._write_lock:
                        conn = db.get_connection()
                        try:
                            with conn:
                                conn.executemany(
                                    """
                                    INSERT OR REPLACE INTO inlog_telemetria (
                                        data_iso, placa, identificador, data_hora, tempo_movimento,
                                        tempo_em_faixa_verde, tempo_disponivel_faixa_verde,
                                        tempo_total_agrupador, extras_json
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    registros,
                                )
                        finally:
                            conn.close()

        mapa_placas = self._obter_mapa_placa_contrato() if cid_norm else {}
        placas_cid = {pl for pl, c in mapa_placas.items() if c == cid_norm} if cid_norm else None

        conn = db.get_connection()
        try:
            if placa_hifen:
                rows = conn.execute(
                    "SELECT * FROM inlog_telemetria WHERE data_iso = ? AND placa = ? ORDER BY data_hora ASC",
                    (data_iso, placa_hifen),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM inlog_telemetria WHERE data_iso = ? ORDER BY placa, data_hora ASC",
                    (data_iso,),
                ).fetchall()

            out = []
            for r in rows:
                if placas_cid is not None and r["placa"] not in placas_cid:
                    continue
                item = {
                    "placa": r["placa"],
                    "identificador": r["identificador"],
                    "dataHora": r["data_hora"],
                    "tempoMovimento": r["tempo_movimento"],
                    "tempoEmFaixaVerde": r["tempo_em_faixa_verde"],
                    "tempoDisponivelFaixaVerde": r["tempo_disponivel_faixa_verde"],
                    "tempoTotalAgrupadorVeiculo": r["tempo_total_agrupador"],
                    "contrato_id": mapa_placas.get(r["placa"]),
                }
                extras = json.loads(r["extras_json"] or "{}")
                if extras:
                    item.update(extras)
                out.append(item)
            return out
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # STATUS / SAÚDE DA CONEXÃO
    # --------------------------------------------------------------------------
    def status_conexao(self) -> Dict[str, Any]:
        """Verifica saúde da conexão com a Inlog e retorna diagnóstico."""
        if not self.is_configured():
            return {"status": "SIMULAÇÃO", "mensagem": "Credenciais não configuradas (.env)"}

        token = self.get_token()
        if not token:
            return {"status": "ERRO_AUTH", "mensagem": "Falha ao obter token OAuth2"}

        sucesso, dados, code, msg = self._request_get("/api/Veiculos/TempoReal", timeout=15)
        if sucesso and isinstance(dados, list):
            return {"status": "ONLINE", "mensagem": f"Inlog ativa — {len(dados)} veículos em tempo real"}
        return {"status": "ERRO_API", "mensagem": f"HTTP {code}: {msg}"}

    # --------------------------------------------------------------------------
    # SIMULAÇÃO APENAS QUANDO CREDENCIAIS NÃO ESTÃO CONFIGURADAS
    # --------------------------------------------------------------------------
    def _mock_veiculos(self, cidade_norm: str) -> List[Dict[str, Any]]:
        mapa_placas = {
            "jaboatao": ["LVE-5J16", "IZR-9B32", "DGG-8F39"],
            "cabo": ["RET-0366", "RET-4258", "RET-4551"],
            "olinda": ["PFW-4421", "PEH-9901"],
            "caruaru": ["PEJ-8831", "KMT-4510"],
            "cuiaba": ["TTO-6E10", "RBC-4E12"],
        }
        placas = mapa_placas.get(cidade_norm, ["LOC-1010", "LOC-2020"])
        return [
            {
                "placaVeiculo": p,
                "identificadorVeiculo": f"LOC-{p.replace('-', '')}",
                "tipoVeiculo": "COMPACTADOR",
                "descricaoVeiculo": f"Coletor {p}",
                "statusIgnicaoVeiculo": "L",
                "velocidade": 13.8,
                "statusConexaoAVL": "T",
                "gruposControle": [{"descricao": f"{cidade_norm.upper()} - DOMICILIAR"}],
            }
            for p in placas
        ]

    def _mock_gps(self, placa: str, data_iso: str) -> List[Dict[str, Any]]:
        import random

        random.seed(hash(placa + data_iso))
        lat, lon = -8.1930, -34.9364
        pontos = []
        for i in range(40):
            lat += (random.random() - 0.5) * 0.0007
            lon += (random.random() - 0.5) * 0.0007
            pontos.append(
                {
                    "dataHora": f"{data_iso}T{8 + (i // 20):02d}:{(i * 3) % 60:02d}:00",
                    "latitude": round(lat, 6),
                    "longitude": round(lon, 6),
                    "velocidade": round(random.uniform(5.0, 18.0), 1),
                    "hodometro": 100000 + i * 40,
                    "placa": placa,
                    "operacao": "DOMICILIAR",
                }
            )
        return pontos
