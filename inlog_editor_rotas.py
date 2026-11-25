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

import heapq
import io
import math
import os
import re
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
import pandas as pd
import pyogrio
import requests
from shapely.geometry import LineString

import malha_rotas

INLOG_LOGIN_URL = "https://locar.inlog.com.br/Rastreamento/Apresentacao/Autenticador/Account/Login"
INLOG_AUTH_HOME_URL = "https://locar.inlog.com.br/Rastreamento/Apresentacao/Autenticador/"
INLOG_VINCULO_URL = "https://locar.inlog.com.br/Rastreamento/Apresentacao/Account/AtivarVinculo"
INLOG_EDITOR_BASE = "https://locar.inlog.com.br/Rastreamento/Apresentacao/EditorRotas/"
INLOG_EDITOR_API_KEY = "0ff6134bec254f9ba1f96d15082d20f2"

INLOG_DEFAULT_USER = os.getenv("INLOG_WEB_USER", "carlos.dorth").strip()
INLOG_DEFAULT_PASS = os.getenv("INLOG_WEB_PASS", "Locar1234").strip()

WGS84_PRJ = (
    'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
)

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

_MALHA_GRAFO_CACHE: Dict[str, Dict[str, Any]] = {}
_MALHA_GRAFO_LOCK = threading.Lock()

_PUBLICACAO_JOBS: Dict[str, Dict[str, Any]] = {}
_PUBLICACAO_LOCK = threading.Lock()


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
        }

    def _runner():
        try:
            _log_job(job_id, f"📦 Lendo .gpkg e convertendo para Shapefile ({setor_codigo})...")
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
                progresso=25,
                zip_bytes=conv["zip_bytes"],
                zip_filename=conv["zip_filename"],
            )
            _log_job(
                job_id,
                f"✅ Shapefile '{conv['zip_filename']}' gerado: {resumo_conv['trechos_shapefile']} trechos "
                f"(Coleta {resumo_conv['km_coleta']} km + Desloc. {resumo_conv['km_deslocamento']} km; "
                f"{resumo_conv['lacunas_preenchidas_malha']}/{resumo_conv['lacunas_detectadas']} lacunas roteirizadas).",
            )

            res_local = None
            if atualizar_local and atualizar_local_fn:
                _log_job(job_id, f"💾 Atualizando arquivo .gpkg em 'Rotas Planejadas' e reconstruindo malha de '{contrato_id}'...")
                res_local = atualizar_local_fn(contrato_id, setor_codigo, conv["gpkg_corrigido_bytes"])
                _atualizar_job(job_id, progresso=42)
                _log_job(
                    job_id,
                    f"✅ Sistema LC atualizado: {os.path.basename(res_local.get('arquivo_destino') or '')} salvo "
                    f"({res_local.get('n_segmentos', 0)} trechos na cidade).",
                )

            res_inlog = None
            if publicar_inlog:
                client = InlogEditorRotasClient(log_cb=lambda m: _log_job(job_id, m))
                client.login(usuario_inlog or INLOG_DEFAULT_USER, senha_inlog or INLOG_DEFAULT_PASS)
                _atualizar_job(job_id, progresso=52)

                v_id, v_label = client.ativar_vinculo_cidade(contrato_id)
                _atualizar_job(job_id, progresso=60)

                dados_setor = client.buscar_dados_setor(contrato_id, setor_codigo, setor_descricao)
                codigo_setor = dados_setor["codigo_setor"]
                _atualizar_job(job_id, progresso=68)

                hist_exc = client.inativar_e_excluir_rotas_antigas(codigo_setor)
                _atualizar_job(job_id, progresso=78)

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
                _atualizar_job(job_id, progresso=88)

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
                    "conversao": resumo_conv,
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
        }


def obter_zip_job_publicacao(job_id: str) -> Optional[Tuple[bytes, str]]:
    with _PUBLICACAO_LOCK:
        job = _PUBLICACAO_JOBS.get(job_id)
        if not job or not job["zip_bytes"]:
            return None
        return job["zip_bytes"], job["zip_filename"] or "rotas.zip"
