"""
Módulo de Conversão GPKG -> Shapefile (.zip) e Automação do Editor de Rotas Inlog
Desenvolvido para sitemaLCv1 - Locar Saneamento Ambiental.

Responsabilidades:
  1. Ler um arquivo .gpkg editado no QGIS (mesmo com feições novas sem seq_id ou lacunas),
     filtrar duplicatas de mão dupla, inserir trechos novos, corrigir orientação,
     preencher eventuais lacunas com caminho mínimo (DESLOCAMENTO) sobre a malha viária
     e gerar tanto o .gpkg corrigido/limpo quanto o Shapefile (.zip) padrão Inlog
     (rotas/Rota_Final_<SETOR>.shp, .shx, .dbf, .prj, .cpg em EPSG:4326 / ISO-8859-1).
  2. Automatizar o fluxo no portal https://locar.inlog.com.br:
     - Login no Autenticador
     - Seleção de vínculo/cidade no perfil do usuário (AtivarVinculo?c=...)
     - Localização do setor e polígono no EditorRotas
     - Inativação e exclusão definitiva da(s) rota(s) antiga(s) do setor
     - Importação da nova rota (CriarRotaCompleta com cálculo idêntico ao AngularJS)
     - Aguardo de processamento, avanço de etapa (se necessário) e ativação da nova rota
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as _wt
import glob
import heapq
import io
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import requests
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree
from shapely.validation import make_valid

import malha_rotas

INLOG_LOGIN_URL = "https://locar.inlog.com.br/Rastreamento/Apresentacao/Autenticador/Account/Login"
INLOG_AUTH_HOME_URL = "https://locar.inlog.com.br/Rastreamento/Apresentacao/Autenticador/"
INLOG_VINCULO_URL = "https://locar.inlog.com.br/Rastreamento/Apresentacao/Account/AtivarVinculo"
INLOG_EDITOR_BASE = "https://locar.inlog.com.br/Rastreamento/Apresentacao/EditorRotas/"
INLOG_EDITOR_API_KEY = "0ff6134bec254f9ba1f96d15082d20f2"

INLOG_DEFAULT_USER = os.getenv("INLOG_WEB_USER", "carlos.dorth").strip()
INLOG_DEFAULT_PASS = os.getenv("INLOG_WEB_PASS", "Locar1234").strip()

GISEYES_DEFAULT_EMPRESA = os.getenv("GISEYES_EMPRESA", "LOCAR").strip()
GISEYES_DEFAULT_USER = os.getenv("GISEYES_USER", "GABRIELLIMA").strip()
GISEYES_DEFAULT_PASS = os.getenv("GISEYES_PASS", "inlog2026").strip()

WGS84_PRJ = (
    'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
)

# Configuração oficial dos contratos para geração de Shapefile de Setor (process_sectors)
CONTRATOS_SETORES_CFG: Dict[str, Tuple[str, str, str]] = {
    "vitoria": ("001", "area", "VITORIA"),
    "paulista": ("002", "area", "PAULISTA"),
    "cabo": ("004", "area", "CABO"),
    "olinda": ("005", "streets", "OLINDA"),
    "caruaru": ("006", "area", "CARUARU"),
    "garanhuns": ("007", "area", "GARANHUNS"),
    "pesqueira": ("008", "area", "PESQUEIRA"),
    "jequie": ("009", "area", "JEQUIE"),
    "cuiaba": ("010", "area", "CUIABA"),
    "varzea": ("011", "area", "VARZEA"),
    "jaboatao": ("012", "area", "JABOATAO"),
    "canela": ("013", "area", "CANELA"),
    "novo hamburgo": ("014", "area", "NOVO HAMBURGO"),
    "novo_hamburgo": ("014", "area", "NOVO HAMBURGO"),
    "seropedica": ("000", "area", "SEROPEDICA"),
}

_MALHA_GRAFO_CACHE: Dict[str, Dict[str, Any]] = {}
_MALHA_GRAFO_LOCK = threading.Lock()

_MALHA_BAIRRO_CACHE: Dict[str, Tuple[gpd.GeoDataFrame, STRtree, List[Any]]] = {}
_MALHA_BAIRRO_LOCK = threading.Lock()

_PUBLICACAO_JOBS: Dict[str, Dict[str, Any]] = {}
_PUBLICACAO_LOCK = threading.Lock()


_SECTOR_CLUSTER_BUFFER_MULT = 0.75
_SECTOR_CLUSTER_BUFFER_MIN = 100.0
_SECTOR_CLUSTER_BUFFER_MAX = 400.0
_SECTOR_CLUSTER_BUFFER_KNN = 5
_SECTOR_MIN_ADAPTIVE_CLUSTER = 3
_SECTOR_BUFFER_MARGIN = 15.0
_SECTOR_SIMPLIFY_TOL = 2.0
_SECTOR_BUFFER_RES = 8
_SECTOR_NAME_MAX_LEN = 250

_SECTOR_WEEKDAY_MAP = {"SEG": 0, "TER": 1, "QUA": 2, "QUI": 3, "SEX": 4, "SAB": 5, "DOM": 6}
_SECTOR_CODE_TO_DAY = {v: k for k, v in _SECTOR_WEEKDAY_MAP.items()}
_SECTOR_AVENUE_HW = frozenset({
    "trunk_link", "tertiary_link", "tertiary", "primary_link", "secondary",
    "trunk", "primary", "motorway_link", "motorway", "secondary_link",
})
_SECTOR_STREET_ABBREV = (
    (r"^AVENIDA ", "AV "),
    (r"^RODOVIA ", "ROD "),
    (r"^ESTRADA ", "ESTR "),
    (r"^TRAVESSA ", "TV "),
)
_SECTOR_AREA_ABBREV = (
    (r"^RESIDENCIAL ", "RES "),
    (r"^CONJUNTO ", "CONJ "),
    (r"^JARDIM ", "JD "),
    (r"^LOTEAMENTO ", "LOT "),
    (r"^PARQUE ", "PQ "),
)
_SECTOR_WEEKDAY_RE = re.compile(r"\b(SEG|TER|QUA|QUI|SEX|SAB|DOM)\b")
_SECTOR_ORDINAL_RE = re.compile(r"\b(?:DOM|SEG|TER|QUA|QUI|SEX|SAB)\s*(?:[1-5]|L)")

# Aliases de contrato -> nome esperado no dropdown de vínculos do Autenticador Inlog
CONTRATO_VINCULO_ALIASES: Dict[str, List[str]] = {
    "olinda": ["OLINDA"],
    "cabo": ["CABO"],
    "caruaru": ["CARUARU"],
    "pesqueira": ["PESQUEIRA"],
    "canela": ["CANELA"],
    "vitoria": ["VITORIA"],
    "cuiaba": ["CUIABA"],
    "jequie": ["JEQUIE"],
    "novo hamburgo": ["NOVO HAMBURGO"],
    "garanhuns": ["GARANHUNS"],
    "paulista": ["PAULISTA"],
    "jaboatao": ["JABOATAO"],
    "varzea": ["VARZEA"],
    "seropedica": ["SEROPEDICA"],
}

def _norm_txt(texto: Any) -> str:
    if texto is None:
        return ""
    s = unicodedata.normalize("NFKD", str(texto))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s.upper()).strip()


def _safe_filename(texto: str, fallback: str = "SETOR") -> str:
    s = re.sub(r'[<>:"/\\|?*]+', "_", str(texto or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s or fallback


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distância esférica em metros (mesmo raio 6371009 m do Google Maps spherical)."""
    r = 6371009.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r * math.asin(min(1.0, math.sqrt(max(0.0, a))))


def _angulo_segmento_graus(c1: Tuple[float, float], c2: Tuple[float, float]) -> float:
    return math.degrees(math.atan2(c2[1] - c1[1], c2[0] - c1[0]))


def _diff_angular_graus(c_prev_a: Tuple[float, float], c_prev_b: Tuple[float, float], c_next_a: Tuple[float, float], c_next_b: Tuple[float, float]) -> float:
    a1 = _angulo_segmento_graus(c_prev_a, c_prev_b)
    a2 = _angulo_segmento_graus(c_next_a, c_next_b)
    return abs((a2 - a1 + 180.0) % 360.0 - 180.0)


def _node_key_from_coord(coord: Tuple[float, float]) -> str:
    return f"pt:{round(float(coord[0]), 6):.6f},{round(float(coord[1]), 6):.6f}"


def _edge_undir_key(u_val: Any, v_val: Any, c0: Tuple[float, float], c1: Tuple[float, float]) -> Tuple[Any, Any]:
    try:
        if u_val is not None and v_val is not None and not pd.isna(u_val) and not pd.isna(v_val):
            a, b = int(u_val), int(v_val)
            return (a, b) if a <= b else (b, a)
    except (TypeError, ValueError):
        pass
    p0 = _node_key_from_coord(c0)
    p1 = _node_key_from_coord(c1)
    return (p0, p1) if p0 <= p1 else (p1, p0)


def _parse_int_or_none(val: Any) -> Optional[int]:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    try:
        if pd.isna(val):
            return None
        return int(val)
    except (TypeError, ValueError):
        return None


def _carregar_grafo_malha(contrato_id: str) -> Optional[Dict[str, Any]]:
    """Carrega e mantém em cache o grafo completo da malha viária da cidade para roteirização de lacunas."""
    cid = str(contrato_id or "").lower().strip()
    if not cid:
        return None

    with _MALHA_GRAFO_LOCK:
        cached = _MALHA_GRAFO_CACHE.get(cid)
        if cached is not None:
            return cached

    path = malha_rotas.arquivo_malha(cid, tipo="COMPLETA") or malha_rotas.arquivo_malha(cid, tipo="TRANSITAVEIS")
    if not path or not os.path.isfile(path):
        return None

    info = pyogrio.read_info(path)
    fields = set(info.get("fields", []).tolist() if hasattr(info.get("fields"), "tolist") else info.get("fields", []))
    cols = [c for c in ["u", "v", "length", "oneway", "name", "highway"] if c in fields]
    gdf = pyogrio.read_dataframe(path, columns=cols, read_geometry=True)
    if gdf.crs is not None and str(gdf.crs).upper() not in ("EPSG:4326", "OGC:CRS84"):
        gdf = gdf.to_crs(4326)

    adj: Dict[int, List[Tuple[int, float, float, List[Tuple[float, float]], str, str]]] = defaultdict(list)
    node_coords: Dict[int, Tuple[float, float]] = {}
    grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)

    for row in gdf.itertuples(index=False):
        geom = getattr(row, "geometry", None)
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == "MultiLineString":
            coords = [tuple(pt[:2]) for part in geom.geoms for pt in part.coords]
        elif geom.geom_type == "LineString":
            coords = [tuple(pt[:2]) for pt in geom.coords]
        else:
            continue
        if len(coords) < 2:
            continue

        u = _parse_int_or_none(getattr(row, "u", None))
        v = _parse_int_or_none(getattr(row, "v", None))
        if u is None or v is None:
            continue

        raw_len = getattr(row, "length", None)
        try:
            length_m = float(raw_len)
            if math.isnan(length_m) or length_m <= 0:
                raise ValueError
        except (TypeError, ValueError):
            length_m = sum(
                _haversine_m(coords[k][1], coords[k][0], coords[k + 1][1], coords[k + 1][0])
                for k in range(len(coords) - 1)
            )

        ow_raw = getattr(row, "oneway", None)
        is_oneway = str(ow_raw or "").strip().lower() in ("1", "1.0", "true", "yes", "sim", "t")
        name = str(getattr(row, "name", "") or "")
        if name.lower() == "nan":
            name = ""
        hwy = str(getattr(row, "highway", "") or "")
        if hwy.lower() == "nan":
            hwy = ""

        adj[u].append((v, length_m, length_m, coords, name, hwy))
        rev_cost = length_m * (3.5 if is_oneway else 1.05)
        adj[v].append((u, rev_cost, length_m, list(reversed(coords)), name, hwy))

        if u not in node_coords:
            node_coords[u] = coords[0]
            grid[(int(coords[0][0] * 1000), int(coords[0][1] * 1000))].append(u)
        if v not in node_coords:
            node_coords[v] = coords[-1]
            grid[(int(coords[-1][0] * 1000), int(coords[-1][1] * 1000))].append(v)

    grafo = {"adj": adj, "node_coords": node_coords, "grid": grid, "path": path}
    with _MALHA_GRAFO_LOCK:
        _MALHA_GRAFO_CACHE[cid] = grafo
    return grafo


def _no_mais_proximo_grafo(grafo: Optional[Dict[str, Any]], coord: Tuple[float, float], max_dist_m: float = 60.0) -> Optional[int]:
    if not grafo:
        return None
    node_coords = grafo["node_coords"]
    grid = grafo["grid"]
    cx, cy = int(coord[0] * 1000), int(coord[1] * 1000)
    best_n = None
    best_d = max_dist_m
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for nid in grid.get((cx + dx, cy + dy), ()):
                nc = node_coords[nid]
                d = _haversine_m(coord[1], coord[0], nc[1], nc[0])
                if d < best_d:
                    best_d = d
                    best_n = nid
    return best_n


def _dijkstra_malha(
    grafo: Optional[Dict[str, Any]],
    u_start: Optional[int],
    v_target: Optional[int],
    max_cost: float = 3500.0,
) -> Tuple[Optional[float], Optional[List[Dict[str, Any]]]]:
    if not grafo or u_start is None or v_target is None:
        return None, None
    if u_start == v_target:
        return 0.0, []
    adj = grafo["adj"]
    if u_start not in adj:
        return None, None

    pq: List[Tuple[float, int, List[Dict[str, Any]]]] = [(0.0, u_start, [])]
    best: Dict[int, float] = {u_start: 0.0}
    while pq:
        cost, cur, path = heapq.heappop(pq)
        if cur == v_target:
            return cost, path
        if cost > best.get(cur, 1e18) or cost > max_cost:
            continue
        for nxt, w_cost, real_len_m, coords, name, hwy in adj.get(cur, ()):
            nc = cost + w_cost
            if nc < best.get(nxt, 1e18) and nc <= max_cost:
                best[nxt] = nc
                step = {
                    "u": cur,
                    "v": nxt,
                    "length_m": real_len_m,
                    "coords": coords,
                    "name": name,
                    "highway": hwy,
                    "tipo": "DESLOCAMENTO",
                    "is_collection": False,
                    "classification": "travel",
                }
                heapq.heappush(pq, (nc, nxt, path + [step]))
    return None, None


def _extrair_tipo_aresta(row_dict: Dict[str, Any]) -> str:
    classif = str(row_dict.get("classification") or "").strip().lower()
    if classif == "manual":
        return "MANUAL"
    if "is_collection" in row_dict and row_dict["is_collection"] is not None and not pd.isna(row_dict["is_collection"]):
        val = row_dict["is_collection"]
        if isinstance(val, str):
            eh_col = val.strip().lower() not in ("0", "0.0", "false", "nao", "não", "f", "")
        else:
            eh_col = bool(val)
        return "COLETA" if eh_col else "DESLOCAMENTO"
    if classif in ("travel", "deslocamento", "displacement", "d"):
        return "DESLOCAMENTO"
    tipo_raw = str(row_dict.get("TIPO") or row_dict.get("tipo") or "").strip().upper()
    if tipo_raw in ("DESLOCAMENTO", "D"):
        return "DESLOCAMENTO"
    if tipo_raw in ("MANUAL", "M"):
        return "MANUAL"
    return "COLETA"


def _reverter_aresta(aresta: Dict[str, Any]) -> Dict[str, Any]:
    copia = dict(aresta)
    copia["u"], copia["v"] = aresta["v"], aresta["u"]
    copia["coords"] = list(reversed(aresta["coords"]))
    return copia


def _encadear_novas_arestas(novas: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Agrupa arestas novas (sem seq_id) em cadeias conectadas e orientadas."""
    restantes = list(novas)
    cadeias: List[List[Dict[str, Any]]] = []
    while restantes:
        atual = restantes.pop(0)
        cadeia = [atual]
        cresceu = True
        while cresceu and restantes:
            cresceu = False
            fim_c = cadeia[-1]["coords"][-1]
            fim_u = cadeia[-1]["v"]
            for idx, cand in enumerate(restantes):
                c0, c1 = cand["coords"][0], cand["coords"][-1]
                if (fim_u is not None and cand["u"] == fim_u) or _haversine_m(fim_c[1], fim_c[0], c0[1], c0[0]) <= 2.0:
                    cadeia.append(restantes.pop(idx))
                    cresceu = True
                    break
                if (fim_u is not None and cand["v"] == fim_u) or _haversine_m(fim_c[1], fim_c[0], c1[1], c1[0]) <= 2.0:
                    cadeia.append(_reverter_aresta(restantes.pop(idx)))
                    cresceu = True
                    break
            if cresceu:
                continue
            ini_c = cadeia[0]["coords"][0]
            ini_u = cadeia[0]["u"]
            for idx, cand in enumerate(restantes):
                c0, c1 = cand["coords"][0], cand["coords"][-1]
                if (ini_u is not None and cand["v"] == ini_u) or _haversine_m(ini_c[1], ini_c[0], c1[1], c1[0]) <= 2.0:
                    cadeia.insert(0, restantes.pop(idx))
                    cresceu = True
                    break
                if (ini_u is not None and cand["u"] == ini_u) or _haversine_m(ini_c[1], ini_c[0], c0[1], c0[0]) <= 2.0:
                    cadeia.insert(0, _reverter_aresta(restantes.pop(idx)))
                    cresceu = True
                    break
        cadeias.append(cadeia)
    return cadeias


def _inserir_cadeias_na_sequencia(
    base: List[Dict[str, Any]],
    cadeias: List[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Insere cadeias novas (adicionadas no QGIS sem seq_id) na melhor posição da sequência."""
    seq = list(base)
    if not seq and cadeias:
        seq = list(cadeias.pop(0))

    pendentes = list(cadeias)
    while pendentes:
        melhor_score = float("inf")
        melhor_idx_c = 0
        melhor_pos = len(seq)
        melhor_rev = False

        for c_idx, cadeia in enumerate(pendentes):
            c_ini = cadeia[0]["coords"][0]
            c_fim = cadeia[-1]["coords"][-1]
            c_u = cadeia[0]["u"]
            c_v = cadeia[-1]["v"]

            # Testar inserção após cada elemento pos-1 (ou antes de 0)
            for pos in range(len(seq) + 1):
                if pos == 0:
                    nxt = seq[0]
                    d_norm = _haversine_m(c_fim[1], c_fim[0], nxt["coords"][0][1], nxt["coords"][0][0])
                    if c_v is not None and c_v == nxt["u"]:
                        d_norm = 0.0
                    d_rev = _haversine_m(c_ini[1], c_ini[0], nxt["coords"][0][1], nxt["coords"][0][0])
                    if c_u is not None and c_u == nxt["u"]:
                        d_rev = 0.0
                elif pos == len(seq):
                    prv = seq[-1]
                    d_norm = _haversine_m(prv["coords"][-1][1], prv["coords"][-1][0], c_ini[1], c_ini[0])
                    if prv["v"] is not None and prv["v"] == c_u:
                        d_norm = 0.0
                    d_rev = _haversine_m(prv["coords"][-1][1], prv["coords"][-1][0], c_fim[1], c_fim[0])
                    if prv["v"] is not None and prv["v"] == c_v:
                        d_rev = 0.0
                else:
                    prv = seq[pos - 1]
                    nxt = seq[pos]
                    d_orig = _haversine_m(prv["coords"][-1][1], prv["coords"][-1][0], nxt["coords"][0][1], nxt["coords"][0][0])
                    d_n1 = 0.0 if (prv["v"] is not None and prv["v"] == c_u) else _haversine_m(prv["coords"][-1][1], prv["coords"][-1][0], c_ini[1], c_ini[0])
                    d_n2 = 0.0 if (c_v is not None and c_v == nxt["u"]) else _haversine_m(c_fim[1], c_fim[0], nxt["coords"][0][1], nxt["coords"][0][0])
                    d_norm = d_n1 + d_n2 - 0.5 * d_orig

                    d_r1 = 0.0 if (prv["v"] is not None and prv["v"] == c_v) else _haversine_m(prv["coords"][-1][1], prv["coords"][-1][0], c_fim[1], c_fim[0])
                    d_r2 = 0.0 if (c_u is not None and c_u == nxt["u"]) else _haversine_m(c_ini[1], c_ini[0], nxt["coords"][0][1], nxt["coords"][0][0])
                    d_rev = d_r1 + d_r2 - 0.5 * d_orig

                if d_norm < melhor_score:
                    melhor_score = d_norm
                    melhor_idx_c = c_idx
                    melhor_pos = pos
                    melhor_rev = False
                if d_rev < melhor_score:
                    melhor_score = d_rev
                    melhor_idx_c = c_idx
                    melhor_pos = pos
                    melhor_rev = True

        escolhida = pendentes.pop(melhor_idx_c)
        if melhor_rev:
            escolhida = [_reverter_aresta(e) for e in reversed(escolhida)]
        seq = seq[:melhor_pos] + escolhida + seq[melhor_pos:]
    return seq


def converter_gpkg_para_shapefile_zip(
    gpkg_bytes: bytes,
    contrato_id: str,
    setor_codigo: str,
    setor_descricao: str,
    macro_setor: str,
    rota_nome: str = "ROTA SUB",
    apelido_rota: str = "ROTA SUB",
    nome_arquivo_base: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Converte um .gpkg editado no QGIS para:
      - Pacote Shapefile (.zip) idêntico ao padrão Inlog (`rotas/Rota_Final_<SETOR>.shp`, etc.)
      - GeoPackage (.gpkg) limpo, sequenciado e com lacunas preenchidas
      - Lista de feições GeoJSON para envio direto ao EditorRotas da Inlog
    """
    if not gpkg_bytes:
        raise ValueError("Arquivo .gpkg vazio.")

    cid = str(contrato_id or "").lower().strip()
    base_name = _safe_filename(nome_arquivo_base or setor_codigo or "ROTA", fallback="ROTA")
    rota_nome_str = str(rota_nome or "ROTA SUB").strip() or "ROTA SUB"
    apelido_str = str(apelido_rota or rota_nome_str).strip() or rota_nome_str
    setor_desc_str = str(setor_descricao or setor_codigo or "").strip()
    macro_str = str(macro_setor or "").strip()

    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, "input.gpkg")
        with open(in_path, "wb") as f:
            f.write(gpkg_bytes)

        gdf_orig = gpd.read_file(in_path)
        orig_crs = gdf_orig.crs
        if orig_crs is not None and str(orig_crs).upper() not in ("EPSG:4326", "OGC:CRS84"):
            gdf_wgs = gdf_orig.to_crs(4326)
        else:
            gdf_wgs = gdf_orig.copy()
            if gdf_wgs.crs is None:
                gdf_wgs = gdf_wgs.set_crs(4326)

        # Extrair lista de arestas normalizadas em WGS84
        seq_col = "seq_id" if "seq_id" in gdf_wgs.columns else ("order" if "order" in gdf_wgs.columns else ("SEQUENCIA" if "SEQUENCIA" in gdf_wgs.columns else None))
        arestas_com_seq: List[Tuple[float, float, Dict[str, Any]]] = []
        arestas_sem_seq: List[Dict[str, Any]] = []

        for row_idx, (_, row) in enumerate(gdf_wgs.iterrows()):
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            partes = list(geom.geoms) if geom.geom_type == "MultiLineString" else ([geom] if geom.geom_type == "LineString" else [])
            if not partes:
                continue

            row_dict = row.to_dict()
            tipo = _extrair_tipo_aresta(row_dict)
            eh_col = tipo != "DESLOCAMENTO"
            classif = "manual" if tipo == "MANUAL" else ("collection" if eh_col else "travel")
            nome_rua = str(row_dict.get("name") or "")
            if nome_rua.lower() == "nan":
                nome_rua = ""
            hwy = str(row_dict.get("highway") or "")
            if hwy.lower() == "nan":
                hwy = ""

            seq_val = None
            if seq_col and row_dict.get(seq_col) is not None and not pd.isna(row_dict.get(seq_col)):
                try:
                    seq_val = float(row_dict[seq_col])
                except (TypeError, ValueError):
                    seq_val = None
            ord_val = row_idx
            if "order" in row_dict and row_dict.get("order") is not None and not pd.isna(row_dict.get("order")):
                try:
                    ord_val = float(row_dict["order"])
                except (TypeError, ValueError):
                    pass

            for p_idx, parte in enumerate(partes):
                coords = [(float(c[0]), float(c[1])) for c in parte.coords]
                if len(coords) < 2:
                    continue
                u_val = _parse_int_or_none(row_dict.get("u")) if p_idx == 0 else None
                v_val = _parse_int_or_none(row_dict.get("v")) if p_idx == len(partes) - 1 else None
                raw_len = row_dict.get("length") if len(partes) == 1 else None
                try:
                    len_m = float(raw_len)
                    if math.isnan(len_m) or len_m <= 0:
                        raise ValueError
                except (TypeError, ValueError):
                    len_m = sum(
                        _haversine_m(coords[k][1], coords[k][0], coords[k + 1][1], coords[k + 1][0])
                        for k in range(len(coords) - 1)
                    )

                aresta = {
                    "u": u_val,
                    "v": v_val,
                    "length_m": len_m,
                    "coords": coords,
                    "name": nome_rua,
                    "highway": hwy,
                    "tipo": tipo,
                    "is_collection": eh_col,
                    "classification": classif,
                }
                if seq_val is not None:
                    arestas_com_seq.append((seq_val, ord_val + p_idx * 0.001, aresta))
                else:
                    arestas_sem_seq.append(aresta)

        if not arestas_com_seq and not arestas_sem_seq:
            raise ValueError("Nenhuma geometria LineString válida encontrada no arquivo .gpkg.")

        arestas_com_seq.sort(key=lambda x: (x[0], x[1]))
        seq_base = [x[2] for x in arestas_com_seq]

        # Etapa A: filtrar duplicatas de mão dupla entre linhas sem seq_id e inserir apenas trechos novos
        descartadas_duplicatas = 0
        novas_inseridas = 0
        if seq_base and arestas_sem_seq:
            undir_base = {
                _edge_undir_key(e["u"], e["v"], e["coords"][0], e["coords"][-1])
                for e in seq_base
            }
            genuinas_novas = []
            for e in arestas_sem_seq:
                uk = _edge_undir_key(e["u"], e["v"], e["coords"][0], e["coords"][-1])
                if uk in undir_base:
                    descartadas_duplicatas += 1
                else:
                    genuinas_novas.append(e)
            if genuinas_novas:
                novas_inseridas = len(genuinas_novas)
                cadeias = _encadear_novas_arestas(genuinas_novas)
                seq_base = _inserir_cadeias_na_sequencia(seq_base, cadeias)
        elif not seq_base and arestas_sem_seq:
            novas_inseridas = len(arestas_sem_seq)
            cadeias = _encadear_novas_arestas(arestas_sem_seq)
            seq_base = _inserir_cadeias_na_sequencia([], cadeias)

        # Etapa B: corrigir orientação e preencher lacunas com DESLOCAMENTO sobre a malha viária
        grafo = _carregar_grafo_malha(cid)
        rota_continua: List[Dict[str, Any]] = []
        lacunas_detectadas = 0
        lacunas_preenchidas = 0
        arestas_desloc_inseridas = 0
        arestas_reorientadas = 0

        if len(seq_base) >= 2:
            e0, e1 = seq_base[0], seq_base[1]
            c0_end = e0["coords"][-1]
            c1_ini, c1_end = e1["coords"][0], e1["coords"][-1]
            toca_normal = (e0["v"] is not None and e0["v"] in (e1["u"], e1["v"])) or (
                _haversine_m(c0_end[1], c0_end[0], c1_ini[1], c1_ini[0]) <= 2.0
                or _haversine_m(c0_end[1], c0_end[0], c1_end[1], c1_end[0]) <= 2.0
            )
            if not toca_normal:
                c0_ini = e0["coords"][0]
                toca_rev = (e0["u"] is not None and e0["u"] in (e1["u"], e1["v"])) or (
                    _haversine_m(c0_ini[1], c0_ini[0], c1_ini[1], c1_ini[0]) <= 2.0
                    or _haversine_m(c0_ini[1], c0_ini[0], c1_end[1], c1_end[0]) <= 2.0
                )
                if toca_rev:
                    seq_base[0] = _reverter_aresta(e0)
                    arestas_reorientadas += 1

        for idx, curr in enumerate(seq_base):
            if not rota_continua:
                rota_continua.append(curr)
                continue

            prev = rota_continua[-1]
            p_end_c = prev["coords"][-1]
            c_ini_c = curr["coords"][0]
            c_end_c = curr["coords"][-1]

            conectado_dir = (prev["v"] is not None and curr["u"] is not None and prev["v"] == curr["u"]) or (
                _haversine_m(p_end_c[1], p_end_c[0], c_ini_c[1], c_ini_c[0]) <= 2.0
            )
            if conectado_dir:
                rota_continua.append(curr)
                continue

            conectado_rev = (prev["v"] is not None and curr["v"] is not None and prev["v"] == curr["v"]) or (
                _haversine_m(p_end_c[1], p_end_c[0], c_end_c[1], c_end_c[0]) <= 2.0
            )
            if conectado_rev:
                rota_continua.append(_reverter_aresta(curr))
                arestas_reorientadas += 1
                continue

            # Há uma lacuna topológica entre prev e curr: tentar preencher pela malha viária
            lacunas_detectadas += 1
            u_src = prev["v"] if (grafo and prev["v"] in grafo["adj"]) else _no_mais_proximo_grafo(grafo, p_end_c)
            u_dst_norm = curr["u"] if (grafo and curr["u"] in grafo["adj"]) else _no_mais_proximo_grafo(grafo, c_ini_c)
            u_dst_rev = curr["v"] if (grafo and curr["v"] in grafo["adj"]) else _no_mais_proximo_grafo(grafo, c_end_c)

            cost_n, path_n = _dijkstra_malha(grafo, u_src, u_dst_norm)
            cost_r, path_r = _dijkstra_malha(grafo, u_src, u_dst_rev)

            usar_rev = False
            melhor_path = path_n
            if path_r is not None and (path_n is None or (cost_r + 1.0 < cost_n)):
                usar_rev = True
                melhor_path = path_r

            if melhor_path:
                lacunas_preenchidas += 1
                for step in melhor_path:
                    rota_continua.append(step)
                    arestas_desloc_inseridas += 1
            if usar_rev:
                rota_continua.append(_reverter_aresta(curr))
                arestas_reorientadas += 1
            else:
                rota_continua.append(curr)

        # Construir GeoPackage limpo e sequenciado (14 colunas padrão)
        rows_gpkg = []
        cum_len = 0.0
        cum_time = 0.0
        for idx, a in enumerate(rota_continua):
            len_m = float(a["length_m"])
            travel_s = len_m / (10.0 / 3.6)  # ~10 km/h coleta
            cum_len += len_m
            cum_time += travel_s
            rows_gpkg.append({
                "u": a["u"] if a["u"] is not None else 0,
                "v": a["v"] if a["v"] is not None else 0,
                "length": len_m,
                "travel_time_s": travel_s,
                "is_collection": bool(a["is_collection"]),
                "classification": a["classification"],
                "name": a["name"] if a["name"] else None,
                "highway": a["highway"] if a["highway"] else None,
                "seq_id": idx + 1,
                "order": idx,
                "cum_length_m": cum_len,
                "cum_time_s": cum_time,
                "cum_time_min": round(cum_time / 60.0, 1),
                "geometry": LineString(a["coords"]),
            })

        gdf_clean_wgs = gpd.GeoDataFrame(rows_gpkg, geometry="geometry", crs="EPSG:4326")
        target_crs = orig_crs if (orig_crs is not None) else "EPSG:31985"
        try:
            gdf_clean_proj = gdf_clean_wgs.to_crs(target_crs)
        except Exception:
            gdf_clean_proj = gdf_clean_wgs

        gpkg_out_path = os.path.join(tmpdir, f"{base_name}.gpkg")
        gdf_clean_proj.to_file(gpkg_out_path, layer=base_name, driver="GPKG")
        with open(gpkg_out_path, "rb") as fg:
            gpkg_corrigido_bytes = fg.read()

        # Etapa C: Agrupamento topológico contínuo em feições Shapefile (<= 200 m para COLETA/MANUAL)
        grupos_shp: List[Dict[str, Any]] = []
        grupo_atual: Optional[Dict[str, Any]] = None

        for a in rota_continua:
            c_list = a["coords"]
            if len(c_list) < 2:
                continue
            if grupo_atual is None:
                grupo_atual = {
                    "tipo": a["tipo"],
                    "coords": list(c_list),
                    "len_m": float(a["length_m"]),
                    "last_u": a["u"],
                    "last_v": a["v"],
                    "last_seg": (c_list[-2], c_list[-1]),
                }
                continue

            mesmo_tipo = grupo_atual["tipo"] == a["tipo"]
            fim_g = grupo_atual["coords"][-1]
            ini_a = c_list[0]
            conectado = (
                (grupo_atual["last_v"] is not None and a["u"] is not None and grupo_atual["last_v"] == a["u"])
                or _haversine_m(fim_g[1], fim_g[0], ini_a[1], ini_a[0]) <= 2.0
            )
            eh_uturn = (
                grupo_atual["last_u"] is not None
                and grupo_atual["last_v"] is not None
                and a["u"] == grupo_atual["last_v"]
                and a["v"] == grupo_atual["last_u"]
            )
            ang = _diff_angular_graus(grupo_atual["last_seg"][0], grupo_atual["last_seg"][1], c_list[0], c_list[1])
            ang_suave = ang <= 60.0
            limite_m = 200.0 if grupo_atual["tipo"] in ("COLETA", "MANUAL") else 1500.0
            cabe_len = (grupo_atual["len_m"] + float(a["length_m"])) <= limite_m

            if mesmo_tipo and conectado and (not eh_uturn) and ang_suave and cabe_len:
                if _haversine_m(fim_g[1], fim_g[0], ini_a[1], ini_a[0]) <= 1.0:
                    grupo_atual["coords"].extend(c_list[1:])
                else:
                    grupo_atual["coords"].extend(c_list)
                grupo_atual["len_m"] += float(a["length_m"])
                grupo_atual["last_u"] = a["u"]
                grupo_atual["last_v"] = a["v"]
                grupo_atual["last_seg"] = (c_list[-2], c_list[-1])
            else:
                grupos_shp.append(grupo_atual)
                grupo_atual = {
                    "tipo": a["tipo"],
                    "coords": list(c_list),
                    "len_m": float(a["length_m"]),
                    "last_u": a["u"],
                    "last_v": a["v"],
                    "last_seg": (c_list[-2], c_list[-1]),
                }

        if grupo_atual is not None:
            grupos_shp.append(grupo_atual)

        shp_rows = []
        geojson_features = []
        km_coleta = 0.0
        km_manual = 0.0
        km_desloc = 0.0

        for idx, g in enumerate(grupos_shp):
            seq_num = idx + 1
            tipo_g = g["tipo"]
            len_km = g["len_m"] / 1000.0
            if tipo_g == "DESLOCAMENTO":
                km_desloc += len_km
            elif tipo_g == "MANUAL":
                km_manual += len_km
            else:
                km_coleta += len_km

            props = {
                "ROTA": rota_nome_str,
                "APELIDO": apelido_str,
                "SETOR": setor_desc_str,
                "MACRO": macro_str,
                "SEQUENCIA": int(seq_num),
                "TIPO": tipo_g,
            }
            shp_rows.append({**props, "geometry": LineString(g["coords"])})
            geojson_features.append({
                "type": "Feature",
                "properties": props,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[round(float(x), 7), round(float(y), 7)] for x, y in g["coords"]],
                },
            })

        gdf_shp = gpd.GeoDataFrame(shp_rows, geometry="geometry", crs="EPSG:4326")
        shp_dir = os.path.join(tmpdir, "rotas")
        os.makedirs(shp_dir, exist_ok=True)
        shp_stem = f"Rota_Final_{base_name}"
        shp_path = os.path.join(shp_dir, f"{shp_stem}.shp")
        gdf_shp.to_file(shp_path, driver="ESRI Shapefile", encoding="ISO-8859-1")

        # Garantir .cpg e .prj exatos conforme padrão do rotas.zip
        with open(os.path.join(shp_dir, f"{shp_stem}.cpg"), "wb") as fc:
            fc.write(b"ISO-8859-1")
        with open(os.path.join(shp_dir, f"{shp_stem}.prj"), "wb") as fp:
            fp.write(WGS84_PRJ.encode("ascii"))

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("rotas/", b"")
            for ext in (".cpg", ".dbf", ".prj", ".shp", ".shx"):
                fpath = os.path.join(shp_dir, f"{shp_stem}{ext}")
                if os.path.isfile(fpath):
                    zf.write(fpath, arcname=f"rotas/{shp_stem}{ext}")

        zip_bytes = zip_buf.getvalue()
        zip_filename = f"Rota_Final_{base_name}.zip"

    return {
        "zip_bytes": zip_bytes,
        "zip_filename": zip_filename,
        "shp_stem": shp_stem,
        "gpkg_corrigido_bytes": gpkg_corrigido_bytes,
        "gpkg_filename": f"{base_name}.gpkg",
        "geojson_features": geojson_features,
        "resumo": {
            "arestas_gpkg_originais": len(gdf_orig),
            "arestas_rota_continua": len(rota_continua),
            "duplicatas_mao_dupla_removidas": descartadas_duplicatas,
            "novas_arestas_inseridas": novas_inseridas,
            "arestas_reorientadas": arestas_reorientadas,
            "lacunas_detectadas": lacunas_detectadas,
            "lacunas_preenchidas_malha": lacunas_preenchidas,
            "arestas_deslocamento_inseridas": arestas_desloc_inseridas,
            "trechos_shapefile": len(shp_rows),
            "km_coleta": round(km_coleta, 3),
            "km_manual": round(km_manual, 3),
            "km_deslocamento": round(km_desloc, 3),
            "km_total": round(km_coleta + km_manual + km_desloc, 3),
            "setor_dbf": setor_desc_str,
            "macro_dbf": macro_str,
            "rota_dbf": rota_nome_str,
            "apelido_dbf": apelido_str,
        },
    }


# ==============================================================================
# GERAÇÃO DE SHAPEFILE DE SETOR (RÉPLICA DE process_sectors)
# ==============================================================================
def _sector_safe_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, float) and math.isnan(val):
        return ""
    s = str(val).strip()
    return "" if s.lower() in ("nan", "none", "null") else s


def _sector_normalize_label(text: str) -> str:
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    s = re.sub(r"[^A-Z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _sector_apply_abbrev(text: str, table: Tuple[Tuple[str, str], ...]) -> str:
    out = _sector_normalize_label(text)
    for pat, repl in table:
        out = re.sub(pat, repl, out)
    return out.strip()


def _sector_area_to_label(area_name: str) -> str:
    parts = [p.strip() for p in re.split(r"\s+-\s+|\s*/\s*", str(area_name or "")) if p.strip()]
    return " - ".join(_sector_apply_abbrev(p, _SECTOR_AREA_ABBREV) for p in parts if p)


def _sector_load_malha_indexed(contrato_id: str) -> Optional[Tuple[gpd.GeoDataFrame, STRtree, List[Any]]]:
    cid = str(contrato_id or "").lower().strip()
    if not cid:
        return None
    with _MALHA_BAIRRO_LOCK:
        cached = _MALHA_BAIRRO_CACHE.get(cid)
        if cached is not None:
            return cached

    path = malha_rotas.arquivo_malha(cid, tipo="TRANSITAVEIS") or malha_rotas.arquivo_malha(cid, tipo="COMPLETA")
    if not path or not os.path.isfile(path):
        return None
    try:
        gdf = gpd.read_file(path)
    except Exception:
        return None
    if "BAIRRO" not in gdf.columns:
        return None
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:31985", allow_override=True)
    elif gdf.crs.to_epsg() != 31985:
        gdf = gdf.to_crs("EPSG:31985")
    for col in ("NU", "FCU", "AGLOM", "name", "highway", "BAIRRO"):
        if col not in gdf.columns:
            gdf[col] = ""
    geoms_list = list(gdf.geometry)
    tree = STRtree(geoms_list)
    bundle = (gdf, tree, geoms_list)
    with _MALHA_BAIRRO_LOCK:
        _MALHA_BAIRRO_CACHE[cid] = bundle
    return bundle


def _sector_compose_labels(enriched_segments: List[Dict[str, Any]]) -> List[str]:
    coll = [s for s in enriched_segments if s.get("is_collection")]
    if not coll:
        return []
    bairro_total: Dict[str, float] = defaultdict(float)
    bairro_rest_total: Dict[str, float] = defaultdict(float)
    bairro_rest_av: Dict[str, float] = defaultdict(float)
    sub_total: Dict[str, float] = defaultdict(float)
    sub_parent_b: Dict[str, Set[str]] = defaultdict(set)
    av_total: Dict[str, float] = defaultdict(float)
    av_bairro: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for seg in coll:
        ln = float(seg.get("length_m") or 0.0)
        b = seg.get("bairro") or ""
        subs = seg.get("subdivisions") or []
        nm = seg.get("name") or ""
        hw = str(seg.get("highway") or "").lower()
        is_av = (hw in _SECTOR_AVENUE_HW) or _sector_normalize_label(nm).startswith("AVENIDA ")

        if b:
            bairro_total[b] += ln
            if not subs:
                bairro_rest_total[b] += ln
                if is_av:
                    bairro_rest_av[b] += ln
        for sn, _ in subs:
            sub_total[sn] += ln
            if b:
                sub_parent_b[sn].add(b)
        if is_av and nm:
            av_total[nm] += ln
            if b:
                av_bairro[nm][b] += ln

    bairro_cands: Dict[str, float] = {}
    for b, total in bairro_total.items():
        rest = bairro_rest_total.get(b, 0.0)
        if total > 0 and (rest / total) < 0.1:
            continue
        if rest > 0 and (bairro_rest_av.get(b, 0.0) / rest) >= 0.9:
            continue
        if rest > 0:
            bairro_cands[b] = rest

    av_cands: Dict[str, float] = {}
    for nm, total in av_total.items():
        in_mixed = sum(av_bairro[nm].get(b, 0.0) for b in bairro_cands)
        if total > 0 and (in_mixed / total) >= 0.9:
            continue
        av_cands[nm] = total

    all_lens = list(bairro_cands.values()) + list(sub_total.values()) + list(av_cands.values())
    if not all_lens:
        return []
    thr = 0.15 * max(all_lens)

    princ_b = {b for b, v in bairro_cands.items() if v >= thr}
    princ_s = {s for s, v in sub_total.items() if v >= thr}
    princ_a = {a for a, v in av_cands.items() if v >= thr}

    covered_b: Set[str] = set()
    for s in princ_s:
        s_lbl = _sector_area_to_label(s)
        for pb in sub_parent_b.get(s, ()):
            b_lbl = _sector_area_to_label(pb)
            if b_lbl and re.search(rf"(^|\s){re.escape(b_lbl)}($|\s)", s_lbl):
                covered_b.add(pb)

    emitted: Set[str] = set()
    labels: List[str] = []
    for seg in sorted(coll, key=lambda x: x.get("seq_id", 0)):
        nm = seg.get("name") or ""
        if nm in princ_a:
            tok = _sector_apply_abbrev(nm, _SECTOR_STREET_ABBREV)
            if tok and tok not in emitted:
                emitted.add(tok)
                labels.append(tok)
        for sn, _ in (seg.get("subdivisions") or []):
            if sn in princ_s:
                tok = _sector_area_to_label(sn)
                if tok and tok not in emitted:
                    emitted.add(tok)
                    labels.append(tok)
        b = seg.get("bairro") or ""
        if b in princ_b and b not in covered_b:
            tok = _sector_area_to_label(b)
            if tok and tok not in emitted:
                emitted.add(tok)
                labels.append(tok)
    return labels


def _sector_build_full_name(contrato: str, nickname: str, labels: List[str]) -> str:
    nick_up = str(nickname or "").strip().upper()
    m = re.match(r"^([A-ZÀ-Ÿ]+)(?:\s+(\d+))?(?:\s+(.*))?$", nick_up)
    if m:
        prefix, num, raw_suf = m.group(1), m.group(2), m.group(3)
        head_parts = [f"{prefix} {num}" if num else prefix]
        if raw_suf:
            head_parts.append(re.sub(r"\s+", " ", raw_suf.strip()))
        head = f"{contrato} - {' '.join(head_parts)}"
    else:
        head = f"{contrato} - {nick_up}"

    seen: Set[str] = set()
    uniq: List[str] = []
    for lbl in labels:
        if lbl and lbl not in seen:
            seen.add(lbl)
            uniq.append(lbl)
    while uniq:
        cand = head + " - " + " - ".join(uniq)
        if len(cand) <= _SECTOR_NAME_MAX_LEN:
            return cand
        uniq.pop()
    return head[:_SECTOR_NAME_MAX_LEN]


def _sector_detectar_turno(contrato_id: str, setor_codigo: str) -> str:
    """Detecta turno (DIURNO/NOTURNO) olhando primeiro Rotas Planejadas e depois o prefixo."""
    cid = str(contrato_id or "").lower().strip()
    setor_norm = _norm_txt(setor_codigo)
    try:
        base_dir = malha_rotas.PASTA_ROTAS
        if os.path.isdir(base_dir):
            for entry in os.listdir(base_dir):
                eu = _norm_txt(entry)
                if not eu.startswith(_norm_txt(cid.replace("_", " "))):
                    continue
                rotas_dir = os.path.join(base_dir, entry, "rotas")
                if not os.path.isdir(rotas_dir):
                    continue
                for fn in os.listdir(rotas_dir):
                    if fn.lower().endswith(".gpkg") and _norm_txt(os.path.splitext(fn)[0]) == setor_norm:
                        if "NOTURN" in eu:
                            return "NOTURNO"
                        if "DIURN" in eu:
                            return "DIURNO"
    except Exception:
        pass
    pref_m = re.match(r"^(SC[A-Z]*)", setor_norm)
    pref = pref_m.group(1) if pref_m else ""
    if pref.endswith(("N", "V")) and not pref.startswith("SCDOM"):
        return "NOTURNO"
    return "DIURNO"


def _sector_format_frequency(setor_codigo: str, turno: str) -> str:
    nick = str(setor_codigo or "").strip().upper()
    m = re.match(r"^(SC[A-Z]*)(?:\s+(\d+))?(?:\s+(.*))?$", nick)
    days: Set[int] = set()
    if m:
        pref = m.group(1) or ""
        suffix = (m.group(3) or "").strip()
        if suffix:
            # Ranges "SEG A SEX"
            for mr in re.finditer(r"\b(SEG|TER|QUA|QUI|SEX|SAB|DOM)\s*A\s*(SEG|TER|QUA|QUI|SEX|SAB|DOM)\b", suffix):
                d1, d2 = _SECTOR_WEEKDAY_MAP[mr.group(1)], _SECTOR_WEEKDAY_MAP[mr.group(2)]
                for d in range(d1, d2 + 1):
                    days.add(d)
            cleaned = _SECTOR_ORDINAL_RE.sub("", re.sub(r"\b(SEG|TER|QUA|QUI|SEX|SAB|DOM)\s*A\s*(SEG|TER|QUA|QUI|SEX|SAB|DOM)\b", "", suffix))
            for md in _SECTOR_WEEKDAY_RE.finditer(cleaned):
                days.add(_SECTOR_WEEKDAY_MAP[md.group(1)])
            if not days:
                for md in _SECTOR_WEEKDAY_RE.finditer(suffix):
                    days.add(_SECTOR_WEEKDAY_MAP[md.group(1)])
        if not days:
            base = pref if pref.startswith(("SCDOM", "SCDIST")) else re.sub(r"[DNVM]$", "", pref)
            if base.startswith("SCDOM"):
                days = {6}
            elif base.startswith("SCD"):
                days = {0, 1, 2, 3, 4, 5}
            elif base.startswith("SCP"):
                days = {0, 2, 4}
            elif base.startswith("SCI"):
                days = {1, 3, 5}

    fs = frozenset(days)
    if fs == frozenset({0, 1, 2, 3, 4, 5}) or fs == frozenset({0, 1, 2, 3, 4, 5, 6}):
        return f"DIARIO - {turno}"
    if fs == frozenset({0, 2, 4}):
        return f"SEG/QUA/SEX - {turno}"
    if fs == frozenset({1, 3, 5}):
        return f"TER/QUI/SAB - {turno}"
    sd = sorted(days)
    if len(sd) == 1:
        d_str = "DOMINGO" if sd[0] == 6 else _SECTOR_CODE_TO_DAY[sd[0]]
        return f"{d_str} - {turno}"
    if len(sd) == 2:
        return f"{_SECTOR_CODE_TO_DAY[sd[0]]} E {_SECTOR_CODE_TO_DAY[sd[1]]} - {turno}"
    if len(sd) >= 3 and all(sd[i + 1] - sd[i] == 1 for i in range(len(sd) - 1)):
        return f"{_SECTOR_CODE_TO_DAY[sd[0]]} A {_SECTOR_CODE_TO_DAY[sd[-1]]} - {turno}"
    if sd:
        return "/".join(_SECTOR_CODE_TO_DAY[d] for d in sd) + f" - {turno}"
    return f"DIARIO - {turno}"


def _sector_cluster_segments(geoms: List[Any]) -> List[List[int]]:
    n = len(geoms)
    if n == 0:
        return []
    coords = np.empty((n, 2), dtype=float)
    for i, gm in enumerate(geoms):
        c = gm.interpolate(0.5, normalized=True)
        coords[i, 0], coords[i, 1] = c.x, c.y

    if n < 2:
        buffer_r = _SECTOR_CLUSTER_BUFFER_MIN
    else:
        k = min(_SECTOR_CLUSTER_BUFFER_KNN, n - 1)
        diffs = coords[:, np.newaxis, :] - coords[np.newaxis, :, :]
        dmat = np.sqrt(np.sum(diffs ** 2, axis=2))
        np.fill_diagonal(dmat, np.inf)
        knn_d = np.partition(dmat, k - 1, axis=1)[:, k - 1]
        med_nn = float(np.median(knn_d))
        buffer_r = float(np.clip(med_nn * _SECTOR_CLUSTER_BUFFER_MULT, _SECTOR_CLUSTER_BUFFER_MIN, _SECTOR_CLUSTER_BUFFER_MAX))

    merged = unary_union([gm.buffer(buffer_r) for gm in geoms])
    if isinstance(merged, Polygon):
        regions = [merged]
    elif isinstance(merged, MultiPolygon):
        regions = list(merged.geoms)
    else:
        regions = [g for g in getattr(merged, "geoms", []) if isinstance(g, Polygon)]

    prepped = [prep(r) for r in regions]
    clusters: List[List[int]] = [[] for _ in regions]
    for idx, gm in enumerate(geoms):
        c = gm.interpolate(0.5, normalized=True)
        assigned = False
        for ri, pr in enumerate(prepped):
            if pr.contains(c):
                clusters[ri].append(idx)
                assigned = True
                break
        if not assigned and regions:
            best_ri = min(range(len(regions)), key=lambda ri: regions[ri].exterior.distance(c))
            clusters[best_ri].append(idx)

    clusters = [cl for cl in clusters if cl]
    large = [cl for cl in clusters if len(cl) >= _SECTOR_MIN_ADAPTIVE_CLUSTER]
    small = [cl for cl in clusters if len(cl) < _SECTOR_MIN_ADAPTIVE_CLUSTER]
    if large and small:
        l_cx = [(sum(geoms[i].interpolate(0.5, normalized=True).x for i in cl) / len(cl),
                 sum(geoms[i].interpolate(0.5, normalized=True).y for i in cl) / len(cl)) for cl in large]
        res_map = {i: list(cl) for i, cl in enumerate(large)}
        for sc in small:
            sx = sum(geoms[i].interpolate(0.5, normalized=True).x for i in sc) / len(sc)
            sy = sum(geoms[i].interpolate(0.5, normalized=True).y for i in sc) / len(sc)
            best_li = min(range(len(large)), key=lambda li: math.hypot(sx - l_cx[li][0], sy - l_cx[li][1]))
            res_map[best_li].extend(sc)
        clusters = list(res_map.values())

    clusters.sort(key=lambda cl: (-len(cl), cl[0]))
    return clusters


def _sector_ensure_polygon(geom: Any) -> Optional[Any]:
    if geom is None or geom.is_empty:
        return None
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]
    if not polys:
        return None
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def _sector_draw(cluster_geoms: List[Any], style: str) -> Optional[Any]:
    buffered = [gm.buffer(_SECTOR_BUFFER_MARGIN, cap_style="round", join_style="round", resolution=_SECTOR_BUFFER_RES) for gm in cluster_geoms]
    base = _sector_ensure_polygon(unary_union(buffered))
    if base is None:
        return None
    if style != "streets":
        if isinstance(base, Polygon):
            base = Polygon(base.exterior)
        elif isinstance(base, MultiPolygon):
            base = MultiPolygon([Polygon(p.exterior) for p in base.geoms])
    simp = make_valid(base.simplify(_SECTOR_SIMPLIFY_TOL, preserve_topology=True))
    res = _sector_ensure_polygon(simp)
    if res is None:
        return None
    if isinstance(res, MultiPolygon):
        parts = [p for p in res.geoms if p.area >= 1.0]
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else MultiPolygon(parts)
    return res if res.area >= 1.0 else None


def gerar_shapefile_setor_zip(
    gpkg_bytes: bytes,
    contrato_id: str,
    setor_codigo: str,
    macro_setor: Optional[str] = None,
    setor_descricao_fallback: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Gera o Shapefile (.zip) do polígono de setor compatível com GIS Eyes (clientetvt4.exe)
    e padrão GARANHUNS_-_NOTURNO.zip (`Setor - <APELIDO>.shp` com UTF-8 e EPSG:4326).
    """
    cid = str(contrato_id or "").lower().strip()
    cid_key = cid.replace("_", " ")
    cfg = CONTRATOS_SETORES_CFG.get(cid) or CONTRATOS_SETORES_CFG.get(cid_key)
    contract_code, style, contract_name = cfg if cfg else ("000", "area", cid_key.upper() or "CONTRATO")

    base_nick = _safe_filename(setor_codigo, "SETOR")
    turno = _sector_detectar_turno(cid, base_nick)
    pasta_contrato = f"{contract_name} - {turno}"
    macro_str = (str(macro_setor or "").strip() or f"{contract_name} - DOMICILIAR").upper()

    with tempfile.TemporaryDirectory(prefix="gpkg_setor_") as tmpdir:
        in_gpkg = os.path.join(tmpdir, "input_rota.gpkg")
        with open(in_gpkg, "wb") as f:
            f.write(gpkg_bytes)
        gdf_orig = gpd.read_file(in_gpkg)
        if gdf_orig.empty:
            raise ValueError("Arquivo .gpkg está vazio.")

        if gdf_orig.crs is None:
            gdf_31985 = gdf_orig.set_crs("EPSG:31985", allow_override=True)
        elif gdf_orig.crs.to_epsg() != 31985:
            gdf_31985 = gdf_orig.to_crs("EPSG:31985")
        else:
            gdf_31985 = gdf_orig.copy()

        # Deduplicação por par (u, v) não dirigido e extração das linhas válidas
        seen_uv: Set[Tuple[int, int]] = set()
        geoms_31985: List[Any] = []
        seg_meta: List[Tuple[int, bool]] = []
        seq_counter = 0

        for _, row in gdf_31985.iterrows():
            gm = row.geometry
            if gm is None or gm.is_empty:
                continue
            u_i = _parse_int_or_none(row.get("u"))
            v_i = _parse_int_or_none(row.get("v"))
            if u_i is not None and v_i is not None:
                ek = (min(u_i, v_i), max(u_i, v_i))
                if ek in seen_uv:
                    continue
                seen_uv.add(ek)

            # Extrai LineString simples se for MultiLineString
            lines = list(gm.geoms) if gm.geom_type == "MultiLineString" else ([gm] if gm.geom_type == "LineString" else [])
            if not lines:
                continue
            line_gm = max(lines, key=lambda l: l.length) if len(lines) > 1 else lines[0]

            seq_counter += 1
            sq = _parse_int_or_none(row.get("seq_id"))
            is_col = _extrair_tipo_aresta(row.to_dict()) != "DESLOCAMENTO"

            geoms_31985.append(line_gm)
            seg_meta.append((sq if sq is not None else seq_counter, is_col))

        if not geoms_31985:
            raise ValueError("Nenhuma geometria de linha válida encontrada no .gpkg para gerar o setor.")

        # Enriquecimento com Bairros e composição do campo SETOR completo
        setor_full = ""
        bairros_labels: List[str] = []
        malha_bundle = _sector_load_malha_indexed(cid)
        if malha_bundle is not None:
            gdf_malha, tree, geoms_malha = malha_bundle
            enriched_segs: List[Dict[str, Any]] = []
            for idx_g, (gm, (sq_v, is_c)) in enumerate(zip(geoms_31985, seg_meta)):
                mid = gm.interpolate(0.5, normalized=True)
                n_idx = int(tree.nearest(mid))
                mrow = gdf_malha.iloc[n_idx]
                subs = []
                for col_s in ("NU", "FCU", "AGLOM"):
                    sv = _sector_safe_str(mrow.get(col_s))
                    if sv:
                        subs.append((sv, col_s))
                enriched_segs.append({
                    "seq_id": sq_v,
                    "is_collection": is_c,
                    "bairro": _sector_safe_str(mrow.get("BAIRRO")),
                    "subdivisions": subs,
                    "name": _sector_safe_str(mrow.get("name")),
                    "highway": _sector_safe_str(mrow.get("highway")).lower(),
                    "length_m": float(gm.length),
                })
            bairros_labels = _sector_compose_labels(enriched_segs)
            if bairros_labels:
                setor_full = _sector_build_full_name(contract_name, base_nick, bairros_labels)

        if not setor_full:
            setor_full = str(setor_descricao_fallback or f"{contract_name} - {base_nick}").upper()

        m_code = re.search(r"(\d+)\s*$", base_nick) or re.search(r"(\d+)", base_nick)
        num_code = m_code.group(1) if m_code else "0"
        identifier = f"{contract_code}{num_code}"
        frequency = _sector_format_frequency(base_nick, turno)

        clusters = _sector_cluster_segments(geoms_31985)
        n_clusters = len(clusters)
        feat_rows: List[Dict[str, Any]] = []
        for cl_idx, cl_indices in enumerate(clusters, start=1):
            poly = _sector_draw([geoms_31985[i] for i in cl_indices], style)
            if poly is None:
                continue
            if isinstance(poly, Polygon):
                poly = MultiPolygon([poly])
            feat_rows.append({
                "SETOR": setor_full,
                "APELIDO": base_nick,
                "IDENTIF": identifier,
                "FREQUENCIA": frequency,
                "MACRO": macro_str,
                "contract": contract_name,
                "code": num_code,
                "cluster": int(cl_idx),
                "n_clusters": int(n_clusters),
                "n_segs": int(len(cl_indices)),
                "geometry": poly,
            })

        if not feat_rows:
            raise ValueError("Nenhuma geometria poligonal pôde ser gerada para o setor.")

        gdf_out = gpd.GeoDataFrame(feat_rows, geometry="geometry", crs="EPSG:31985").to_crs("EPSG:4326")
        out_dir = os.path.join(tmpdir, pasta_contrato)
        os.makedirs(out_dir, exist_ok=True)
        shp_stem = f"Setor - {base_nick}"
        shp_path = os.path.join(out_dir, f"{shp_stem}.shp")
        gdf_out.to_file(shp_path, driver="ESRI Shapefile", encoding="utf-8")

        with open(os.path.join(out_dir, f"{shp_stem}.cpg"), "wb") as fc:
            fc.write(b"UTF-8")
        with open(os.path.join(out_dir, f"{shp_stem}.prj"), "wb") as fp:
            fp.write(WGS84_PRJ.encode("ascii"))

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{pasta_contrato}/", b"")
            for ext in (".cpg", ".dbf", ".prj", ".shp", ".shx"):
                fpath = os.path.join(out_dir, f"{shp_stem}{ext}")
                if os.path.isfile(fpath):
                    zf.write(fpath, arcname=f"{pasta_contrato}/{shp_stem}{ext}")

        return {
            "zip_bytes": zip_buf.getvalue(),
            "zip_filename": f"Setor - {base_nick}.zip",
            "shp_stem": shp_stem,
            "pasta_contrato": pasta_contrato,
            "resumo": {
                "setor": setor_full,
                "apelido": base_nick,
                "identif": identifier,
                "frequencia": frequency,
                "macro": macro_str,
                "estilo": style,
                "turno": turno,
                "clusters": n_clusters,
                "segmentos": len(geoms_31985),
                "feicoes_shp": len(feat_rows),
                "operacao_giseyes": "DOMICILIAR F2" if cid == "cuiaba" else "DOMICILIAR F1",
            },
        }


# ==============================================================================
# AUTOMAÇÃO DESKTOP DO GIS EYES (clientetvt4.exe)
# ==============================================================================
def localizar_clientetvt4_exe() -> Optional[str]:
    """Localiza o executável clientetvt4.exe instalado nesta máquina Windows."""
    candidatos = [
        os.getenv("GISEYES_EXE_PATH", "").strip(),
        os.path.join(os.path.expanduser("~"), "Downloads", "GisEyes", "GisEyes", "clientetvt4.exe"),
        os.path.join(os.path.expanduser("~"), "Downloads", "FS - DIE", "FS - DIE", "GisEyes", "clientetvt4.exe"),
        r"C:\GisEyes\clientetvt4.exe",
        r"C:\Program Files (x86)\GisEyes\clientetvt4.exe",
        r"C:\Program Files\GisEyes\clientetvt4.exe",
    ]
    for p in candidatos:
        if p and os.path.isfile(p):
            return p
    for pattern in (
        os.path.join(os.path.expanduser("~"), "Downloads", "**", "clientetvt4.exe"),
        os.path.join(os.path.expanduser("~"), "Desktop", "**", "clientetvt4.exe"),
    ):
        for p in glob.iglob(pattern, recursive=True):
            if os.path.isfile(p):
                return p
    return None


def atualizar_setor_clientetvt4(
    zip_setor_bytes: bytes,
    contrato_id: str,
    empresa: str = GISEYES_DEFAULT_EMPRESA,
    usuario: str = GISEYES_DEFAULT_USER,
    senha: str = GISEYES_DEFAULT_PASS,
    log_cb: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    Abre o clientetvt4.exe (GIS Eyes), faz login, abre Cadastros -> GIS -> Importador,
    carrega o Shapefile do setor, mapeia os combos (DOMICILIAR F2 para Cuiabá, DOMICILIAR F1
    para as demais cidades) e executa Processar para substituir o setor no servidor.
    """
    def _log(msg: str) -> None:
        if log_cb:
            log_cb(msg)

    if os.name != "nt" or sys.platform != "win32":
        raise RuntimeError(
            "O GIS Eyes (clientetvt4.exe) é um aplicativo Windows desktop e não pode ser executado neste servidor. "
            "Baixe o arquivo Shape Setor (.zip) gerado para importar localmente."
        )

    exe_path = localizar_clientetvt4_exe()
    if not exe_path:
        raise FileNotFoundError(
            "Executável 'clientetvt4.exe' não encontrado no PC. "
            "Configure GISEYES_EXE_PATH ou mantenha o GIS Eyes na pasta Downloads."
        )

    cid = str(contrato_id or "").lower().strip()
    operacao_nome = "DOMICILIAR F2" if cid == "cuiaba" else "DOMICILIAR F1"
    operacao_idx = 14 if cid == "cuiaba" else 0

    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
    WM_SETTEXT = 0x000C
    WM_GETTEXT = 0x000D
    WM_GETTEXTLENGTH = 0x000E
    WM_CHAR = 0x0102
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_MOUSEMOVE = 0x0200
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_COMMAND = 0x0111
    VK_UP = 0x26
    VK_DOWN = 0x28

    def _get_text(hwnd: int) -> str:
        ln = user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0)
        b = ctypes.create_unicode_buffer(max(256, ln + 2))
        user32.SendMessageW(hwnd, WM_GETTEXT, len(b), ctypes.byref(b))
        return b.value

    def _get_cls(hwnd: int) -> str:
        b = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, b, 256)
        return b.value

    def _get_rect(hwnd: int) -> Tuple[int, int, int, int]:
        rc = _wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rc))
        return (rc.left, rc.top, rc.right, rc.bottom)

    def _click(hwnd: int, x: Optional[int] = None, y: Optional[int] = None) -> None:
        rc = _get_rect(hwnd)
        cx = max(4, (rc[2] - rc[0]) // 2) if x is None else x
        cy = max(4, (rc[3] - rc[1]) // 2) if y is None else y
        lparam = (cy << 16) | (cx & 0xFFFF)
        user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lparam)
        time.sleep(0.05)
        user32.PostMessageW(hwnd, WM_LBUTTONDOWN, 1, lparam)
        time.sleep(0.05)
        user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)

    def _type_edit(hwnd: int, text: str) -> None:
        user32.SendMessageW(hwnd, WM_SETTEXT, 0, ctypes.c_wchar_p(""))
        for _ in range(30):
            user32.SendMessageW(hwnd, WM_CHAR, 0x08, 0)
        for ch in text:
            user32.SendMessageW(hwnd, WM_CHAR, ord(ch), 0)

    def _find_popup_datalist(pid: int) -> Tuple[Optional[int], Optional[Tuple[int, int, int, int]]]:
        found: List[Tuple[int, Tuple[int, int, int, int]]] = []
        def _cb(hwnd, _lp):
            p = _wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value == pid and user32.IsWindowVisible(hwnd) and _get_cls(hwnd) == "TbsPopupDataList":
                found.append((hwnd, _get_rect(hwnd)))
            return True
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        return found[0] if found else (None, None)

    def _select_combo_index(pid: int, combo_hwnd: int, target_index: int) -> None:
        rc = _get_rect(combo_hwnd)
        w, h = rc[2] - rc[0], rc[3] - rc[1]
        _click(combo_hwnd, w - 10, h // 2)
        time.sleep(0.30)
        pop_hwnd, _ = _find_popup_datalist(pid)
        if pop_hwnd:
            _click(pop_hwnd, 40, 10)
            time.sleep(0.20)
        for _ in range(35):
            user32.SendMessageW(combo_hwnd, WM_KEYDOWN, VK_UP, 0)
            user32.SendMessageW(combo_hwnd, WM_KEYUP, VK_UP, 0)
        for _ in range(target_index):
            user32.SendMessageW(combo_hwnd, WM_KEYDOWN, VK_DOWN, 0)
            user32.SendMessageW(combo_hwnd, WM_KEYUP, VK_DOWN, 0)
        time.sleep(0.15)

    with tempfile.TemporaryDirectory(prefix="giseves_imp_") as tmpdir:
        with zipfile.ZipFile(io.BytesIO(zip_setor_bytes)) as zf:
            zf.extractall(tmpdir)
        shp_files = list(glob.iglob(os.path.join(tmpdir, "**", "*.shp"), recursive=True))
        if not shp_files:
            raise RuntimeError("Nenhum arquivo .shp encontrado dentro do ZIP do setor.")
        shp_path = shp_files[0]
        gdf_meta = gpd.read_file(shp_path, rows=1)
        dbf_cols = [c for c in gdf_meta.columns if c != "geometry"]

        def _col_rev_idx(col_name: str) -> int:
            if col_name not in dbf_cols:
                raise RuntimeError(f"Coluna '{col_name}' ausente no Shapefile de setor gerado.")
            return len(dbf_cols) - 1 - dbf_cols.index(col_name)

        _log(f"🖥️ Abrindo GIS Eyes ({os.path.basename(exe_path)}) e conectando como {empresa}/{usuario}...")
        proc: Optional[subprocess.Popen] = None
        try:
            proc = subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path))
            pid = proc.pid

            # 1. Aguardar tela de Login (TfrmLoginSistema)
            login_hwnd = None
            main_hwnd = None
            for _ in range(40):
                if proc.poll() is not None:
                    raise RuntimeError("clientetvt4.exe encerrou inesperadamente durante a abertura.")
                time.sleep(1.0)
                def _find_login(hwnd, _lp):
                    nonlocal login_hwnd, main_hwnd
                    p = _wt.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                    if p.value != pid:
                        return True
                    cls = _get_cls(hwnd)
                    if cls == "TfrmPricipalClienteTVT4" and user32.IsWindowVisible(hwnd):
                        main_hwnd = hwnd
                    elif cls == "TfrmLoginSistema" and user32.IsWindowVisible(hwnd):
                        login_hwnd = hwnd
                    return True
                user32.EnumWindows(WNDENUMPROC(_find_login), 0)
                if login_hwnd and main_hwnd:
                    break

            if not login_hwnd or not main_hwnd:
                raise RuntimeError("Tempo esgotado aguardando a tela de login (Entrada de Usuários) do GIS Eyes.")

            l_children: List[Tuple[int, str, str, Tuple[int, int, int, int]]] = []
            def _enum_login(ch, _lp):
                l_children.append((ch, _get_cls(ch), _get_text(ch), _get_rect(ch)))
                return True
            user32.EnumChildWindows(login_hwnd, WNDENUMPROC(_enum_login), 0)
            edits = sorted([c for c in l_children if c[1] == "TbsSkinEdit"], key=lambda x: x[3][1])
            pwds = [c for c in l_children if c[1] == "TbsSkinPasswordEdit"]
            ok_btns = [c for c in l_children if c[1] == "TbsSkinButton" and "OK" in c[2].upper()]
            if len(edits) < 2 or not pwds or not ok_btns:
                raise RuntimeError("Controles da tela de login do GIS Eyes não reconhecidos.")

            _type_edit(edits[0][0], empresa)
            _type_edit(edits[1][0], usuario)
            _type_edit(pwds[0][0], senha)
            _click(ok_btns[0][0])

            # 2. Aguardar conclusão do login e sumiço de TfrmAguarde
            logged_ok = False
            for _ in range(30):
                time.sleep(1.0)
                st_texts: List[str] = []
                def _enum_st(ch, _lp):
                    if _get_cls(ch) == "TbsSkinStatusPanel":
                        st_texts.append(_get_text(ch))
                    return True
                user32.EnumChildWindows(main_hwnd, WNDENUMPROC(_enum_st), 0)
                aguarde_or_login = False
                err_popups: List[Tuple[int, str]] = []
                def _chk_wait(hwnd, _lp):
                    nonlocal aguarde_or_login
                    p = _wt.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                    if p.value != pid or not user32.IsWindowVisible(hwnd):
                        return True
                    cls = _get_cls(hwnd)
                    if cls in ("TfrmAguarde", "TfrmLoginSistema"):
                        aguarde_or_login = True
                    elif cls == "TbsMessageForm":
                        err_popups.append((hwnd, _get_text(hwnd)))
                    return True
                user32.EnumWindows(WNDENUMPROC(_chk_wait), 0)
                if err_popups:
                    raise RuntimeError(f"GIS Eyes exibiu alerta no login: {err_popups[0][1]}")
                if not aguarde_or_login and any("LOCAR" in t.upper() for t in st_texts):
                    logged_ok = True
                    break

            if not logged_ok:
                raise RuntimeError("Login no GIS Eyes não completou dentro do tempo limite.")

            _log(f"🔓 Login no GIS Eyes concluído ({empresa}/{usuario}). Abrindo Cadastros -> GIS -> Importador...")
            time.sleep(0.8)

            # 3. Abrir Cadastros -> GIS -> Importador
            menubar_hwnd = None
            def _find_menu(ch, _lp):
                nonlocal menubar_hwnd
                if _get_cls(ch) == "TbsSkinMainMenuBar":
                    menubar_hwnd = ch
                return True
            user32.EnumChildWindows(main_hwnd, WNDENUMPROC(_find_menu), 0)
            if not menubar_hwnd:
                raise RuntimeError("Barra de menu principal do GIS Eyes não localizada.")

            _click(menubar_hwnd, 70, 10)
            time.sleep(0.7)
            pop1 = None
            pop1_rc = None
            def _find_pop1(hwnd, _lp):
                nonlocal pop1, pop1_rc
                p = _wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                if p.value == pid and user32.IsWindowVisible(hwnd) and _get_cls(hwnd) == "TbsSkinPopupWindow":
                    pop1 = hwnd
                    pop1_rc = _get_rect(hwnd)
                return True
            user32.EnumWindows(WNDENUMPROC(_find_pop1), 0)
            if not pop1 or not pop1_rc:
                raise RuntimeError("Menu 'Cadastros' do GIS Eyes não abriu.")

            _click(pop1, (pop1_rc[2] - pop1_rc[0]) // 2, int((pop1_rc[3] - pop1_rc[1]) * 5.5 / 8))
            time.sleep(0.7)

            pop2 = None
            pop2_rc = None
            def _find_pop2(hwnd, _lp):
                nonlocal pop2, pop2_rc
                p = _wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                if p.value == pid and user32.IsWindowVisible(hwnd) and _get_cls(hwnd) == "TbsSkinPopupWindow" and hwnd != pop1:
                    pop2 = hwnd
                    pop2_rc = _get_rect(hwnd)
                return True
            user32.EnumWindows(WNDENUMPROC(_find_pop2), 0)
            if not pop2 or not pop2_rc:
                raise RuntimeError("Submenu 'GIS' do GIS Eyes não abriu.")

            _click(pop2, (pop2_rc[2] - pop2_rc[0]) // 2, 14)

            # Aguardar TfrmImportadorShape e botões Abrir/Processar
            btn_abrir = None
            btn_processar = None
            for _ in range(15):
                time.sleep(0.5)
                def _find_imp(ch, _lp):
                    nonlocal btn_abrir, btn_processar
                    if user32.IsWindowVisible(ch) and _get_cls(ch) == "TbsSkinButton":
                        t = _get_text(ch)
                        if t == "Abrir":
                            btn_abrir = ch
                        elif t == "Processar":
                            btn_processar = ch
                    return True
                user32.EnumChildWindows(main_hwnd, WNDENUMPROC(_find_imp), 0)
                if btn_abrir and btn_processar:
                    break
            if not btn_abrir or not btn_processar:
                raise RuntimeError("Tela do Importador GIS não abriu corretamente.")

            # 4. Clicar em Abrir e enviar o .shp no diálogo #32770
            _click(btn_abrir)
            dlg_hwnd = None
            for _ in range(15):
                time.sleep(0.5)
                def _find_dlg(hwnd, _lp):
                    nonlocal dlg_hwnd
                    p = _wt.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                    if p.value == pid and user32.IsWindowVisible(hwnd) and _get_cls(hwnd) == "#32770" and _get_text(hwnd) == "Abrir":
                        dlg_hwnd = hwnd
                    return True
                user32.EnumWindows(WNDENUMPROC(_find_dlg), 0)
                if dlg_hwnd:
                    break
            if not dlg_hwnd:
                raise RuntimeError("Diálogo 'Abrir' de arquivo do Windows não apareceu.")

            edit_1148 = None
            btn_1 = None
            def _enum_dlg(ch, _lp):
                nonlocal edit_1148, btn_1
                cid_ctrl = user32.GetDlgCtrlID(ch)
                ccls = _get_cls(ch)
                if ccls == "Edit" and cid_ctrl == 1148:
                    edit_1148 = ch
                elif ccls == "Button" and cid_ctrl == 1:
                    btn_1 = ch
                return True
            user32.EnumChildWindows(dlg_hwnd, WNDENUMPROC(_enum_dlg), 0)
            if not edit_1148 or not btn_1:
                raise RuntimeError("Campo de caminho do arquivo no diálogo Abrir não encontrado.")

            user32.SendMessageW(edit_1148, WM_SETTEXT, 0, ctypes.c_wchar_p(shp_path))
            time.sleep(0.25)
            user32.SendMessageW(dlg_hwnd, WM_COMMAND, 1, btn_1)

            # Aguardar diálogo fechar e Importador carregar o shapefile
            loaded_shp = False
            for _ in range(20):
                time.sleep(0.5)
                if not user32.IsWindowVisible(dlg_hwnd):
                    titles: List[str] = []
                    def _chk_imp(ch, _lp):
                        if _get_cls(ch) == "TfrmImportadorShape":
                            titles.append(_get_text(ch))
                        return True
                    user32.EnumChildWindows(main_hwnd, WNDENUMPROC(_chk_imp), 0)
                    if any(".shp" in t.lower() for t in titles):
                        loaded_shp = True
                        break
            if not loaded_shp:
                time.sleep(1.5)

            # 5. Mapear os 8 TbsSkinDBLookupComboBox da aba Setores
            combos: List[Tuple[int, Tuple[int, int, int, int]]] = []
            def _enum_combos(ch, _lp):
                if user32.IsWindowVisible(ch) and _get_cls(ch) == "TbsSkinDBLookupComboBox":
                    combos.append((ch, _get_rect(ch)))
                return True
            user32.EnumChildWindows(main_hwnd, WNDENUMPROC(_enum_combos), 0)
            combos.sort(key=lambda x: x[1][1])
            if len(combos) < 8:
                raise RuntimeError(f"Esperados 8 combos na aba Setores, encontrados {len(combos)}.")

            _log(f"⚙️ Mapeando colunas do setor e configurando Operação '{operacao_nome}' no GIS Eyes...")
            _select_combo_index(pid, combos[0][0], _col_rev_idx("SETOR"))       # SETOR
            _select_combo_index(pid, combos[1][0], 0)                            # TIPO DO SETOR -> DOMICILIAR
            _select_combo_index(pid, combos[2][0], _col_rev_idx("APELIDO"))     # APELIDO
            _select_combo_index(pid, combos[3][0], _col_rev_idx("IDENTIF"))     # IDENTIFICADOR
            _select_combo_index(pid, combos[4][0], _col_rev_idx("FREQUENCIA"))  # FREQUENCIA
            _select_combo_index(pid, combos[5][0], _col_rev_idx("MACRO"))       # MACRO SETOR
            _select_combo_index(pid, combos[7][0], operacao_idx)                 # OPERACAO

            # 6. Clicar em Processar e confirmar os diálogos TbsMessageForm
            _log("🚀 Clicando em Processar no GIS Eyes para gravar o setor na base Inlog...")
            _click(btn_processar)

            mensagens_confirmadas: List[str] = []
            deadline = time.time() + 60.0
            while time.time() < deadline:
                time.sleep(0.8)
                msgs_now: List[Tuple[int, str, Optional[int]]] = []
                def _find_msgs(hwnd, _lp):
                    p = _wt.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                    if p.value == pid and user32.IsWindowVisible(hwnd) and _get_cls(hwnd) == "TbsMessageForm":
                        ok_btn = None
                        def _mb(ch, _lp2):
                            nonlocal ok_btn
                            if _get_cls(ch) == "TbsSkinButton" and _get_text(ch).upper() in ("OK", "SIM"):
                                ok_btn = ch
                            return True
                        user32.EnumChildWindows(hwnd, WNDENUMPROC(_mb), 0)
                        msgs_now.append((hwnd, _get_text(hwnd), ok_btn))
                    return True
                user32.EnumWindows(WNDENUMPROC(_find_msgs), 0)
                if msgs_now:
                    for m_hwnd, m_title, ok_btn in msgs_now:
                        mensagens_confirmadas.append(m_title or "Informação")
                        _log(f"💬 Diálogo GIS Eyes: '{m_title}' -> clicando OK...")
                        if ok_btn:
                            _click(ok_btn)
                        else:
                            user32.PostMessageW(m_hwnd, WM_KEYDOWN, 0x0D, 0)
                    time.sleep(1.5)
                    # Verifica se ainda há algum outro diálogo pertencente a este processo
                    extra_open: List[int] = []
                    def _chk_extra(hw, _lp):
                        p2 = _wt.DWORD()
                        user32.GetWindowThreadProcessId(hw, ctypes.byref(p2))
                        if p2.value == pid and user32.IsWindowVisible(hw) and _get_cls(hw) == "TbsMessageForm":
                            extra_open.append(hw)
                        return True
                    user32.EnumWindows(WNDENUMPROC(_chk_extra), 0)
                    if not extra_open:
                        break

            if not mensagens_confirmadas:
                raise RuntimeError("Tempo esgotado aguardando confirmação final após clicar em Processar no GIS Eyes.")

            _log(f"✅ Setor atualizado com sucesso no GIS Eyes (Operação: {operacao_nome})!")
            return {
                "sucesso": True,
                "operacao": operacao_nome,
                "shp_arquivo": os.path.basename(shp_path),
                "mensagens": mensagens_confirmadas,
            }
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=4)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass


# ==============================================================================
# RÉPLICA EXATA DA LÓGICA GEOMÉTRICA DO ANGULARJS (index.controller.js + utils)
# ==============================================================================
def _is_inside_area_single(points: List[Dict[str, float]], lng: float, lat: float) -> bool:
    """Réplica exata de utilsNavegacao.IsInsideArea."""
    if not points:
        return False
    c = False
    j = len(points) - 1
    for i in range(len(points)):
        pi = points[i]
        pj = points[j]
        if ((pi["Latitude"] <= lat < pj["Latitude"]) or (pj["Latitude"] <= lat < pi["Latitude"])) and (
            lng < (pj["Longitude"] - pi["Longitude"]) * (lat - pi["Latitude"]) / (pj["Latitude"] - pi["Latitude"]) + pi["Longitude"]
        ):
            c = not c
        j = i
    return c


def _is_inside_setor_areas(areas_setor: List[Dict[str, Any]], lng: float, lat: float) -> bool:
    if not areas_setor:
        return True
    polys: List[List[Dict[str, float]]] = []
    for area in areas_setor:
        coords = area.get("Coordenadas") or []
        if coords:
            polys.append([
                {
                    "Latitude": float(item["Latitude"]) / 1_000_000.0,
                    "Longitude": float(item["Longitude"]) / 1_000_000.0,
                }
                for item in coords
            ])
    if not polys:
        return True
    return any(_is_inside_area_single(poly, lng, lat) for poly in polys)


def _compute_angle_true_north(lat_a: float, lng_a: float, lat_b: float, lng_b: float) -> float:
    rad_lat_a = math.radians(lat_a)
    rad_lng_a = math.radians(lng_a)
    rad_lat_b = math.radians(lat_b)
    rad_lng_b = math.radians(lng_b) - rad_lng_a
    calc = math.degrees(
        math.atan2(
            math.sin(rad_lng_b) * math.cos(rad_lat_b),
            math.cos(rad_lat_a) * math.sin(rad_lat_b)
            - math.sin(rad_lat_a) * math.cos(rad_lat_b) * math.cos(rad_lng_b),
        )
    )
    result = ((calc + 180.0) % 360.0 + 360.0) % 360.0 - 180.0
    return result if result > 0 else result + 360.0


def _compute_angle_between_three_points(
    prev_pt: Dict[str, float], curr_pt: Dict[str, float], next_pt: Dict[str, float]
) -> float:
    ab = math.hypot(curr_pt["Lat"] - prev_pt["Lat"], curr_pt["Lng"] - prev_pt["Lng"])
    bc = math.hypot(curr_pt["Lat"] - next_pt["Lat"], curr_pt["Lng"] - next_pt["Lng"])
    ac = math.hypot(next_pt["Lat"] - prev_pt["Lat"], next_pt["Lng"] - prev_pt["Lng"])
    denom = 2.0 * bc * ab
    if denom <= 0:
        return float("nan")
    cos_val = max(-1.0, min(1.0, (bc * bc + ab * ab - ac * ac) / denom))
    return math.degrees(math.acos(cos_val))


def _is_to_right(nxt: float, prv: float) -> bool:
    diff = nxt - prv
    if diff < -180.0:
        diff += 360.0
    elif diff > 180.0:
        diff -= 360.0
    return diff > 0.0


def _get_directions(
    prev_pt: Optional[Dict[str, float]],
    curr_pt: Dict[str, float],
    next_pt: Optional[Dict[str, float]],
) -> int:
    """Réplica exata de utilsNavegacao.GetDiretions (códigos 0..10)."""
    if next_pt and prev_pt:
        ang_prev = _compute_angle_true_north(prev_pt["Lat"], prev_pt["Lng"], curr_pt["Lat"], curr_pt["Lng"])
        ang_next = _compute_angle_true_north(curr_pt["Lat"], curr_pt["Lng"], next_pt["Lat"], next_pt["Lng"])
        angle = _compute_angle_between_three_points(prev_pt, curr_pt, next_pt)
        if not math.isnan(angle):
            right = _is_to_right(ang_next, ang_prev)
            if 0.0 < angle <= 10.0:
                return 8 if right else 9
            if 10.0 < angle <= 45.0:
                return 6 if right else 7
            if 45.0 < angle <= 135.0:
                return 2 if right else 3
            if 135.0 < angle <= 170.0:
                return 4 if right else 5
            if 170.0 < angle <= 180.0:
                return 1
        return 1
    if prev_pt and curr_pt and not next_pt:
        return 10
    return 0


def construir_trechos_inlog(
    geojson_features: List[Dict[str, Any]],
    areas_setor: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Replica exatamente `getTrechos` -> `getTodosPontos` -> `getPontosUnicos`
    -> `atualizaInstrucaoDistanciaESequencia` de `index.controller.js`.
    """
    features_ord = sorted(geojson_features, key=lambda f: int(f["properties"]["SEQUENCIA"]))
    pontos_totais: List[Dict[str, Any]] = []
    tipo_por_trecho: Dict[int, str] = {}

    for feat in features_ord:
        props = feat["properties"]
        seq = int(props["SEQUENCIA"])
        tipo_txt = str(props.get("TIPO") or "COLETA").strip().upper()
        if tipo_txt == "DESLOCAMENTO":
            tipo_codigo = "d"
        elif tipo_txt == "MANUAL":
            tipo_codigo = "m"
        else:
            tipo_codigo = "c"
        tipo_por_trecho[seq] = tipo_codigo

        coords = feat["geometry"]["coordinates"]
        for z, coord in enumerate(coords):
            lng = float(coord[0])
            lat = float(coord[1])
            pontos_totais.append({
                "Longitude": lng * 1_000_000.0,
                "Latitude": lat * 1_000_000.0,
                "Lng": lng,
                "Lat": lat,
                "Trecho": seq,
                "SequenciaPonto": z,
                "PacmanAtivo": False,
                "PossuiGeoCode": False,
                "DentroSetor": _is_inside_setor_areas(areas_setor, lng, lat),
            })

    pontos_totais.sort(key=lambda p: (p["Trecho"], p["SequenciaPonto"]))

    # getPontosUnicos: remove pontos consecutivos com mesma Lat e Lng
    pontos_unicos: List[Dict[str, Any]] = []
    for p in pontos_totais:
        if pontos_unicos:
            ant = pontos_unicos[-1]
            if ant["Lat"] == p["Lat"] and ant["Lng"] == p["Lng"]:
                continue
        pontos_unicos.append(p)

    # atualizaInstrucaoDistanciaESequencia
    n_pts = len(pontos_unicos)
    for z, pt in enumerate(pontos_unicos):
        prev_pt = pontos_unicos[z - 1] if z > 0 else None
        next_pt = pontos_unicos[z + 1] if (z + 1) < n_pts else None
        pt["Instrucao"] = _get_directions(prev_pt, pt, next_pt)
        pt["Distancia"] = int(_haversine_m(prev_pt["Lat"], prev_pt["Lng"], pt["Lat"], pt["Lng"])) if prev_pt else 0
        pt["Sequencia"] = z + 1

    pontos_por_trecho: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for pt in pontos_unicos:
        pontos_por_trecho[pt["Trecho"]].append(pt)

    trechos: List[Dict[str, Any]] = []
    for feat in features_ord:
        seq = int(feat["properties"]["SEQUENCIA"])
        pts = sorted(pontos_por_trecho.get(seq, []), key=lambda x: x["SequenciaPonto"])
        if not pts:
            continue
        trechos.append({
            "Sequencia": seq,
            "Tipo": tipo_por_trecho[seq],
            "Pontos": pts,
        })
    return trechos


# ==============================================================================
# CLIENTE DE AUTOMAÇÃO WEB INLOG EDITOR ROTAS
# ==============================================================================
class InlogEditorRotasClient:
    def __init__(self, log_cb: Optional[Callable[[str], None]] = None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        })
        self.api_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "x-api-key": INLOG_EDITOR_API_KEY,
            "Accept": "application/json, text/plain, */*",
        }
        self._log_cb = log_cb
        self.vinculos: List[Tuple[str, str]] = []

    def _log(self, msg: str) -> None:
        if self._log_cb:
            self._log_cb(msg)

    def _api_get(self, action: str, params: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Any:
        url = INLOG_EDITOR_BASE + action.lstrip("/")
        resp = self.session.get(url, params=params, headers=self.api_headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def _api_post(self, action: str, payload: Any, timeout: int = 60) -> Any:
        url = INLOG_EDITOR_BASE + action.lstrip("/")
        resp = self.session.post(url, json=payload, headers=self.api_headers, timeout=timeout)
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            return {"raw_text": resp.text}

    def login(self, username: str = INLOG_DEFAULT_USER, password: str = INLOG_DEFAULT_PASS) -> List[Tuple[str, str]]:
        self._log(f"🔐 Acessando Autenticador Inlog e autenticando usuário '{username}'...")
        r_get = self.session.get(INLOG_LOGIN_URL, timeout=20)
        r_get.raise_for_status()

        m_tok = re.search(
            r'name="__RequestVerificationToken"[^>]*value="([^"]+)"|value="([^"]+)"[^>]*name="__RequestVerificationToken"',
            r_get.text,
        )
        if not m_tok:
            raise RuntimeError("Token anti-CSRF (__RequestVerificationToken) não encontrado na página de login da Inlog.")
        token_csrf = m_tok.group(1) or m_tok.group(2)

        r_post = self.session.post(
            INLOG_LOGIN_URL,
            data={
                "__RequestVerificationToken": token_csrf,
                "UserName": username,
                "Password": password,
                "entrar": "Entrar",
                "UsuarioAutenticador": "",
                "UrlAutenticador": "",
                "EsqueceuSenha": "False",
            },
            timeout=25,
        )
        r_post.raise_for_status()
        if "Account/Login" in r_post.url and "AtivarVinculo" not in r_post.text:
            raise RuntimeError("Falha ao autenticar no portal Inlog com o usuário/senha configurados.")

        vinculos = re.findall(r"AtivarVinculo\?c=(\d+)[^>]*>\s*([^<\r\n]+)", r_post.text)
        if not vinculos:
            r_home = self.session.get(INLOG_AUTH_HOME_URL, timeout=20)
            vinculos = re.findall(r"AtivarVinculo\?c=(\d+)[^>]*>\s*([^<\r\n]+)", r_home.text)

        self.vinculos = [(cid_num, lbl.strip()) for cid_num, lbl in vinculos]
        if not self.vinculos:
            raise RuntimeError("Login efetuado, porém nenhum vínculo de cidade/perfil foi encontrado no menu do usuário.")

        self._log(f"✅ Login realizado com sucesso ({len(self.vinculos)} cidades/vínculos disponíveis no perfil).")
        return self.vinculos

    def ativar_vinculo_cidade(self, contrato_id: str) -> Tuple[str, str]:
        cid = _norm_txt(contrato_id).lower()
        aliases = CONTRATO_VINCULO_ALIASES.get(cid, [_norm_txt(contrato_id)])

        escolhido: Optional[Tuple[str, str]] = None
        for v_id, v_label in self.vinculos:
            lbl_norm = _norm_txt(v_label)
            parte_cidade = lbl_norm.split("/")[0].strip()
            for al in aliases:
                if parte_cidade == al or parte_cidade.startswith(al + " ") or al in parte_cidade:
                    escolhido = (v_id, v_label)
                    break
            if escolhido:
                break

        if not escolhido:
            disponiveis = ", ".join(lbl for _, lbl in self.vinculos)
            raise RuntimeError(f"Cidade '{contrato_id}' não encontrada nos vínculos do perfil Inlog ({disponiveis}).")

        v_id, v_label = escolhido
        self._log(f"🏙️ Selecionando perfil/cidade na Inlog: {v_label} (vínculo c={v_id})...")
        r_vinc = self.session.get(INLOG_VINCULO_URL, params={"c": v_id}, timeout=25)
        r_vinc.raise_for_status()

        # Abre a página do EditorRotas para consolidar cookies/sessão do módulo
        r_ed = self.session.get(INLOG_EDITOR_BASE, timeout=25)
        r_ed.raise_for_status()
        self._log(f"✅ Cidade '{v_label}' ativada e Editor de Rotas carregado.")
        return v_id, v_label

    def buscar_dados_setor(
        self,
        contrato_id: str,
        setor_codigo: str,
        setor_descricao_preferida: Optional[str] = None,
    ) -> Dict[str, Any]:
        setores = self._api_get("EditorRotas/CarregarSetores")
        if not isinstance(setores, list):
            raise RuntimeError("Resposta inesperada de CarregarSetores na Inlog.")

        alvo_desc_norm = _norm_txt(setor_descricao_preferida)
        alvo_cod_norm = _norm_txt(setor_codigo)
        cod_canon = malha_rotas.extrair_codigo_setor(setor_codigo) or malha_rotas.extrair_codigo_setor(setor_descricao_preferida)

        setor_match = None
        # 1. Casamento exato pela descrição completa
        if alvo_desc_norm:
            for s in setores:
                if _norm_txt(s.get("Descricao")) == alvo_desc_norm:
                    setor_match = s
                    break
        # 2. Casamento exato pelo apelido/código
        if not setor_match and alvo_cod_norm:
            for s in setores:
                if _norm_txt(s.get("Apelido")) == alvo_cod_norm or _norm_txt(s.get("Descricao")) == alvo_cod_norm:
                    setor_match = s
                    break
        # 3. Casamento canônico (PREFIXO, NUMERO)
        if not setor_match and cod_canon:
            for s in setores:
                sc_desc = malha_rotas.extrair_codigo_setor(s.get("Descricao"))
                sc_ap = malha_rotas.extrair_codigo_setor(s.get("Apelido"))
                if sc_desc == cod_canon or sc_ap == cod_canon:
                    setor_match = s
                    break
        # 4. Fallback substring
        if not setor_match and alvo_cod_norm:
            for s in setores:
                if alvo_cod_norm in _norm_txt(s.get("Descricao")) or alvo_cod_norm in _norm_txt(s.get("Apelido")):
                    setor_match = s
                    break

        if not setor_match:
            raise RuntimeError(
                f"Setor '{setor_descricao_preferida or setor_codigo}' não encontrado no EditorRotas da cidade '{contrato_id}'."
            )

        codigo_setor = int(setor_match["Codigo"])
        desc_oficial = str(setor_match.get("Descricao") or "").strip()
        apelido_oficial = str(setor_match.get("Apelido") or "").strip()

        areas = self._api_get(
            "EditorRotas/CarregarFormaSetorPorDescricao",
            params={"descricaoSetor": desc_oficial, "codigoRota": 0, "temp": "true"},
        )
        if not isinstance(areas, list):
            areas = []

        # Consulta grid para descobrir MacroSetor associado ao setor
        macro_desc = ""
        try:
            grid_res = self._api_get(
                "EditorRotas/CarregarSetoresUsuario",
                params={"skip": 0, "take": 300, "colOrdenada": "Setor", "revertSort": "false"},
            )
            for it in (grid_res.get("Grid") or {}).get("Data") or []:
                if int(it.get("CodigoSetor") or 0) == codigo_setor:
                    macro_desc = str(it.get("MacroSetor") or "").strip()
                    break
        except Exception:
            pass

        macros_lista = self._api_get("EditorRotas/CarregarMacroSetores") or []
        tipos_lista = self._api_get("EditorRotas/CarregarTiposSetor") or []

        self._log(
            f"📍 Setor localizado na Inlog: '{desc_oficial}' (CodigoSetor={codigo_setor}, Apelido='{apelido_oficial}', Macro='{macro_desc or '—'}')."
        )
        return {
            "codigo_setor": codigo_setor,
            "descricao": desc_oficial,
            "apelido": apelido_oficial,
            "macro_setor": macro_desc,
            "areas": areas,
            "macros_disponiveis": macros_lista if isinstance(macros_lista, list) else [],
            "tipos_disponiveis": tipos_lista if isinstance(tipos_lista, list) else [],
        }

    def listar_rotas_setor(self, codigo_setor: int) -> List[Dict[str, Any]]:
        res = self._api_get("EditorRotas/CarregarRotasSetor", params={"codigoSetor": codigo_setor})
        if not isinstance(res, list):
            return []
        return [r for r in res if str(r.get("Apagado") or "F").upper() != "T"]

    def inativar_e_excluir_rotas_antigas(self, codigo_setor: int) -> List[Dict[str, Any]]:
        rotas = self.listar_rotas_setor(codigo_setor)
        if not rotas:
            self._log("ℹ️ Nenhuma rota antiga encontrada no setor para inativar/excluir.")
            return []

        historico: List[Dict[str, Any]] = []
        for r in rotas:
            cod_r = int(r["Codigo"])
            rotas_temp = bool(r.get("RotasTemp", False))
            ativo = str(r.get("Ativo") or "F").upper() == "T"
            desc = str(r.get("Descricao") or r.get("Apelido") or cod_r)

            if ativo or not rotas_temp:
                self._log(f"⏸️ Inativando rota antiga '{desc}' (CodigoRota={cod_r}, RotasTemp={rotas_temp})...")
                res_inat = self._api_post(
                    "EditorRotas/ExcluirSetorRota",
                    {"codigoSetorRota": cod_r, "rotasTemp": rotas_temp},
                )
                historico.append({"acao": "inativar", "codigo_rota": cod_r, "resposta": res_inat})
                if not res_inat.get("Sucesso", True):
                    raise RuntimeError(
                        f"Falha ao inativar rota antiga {cod_r}: {res_inat.get('MensagemErro') or res_inat}"
                    )
                time.sleep(0.8)

        # Segunda passagem: excluir definitivamente rotas inativas/temporárias restantes
        rotas_apos = self.listar_rotas_setor(codigo_setor)
        for r in rotas_apos:
            cod_r = int(r["Codigo"])
            rotas_temp = bool(r.get("RotasTemp", False))
            desc = str(r.get("Descricao") or r.get("Apelido") or cod_r)
            self._log(f"🗑️ Excluindo definitivamente rota '{desc}' (CodigoRota={cod_r}, RotasTemp={rotas_temp})...")
            res_exc = self._api_post(
                "EditorRotas/ExcluirSetorRota",
                {"codigoSetorRota": cod_r, "rotasTemp": rotas_temp},
            )
            historico.append({"acao": "excluir", "codigo_rota": cod_r, "resposta": res_exc})
            if not res_exc.get("Sucesso", True):
                raise RuntimeError(
                    f"Falha ao excluir rota antiga {cod_r}: {res_exc.get('MensagemErro') or res_exc}"
                )
            time.sleep(0.8)

        restantes = self.listar_rotas_setor(codigo_setor)
        if restantes:
            self._log(f"⚠️ Aviso: ainda constam {len(restantes)} registro(s) no setor após exclusão: {[x.get('Codigo') for x in restantes]}")
        else:
            self._log("✅ Rota antiga inativada e excluída do setor com sucesso.")
        return historico

    def importar_rota_shapefile(
        self,
        codigo_setor: int,
        areas_setor: List[Dict[str, Any]],
        geojson_features: List[Dict[str, Any]],
        rota_nome: str,
        apelido_rota: str,
        codigo_tipo_setor: Optional[int] = None,
        codigo_macro_setor: Optional[int] = None,
    ) -> int:
        trechos = construir_trechos_inlog(geojson_features, areas_setor)
        if not trechos:
            raise RuntimeError("Nenhum trecho válido gerado para importação na Inlog.")

        total_pts = sum(len(t["Pontos"]) for t in trechos)
        self._log(f"📤 Importando nova rota '{rota_nome}' na Inlog ({len(trechos)} trechos, {total_pts} pontos)...")

        rotas_antes = {int(r["Codigo"]) for r in self.listar_rotas_setor(codigo_setor)}

        payload = {
            "Descricao": rota_nome,
            "Apelido": apelido_rota,
            "CodigoSetor": codigo_setor,
            "CodigoTipoSetor": codigo_tipo_setor,
            "CodigoMacroSetor": codigo_macro_setor,
            "RotasTemp": True,
            "Trechos": trechos,
        }
        res = self._api_post("EditorRotas/CriarRotaCompleta", payload, timeout=90)
        if isinstance(res, dict) and res.get("Sucesso") is False:
            raise RuntimeError(f"Inlog rejeitou CriarRotaCompleta: {res.get('MensagemErro') or res}")

        # Aguardar a criação assíncrona no servidor da Inlog
        nova_rota_cod = None
        for _ in range(45):
            rotas_agora = self.listar_rotas_setor(codigo_setor)
            novas = [r for r in rotas_agora if int(r["Codigo"]) not in rotas_antes]
            if novas:
                nova_rota_cod = int(novas[-1]["Codigo"])
                break
            time.sleep(2.0)

        if nova_rota_cod is None:
            raise RuntimeError("Tempo esgotado aguardando a nova rota aparecer em CarregarRotasSetor após CriarRotaCompleta.")

        self._log(f"✅ Nova rota temporária criada na Inlog (CodigoRota={nova_rota_cod}). Aguardando processamento...")
        return nova_rota_cod

    def ativar_rota_mobile(self, codigo_rota: int, ativo: bool = True) -> bool:
        """Marca ou desmarca a coluna Mobile da rota no EditorRotas da Inlog."""
        res_mob = self._api_get(
            "EditorRotas/AtivarDesativarRotaMobile",
            params={"codigoRota": int(codigo_rota), "ativo": "true" if ativo else "false"},
        )
        ok = bool(isinstance(res_mob, dict) and res_mob.get("Sucesso"))
        if ok:
            self._log(f"📱 Coluna Mobile marcada com sucesso para a rota {codigo_rota}.")
        else:
            msg = res_mob.get("MensagemErro") if isinstance(res_mob, dict) else res_mob
            self._log(f"⚠️ Aviso ao marcar coluna Mobile da rota {codigo_rota}: {msg}")
        return ok

    def aguardar_e_ativar_nova_rota(
        self,
        codigo_setor: int,
        codigo_rota_nova: int,
        data_ativacao_str: Optional[str] = None,
    ) -> Dict[str, Any]:
        info: Dict[str, Any] = {}
        rota_temp_flag = True

        # 1. Aguardar conclusão do processamento de geocode/criação
        for _ in range(45):
            rotas_setor = self.listar_rotas_setor(codigo_setor)
            match_r = next((r for r in rotas_setor if int(r["Codigo"]) == codigo_rota_nova), None)
            if match_r:
                rota_temp_flag = bool(match_r.get("RotasTemp", True))
            info = self._api_get(
                "EditorRotas/CarregarInformacaoRotaPorCodigoRota",
                params={"codigoRota": codigo_rota_nova, "rotaTemp": str(rota_temp_flag).lower()},
            )
            aguardando = str(info.get("AguardandoProcessamento") or "F").upper() == "T"
            etapa_enum = int(info.get("EtapaEnum") or 0)
            status_txt = str(info.get("Status") or "")
            if not aguardando and (etapa_enum >= 3 or status_txt in ("Completa", "Incompleta", "Ativa")):
                break
            time.sleep(2.0)

        etapa_enum = int(info.get("EtapaEnum") or 0)
        estado = str(info.get("Etapa") or info.get("Estado") or "").upper()
        self._log(
            f"🔎 Estado da nova rota {codigo_rota_nova}: Status='{info.get('Status')}', Geocode='{info.get('Geocode')}', "
            f"Etapa={etapa_enum} ({estado}), KmTotal='{info.get('KmTotal')}'."
        )

        # 2. Se parou na Etapa 1 ou 2, avança até a Etapa 3 (Status 'Completa' / Etapa 'I')
        tentativas_avanco = 0
        while etapa_enum < 3 and estado in ("T", "S", "G") and tentativas_avanco < 3:
            tentativas_avanco += 1
            self._log(f"⏭️ Avançando etapa da rota {codigo_rota_nova} (etapa atual {etapa_enum}/{estado})...")
            res_av = self._api_post("EditorRotas/AvancarEtapa", {"codigoRota": codigo_rota_nova})
            if isinstance(res_av, dict) and res_av.get("Sucesso") is False:
                raise RuntimeError(f"Erro ao avançar etapa da rota {codigo_rota_nova}: {res_av.get('MensagemErro') or res_av}")
            time.sleep(2.0)
            info = self._api_get(
                "EditorRotas/CarregarInformacaoRotaPorCodigoRota",
                params={"codigoRota": codigo_rota_nova, "rotaTemp": "true"},
            )
            etapa_enum = int(info.get("EtapaEnum") or 0)
            estado = str(info.get("Etapa") or info.get("Estado") or "").upper()
            self._log(
                f"🔎 Pós-avanço rota {codigo_rota_nova}: Status='{info.get('Status')}', Geocode='{info.get('Geocode')}', "
                f"Etapa={etapa_enum} ({estado})."
            )

        # 3. Ativar a nova rota na Etapa 3
        data_ag = data_ativacao_str or datetime.now().strftime("%d/%m/%Y")
        self._log(f"⚡ Ativando nova rota {codigo_rota_nova} no setor (dataAgendamento='{data_ag}')...")
        res_at = self._api_post(
            "Etapa3/AtivarSetorRota",
            {"codigoRota": codigo_rota_nova, "rotaTemp": True, "dataAgendamento": data_ag},
        )
        if isinstance(res_at, dict) and res_at.get("Sucesso") is False:
            raise RuntimeError(f"Erro ao ativar rota {codigo_rota_nova}: {res_at.get('MensagemErro') or res_at}")

        # 4. Aguardar a ativação consolidar em background (Inlog migra RotasTemp=True para novo CodigoRota permanente com RotasTemp=False e Ativo='T')
        info_final = info
        for _ in range(30):
            rotas_setor = self.listar_rotas_setor(codigo_setor)
            match_r = next((r for r in rotas_setor if int(r["Codigo"]) == codigo_rota_nova), None)
            if not match_r:
                # Ao ativar, a Inlog cria o registro permanente em RotasTemp=False com novo CodigoRota e remove o temporário
                match_r = next(
                    (
                        r for r in rotas_setor
                        if str(r.get("Ativo") or "F").upper() == "T" and not bool(r.get("RotasTemp", True))
                    ),
                    None,
                )
            if match_r:
                cod_eff = int(match_r["Codigo"])
                rt_now = bool(match_r.get("RotasTemp", False))
                ativo_now = str(match_r.get("Ativo") or "F").upper() == "T"
                info_final = self._api_get(
                    "EditorRotas/CarregarInformacaoRotaPorCodigoRota",
                    params={"codigoRota": cod_eff, "rotaTemp": str(rt_now).lower()},
                )
                if ativo_now or str(info_final.get("Status") or "") == "Ativa":
                    info_final["CodigoRota"] = cod_eff
                    info_final["RotaTemp"] = rt_now
                    info_final["Mobile"] = self.ativar_rota_mobile(cod_eff, True)
                    self._log(
                        f"🏁 Rota {cod_eff} ATIVA na Inlog! "
                        f"Total={info_final.get('KmTotal')}, Coleta={info_final.get('KmColeta')}, Desloc.={info_final.get('KmDeslocamento')}."
                    )
                    return info_final
            time.sleep(2.0)

        self._log(f"⚠️ Ativação enviada; status atual retornado pela Inlog: {info_final.get('Status')} (Ativo={info_final.get('Ativo')}).")
        return info_final


# ==============================================================================
# GERENCIADOR DE JOBS ASSÍNCRONOS PARA UI
# ==============================================================================
def _atualizar_job(job_id: str, **kwargs: Any) -> None:
    with _PUBLICACAO_LOCK:
        job = _PUBLICACAO_JOBS.get(job_id)
        if not job:
            return
        job.update(kwargs)


def _log_job(job_id: str, mensagem: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    with _PUBLICACAO_LOCK:
        job = _PUBLICACAO_JOBS.get(job_id)
        if not job:
            return
        job["logs"].append(f"[{ts}] {mensagem}")
        job["etapa_atual"] = mensagem


def iniciar_job_publicacao_inlog(
    gpkg_bytes: bytes,
    contrato_id: str,
    setor_codigo: str,
    setor_descricao: Optional[str] = None,
    macro_setor: Optional[str] = None,
    rota_nome: str = "ROTA SUB",
    apelido_rota: str = "ROTA SUB",
    atualizar_local: bool = True,
    publicar_inlog: bool = True,
    atualizar_setor_gis: bool = True,
    sol_id: Optional[str] = None,
    usuario_inlog: Optional[str] = None,
    senha_inlog: Optional[str] = None,
    atualizar_local_fn: Optional[Callable[[str, str, bytes], Dict[str, Any]]] = None,
    concluir_solicitacao_fn: Optional[Callable[[str, str], None]] = None,
) -> Dict[str, Any]:
    job_id = f"pub_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
    with _PUBLICACAO_LOCK:
        # Limpeza leve de jobs antigos
        if len(_PUBLICACAO_JOBS) > 25:
            antigos = sorted(_PUBLICACAO_JOBS.keys())[:-20]
            for k in antigos:
                _PUBLICACAO_JOBS.pop(k, None)
        _PUBLICACAO_JOBS[job_id] = {
            "job_id": job_id,
            "status": "EM_ANDAMENTO",
            "progresso": 5,
            "etapa_atual": "Iniciando processamento do arquivo .gpkg...",
            "logs": [],
            "erro": None,
            "resultado": None,
            "zip_bytes": None,
            "zip_filename": None,
            "zip_setor_bytes": None,
            "zip_setor_filename": None,
        }

    def _runner():
        try:
            _log_job(job_id, f"📦 Lendo .gpkg e convertendo para Shapefile da Rota ({setor_codigo})...")
            conv = converter_gpkg_para_shapefile_zip(
                gpkg_bytes=gpkg_bytes,
                contrato_id=contrato_id,
                setor_codigo=setor_codigo,
                setor_descricao=setor_descricao or f"{contrato_id.upper()} - {setor_codigo}",
                macro_setor=macro_setor or f"{contrato_id.upper()} - DOMICILIAR",
                rota_nome=rota_nome,
                apelido_rota=apelido_rota,
                nome_arquivo_base=setor_codigo,
            )
            resumo_conv = conv["resumo"]
            _atualizar_job(
                job_id,
                progresso=18,
                zip_bytes=conv["zip_bytes"],
                zip_filename=conv["zip_filename"],
            )
            _log_job(
                job_id,
                f"✅ Shapefile Rota '{conv['zip_filename']}' gerado: {resumo_conv['trechos_shapefile']} trechos "
                f"(Coleta {resumo_conv['km_coleta']} km + Desloc. {resumo_conv['km_deslocamento']} km; "
                f"{resumo_conv['lacunas_preenchidas_malha']}/{resumo_conv['lacunas_detectadas']} lacunas roteirizadas).",
            )

            _log_job(job_id, f"🗺️ Gerando Shapefile do Setor ({setor_codigo})...")
            conv_setor = gerar_shapefile_setor_zip(
                gpkg_bytes=conv["gpkg_corrigido_bytes"],
                contrato_id=contrato_id,
                setor_codigo=setor_codigo,
                macro_setor=macro_setor,
                setor_descricao_fallback=setor_descricao,
            )
            resumo_setor = conv_setor["resumo"]
            _atualizar_job(
                job_id,
                progresso=28,
                zip_setor_bytes=conv_setor["zip_bytes"],
                zip_setor_filename=conv_setor["zip_filename"],
            )
            _log_job(
                job_id,
                f"✅ Shapefile Setor '{conv_setor['zip_filename']}' gerado: "
                f"SETOR='{resumo_setor['setor']}' | IDENTIF={resumo_setor['identif']} | "
                f"FREQ='{resumo_setor['frequencia']}' ({resumo_setor['clusters']} cluster(s)).",
            )

            res_gis = None
            if atualizar_setor_gis:
                res_gis = atualizar_setor_clientetvt4(
                    zip_setor_bytes=conv_setor["zip_bytes"],
                    contrato_id=contrato_id,
                    log_cb=lambda m: _log_job(job_id, m),
                )
                _atualizar_job(job_id, progresso=48)

            res_local = None
            if atualizar_local and atualizar_local_fn:
                _log_job(job_id, f"💾 Atualizando arquivo .gpkg em 'Rotas Planejadas' e reconstruindo malha de '{contrato_id}'...")
                res_local = atualizar_local_fn(contrato_id, setor_codigo, conv["gpkg_corrigido_bytes"])
                _atualizar_job(job_id, progresso=60)
                _log_job(
                    job_id,
                    f"✅ Sistema LC atualizado: {os.path.basename(res_local.get('arquivo_destino') or '')} salvo "
                    f"({res_local.get('n_segmentos', 0)} trechos na cidade).",
                )

            res_inlog = None
            if publicar_inlog:
                client = InlogEditorRotasClient(log_cb=lambda m: _log_job(job_id, m))
                client.login(usuario_inlog or INLOG_DEFAULT_USER, senha_inlog or INLOG_DEFAULT_PASS)
                _atualizar_job(job_id, progresso=66)

                v_id, v_label = client.ativar_vinculo_cidade(contrato_id)
                _atualizar_job(job_id, progresso=72)

                dados_setor = client.buscar_dados_setor(contrato_id, setor_codigo, setor_descricao)
                codigo_setor = dados_setor["codigo_setor"]
                _atualizar_job(job_id, progresso=78)

                hist_exc = client.inativar_e_excluir_rotas_antigas(codigo_setor)
                _atualizar_job(job_id, progresso=84)

                cod_macro = None
                macro_alvo = _norm_txt(macro_setor or dados_setor.get("macro_setor"))
                if macro_alvo:
                    for m_it in dados_setor.get("macros_disponiveis") or []:
                        if _norm_txt(m_it.get("Descricao")) == macro_alvo:
                            cod_macro = int(m_it["Codigo"])
                            break

                nova_rota_id = client.importar_rota_shapefile(
                    codigo_setor=codigo_setor,
                    areas_setor=dados_setor["areas"],
                    geojson_features=conv["geojson_features"],
                    rota_nome=rota_nome,
                    apelido_rota=apelido_rota,
                    codigo_macro_setor=cod_macro,
                )
                _atualizar_job(job_id, progresso=90)

                info_final = client.aguardar_e_ativar_nova_rota(codigo_setor, nova_rota_id)
                codigo_rota_ativa = int((info_final or {}).get("CodigoRota") or nova_rota_id)
                rt_final = bool((info_final or {}).get("RotaTemp", False))
                _atualizar_job(job_id, progresso=96)

                res_inlog = {
                    "vinculo_id": v_id,
                    "vinculo_label": v_label,
                    "codigo_setor": codigo_setor,
                    "descricao_setor": dados_setor["descricao"],
                    "rotas_antigas_removidas": hist_exc,
                    "codigo_rota_nova": codigo_rota_ativa,
                    "rota_inlog": info_final,
                    "url_editor": f"{INLOG_EDITOR_BASE}Etapa3?CodigoSetor={codigo_setor}&CodigoRota={codigo_rota_ativa}&rt={'true' if rt_final else 'false'}",
                }

            if sol_id and concluir_solicitacao_fn:
                nota_auto = (
                    f"Rota {setor_codigo} convertida (.shp) e publicada automaticamente em {datetime.now().strftime('%d/%m/%Y %H:%M')}. "
                    f"Trechos SHP: {resumo_conv['trechos_shapefile']} (Coleta {resumo_conv['km_coleta']} km | Desloc. {resumo_conv['km_deslocamento']} km)."
                )
                if res_gis:
                    nota_auto += f" GIS Eyes atualizado ({res_gis.get('operacao')})."
                if res_inlog:
                    nota_auto += f" Inlog CodigoRota={res_inlog['codigo_rota_nova']} (Status: {(res_inlog['rota_inlog'] or {}).get('Status', 'Ativa')})."
                concluir_solicitacao_fn(sol_id, nota_auto)

            _atualizar_job(
                job_id,
                status="CONCLUIDO",
                progresso=100,
                etapa_atual="Processo concluído com sucesso!",
                resultado={
                    "sucesso": True,
                    "zip_filename": conv["zip_filename"],
                    "zip_setor_filename": conv_setor["zip_filename"],
                    "conversao": resumo_conv,
                    "setor_shp": resumo_setor,
                    "giseyes": res_gis,
                    "sistema_local": res_local,
                    "inlog": res_inlog,
                },
            )
            _log_job(job_id, "🎉 Fluxo completo finalizado com sucesso!")
        except Exception as exc:
            _log_job(job_id, f"❌ Erro: {exc}")
            _atualizar_job(
                job_id,
                status="ERRO",
                erro=str(exc),
                etapa_atual=f"Erro: {exc}",
            )

    threading.Thread(target=_runner, daemon=True).start()
    return {"job_id": job_id, "status": "EM_ANDAMENTO"}


def obter_status_job_publicacao(job_id: str) -> Optional[Dict[str, Any]]:
    with _PUBLICACAO_LOCK:
        job = _PUBLICACAO_JOBS.get(job_id)
        if not job:
            return None
        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "progresso": job["progresso"],
            "etapa_atual": job["etapa_atual"],
            "logs": list(job["logs"]),
            "erro": job["erro"],
            "resultado": job["resultado"],
            "tem_zip": job["zip_bytes"] is not None,
            "zip_filename": job["zip_filename"],
            "tem_zip_setor": job.get("zip_setor_bytes") is not None,
            "zip_setor_filename": job.get("zip_setor_filename"),
        }


def obter_zip_job_publicacao(job_id: str) -> Optional[Tuple[bytes, str]]:
    with _PUBLICACAO_LOCK:
        job = _PUBLICACAO_JOBS.get(job_id)
        if not job or not job["zip_bytes"]:
            return None
        return job["zip_bytes"], job["zip_filename"] or "rotas.zip"


def obter_zip_setor_job_publicacao(job_id: str) -> Optional[Tuple[bytes, str]]:
    with _PUBLICACAO_LOCK:
        job = _PUBLICACAO_JOBS.get(job_id)
        if not job or not job.get("zip_setor_bytes"):
            return None
        return job["zip_setor_bytes"], job.get("zip_setor_filename") or "setor.zip"
