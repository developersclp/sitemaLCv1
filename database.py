"""
Módulo de Banco de Dados SQLite Otimizado (database.py)
Desenvolvido para sitemaLCv1 - Locar Saneamento Ambiental.

Gerencia o banco local locar.db com modo WAL para alta performance,
persistência das malhas viárias editáveis (QGIS Web) e cache/histórico completo
dos 10 endpoints da API oficial Inlog (Inlog.Integration.Coleta.Api).
"""

import os
import shutil
import sqlite3
import json
import math
import re
import threading
from datetime import datetime, date
from typing import Dict, List, Optional, Any

try:
    import psycopg
except ImportError:
    psycopg = None

try:
    from psycopg_pool import ConnectionPool
except ImportError:
    ConnectionPool = None

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_SEED_DB_PATH = os.path.join(_BASE_DIR, "locar.db")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
IS_POSTGRES = DATABASE_URL.lower().startswith(("postgres://", "postgresql://"))
_PG_POOL = None
_PG_POOL_LOCK = threading.Lock()


def _resolver_db_path() -> str:
    """Resolve o caminho do banco SQLite (suporta Railway Volume e DATABASE_PATH)."""
    if IS_POSTGRES:
        return "postgresql"
    env_db = os.getenv("DATABASE_PATH", "").strip()
    vol_dir = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if env_db:
        alvo = os.path.abspath(env_db)
    elif vol_dir:
        alvo = os.path.join(os.path.abspath(vol_dir), "locar.db")
    else:
        return _SEED_DB_PATH

    parent = os.path.dirname(alvo)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if not os.path.exists(alvo) and os.path.exists(_SEED_DB_PATH) and os.path.abspath(_SEED_DB_PATH) != alvo:
        try:
            shutil.copy2(_SEED_DB_PATH, alvo)
            print(f"[DB] Banco inicial copiado para volume persistente: {alvo}")
        except Exception as e:
            print(f"[DB] Aviso ao copiar seed para volume ({alvo}): {e}")
    return alvo


DB_PATH = _resolver_db_path()
_write_lock = threading.RLock()


def _normalize_pg_val(val: Any) -> Any:
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(val, date):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, memoryview):
        return bytes(val)
    return val


class _PgRow:
    """Row compatível com sqlite3.Row (suporta row['col'], row[0], dict(row), dict(fetchall()))."""
    __slots__ = ("_cols", "_vals", "_map")

    def __init__(self, cols, vals):
        self._cols = tuple(cols)
        norm_vals = tuple(_normalize_pg_val(v) for v in vals)
        self._vals = norm_vals
        self._map = dict(zip(self._cols, norm_vals))

    def keys(self):
        return list(self._cols)

    def __len__(self):
        return len(self._vals)

    def __iter__(self):
        return iter(self._vals)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._vals[key]
        return self._map[key]

    def get(self, key, default=None):
        return self._map.get(key, default)


def _translate_sql_for_pg(sql: str):
    """Traduz placeholders '?' e pequenos dialetos SQLite para PostgreSQL."""
    out = []
    in_single = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            out.append(ch)
            if in_single and i + 1 < n and sql[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            in_single = not in_single
        elif ch == "?" and not in_single:
            out.append("%s")
        else:
            out.append(ch)
        i += 1

    pg_sql = "".join(out).strip()
    if re.match(r"^INSERT\s+OR\s+IGNORE\s+INTO\b", pg_sql, flags=re.IGNORECASE):
        pg_sql = re.sub(r"^INSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", pg_sql, count=1, flags=re.IGNORECASE)
        if "ON CONFLICT" not in pg_sql.upper():
            pg_sql = pg_sql.rstrip(";") + " ON CONFLICT DO NOTHING"

    has_returning = False
    m_ins = re.match(r"^INSERT\s+INTO\s+([a-zA-Z0-9_]+)\b", pg_sql, flags=re.IGNORECASE)
    if m_ins:
        tbl = m_ins.group(1).lower()
        if tbl in ("segmentos_viarios", "auditorias", "inlog_alarmes", "inlog_falhas", "inlog_sync_logs", "segmentos_viarios_legado"):
            if "RETURNING" not in pg_sql.upper() and "ON CONFLICT DO NOTHING" not in pg_sql.upper():
                pg_sql = pg_sql.rstrip(";") + " RETURNING id"
                has_returning = True
    return pg_sql, has_returning


class _PgCursor:
    def __init__(self, raw_cur):
        self._cur = raw_cur
        self.lastrowid = None
        self.rowcount = 0

    def _wrap_row(self, row):
        if row is None or not self._cur.description:
            return row
        cols = [d.name for d in self._cur.description]
        return _PgRow(cols, row)

    def execute(self, sql: str, params: Any = ()):
        pg_sql, has_returning = _translate_sql_for_pg(sql)
        self._cur.execute(pg_sql, params or ())
        self.rowcount = self._cur.rowcount
        if has_returning and self._cur.description:
            r = self._cur.fetchone()
            self.lastrowid = r[0] if r else None
        return self

    def executemany(self, sql: str, seq_of_params):
        pg_sql, _ = _translate_sql_for_pg(sql)
        # Remove RETURNING em executemany em lote
        pg_sql = re.sub(r"\s+RETURNING\s+id\s*$", "", pg_sql, flags=re.IGNORECASE)
        self._cur.executemany(pg_sql, seq_of_params)
        self.rowcount = self._cur.rowcount
        return self

    def fetchone(self):
        return self._wrap_row(self._cur.fetchone())

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows or not self._cur.description:
            return rows
        cols = [d.name for d in self._cur.description]
        return [_PgRow(cols, r) for r in rows]

    def close(self):
        self._cur.close()


class _PgConnection:
    """Wrapper sobre psycopg.Connection compatível com a API do sqlite3.Connection."""
    def __init__(self, raw_conn, pool=None):
        self._conn = raw_conn
        self._pool = pool
        self._closed = False

    def cursor(self) -> _PgCursor:
        return _PgCursor(self._conn.cursor())

    def execute(self, sql: str, params: Any = ()) -> _PgCursor:
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def executemany(self, sql: str, seq_of_params) -> _PgCursor:
        cur = self.cursor()
        cur.executemany(sql, seq_of_params)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if not self._conn.autocommit:
                self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
        if self._pool is not None:
            self._pool.putconn(self._conn)
        else:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self._conn.rollback()
        else:
            self._conn.commit()
        return False


def _get_pg_pool():
    global _PG_POOL
    if _PG_POOL is not None:
        return _PG_POOL
    if psycopg is None:
        raise RuntimeError("DATABASE_URL aponta para PostgreSQL, mas 'psycopg' não está instalado.")
    with _PG_POOL_LOCK:
        if _PG_POOL is None and ConnectionPool is not None:
            _PG_POOL = ConnectionPool(DATABASE_URL, min_size=1, max_size=24, timeout=15.0, open=True)
    return _PG_POOL


def get_connection():
    """Cria conexão com SQLite (local) ou PostgreSQL (Railway DATABASE_URL)."""
    if IS_POSTGRES:
        pool = _get_pg_pool()
        if pool is not None:
            raw_conn = pool.getconn()
            return _PgConnection(raw_conn, pool=pool)
        raw_conn = psycopg.connect(DATABASE_URL)
        return _PgConnection(raw_conn, pool=None)

    db_file = _resolver_db_path()
    conn = sqlite3.connect(db_file, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 15000;")
    conn.execute("PRAGMA cache_size = -64000;")  # 64MB cache em RAM
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db():
    """Cria as tabelas, colunas de migração e índices se não existirem (SQLite e PostgreSQL)."""
    conn = get_connection()
    with conn:
        ddl_script = """
            CREATE TABLE IF NOT EXISTS contratos (
                id TEXT PRIMARY KEY,
                nome TEXT NOT NULL,
                centro_lat REAL,
                centro_lon REAL,
                zoom_padrao REAL DEFAULT 12
            );

            CREATE TABLE IF NOT EXISTS segmentos_viarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contrato_id TEXT NOT NULL,
                fid INTEGER,
                nome_rua TEXT,
                setor TEXT,
                extensao_m REAL,
                geom_json TEXT NOT NULL,
                tipo TEXT DEFAULT 'COLETA',
                is_manual INTEGER DEFAULT 0,
                is_priority INTEGER DEFAULT 0,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (contrato_id) REFERENCES contratos(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_seg_contrato ON segmentos_viarios(contrato_id);
            CREATE INDEX IF NOT EXISTS idx_seg_setor ON segmentos_viarios(contrato_id, setor);

            CREATE TABLE IF NOT EXISTS auditorias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contrato_id TEXT NOT NULL,
                data_iso TEXT NOT NULL,
                turno TEXT DEFAULT 'DIURNO',
                pct_cobertura REAL,
                km_total REAL,
                km_coletado REAL,
                km_omissao REAL,
                total_vias INTEGER,
                vias_omissao INTEGER,
                placas_ativas_json TEXT,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(contrato_id, data_iso, turno)
            );
            CREATE INDEX IF NOT EXISTS idx_audit_contrato_data ON auditorias(contrato_id, data_iso);

            CREATE TABLE IF NOT EXISTS auditoria_detalhes (
                auditoria_id INTEGER NOT NULL,
                segmento_id INTEGER NOT NULL,
                status TEXT NOT NULL, -- 'collected', 'not_collected', 'possible_gps_gap', 'possible_manual'
                placas_responsaveis TEXT,
                PRIMARY KEY (auditoria_id, segmento_id),
                FOREIGN KEY (auditoria_id) REFERENCES auditorias(id) ON DELETE CASCADE,
                FOREIGN KEY (segmento_id) REFERENCES segmentos_viarios(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_det_status ON auditoria_detalhes(auditoria_id, status);

            -- ================================================================
            -- TABELAS DE INTEGRAÇÃO INLOG (10 ENDPOINTS + LOGS)
            -- ================================================================
            CREATE TABLE IF NOT EXISTS inlog_veiculos (
                placa TEXT PRIMARY KEY,
                identificador TEXT,
                tipo TEXT,
                numero_serie_rastreador TEXT,
                modelo TEXT,
                chassi TEXT,
                ano TEXT,
                fabricante TEXT,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS inlog_veiculos_tempo_real (
                placa TEXT PRIMARY KEY,
                identificador TEXT,
                tipo_veiculo TEXT,
                descricao_veiculo TEXT,
                matricula_motorista TEXT,
                numero_serie_rastreador TEXT,
                status_conexao_avl TEXT,
                status_ignicao TEXT,
                data_hora_atualizacao TEXT,
                velocidade REAL,
                sensor1 TEXT,
                sensor2 TEXT,
                sensor3 TEXT,
                latitude REAL,
                longitude REAL,
                operacoes_json TEXT,
                grupos_controle_json TEXT,
                contrato_id TEXT,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_vtr_contrato ON inlog_veiculos_tempo_real(contrato_id);

            CREATE TABLE IF NOT EXISTS inlog_setores (
                identificador TEXT PRIMARY KEY,
                descricao TEXT,
                apelido TEXT,
                tipo TEXT,
                grupo_setor TEXT,
                operacao TEXT,
                macro_setor TEXT,
                contrato_id TEXT,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_iset_contrato ON inlog_setores(contrato_id);

            CREATE TABLE IF NOT EXISTS inlog_setores_tempo_real (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                macro_setor TEXT,
                setor TEXT,
                identificador_veiculo TEXT,
                placa_veiculo TEXT,
                percentual_conclusao REAL,
                percentual_ocupacao REAL,
                data_hora_inicio TEXT,
                controle TEXT,
                turno TEXT,
                status_atividade TEXT,
                situacao TEXT,
                logradouro TEXT,
                motorista TEXT,
                velocidade REAL,
                status_ignicao TEXT,
                contrato_id TEXT,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(macro_setor, setor, turno)
            );
            CREATE INDEX IF NOT EXISTS idx_istr_contrato ON inlog_setores_tempo_real(contrato_id);

            CREATE TABLE IF NOT EXISTS inlog_motoristas (
                matricula TEXT PRIMARY KEY,
                nome TEXT,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS inlog_historico_gps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                placa TEXT NOT NULL,
                identificador TEXT,
                data_iso TEXT NOT NULL,
                data_hora TEXT NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                velocidade REAL DEFAULT 0,
                hodometro REAL DEFAULT 0,
                horimetro REAL DEFAULT 0,
                consumo REAL DEFAULT 0,
                operacao TEXT,
                motorista TEXT,
                matricula_motorista TEXT,
                unidade TEXT,
                UNIQUE(placa, data_hora)
            );
            CREATE INDEX IF NOT EXISTS idx_gps_placa_data ON inlog_historico_gps(placa, data_iso);
            CREATE INDEX IF NOT EXISTS idx_gps_data ON inlog_historico_gps(data_iso);

            CREATE TABLE IF NOT EXISTS inlog_relatorios_diarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_iso TEXT NOT NULL,
                data_execucao TEXT,
                grupo_controle TEXT,
                operacao TEXT,
                macro_setor TEXT,
                setor TEXT,
                veiculo TEXT,
                placa TEXT,
                controle TEXT,
                macros_json TEXT,
                peso_total_kg REAL DEFAULT 0,
                tickets_json TEXT,
                contrato_id TEXT,
                UNIQUE(data_iso, placa, controle, setor)
            );
            CREATE INDEX IF NOT EXISTS idx_ird_contrato_data ON inlog_relatorios_diarios(contrato_id, data_iso);

            CREATE TABLE IF NOT EXISTS inlog_alarmes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_iso TEXT NOT NULL,
                placa TEXT,
                identificador TEXT,
                tipo TEXT,
                descricao TEXT,
                codigo_alarme TEXT,
                motorista TEXT,
                matricula_motorista TEXT,
                unidade TEXT,
                operacao TEXT,
                data_hora_inicio TEXT,
                data_hora_fim TEXT,
                duracao TEXT,
                latitude REAL,
                longitude REAL,
                status TEXT,
                informacao TEXT,
                contrato_id TEXT,
                UNIQUE(placa, codigo_alarme, data_hora_inicio)
            );
            CREATE INDEX IF NOT EXISTS idx_ialarm_contrato_data ON inlog_alarmes(contrato_id, data_iso);

            CREATE TABLE IF NOT EXISTS inlog_falhas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_iso TEXT NOT NULL,
                placa TEXT,
                identificador TEXT,
                tipo TEXT,
                descricao TEXT,
                codigo_falha TEXT,
                motorista TEXT,
                matricula_motorista TEXT,
                unidade TEXT,
                operacao TEXT,
                data_hora_inicio TEXT,
                data_hora_fim TEXT,
                duracao TEXT,
                latitude REAL,
                longitude REAL,
                codigo_falha_modulo TEXT,
                descricao_falha_modulo TEXT,
                hodometro REAL,
                horimetro REAL,
                contrato_id TEXT,
                UNIQUE(placa, codigo_falha, data_hora_inicio)
            );
            CREATE INDEX IF NOT EXISTS idx_ifalha_contrato_data ON inlog_falhas(contrato_id, data_iso);

            CREATE TABLE IF NOT EXISTS inlog_telemetria (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_iso TEXT NOT NULL,
                placa TEXT NOT NULL,
                identificador TEXT,
                data_hora TEXT NOT NULL,
                tempo_movimento REAL,
                tempo_em_faixa_verde REAL,
                tempo_disponivel_faixa_verde REAL,
                tempo_total_agrupador REAL,
                extras_json TEXT,
                UNIQUE(placa, data_hora)
            );
            CREATE INDEX IF NOT EXISTS idx_itel_placa_data ON inlog_telemetria(placa, data_iso);
            CREATE INDEX IF NOT EXISTS idx_itel_data ON inlog_telemetria(data_iso);

            CREATE TABLE IF NOT EXISTS inlog_sync_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                endpoint TEXT NOT NULL,
                parametros TEXT,
                status_http INTEGER,
                sucesso INTEGER,
                qtd_registros INTEGER,
                duracao_ms INTEGER,
                mensagem TEXT
            );

            CREATE TABLE IF NOT EXISTS projetos_malha (
                contrato_id TEXT PRIMARY KEY,
                malha_arquivo TEXT,
                malha_mtime REAL,
                rotas_dir TEXT,
                rotas_mtime REAL,
                n_rotas INTEGER,
                n_segmentos INTEGER,
                km_total REAL,
                n_prioritarias INTEGER,
                n_preservados INTEGER,
                construido_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS segmentos_viarios_legado (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_original INTEGER,
                contrato_id TEXT,
                fid INTEGER,
                nome_rua TEXT,
                setor TEXT,
                extensao_m REAL,
                geom_json TEXT,
                tipo TEXT,
                is_manual INTEGER,
                is_priority INTEGER,
                atualizado_em TIMESTAMP,
                arquivado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS links_snapshot (
                id TEXT PRIMARY KEY,
                chave_params TEXT NOT NULL,
                contrato_id TEXT NOT NULL,
                data_iso TEXT NOT NULL,
                periodo TEXT NOT NULL DEFAULT 'diario',
                parametros_json TEXT NOT NULL,
                payload_gzip BLOB NOT NULL,
                csv_gzip BLOB,
                csv_filename TEXT,
                sha256 TEXT NOT NULL,
                tamanho_raw INTEGER DEFAULT 0,
                tamanho_gzip INTEGER DEFAULT 0,
                janela_fechada INTEGER DEFAULT 0,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT NOT NULL,
                expira_em TEXT,
                revogado INTEGER DEFAULT 0,
                acessos INTEGER DEFAULT 0,
                ultimo_acesso TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_snap_chave ON links_snapshot(chave_params);
            CREATE INDEX IF NOT EXISTS idx_snap_contrato ON links_snapshot(contrato_id, data_iso);

            CREATE TABLE IF NOT EXISTS engine_meta (
                chave TEXT PRIMARY KEY,
                valor TEXT
            );

            CREATE TABLE IF NOT EXISTS inlog_gps_janelas (
                placa TEXT NOT NULL,
                data_iso TEXT NOT NULL,
                fetch_ate TEXT NOT NULL,
                PRIMARY KEY (placa, data_iso)
            );

            CREATE TABLE IF NOT EXISTS auditorias_periodo_cache (
                chave TEXT PRIMARY KEY,
                criado_em TEXT,
                payload_json TEXT
            );
        """

        cur = conn.cursor()
        if IS_POSTGRES:
            ddl_pg = ddl_script.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            ddl_pg = re.sub(r"\bBLOB\b", "BYTEA", ddl_pg)
            for stmt in ddl_pg.split(";"):
                s = stmt.strip()
                if s:
                    cur.execute(s)
        else:
            conn.executescript(ddl_script)

        # Migração segura de colunas extras em segmentos_viarios (caso banco antigo)
        migracoes = [
            ("tipo", "TEXT DEFAULT 'COLETA'"),
            ("is_manual", "INTEGER DEFAULT 0"),
            ("is_priority", "INTEGER DEFAULT 0"),
            ("atualizado_em", "TIMESTAMP"),
            ("edge_key", "TEXT"),
            ("setores_json", "TEXT"),
            ("origem", "TEXT DEFAULT 'LEGADO'"),
            ("bairro", "TEXT"),
        ]
        if IS_POSTGRES:
            for col_name, col_def in migracoes:
                cur.execute(f"ALTER TABLE segmentos_viarios ADD COLUMN IF NOT EXISTS {col_name} {col_def}")
        else:
            cur.execute("PRAGMA table_info(segmentos_viarios)")
            colunas_existentes = {row["name"] for row in cur.fetchall()}
            for col_name, col_def in migracoes:
                if col_name not in colunas_existentes:
                    cur.execute(f"ALTER TABLE segmentos_viarios ADD COLUMN {col_name} {col_def}")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_seg_edge_key ON segmentos_viarios(contrato_id, edge_key)")

        # Popula contratos padrão com ON CONFLICT DO NOTHING (padrão SQLite + PostgreSQL)
        contratos_iniciais = [
            ("jaboatao", "Jaboatão dos Guararapes", -8.1930, -34.9364, 12),
            ("cabo", "Cabo de Santo Agostinho", -8.2833, -35.0333, 12),
            ("olinda", "Olinda", -7.9986, -34.8459, 13),
            ("paulista", "Paulista", -7.9408, -34.8728, 12.5),
            ("caruaru", "Caruaru", -8.2838, -35.9761, 12),
            ("cuiaba", "Cuiabá", -15.6014, -56.0979, 12),
            ("garanhuns", "Garanhuns", -8.8907, -36.4966, 13),
            ("pesqueira", "Pesqueira", -8.3586, -36.6961, 13),
            ("vitoria", "Vitória de Santo Antão", -8.1189, -35.2928, 13),
            ("canela", "Canela", -29.3644, -50.8119, 13),
            ("jequie", "Jequié", -13.8575, -40.0839, 13),
            ("novo hamburgo", "Novo Hamburgo", -29.6789, -51.1306, 12.5),
        ]
        cur.executemany(
            "INSERT INTO contratos (id, nome, centro_lat, centro_lon, zoom_padrao) VALUES (?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
            contratos_iniciais,
        )
        cur.execute("DELETE FROM contratos WHERE id = 'varzea'")
    conn.close()


def calcular_extensao_coords(coordinates: List[List[float]]) -> float:
    """Calcula a distância total Haversine em metros ao longo de uma lista de [lon, lat]."""
    if not coordinates or len(coordinates) < 2:
        return 0.0

    raio_terra = 6371000.0
    total_m = 0.0
    for i in range(len(coordinates) - 1):
        lon1, lat1 = float(coordinates[i][0]), float(coordinates[i][1])
        lon2, lat2 = float(coordinates[i + 1][0]), float(coordinates[i + 1][1])

        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)

        a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2
        c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
        total_m += raio_terra * c

    return round(total_m, 2)


def salvar_ou_atualizar_segmento(dados: Dict[str, Any]) -> Dict[str, Any]:
    """Cria ou atualiza um segmento viário no SQLite (Modo Edição GIS)."""
    seg_id = dados.get("id")
    contrato_id = str(dados.get("contrato_id", "")).lower().strip()
    nome_rua = str(dados.get("nome_rua") or "Via Sem Nome").strip()
    setor = str(dados.get("setor") or "S/S").strip()
    tipo = str(dados.get("tipo") or "COLETA").strip().upper()
    is_manual = 1 if (dados.get("is_manual") or tipo == "MANUAL") else 0
    is_priority = 1 if dados.get("is_priority") else 0
    coordinates = dados.get("coordinates")

    with _write_lock:
        conn = get_connection()
        try:
            with conn:
                cur = conn.cursor()
                if seg_id:
                    cur.execute("SELECT * FROM segmentos_viarios WHERE id = ?", (int(seg_id),))
                    existente = cur.fetchone()
                    if not existente:
                        raise ValueError(f"Segmento ID {seg_id} não encontrado.")

                    if coordinates and isinstance(coordinates, list) and len(coordinates) >= 2:
                        extensao_m = calcular_extensao_coords(coordinates)
                        geom_json = json.dumps(coordinates)
                    else:
                        extensao_m = float(existente["extensao_m"] or 0.0)
                        geom_json = existente["geom_json"]

                    cur.execute(
                        """
                        UPDATE segmentos_viarios
                        SET nome_rua = ?, setor = ?, tipo = ?, is_manual = ?, is_priority = ?,
                            extensao_m = ?, geom_json = ?, origem = 'EDICAO_USUARIO',
                            atualizado_em = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (nome_rua, setor, tipo, is_manual, is_priority, extensao_m, geom_json, int(seg_id)),
                    )
                    final_id = int(seg_id)
                else:
                    if not contrato_id:
                        raise ValueError("contrato_id é obrigatório para criar um novo segmento.")
                    if not coordinates or not isinstance(coordinates, list) or len(coordinates) < 2:
                        raise ValueError("Coordenadas inválidas: informe ao menos 2 pontos [lon, lat].")

                    extensao_m = calcular_extensao_coords(coordinates)
                    geom_json = json.dumps(coordinates)
                    cur.execute(
                        "SELECT COALESCE(MAX(fid), 0) + 1 AS prox_fid FROM segmentos_viarios WHERE contrato_id = ?",
                        (contrato_id,),
                    )
                    prox_fid = cur.fetchone()["prox_fid"]

                    cur.execute(
                        """
                        INSERT INTO segmentos_viarios
                            (contrato_id, fid, nome_rua, setor, extensao_m, geom_json, tipo, is_manual, is_priority, origem, atualizado_em)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'EDICAO_USUARIO', CURRENT_TIMESTAMP)
                        """,
                        (contrato_id, prox_fid, nome_rua, setor, extensao_m, geom_json, tipo, is_manual, is_priority),
                    )
                    final_id = cur.lastrowid

                cur.execute("SELECT * FROM segmentos_viarios WHERE id = ?", (final_id,))
                row = dict(cur.fetchone())
                return row
        finally:
            conn.close()


def excluir_segmento(seg_id: int) -> bool:
    """Remove um segmento viário e seus detalhes de auditoria associados."""
    if not seg_id:
        return False
    with _write_lock:
        conn = get_connection()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM auditoria_detalhes WHERE segmento_id = ?", (int(seg_id),))
                cur.execute("DELETE FROM segmentos_viarios WHERE id = ?", (int(seg_id),))
                return cur.rowcount > 0
        finally:
            conn.close()


def importar_malha_geojson(contrato_id: str, geojson_obj: Dict[str, Any], substituir: bool = False) -> int:
    """Importa um FeatureCollection GeoJSON (LineString / MultiLineString) para segmentos_viarios."""
    contrato_id = str(contrato_id or "").lower().strip()
    if not contrato_id:
        raise ValueError("contrato_id é obrigatório.")

    features = geojson_obj.get("features") if isinstance(geojson_obj, dict) else None
    if not isinstance(features, list) or len(features) == 0:
        raise ValueError("GeoJSON inválido ou sem features.")

    registros = []
    with _write_lock:
        conn = get_connection()
        try:
            with conn:
                cur = conn.cursor()
                if substituir:
                    cur.execute("DELETE FROM auditorias WHERE contrato_id = ?", (contrato_id,))
                    cur.execute("DELETE FROM segmentos_viarios WHERE contrato_id = ?", (contrato_id,))
                    prox_fid = 1
                else:
                    cur.execute(
                        "SELECT COALESCE(MAX(fid), 0) + 1 AS prox_fid FROM segmentos_viarios WHERE contrato_id = ?",
                        (contrato_id,),
                    )
                    prox_fid = cur.fetchone()["prox_fid"]

                for feat in features:
                    if not isinstance(feat, dict):
                        continue
                    props = feat.get("properties") or {}
                    geom = feat.get("geometry") or {}
                    gtype = geom.get("type", "")
                    coords = geom.get("coordinates") or []

                    linhas = []
                    if gtype == "LineString" and len(coords) >= 2:
                        linhas.append(coords)
                    elif gtype == "MultiLineString":
                        for sub in coords:
                            if isinstance(sub, list) and len(sub) >= 2:
                                linhas.append(sub)

                    if not linhas:
                        continue

                    rua = str(props.get("street_name") or props.get("nome_rua") or props.get("name") or "Via Sem Nome").strip()
                    setor = str(props.get("sector") or props.get("setor") or props.get("sector_key") or "S/S").strip()
                    tipo = str(props.get("tipo") or "COLETA").strip().upper()
                    is_manual = 1 if (props.get("is_manual") or tipo == "MANUAL") else 0
                    is_priority = 1 if props.get("is_priority") else 0

                    for linha in linhas:
                        fid = props.get("fid")
                        if fid is None:
                            fid = prox_fid
                            prox_fid += 1
                        ext = float(props.get("len_m") or props.get("extensao_m") or calcular_extensao_coords(linha))
                        registros.append(
                            (contrato_id, int(fid), rua, setor, round(ext, 2), json.dumps(linha), tipo, is_manual, is_priority)
                        )

                if registros:
                    cur.executemany(
                        """
                        INSERT INTO segmentos_viarios
                            (contrato_id, fid, nome_rua, setor, extensao_m, geom_json, tipo, is_manual, is_priority, atualizado_em)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        registros,
                    )
                    cur.execute("DELETE FROM auditorias WHERE contrato_id = ?", (contrato_id,))
            return len(registros)
        finally:
            conn.close()


def registrar_sync_log(
    endpoint: str,
    parametros: str,
    status_http: int,
    sucesso: bool,
    qtd_registros: int,
    duracao_ms: int,
    mensagem: str = "",
):
    """Registra log de chamada HTTP à API Inlog mantendo os últimos 500 registros."""
    with _write_lock:
        conn = get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO inlog_sync_logs
                        (endpoint, parametros, status_http, sucesso, qtd_registros, duracao_ms, mensagem)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        endpoint,
                        parametros[:500],
                        int(status_http),
                        1 if sucesso else 0,
                        int(qtd_registros),
                        int(duracao_ms),
                        (mensagem or "")[:500],
                    ),
                )
                conn.execute(
                    """
                    DELETE FROM inlog_sync_logs
                    WHERE id NOT IN (
                        SELECT id FROM inlog_sync_logs ORDER BY id DESC LIMIT 500
                    )
                    """
                )
        except Exception as e:
            print(f"[DB LOG] Erro ao gravar sync log: {e}")
        finally:
            conn.close()


def importar_malha_cidade_se_necessario(contrato_id: str) -> int:
    """
    Garante que o projeto viário da cidade esteja construído a partir das Malhas Viárias (.gpkg)
    e Rotas Planejadas (.gpkg). Se já houver malha oficial atualizada, retorna o total de trechos.
    Caso não exista .gpkg para a cidade, mantém fallback para arquivos legados se necessário.
    """
    contrato_id = (contrato_id or "").lower().strip()
    if not contrato_id:
        return 0

    try:
        import malha_rotas
        if malha_rotas.arquivo_malha(contrato_id):
            if malha_rotas.projeto_precisa_construir(contrato_id):
                res = malha_rotas.construir_projeto_cidade(contrato_id)
                if res.get("ok"):
                    return int(res.get("n_segmentos") or 0)
            conn = get_connection()
            try:
                total = conn.execute(
                    "SELECT COUNT(*) FROM segmentos_viarios WHERE contrato_id = ?", (contrato_id,)
                ).fetchone()[0]
                if total > 0:
                    return total
            finally:
                conn.close()
    except Exception as e:
        print(f"[DB] Aviso ao construir projeto via malha_rotas para {contrato_id}: {e}")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM segmentos_viarios WHERE contrato_id = ?", (contrato_id,))
    total = cur.fetchone()[0]
    if total > 0:
        conn.close()
        return total

    pasta_5 = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "5"))
    mapa_arquivos = {
        "jaboatao": "Jaboatao_dia-05.11.2026.html",
        "cabo": "Cabo_dia-05.11.2026.html",
        "olinda": "Olinda_dia-05.11.2026.html",
        "paulista": "Paulista_dia-05.11.2026.html",
        "caruaru": "Caruaru_dia-05.11.2026.html",
        "cuiaba": "Cuiaba_dia-05.11.2026.html",
        "garanhuns": "Garanhuns_dia-05.11.2026.html",
        "pesqueira": "Pesqueira_dia-05.11.2026.html",
        "vitoria": "Vitoria_dia-05.11.2026.html",
        "canela": "Canela_dia-05.11.2026.html",
        "jequie": "Jequie_dia-05.11.2026.html",
        "novo hamburgo": "Novo hamburgo_dia-05.11.2026.html",
    }

    arquivo_nome = mapa_arquivos.get(contrato_id.lower())
    if not arquivo_nome:
        conn.close()
        return 0

    caminho = os.path.join(pasta_5, arquivo_nome)
    if not os.path.exists(caminho):
        conn.close()
        return 0

    with open(caminho, "r", encoding="utf-8", errors="ignore") as f:
        texto = f.read()

    m = re.search(r"window\.SEGMENTS\s*=\s*(\{.*?\});", texto)
    if not m:
        conn.close()
        return 0

    data = json.loads(m.group(1))
    features = data.get("features", [])

    registros = []
    for f in features:
        props = f.get("properties", {})
        geom = f.get("geometry", {})
        fid = props.get("fid", 0)
        rua = props.get("street_name") or "Via Sem Nome"
        setor = props.get("sector") or props.get("sector_key") or "S/S"
        extensao = float(props.get("len_m", 50.0))
        geom_str = json.dumps(geom.get("coordinates", []))
        registros.append((contrato_id, fid, rua, setor, extensao, geom_str))

    with conn:
        conn.executemany(
            """
            INSERT INTO segmentos_viarios (contrato_id, fid, nome_rua, setor, extensao_m, geom_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            registros,
        )

    conn.close()
    return len(registros)


# Inicializa banco na importação do módulo
init_db()
