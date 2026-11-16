"""
Malha Viária (GeoPackage) + Rotas Planejadas por Setor + Vínculo com Setores Inlog (malha_rotas.py)
Desenvolvido para sitemaLCv1 - Locar Saneamento Ambiental.

Fluxo de dados:
  1. MALHA VIÁRIA  -> `Malhas Viárias - Somente Transitáveis/Malha <CIDADE>.gpkg` (base geométrica oficial
                      das ruas; arestas OSM identificadas por u, v, key).
  2. ROTA PLANEJADA -> `Rotas Planejadas/<CIDADE> - <TURNO>/rotas/<SETOR>.gpkg` (sequência ordenada de
                      arestas da MESMA malha, com flag is_collection = trecho de coleta x deslocamento).
  3. INLOG          -> GET /api/Setores/TempoReal e GET /api/RelatorioDiario dizem QUAL VEÍCULO executa
                      QUAL SETOR no dia. O nome do setor Inlog é resolvido para o arquivo de rota.

O "projeto" auditado de cada cidade (tabela segmentos_viarios) passa a ser formado pelas arestas
da malha viária que pertencem a algum trecho de coleta planejado, cada uma marcada com os setores
a que pertence (setores_json). Nada é inventado: toda geometria vem da malha oficial.
"""

import ast
import glob
import json
import math
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import database as db
from inlog_service import normalizar_ascii

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DIR_MALHA_TRANSITAVEIS = "Malhas Viárias - Somente Transitáveis"
DIR_MALHA_COMPLETA = "Malhas Viárias - Completas"

DIAS_SEMANA = {"SEG": 0, "TER": 1, "QUA": 2, "QUI": 3, "SEX": 4, "SAB": 5, "DOM": 6}
DIAS_ALIASES = {
    "SEGUNDA": "SEG", "TERCA": "TER", "QUARTA": "QUA", "QUINTA": "QUI",
    "SEXTA": "SEX", "SABADO": "SAB", "DOMINGO": "DOM",
}

_RE_CODIGO = re.compile(r"\b(SC[A-Z]{1,6}|FEIRA)\s*0*(\d{1,4})\b")


# ==============================================================================
# CLASSIFICAÇÃO PADRÃO DE SIGLAS (PAR / ÍMPAR / DIURNO / NOTURNO)
# ==============================================================================
def classificar_setor_sigla(setor_nome: Any) -> Dict[str, Any]:
    """
    Analisa a sigla padrão do setor conforme regra das 4 letras:
      - 1ª e 2ª letras: 'SC' (Setor de Coleta - Fixo)
      - 3ª letra: 'P' (Par) ou 'I' (Ímpar) (ou 'D' Diário)
      - 4ª letra: 'D' (Diurno) ou 'N' (Noturno)

    Regra de frequência:
      - PAR (3ª letra 'P', ex: SCPD, SCPN): Segunda, Quarta e Sexta (wd in 0, 2, 4)
      - ÍMPAR (3ª letra 'I', ex: SCID, SCIN): Terça, Quinta e Sábado (wd in 1, 3, 5)
      - DIÁRIO (3ª letra 'D', ex: SCDD, SCDN): Segunda a Sábado
      - DOMINGO (ex: FEIRA, DOM): Domingo
    """
    s = str(setor_nome or "").upper().strip()
    if "DOM" in s or "FEIRA" in s:
        return {
            "sigla": "DOM",
            "l3": "DOM",
            "l4": "",
            "freq": "DOMINGO",
            "turno": "DIURNO",
            "descricao": "Coletas de Domingo / Feiras",
        }
    m = re.search(r"\bSC([A-Z])([A-Z])(?:\s*|\b|\d)", s)
    if m:
        l3 = m.group(1)
        l4 = m.group(2)
        freq = "PAR" if l3 == "P" else ("IMPAR" if l3 == "I" else ("DIARIO" if l3 == "D" else "OUTRO"))
        turno = "DIURNO" if l4 in ("D", "M") else ("NOTURNO" if l4 in ("N", "V") else "OUTRO")
        desc = f"Setor Coleta {'Par' if freq == 'PAR' else ('Ímpar' if freq == 'IMPAR' else 'Diário')} {'Diurno' if turno == 'DIURNO' else 'Noturno'}"
        return {
            "sigla": f"SC{l3}{l4}",
            "l3": l3,
            "l4": l4,
            "freq": freq,
            "turno": turno,
            "descricao": desc,
        }
    return {
        "sigla": "OUTRO",
        "l3": "",
        "l4": "",
        "freq": "OUTRO",
        "turno": "OUTRO",
        "descricao": "Geral",
    }


def setor_visivel_na_data(setor_nome: Any, data_iso: str) -> bool:
    """
    Regra padrão de exibição para todas as cidades:
    - Segunda, Quarta e Sexta (wd in 0, 2, 4): só exibe os setores PAR (3ª letra 'P').
    - Terça, Quinta e Sábado (wd in 1, 3, 5): só exibe os setores ÍMPAR (3ª letra 'I').
    - Domingo (wd == 6): só exibe setores de domingo.
    - Setores diários (ex: SCDD, SCDN) são exibidos de segunda a sábado.
    """
    try:
        dt = datetime.strptime(str(data_iso)[:10], "%Y-%m-%d")
        wd = dt.weekday()
    except Exception:
        return True

    info = classificar_setor_sigla(setor_nome)
    freq = info["freq"]

    if freq == "PAR":
        return wd in (0, 2, 4)
    elif freq == "IMPAR":
        return wd in (1, 3, 5)
    elif freq == "DOMINGO":
        return wd == 6
    elif freq == "DIARIO":
        return wd in (0, 1, 2, 3, 4, 5)
    else:
        return wd in (0, 1, 2, 3, 4, 5)


def info_frequencia_data(data_iso: str) -> Dict[str, Any]:
    """Retorna metadados operacionais da data selecionada para exibição no painel."""
    try:
        dt = datetime.strptime(str(data_iso)[:10], "%Y-%m-%d")
        wd = dt.weekday()
    except Exception:
        return {"dia_semana": "", "frequencia": "TODOS", "rotulo": "Todos os setores"}

    nomes_dias = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo"]
    dia_nome = nomes_dias[wd]

    if wd in (0, 2, 4):
        return {
            "weekday": wd,
            "dia_semana": dia_nome,
            "frequencia": "PAR",
            "siglas_esperadas": ["SCPD", "SCPN"],
            "rotulo": f"{dia_nome} · Frequência PAR (SCPD / SCPN)",
            "descricao": "Segunda, Quarta e Sexta exibem exclusivamente os setores PAR (3ª letra P)",
        }
    elif wd in (1, 3, 5):
        return {
            "weekday": wd,
            "dia_semana": dia_nome,
            "frequencia": "IMPAR",
            "siglas_esperadas": ["SCID", "SCIN"],
            "rotulo": f"{dia_nome} · Frequência ÍMPAR (SCID / SCIN)",
            "descricao": "Terça, Quinta e Sábado exibem exclusivamente os setores ÍMPAR (3ª letra I)",
        }
    else:
        return {
            "weekday": wd,
            "dia_semana": dia_nome,
            "frequencia": "DOMINGO",
            "siglas_esperadas": ["DOM", "FEIRA"],
            "rotulo": f"{dia_nome} · Coletas Especiais de Domingo",
            "descricao": "Domingo exibe coletas de feiras e operações de domingo",
        }


_build_locks: Dict[str, threading.Lock] = {}
_build_locks_guard = threading.Lock()
_rotas_index_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_rota_edges_cache: "OrderedDict[Tuple[str, float], List[Dict[str, Any]]]" = OrderedDict()
_rota_cache_lock = threading.Lock()
_malha_view_cache: "OrderedDict[str, Any]" = OrderedDict()
_malha_view_lock = threading.Lock()


# ==============================================================================
# CAMINHOS E DESCOBERTA DE ARQUIVOS
# ==============================================================================
def nome_cidade_arquivo(contrato_id: str) -> str:
    """'novo hamburgo' -> 'NOVO HAMBURGO' (padrão dos nomes de arquivos/pastas)."""
    return normalizar_ascii(contrato_id)


def _tipo_malha_padrao() -> str:
    tipo = normalizar_ascii(os.getenv("MALHA_TIPO", "TRANSITAVEIS"))
    return "COMPLETA" if tipo.startswith("COMPLET") else "TRANSITAVEIS"


def _dir_malhas(tipo: str) -> str:
    base = os.getenv("MALHAS_DIR") or BASE_DIR
    return os.path.join(base, DIR_MALHA_COMPLETA if tipo == "COMPLETA" else DIR_MALHA_TRANSITAVEIS)


def arquivo_malha(contrato_id: str, tipo: Optional[str] = None) -> Optional[str]:
    """Retorna o GeoPackage da malha viária da cidade (prioriza o tipo configurado em MALHA_TIPO)."""
    cidade = nome_cidade_arquivo(contrato_id)
    tipo = tipo or _tipo_malha_padrao()
    ordem = [tipo, "COMPLETA" if tipo == "TRANSITAVEIS" else "TRANSITAVEIS"]
    for t in ordem:
        pasta = _dir_malhas(t)
        if not os.path.isdir(pasta):
            continue
        for nome in os.listdir(pasta):
            if not nome.lower().endswith(".gpkg"):
                continue
            stem = normalizar_ascii(os.path.splitext(nome)[0])
            stem = stem.replace("MALHA COMPLETA ", "").replace("MALHA ", "").strip()
            if stem == cidade:
                return os.path.join(pasta, nome)
    return None


def dir_rotas() -> Optional[str]:
    """Pasta raiz das rotas planejadas (configurável por ROTAS_PLANEJADAS_DIR)."""
    candidatos = [
        os.getenv("ROTAS_PLANEJADAS_DIR"),
        os.path.join(BASE_DIR, "Rotas Planejadas"),
        os.path.join(BASE_DIR, "..", "Correções", "Edições", "Rotas Para Editar"),
    ]
    for c in candidatos:
        if c and os.path.isdir(c):
            return os.path.abspath(c)
    return None


def _norm_txt(texto: Any) -> str:
    t = normalizar_ascii(str(texto or ""))
    t = re.sub(r"([A-Z])(\d)", r"\1 \2", t)
    return re.sub(r"\s+", " ", t).strip()


def extrair_codigo_setor(texto: Any) -> Optional[Tuple[str, int]]:
    """'CARUARU - SCPD 85 - SEG e QUA' -> ('SCPD', 85); 'DOMINGO FEIRA 74' / 'SCFEIRA 74 DOM' -> ('FEIRA', 74)."""
    m = _RE_CODIGO.search(_norm_txt(texto))
    if not m:
        return None
    pref = m.group(1)
    if pref.endswith("FEIRA"):
        pref = "FEIRA"
    return pref, int(m.group(2))


def extrair_dias_setor(texto: Any) -> Set[str]:
    dias: Set[str] = set()
    for tok in re.findall(r"[A-Z]+", _norm_txt(texto)):
        tok = DIAS_ALIASES.get(tok, tok)
        if tok in DIAS_SEMANA:
            dias.add(tok)
    return dias


def listar_rotas_cidade(contrato_id: str) -> List[Dict[str, Any]]:
    """Indexa os arquivos de rota planejada da cidade: [{apelido, turno, path, codigo, dias, mtime}]."""
    raiz = dir_rotas()
    if not raiz:
        return []
    cidade = nome_cidade_arquivo(contrato_id)
    chave = f"{raiz}|{cidade}"
    agora = time.time()
    cache = _rotas_index_cache.get(chave)
    if cache and agora - cache[0] < 120:
        return cache[1]

    rotas: List[Dict[str, Any]] = []
    for pasta in sorted(os.listdir(raiz)):
        caminho_pasta = os.path.join(raiz, pasta)
        if not os.path.isdir(caminho_pasta):
            continue
        partes = normalizar_ascii(pasta).split(" - ")
        if len(partes) < 2 or partes[0].strip() != cidade:
            continue
        turno = partes[-1].strip()
        for arq in sorted(glob.glob(os.path.join(caminho_pasta, "**", "*.gpkg"), recursive=True)):
            apelido = os.path.splitext(os.path.basename(arq))[0].strip()
            rotas.append({
                "apelido": apelido,
                "turno": turno,
                "path": arq,
                "codigo": extrair_codigo_setor(apelido),
                "dias": sorted(extrair_dias_setor(apelido)),
                "mtime": os.path.getmtime(arq),
            })
    _rotas_index_cache[chave] = (agora, rotas)
    return rotas


def resolver_rota_setor(
    contrato_id: str,
    setor_inlog: str,
    data_iso: Optional[str] = None,
    turno: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Resolve o nome do setor retornado pela Inlog para o arquivo de rota planejada.
    Critérios (determinísticos): código do setor (ex: SCPD 85) -> turno -> dias da semana no nome
    -> dia da semana da data consultada -> rota sem restrição de dia.
    Setores operacionais sem rota de coleta (APOIO, OFICINA, DESCARREGO, ENTULHO...) retornam None.
    """
    codigo = extrair_codigo_setor(setor_inlog)
    if not codigo:
        return None
    rotas = listar_rotas_cidade(contrato_id)
    cands = [r for r in rotas if r["codigo"] == codigo]
    if not cands:
        return None
    turno_n = normalizar_ascii(turno)
    if turno_n:
        mesmo_turno = [r for r in cands if r["turno"] == turno_n]
        if mesmo_turno:
            cands = mesmo_turno
    if len(cands) == 1:
        return cands[0]

    dias_inlog = extrair_dias_setor(setor_inlog)
    if dias_inlog:
        exatos = [r for r in cands if set(r["dias"]) == dias_inlog]
        if exatos:
            return exatos[0]
    if data_iso:
        try:
            wd = datetime.strptime(data_iso, "%Y-%m-%d").weekday()
            dia_txt = [k for k, v in DIAS_SEMANA.items() if v == wd][0]
            do_dia = [r for r in cands if dia_txt in r["dias"]]
            if do_dia:
                return sorted(do_dia, key=lambda r: len(r["dias"]))[0]
        except ValueError:
            pass
    sem_dia = [r for r in cands if not r["dias"]]
    return sem_dia[0] if sem_dia else cands[0]


# ==============================================================================
# UTILITÁRIOS GEOMÉTRICOS
# ==============================================================================
def _nome_rua(valor: Any) -> str:
    if valor is None:
        return "Via Sem Nome"
    if isinstance(valor, float) and math.isnan(valor):
        return "Via Sem Nome"
    if isinstance(valor, (list, tuple)):
        itens = [str(v).strip() for v in valor if str(v).strip()]
        return " / ".join(itens) if itens else "Via Sem Nome"
    txt = str(valor).strip()
    if txt.startswith("[") and txt.endswith("]"):
        try:
            lista = ast.literal_eval(txt)
            if isinstance(lista, (list, tuple)):
                return _nome_rua(list(lista))
        except Exception:
            pass
    return txt or "Via Sem Nome"


def _bool_coleta(valor: Any) -> bool:
    if valor is None:
        return True
    if isinstance(valor, float) and math.isnan(valor):
        return True
    if isinstance(valor, str):
        return valor.strip().lower() not in ("0", "0.0", "false", "nao", "não", "f")
    return bool(valor)


def _chave_aresta(u: Any, v: Any, k: Any) -> str:
    try:
        a, b = int(u), int(v)
    except (TypeError, ValueError):
        a, b = str(u), str(v)
    if a > b:
        a, b = b, a
    try:
        kk = int(k)
    except (TypeError, ValueError):
        kk = 0
    return f"{a}-{b}-{kk}"


def _coords_linha(geom: Any) -> List[List[float]]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [[round(x, 7), round(y, 7)] for x, y in geom.coords]
    if geom.geom_type == "MultiLineString":
        out: List[List[float]] = []
        for parte in geom.geoms:
            out.extend([[round(x, 7), round(y, 7)] for x, y in parte.coords])
        return out
    return []


def _extensao_coords(coords: List[List[float]]) -> float:
    return db.calcular_extensao_coords(coords)


def _ler_gpkg(path: str, colunas_desejadas: List[str], com_geometria: bool = True):
    import pyogrio

    info = pyogrio.read_info(path)
    disponiveis = set(info.get("fields", []).tolist() if hasattr(info.get("fields"), "tolist") else info.get("fields", []))
    cols = [c for c in colunas_desejadas if c in disponiveis]
    df = pyogrio.read_dataframe(path, columns=cols, read_geometry=com_geometria)
    if com_geometria and df.crs is not None and str(df.crs).upper() not in ("EPSG:4326", "OGC:CRS84"):
        df = df.to_crs(4326)
    return df


# ==============================================================================
# ROTA PLANEJADA (ARESTAS ORDENADAS)
# ==============================================================================
def carregar_rota(rota: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Lê a sequência ordenada de arestas de um setor (cache LRU em memória por arquivo/mtime)."""
    path = rota["path"]
    chave = (path, rota.get("mtime") or os.path.getmtime(path))
    with _rota_cache_lock:
        if chave in _rota_edges_cache:
            _rota_edges_cache.move_to_end(chave)
            return _rota_edges_cache[chave]

    df = _ler_gpkg(path, ["u", "v", "key", "is_collection", "classification", "seq_id", "order", "length", "name", "BAIRRO"])
    if "seq_id" in df.columns:
        df = df.sort_values("seq_id", kind="stable")
    elif "order" in df.columns:
        df = df.sort_values("order", kind="stable")

    arestas: List[Dict[str, Any]] = []
    for i, row in enumerate(df.itertuples(index=False)):
        coords = _coords_linha(row.geometry)
        if len(coords) < 2:
            continue
        comp = getattr(row, "length", None)
        try:
            comp = float(comp)
            if math.isnan(comp):
                raise ValueError
        except (TypeError, ValueError):
            comp = _extensao_coords(coords)
        classif = str(getattr(row, "classification", "") or "").lower()
        eh_manual = (classif == "manual")
        arestas.append({
            "seq": i + 1,
            "edge_key": _chave_aresta(getattr(row, "u", i), getattr(row, "v", i), getattr(row, "key", 0)),
            "is_collection": _bool_coleta(getattr(row, "is_collection", True)) or eh_manual,
            "is_manual": eh_manual,
            "nome": _nome_rua(getattr(row, "name", None)),
            "bairro": str(getattr(row, "BAIRRO", "") or "").strip(),
            "len_m": round(comp, 2),
            "coords": coords,
        })

    with _rota_cache_lock:
        _rota_edges_cache[chave] = arestas
        while len(_rota_edges_cache) > 96:
            _rota_edges_cache.popitem(last=False)
    return arestas


# ==============================================================================
# CONSTRUÇÃO DO PROJETO DA CIDADE (MALHA x ROTAS)
# ==============================================================================
def _lock_cidade(contrato_id: str) -> threading.Lock:
    with _build_locks_guard:
        if contrato_id not in _build_locks:
            _build_locks[contrato_id] = threading.Lock()
        return _build_locks[contrato_id]


def _assinatura_fontes(contrato_id: str) -> Tuple[Optional[str], float, float, int]:
    malha = arquivo_malha(contrato_id)
    malha_mtime = os.path.getmtime(malha) if malha else 0.0
    rotas = listar_rotas_cidade(contrato_id)
    rotas_mtime = max((r["mtime"] for r in rotas), default=0.0)
    return malha, round(malha_mtime, 1), round(rotas_mtime, 1), len(rotas)


def status_projeto(contrato_id: str) -> Optional[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT * FROM projetos_malha WHERE contrato_id = ?", (contrato_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def projeto_precisa_construir(contrato_id: str) -> bool:
    malha, malha_mtime, rotas_mtime, n_rotas = _assinatura_fontes(contrato_id)
    if not malha:
        return False
    st = status_projeto(contrato_id)
    if not st:
        return True
    return (
        round(float(st.get("malha_mtime") or 0), 1) != malha_mtime
        or round(float(st.get("rotas_mtime") or 0), 1) != rotas_mtime
        or int(st.get("n_rotas") or 0) != n_rotas
        or os.path.basename(st.get("malha_arquivo") or "") != os.path.basename(malha)
    )


def construir_projeto_cidade(contrato_id: str, forcar: bool = False) -> Dict[str, Any]:
    """
    Gera o projeto auditável da cidade a partir da malha viária oficial + rotas planejadas.
    Idempotente: só reconstrói quando a malha ou as rotas mudaram (mtime) ou quando forçado.
    Segmentos anteriores são preservados em segmentos_viarios_legado (sem perda de dados);
    edições manuais do usuário são mantidas e flags de rua prioritária são transferidas.
    """
    contrato_id = contrato_id.lower().strip()
    with _lock_cidade(contrato_id):
        if not forcar and not projeto_precisa_construir(contrato_id):
            st = status_projeto(contrato_id)
            return {"ok": bool(st), "reconstruido": False, **(st or {})}

        malha_path, malha_mtime, rotas_mtime, n_rotas = _assinatura_fontes(contrato_id)
        if not malha_path:
            return {"ok": False, "reconstruido": False, "motivo": "Malha viária .gpkg não encontrada para a cidade."}

        t0 = time.time()
        malha = _ler_gpkg(malha_path, ["u", "v", "key", "name", "length", "highway", "BAIRRO"])
        idx_malha: Dict[str, int] = {}
        if {"u", "v"}.issubset(malha.columns):
            keys_col = malha["key"] if "key" in malha.columns else [0] * len(malha)
            for pos, (u, v, k) in enumerate(zip(malha["u"], malha["v"], keys_col)):
                ek = _chave_aresta(u, v, k)
                if ek not in idx_malha:
                    idx_malha[ek] = pos

        # Setores (com trecho de coleta) a que cada aresta pertence + geometria de fallback
        setores_por_aresta: Dict[str, Set[str]] = {}
        manual_por_aresta: Dict[str, bool] = {}
        fallback: Dict[str, Dict[str, Any]] = {}
        rotas = listar_rotas_cidade(contrato_id)
        for rota in rotas:
            for a in carregar_rota(rota):
                if not a["is_collection"]:
                    continue
                setores_por_aresta.setdefault(a["edge_key"], set()).add(rota["apelido"])
                if a["is_manual"]:
                    manual_por_aresta[a["edge_key"]] = True
                if a["edge_key"] not in idx_malha and a["edge_key"] not in fallback:
                    fallback[a["edge_key"]] = a

        if not setores_por_aresta:
            # Cidade sem rotas planejadas: usa a malha inteira (sem setor) como projeto
            setores_por_aresta = {ek: set() for ek in idx_malha.keys()}

        highways_manuais = {"footway", "steps", "pedestrian", "path", "corridor", "living_street"}
        registros: List[Dict[str, Any]] = []
        geoms = malha.geometry.values
        for ek, setores in setores_por_aresta.items():
            setores_ord = sorted(setores)
            pos = idx_malha.get(ek)
            hw_val = ""
            if pos is not None:
                coords = _coords_linha(geoms[pos])
                nome = _nome_rua(malha["name"].iloc[pos]) if "name" in malha.columns else "Via Sem Nome"
                bairro = str(malha["BAIRRO"].iloc[pos] or "").strip() if "BAIRRO" in malha.columns else ""
                comp = malha["length"].iloc[pos] if "length" in malha.columns else None
                hw_val = str(malha["highway"].iloc[pos] or "").lower() if "highway" in malha.columns else ""
                fid = int(malha.index[pos]) + 1
            else:
                fb = fallback.get(ek)
                if not fb:
                    continue
                coords, nome, bairro, comp, fid = fb["coords"], fb["nome"], fb["bairro"], fb["len_m"], None
            if len(coords) < 2:
                continue
            try:
                comp = float(comp)
                if math.isnan(comp) or comp <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                comp = _extensao_coords(coords)
            eh_manual = bool(manual_por_aresta.get(ek)) or any(h in hw_val for h in highways_manuais)
            registros.append({
                "fid": fid,
                "nome_rua": nome,
                "setor": setores_ord[0] if setores_ord else "S/S",
                "setores": setores_ord,
                "extensao_m": round(comp, 2),
                "coords": coords,
                "tipo": "MANUAL" if eh_manual else "COLETA",
                "is_manual": 1 if eh_manual else 0,
                "edge_key": ek,
                "bairro": bairro if bairro.lower() != "nan" else "",
            })

        resultado = _gravar_projeto(contrato_id, registros, malha_path, malha_mtime, rotas_mtime, n_rotas)
        resultado["duracao_s"] = round(time.time() - t0, 2)
        resultado["arestas_malha"] = len(idx_malha)
        resultado["arestas_fora_da_malha"] = len(fallback)
        print(
            f"[MALHA] {contrato_id}: projeto construído com {resultado['n_segmentos']} trechos "
            f"({resultado['km_total']} km, {n_rotas} rotas) em {resultado['duracao_s']}s"
        )
        return resultado


def _midpoint(coords: List[List[float]]) -> Tuple[float, float]:
    c = coords[len(coords) // 2]
    if len(coords) >= 2 and len(coords) % 2 == 0:
        c2 = coords[len(coords) // 2 - 1]
        return (c[0] + c2[0]) / 2.0, (c[1] + c2[1]) / 2.0
    return float(c[0]), float(c[1])


def _gravar_projeto(
    contrato_id: str,
    registros: List[Dict[str, Any]],
    malha_path: str,
    malha_mtime: float,
    rotas_mtime: float,
    n_rotas: int,
) -> Dict[str, Any]:
    with db._write_lock:
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            existentes = [dict(r) for r in cur.execute(
                "SELECT * FROM segmentos_viarios WHERE contrato_id = ?", (contrato_id,)
            ).fetchall()]

            # 1. Prioridades: por edge_key (projetos anteriores da malha) e por proximidade (legado)
            prio_keys = {e["edge_key"] for e in existentes if e.get("is_priority") and e.get("edge_key")}
            prio_pts: List[Tuple[float, float]] = []
            for e in existentes:
                if e.get("is_priority") and not e.get("edge_key"):
                    try:
                        prio_pts.append(_midpoint(json.loads(e["geom_json"])))
                    except Exception:
                        pass
            grade_prio: Dict[Tuple[int, int], List[Tuple[float, float]]] = {}
            for lon, lat in prio_pts:
                grade_prio.setdefault((int(lon * 2000), int(lat * 2000)), []).append((lon, lat))
            tol = 0.00012  # ~13 m
            for r in registros:
                if r["edge_key"] in prio_keys:
                    r["is_priority"] = 1
                    continue
                r["is_priority"] = 0
                if grade_prio:
                    lon, lat = _midpoint(r["coords"])
                    cx, cy = int(lon * 2000), int(lat * 2000)
                    achou = False
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            for plon, plat in grade_prio.get((cx + dx, cy + dy), []):
                                if abs(plon - lon) <= tol and abs(plat - lat) <= tol:
                                    achou = True
                                    break
                            if achou:
                                break
                        if achou:
                            break
                    if achou:
                        r["is_priority"] = 1

            # 2. Edições do usuário preservadas (desenhadas/alteradas no editor GIS)
            preservar_ids = [
                e["id"] for e in existentes
                if (e.get("origem") or "") != "MALHA_GPKG"
                and (e.get("is_manual") or (e.get("tipo") or "COLETA") != "COLETA" or e.get("atualizado_em"))
            ]

            with conn:
                # 3. Backup integral (sem perda) dos segmentos anteriores que não vieram da malha
                legado = [e for e in existentes if (e.get("origem") or "") != "MALHA_GPKG"]
                if legado:
                    cur.executemany(
                        """
                        INSERT INTO segmentos_viarios_legado
                            (id_original, contrato_id, fid, nome_rua, setor, extensao_m, geom_json,
                             tipo, is_manual, is_priority, atualizado_em)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (e["id"], e["contrato_id"], e.get("fid"), e.get("nome_rua"), e.get("setor"),
                             e.get("extensao_m"), e["geom_json"], e.get("tipo"), e.get("is_manual"),
                             e.get("is_priority"), e.get("atualizado_em"))
                            for e in legado
                        ],
                    )
                cur.execute("DELETE FROM auditorias WHERE contrato_id = ?", (contrato_id,))
                if preservar_ids:
                    ph = ",".join("?" * len(preservar_ids))
                    cur.execute(
                        f"UPDATE segmentos_viarios SET origem = 'EDICAO_USUARIO' WHERE id IN ({ph})",
                        preservar_ids,
                    )
                    cur.execute(
                        f"DELETE FROM segmentos_viarios WHERE contrato_id = ? AND id NOT IN ({ph})",
                        [contrato_id] + preservar_ids,
                    )
                else:
                    cur.execute("DELETE FROM segmentos_viarios WHERE contrato_id = ?", (contrato_id,))

                cur.executemany(
                    """
                    INSERT INTO segmentos_viarios
                        (contrato_id, fid, nome_rua, setor, extensao_m, geom_json, tipo, is_manual,
                         is_priority, atualizado_em, edge_key, setores_json, origem, bairro)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 'MALHA_GPKG', ?)
                    """,
                    [
                        (contrato_id, r["fid"], r["nome_rua"], r["setor"], r["extensao_m"],
                         json.dumps(r["coords"]), r["tipo"], r["is_manual"], r["is_priority"],
                         r["edge_key"], json.dumps(r["setores"], ensure_ascii=False), r["bairro"])
                        for r in registros
                    ],
                )
                km_total = round(sum(r["extensao_m"] for r in registros) / 1000.0, 2)
                cur.execute(
                    """
                    INSERT OR REPLACE INTO projetos_malha
                        (contrato_id, malha_arquivo, malha_mtime, rotas_dir, rotas_mtime, n_rotas,
                         n_segmentos, km_total, n_prioritarias, n_preservados, construido_em)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (contrato_id, malha_path, malha_mtime, dir_rotas(), rotas_mtime, n_rotas,
                     len(registros), km_total, sum(r["is_priority"] for r in registros), len(preservar_ids)),
                )
            return {
                "ok": True,
                "reconstruido": True,
                "contrato_id": contrato_id,
                "malha_arquivo": malha_path,
                "n_rotas": n_rotas,
                "n_segmentos": len(registros),
                "km_total": km_total,
                "n_prioritarias": sum(r["is_priority"] for r in registros),
                "n_preservados": len(preservar_ids),
                "n_legado_backup": len(legado),
            }
        finally:
            conn.close()


# ==============================================================================
# MALHA VIÁRIA COMPLETA PARA VISUALIZAÇÃO (RECORTE POR VIEWPORT)
# ==============================================================================
def _malha_visual(contrato_id: str):
    with _malha_view_lock:
        if contrato_id in _malha_view_cache:
            _malha_view_cache.move_to_end(contrato_id)
            return _malha_view_cache[contrato_id]
    path = arquivo_malha(contrato_id)
    if not path:
        return None
    gdf = _ler_gpkg(path, ["u", "v", "key", "name", "highway"])
    if {"u", "v"}.issubset(gdf.columns):
        keys_col = gdf["key"] if "key" in gdf.columns else 0
        gdf = gdf.assign(_ek=[_chave_aresta(u, v, k) for u, v, k in zip(gdf["u"], gdf["v"], keys_col if not isinstance(keys_col, int) else [0] * len(gdf))])
        gdf = gdf.drop_duplicates("_ek")
    gdf = gdf.reset_index(drop=True)
    _ = gdf.sindex
    with _malha_view_lock:
        _malha_view_cache[contrato_id] = gdf
        while len(_malha_view_cache) > 2:
            _malha_view_cache.popitem(last=False)
    return gdf


def malha_viewport(contrato_id: str, bbox: Tuple[float, float, float, float], limite: int = 8000) -> Dict[str, Any]:
    """Retorna as ruas da malha viária oficial dentro do retângulo visível (lon_min, lat_min, lon_max, lat_max)."""
    gdf = _malha_visual(contrato_id.lower().strip())
    if gdf is None:
        return {"type": "FeatureCollection", "features": [], "truncado": False}
    idx = list(gdf.sindex.query(__import__("shapely").geometry.box(*bbox), predicate="intersects"))
    truncado = len(idx) > limite
    idx = idx[:limite]
    feats = []
    for i in idx:
        row = gdf.iloc[i]
        coords = _coords_linha(row.geometry)
        if len(coords) < 2:
            continue
        feats.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "nome": _nome_rua(row.get("name")),
                "highway": _nome_rua(row.get("highway")) if row.get("highway") is not None else "",
            },
        })
    return {"type": "FeatureCollection", "features": feats, "truncado": truncado, "total": len(feats)}


def preconstruir_todas_cidades():
    """Executado em segundo plano no start do servidor: deixa todos os projetos prontos."""
    conn = db.get_connection()
    try:
        ids = [r["id"] for r in conn.execute("SELECT id FROM contratos ORDER BY id").fetchall()]
    finally:
        conn.close()
    for cid in ids:
        try:
            if projeto_precisa_construir(cid):
                construir_projeto_cidade(cid)
        except Exception as e:
            print(f"[MALHA] Falha ao construir projeto de {cid}: {e}")
