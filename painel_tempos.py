"""
Módulo de Inteligência e Gestão de Tempos Operacionais (painel_tempos.py)
Desenvolvido para sitemaLCv1 - Locar Saneamento Ambiental.

Calcula, consolida e cacheia todas as métricas de tempo da operação diária e mensal
de cada cidade (disponível no Link Compartilhado da cidade com isolamento estrito
e no Painel Central Administrativo):
  - Duração total e efetiva de cada setor (início, fim, janela, movimento, parado, intervalo externo)
  - Relação Setor x Caminhão (tempo exato de cada caminhão em cada setor, entradas/saídas, passagens, km)
  - Decomposição completa da jornada de cada caminhão:
      * Garagem (pré-saída, pós-retorno, paradas intermediárias)
      * Trajeto (5 sub-tipos: Garagem→Setor, Setor→Aterro, Aterro→Setor, Entre Setores, Retorno→Garagem)
      * Coleta em Setor (movimento vs parado)
      * Aterro / Transbordo (viagens, duração total/média, tickets e pesagem)
      * Refeição, SOS/Manutenção, Paradas Externas > 5 min
  - Linha do Tempo Gantt 24h por caminhão e por setor, Curva Horária da Frota,
    Evolução Diária Mensal e Exportação Executiva Excel (.xlsx).
"""

from __future__ import annotations

import calendar
import gzip
import io
import json
import math
import re
import secrets
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import database as db
import indicadores
import malha_rotas
from inlog_service import formatar_placa_hifen, janela_operacional_cidade, normalizar_ascii

VERSAO_CACHE_TEMPOS = "v2"

RAIO_FROTA_LOCAL_KM = 65.0
RAIO_GARAGEM_M = 320.0
RAIO_ATERRO_M = 650.0
RAIO_SETOR_M = 38.0
VEL_MAX_GARAGEM_KMH = 20.0
VEL_MAX_ATERRO_KMH = 42.0
VEL_MAX_SETOR_KMH = 38.0
VEL_MOVIMENTO_MIN_KMH = 1.5
MIN_PARADA_EXTERNA_S = 300.0
MIN_TEMPO_ATERRO_S = 180.0
MIN_TEMPO_SETOR_TRANSITO_S = 180.0
MAX_GAP_GPS_ATIVO_S = 2700.0  # 45 min max de interpolação contínua em perda de sinal
HISTERESE_SETOR_MAX_S = 240.0
HISTERESE_SETOR_MAX_M = 900.0

_JOBS_TEMPOS_LOCK = threading.Lock()
_JOBS_TEMPOS: Dict[str, Dict[str, Any]] = {}
_JOB_TEMPOS_TTL_S = 1800

_HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFFFF")
_HEADER_FILL = PatternFill(fill_type="solid", start_color="FF1F5C3A", end_color="FF1F5C3A")
_HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)

_NOMES_DIAS_CURTOS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]


def _parse_dt(dt_str: Any) -> Optional[datetime]:
    s = str(dt_str or "").strip().replace(" ", "T")[:19]
    if len(s) < 19:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _fmt_dt(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else ""


def _fmt_hhmm(dt: Optional[datetime]) -> str:
    return dt.strftime("%H:%M") if dt else "--:--"


def _fmt_duracao(seg: float) -> str:
    s = max(0, int(round(float(seg or 0.0))))
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h:02d}h{m:02d}"


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371009.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * r * math.asin(min(1.0, math.sqrt(max(0.0, a))))


def _media_circular_horas_s(timestamps_s: List[float]) -> Optional[str]:
    """Calcula o horário médio (HH:MM) a partir de segundos relativos ao início da janela."""
    if not timestamps_s:
        return None
    media = sum(timestamps_s) / len(timestamps_s)
    hh = int(media // 3600) % 24
    mm = int((media % 3600) // 60)
    return f"{hh:02d}:{mm:02d}"


def _sec_na_janela(dt: Optional[datetime], dt_ini_op: datetime) -> Optional[float]:
    if not dt:
        return None
    return max(0.0, (dt - dt_ini_op).total_seconds())


def resolver_periodo_tempos(
    modo: str,
    data_str: Optional[str] = None,
    mes_str: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve período 'dia' ou 'mes' reaproveitando as validações oficiais de indicadores."""
    per = indicadores.resolver_periodo_indicadores(modo=modo, data_str=data_str, mes_str=mes_str)
    if per["modo"] == "mes":
        mes_ref = per["data_inicial_iso"][:7]
        rotulo = f"Mês {per['data_inicial'].strftime('%m/%Y')}"
        if per["mes_em_andamento"]:
            rotulo += f" (até {per['data_final'].strftime('%d/%m')})"
    else:
        mes_ref = per["data_inicial_iso"][:7]
        rotulo = f"Dia {per['data_inicial'].strftime('%d/%m/%Y')}"
    return {
        **per,
        "mes_ref": mes_ref,
        "rotulo": rotulo,
    }


def _obter_info_contrato(contrato_id: str) -> Dict[str, Any]:
    cid = str(contrato_id or "").lower().strip()
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT id, nome, centro_lat, centro_lon, zoom_padrao FROM contratos WHERE id = ?",
            (cid,),
        ).fetchone()
    finally:
        conn.close()
    if row:
        return {
            "id": str(row["id"]).lower().strip(),
            "nome": str(row["nome"]),
            "centro_lat": float(row["centro_lat"] or -8.1930),
            "centro_lon": float(row["centro_lon"] or -34.9364),
            "zoom": float(row["zoom_padrao"] or 12.0),
        }
    return {
        "id": cid,
        "nome": cid.title(),
        "centro_lat": -8.1930,
        "centro_lon": -34.9364,
        "zoom": 12.0,
    }


def _carregar_geofences_salvas(contrato_id: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    conn = None
    try:
        conn = db.get_connection()
        row = conn.execute(
            "SELECT garagens_json, aterros_json FROM cidades_geofences WHERE contrato_id = ?",
            (contrato_id,),
        ).fetchone()
        if not row:
            return [], []
        g = json.loads(row["garagens_json"] or "[]")
        a = json.loads(row["aterros_json"] or "[]")
        return (g if isinstance(g, list) else []), (a if isinstance(a, list) else [])
    except Exception:
        return [], []
    finally:
        if conn is not None:
            conn.close()


def _salvar_geofences(contrato_id: str, garagens: List[Dict[str, Any]], aterros: List[Dict[str, Any]]) -> None:
    if not garagens and not aterros:
        return
    agora = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with db._write_lock:
        conn = None
        try:
            conn = db.get_connection()
            with conn:
                conn.execute(
                    """
                    INSERT INTO cidades_geofences (contrato_id, garagens_json, aterros_json, atualizado_em)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (contrato_id) DO UPDATE SET
                        garagens_json = excluded.garagens_json,
                        aterros_json = excluded.aterros_json,
                        atualizado_em = excluded.atualizado_em
                    """,
                    (
                        contrato_id,
                        json.dumps(garagens, ensure_ascii=False),
                        json.dumps(aterros, ensure_ascii=False),
                        agora,
                    ),
                )
        except Exception as e:
            print(f"[TEMPOS] Aviso ao salvar geofences ({contrato_id}): {e}")
        finally:
            if conn is not None:
                conn.close()


def _carregar_auxiliares_locais(
    contrato_id: str,
    data_iso: str,
    dia_seg: str,
    placas: List[str],
) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, int]]:
    """Lê motoristas, cadastro de veículos, telemetria e alarmes já cacheados no banco (sem bloquear rede)."""
    motoristas: Dict[str, str] = {}
    veiculos_meta: Dict[str, Dict[str, Any]] = {}
    telemetria_map: Dict[str, Dict[str, Any]] = {}
    alarmes_count: Dict[str, int] = Counter()

    conn = None
    try:
        conn = db.get_connection()
        for r in conn.execute("SELECT matricula, nome FROM inlog_motoristas").fetchall():
            mat = str(r["matricula"] or "").strip()
            if mat:
                motoristas[mat] = str(r["nome"] or "").strip()

        for r in conn.execute("SELECT placa, tipo, modelo, fabricante, ano FROM inlog_veiculos").fetchall():
            pl = str(r["placa"] or "").strip()
            if pl:
                veiculos_meta[pl] = {
                    "tipo": str(r["tipo"] or "").strip(),
                    "modelo": str(r["modelo"] or "").strip(),
                    "fabricante": str(r["fabricante"] or "").strip(),
                    "ano": str(r["ano"] or "").strip(),
                }

        for r in conn.execute(
            "SELECT placa, tipo_veiculo, matricula_motorista FROM inlog_veiculos_tempo_real WHERE contrato_id = ?",
            (contrato_id,),
        ).fetchall():
            pl = str(r["placa"] or "").strip()
            if pl:
                meta = veiculos_meta.setdefault(pl, {})
                if not meta.get("tipo") and r["tipo_veiculo"]:
                    meta["tipo"] = str(r["tipo_veiculo"]).strip()
                if r["matricula_motorista"]:
                    meta["matricula_rt"] = str(r["matricula_motorista"]).strip()

        if placas:
            phs = ",".join(["?"] * len(placas))
            for r in conn.execute(
                f"""
                SELECT placa,
                       SUM(COALESCE(tempo_movimento, 0)) AS mov_s,
                       SUM(COALESCE(tempo_em_faixa_verde, 0)) AS fv_s,
                       SUM(COALESCE(tempo_disponivel_faixa_verde, 0)) AS disp_s
                FROM inlog_telemetria
                WHERE data_iso IN (?, ?) AND placa IN ({phs})
                GROUP BY placa
                """,
                [data_iso, dia_seg] + placas,
            ).fetchall():
                disp = float(r["disp_s"] or 0.0)
                fv = float(r["fv_s"] or 0.0)
                telemetria_map[str(r["placa"])] = {
                    "tempo_movimento_tel_s": float(r["mov_s"] or 0.0),
                    "pct_faixa_verde": round(fv / disp * 100.0, 1) if disp > 0 else None,
                }

            for r in conn.execute(
                f"""
                SELECT placa, COUNT(*) AS qtd
                FROM inlog_alarmes
                WHERE data_iso IN (?, ?) AND placa IN ({phs})
                GROUP BY placa
                """,
                [data_iso, dia_seg] + placas,
            ).fetchall():
                alarmes_count[str(r["placa"])] = int(r["qtd"] or 0)
    except Exception as e:
        print(f"[TEMPOS] Aviso ao carregar auxiliares locais: {e}")
    finally:
        if conn is not None:
            conn.close()

    return motoristas, veiculos_meta, telemetria_map, alarmes_count


def _filtrar_gps_local(
    gps_por_placa: Dict[str, List[Dict[str, Any]]],
    ref_lat: float,
    ref_lon: float,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Set[str]]:
    """
    Mantém apenas pontos GPS dentro de RAIO_FROTA_LOCAL_KM do centro da cidade.
    Se a maioria dos pontos de uma placa estiver fora (>65km), descarta a placa e
    a retorna em `rejeitadas_remotas` para evitar contaminação por veículos compartilhados.
    """
    filtrado: Dict[str, List[Dict[str, Any]]] = {}
    rejeitadas_remotas: Set[str] = set()
    for placa, pts in (gps_por_placa or {}).items():
        if not pts:
            continue
        locais: List[Dict[str, Any]] = []
        validos_total = 0
        for p in pts:
            lat = float(p.get("latitude") or 0.0)
            lon = float(p.get("longitude") or 0.0)
            dt = _parse_dt(p.get("dataHora"))
            if (lat == 0.0 and lon == 0.0) or dt is None:
                continue
            validos_total += 1
            if _haversine_m(ref_lat, ref_lon, lat, lon) <= RAIO_FROTA_LOCAL_KM * 1000.0:
                p_copy = dict(p)
                p_copy["_dt"] = dt
                p_copy["_lat"] = lat
                p_copy["_lon"] = lon
                p_copy["_vel"] = float(p.get("velocidade") or 0.0)
                locais.append(p_copy)
        if not locais or (validos_total > 0 and (len(locais) / validos_total) < 0.25):
            if validos_total > 0:
                rejeitadas_remotas.add(placa)
            continue
        locais.sort(key=lambda x: x["_dt"])
        filtrado[placa] = locais
    return filtrado, rejeitadas_remotas


def _indexar_setores_xy(
    segmentos_cidade: List[Dict[str, Any]],
    data_iso: str,
    contrato_id: str,
    ref_lat: float,
    ref_lon: float,
) -> Tuple[
    List[Dict[str, Any]],
    Dict[Tuple[int, int], List[Tuple[float, float, int, List[str]]]],
    Dict[str, Dict[str, Any]],
]:
    """Seleciona segmentos agendados no dia e constrói grade espacial 50m x 50m com amostras."""
    cos_lat = math.cos(math.radians(ref_lat))
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * max(0.1, cos_lat)
    cell_size = 50.0

    vis_cache: Dict[str, bool] = {}
    nom_cache: Dict[str, Dict[str, Any]] = {}
    segs_ativos: List[Dict[str, Any]] = []
    sector_grid: Dict[Tuple[int, int], List[Tuple[float, float, int, List[str]]]] = defaultdict(list)
    setores_meta: Dict[str, Dict[str, Any]] = {}

    for seg in segmentos_cidade:
        ativos = []
        for s_nome in seg["setores"]:
            if s_nome not in vis_cache:
                vis_cache[s_nome] = malha_rotas.setor_visivel_na_data(
                    s_nome, data_iso, contrato_id=contrato_id, incluir_diario_domingo=False
                )
            if vis_cache[s_nome]:
                ativos.append(s_nome)
        if not ativos:
            continue

        for s_nome in ativos:
            if s_nome not in nom_cache:
                nom_cache[s_nome] = malha_rotas.analisar_nomenclatura_setor(s_nome, contrato_id=contrato_id)
            if s_nome not in setores_meta:
                nom = nom_cache[s_nome]
                setores_meta[s_nome] = {
                    "codigo": s_nome,
                    "turno": nom["turno"],
                    "agenda_rotulo": nom.get("agenda_rotulo", ""),
                    "regra_tipo": nom.get("regra_tipo", ""),
                }

        try:
            coords = seg.get("_coords")
            if coords is None:
                coords = json.loads(seg["geom_json"])
                seg["_coords"] = coords
        except Exception:
            coords = []

        amostras: Optional[List[Tuple[float, float]]] = seg.get("_amostras_xy")
        if amostras is None:
            xy_pts = [
                ((float(pt[0]) - ref_lon) * m_per_deg_lon, (float(pt[1]) - ref_lat) * m_per_deg_lat)
                for pt in coords
                if isinstance(pt, (list, tuple)) and len(pt) >= 2
            ]
            if len(xy_pts) < 2:
                amostras = xy_pts
            else:
                amostras = [xy_pts[0]]
                for i in range(len(xy_pts) - 1):
                    x1, y1 = xy_pts[i]
                    x2, y2 = xy_pts[i + 1]
                    dist = math.hypot(x2 - x1, y2 - y1)
                    if dist > 12.0:
                        steps = int(dist // 12.0)
                        for s_idx in range(1, steps + 1):
                            frac = s_idx / (steps + 1)
                            amostras.append((x1 + (x2 - x1) * frac, y1 + (y2 - y1) * frac))
                    amostras.append((x2, y2))
            seg["_amostras_xy"] = amostras

        seg_idx = len(segs_ativos)
        segs_ativos.append({
            "id": seg["id"],
            "setores": ativos,
            "extensao_m": float(seg.get("extensao_m") or 0.0),
            "is_manual": bool(seg.get("is_manual")) or str(seg.get("tipo") or "").upper() == "MANUAL",
            "amostras": amostras,
        })
        for sx, sy in amostras:
            gx = int(math.floor(sx / cell_size))
            gy = int(math.floor(sy / cell_size))
            sector_grid[(gx, gy)].append((sx, sy, seg_idx, ativos))

    return segs_ativos, sector_grid, setores_meta


def _ponto_perto_de_setor(
    x: float,
    y: float,
    sector_grid: Dict[Tuple[int, int], List[Tuple[float, float, int, List[str]]]],
    raio_m: float = RAIO_SETOR_M,
) -> Dict[str, float]:
    """Retorna {setor_codigo: menor_distancia_m} dentro do raio."""
    if not sector_grid:
        return {}
    cell_size = 50.0
    gx = int(math.floor(x / cell_size))
    gy = int(math.floor(y / cell_size))
    raio_sq = raio_m * raio_m
    melhores: Dict[str, float] = {}
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for sx, sy, _, ativos in sector_grid.get((gx + dx, gy + dy), []):
                d2 = (sx - x) ** 2 + (sy - y) ** 2
                if d2 <= raio_sq:
                    dist = math.sqrt(d2)
                    for s_nome in ativos:
                        if s_nome not in melhores or dist < melhores[s_nome]:
                            melhores[s_nome] = dist
    return melhores


def _calcular_cobertura_espacial_setor_placa(
    engine: Any,
    gps_filtrado: Dict[str, List[Dict[str, Any]]],
    segs_ativos: List[Dict[str, Any]],
    ref_lat: float,
    ref_lon: float,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[Tuple[str, str], float]]:
    """
    Calcula cobertura física por setor e atribuição exclusiva de metros por placa em cada setor
    (alinhado com engine.py e indicadores.py).
    """
    regras = engine._ler_regras_operacionais()
    pass_grid, dwell_grid, gap_grid, _ = engine._construir_grade_espacial(
        gps_filtrado, ref_lat, ref_lon, regras
    )
    buffer_m = regras["buffer_m"]

    ponto_coletado_grid: Dict[Tuple[int, int], List[Tuple[float, float, str]]] = {}
    pre_info: List[Dict[str, Any]] = []
    for seg in segs_ativos:
        amostras = seg["amostras"]
        n_am = len(amostras)
        matched_pass, placas_pass = engine._consultar_grade(amostras, pass_grid, buffer_m)
        ratio = (matched_pass / n_am) if n_am > 0 else 0.0
        eh_man = seg["is_manual"]
        cob_pass = (n_am <= 4 and matched_pass >= 1) or (ratio >= 0.30 and matched_pass >= 1)
        if cob_pass and not eh_man:
            p0 = list(placas_pass)[0] if placas_pass else ""
            for sx, sy in amostras:
                ponto_coletado_grid.setdefault((int(math.floor(sx / 50.0)), int(math.floor(sy / 50.0))), []).append((sx, sy, p0))
        pre_info.append({
            "seg": seg,
            "amostras": amostras,
            "matched_pass": matched_pass,
            "placas_pass": placas_pass,
            "ratio": ratio,
            "eh_man": eh_man,
            "cob_pass": cob_pass,
        })

    segs_por_setor: Dict[str, List[Tuple[float, str, List[str]]]] = defaultdict(list)
    for item in pre_info:
        seg = item["seg"]
        ext = seg["extensao_m"]
        amostras = item["amostras"]
        matched_pass = item["matched_pass"]
        placas_pass = item["placas_pass"]
        ratio = item["ratio"]
        if item["eh_man"]:
            mp, pmp = engine._consultar_grade(amostras, pass_grid, 50.0)
            md, pmd = engine._consultar_grade(amostras, dwell_grid, 50.0)
            adj = False
            padj: Set[str] = set()
            if not (mp >= 1 or md >= 1):
                for x, y in amostras:
                    cx0, cy0 = int(math.floor(x / 50.0)), int(math.floor(y / 50.0))
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            for px, py, pp in ponto_coletado_grid.get((cx0 + dx, cy0 + dy), []):
                                if math.hypot(x - px, y - py) <= 35.0:
                                    adj = True
                                    if pp:
                                        padj.add(pp)
                                    break
                            if adj:
                                break
                        if adj:
                            break
                    if adj:
                        break
            if mp >= 1 or md >= 1 or adj:
                st, p_resp = "possible_manual", sorted(pmp | pmd | padj)
            else:
                mg, pg = engine._consultar_grade(amostras, gap_grid, 35.0)
                st, p_resp = ("possible_gps_gap", sorted(pg)) if mg >= 1 else ("not_collected", [])
        elif item["cob_pass"]:
            st, p_resp = "collected", sorted(placas_pass)
        else:
            mg, pg = engine._consultar_grade(amostras, gap_grid, 35.0)
            md, pdw = engine._consultar_grade(amostras, dwell_grid, 45.0)
            if mg >= 1 and md == 0:
                st, p_resp = "possible_gps_gap", sorted(pg | placas_pass)
            elif md >= 1 or (matched_pass >= 1 and 0.0 < ratio < 0.30):
                st, p_resp = "possible_manual", sorted(pdw | placas_pass)
            elif mg >= 1:
                st, p_resp = "possible_gps_gap", sorted(pg)
            else:
                st, p_resp = "not_collected", []

        for s_nome in seg["setores"]:
            segs_por_setor[s_nome].append((ext, st, p_resp))

    cobertura_setor: Dict[str, Dict[str, Any]] = {}
    metros_setor_placa: Dict[Tuple[str, str], float] = defaultdict(float)

    for s_nome, lista in segs_por_setor.items():
        prev_m = 0.0
        col_m = 0.0
        man_m = 0.0
        fgps_m = 0.0
        omis_m = 0.0
        presenca_m: Dict[str, float] = defaultdict(float)
        exclusiva_m: Dict[str, float] = defaultdict(float)
        coletados_segs: List[Tuple[float, List[str]]] = []

        for ext, st, placas in lista:
            prev_m += ext
            if st in ("collected", "possible_manual"):
                col_m += ext
                coletados_segs.append((ext, placas))
                if len(placas) == 1:
                    exclusiva_m[placas[0]] += ext
                for pl in placas:
                    presenca_m[pl] += ext
                if st == "possible_manual":
                    man_m += ext
            elif st == "possible_gps_gap":
                fgps_m += ext
            else:
                omis_m += ext

        for ext, placas in coletados_segs:
            if placas:
                melhor_pl = max(placas, key=lambda p: (presenca_m[p], exclusiva_m[p], p))
                metros_setor_placa[(s_nome, melhor_pl)] += ext

        cobertura_setor[s_nome] = {
            "km_previsto": round(prev_m / 1000.0, 2),
            "km_coletado": round(col_m / 1000.0, 2),
            "km_manual": round(man_m / 1000.0, 2),
            "km_falha_gps": round(fgps_m / 1000.0, 2),
            "km_omissao": round(omis_m / 1000.0, 2),
            "pct_cobertura": round(col_m / prev_m * 100.0, 1) if prev_m > 0 else 0.0,
            "pct_manual": round(man_m / prev_m * 100.0, 1) if prev_m > 0 else 0.0,
            "total_vias": len(lista),
        }

    return cobertura_setor, metros_setor_placa


def _parse_rd_dia(
    rd_items: List[Dict[str, Any]],
    dt_ini_op: datetime,
    dt_fim_op: datetime,
    contrato_id: str,
    data_iso: str,
    setores_validos: Set[str],
) -> Dict[str, Dict[str, Any]]:
    """
    Analisa os Relatórios Diários de cada placa dentro da janela operacional.
    Distingue controles reais com horários contínuos de controles manuais sintéticos
    (onde macros têm timestamp idêntico / colapsado).
    """
    por_placa: Dict[str, Dict[str, Any]] = {}
    for item in rd_items or []:
        placa = formatar_placa_hifen(item.get("placa") or item.get("veiculo"))
        if not placa:
            continue
        macros = item.get("macros") or []
        dts_macros = [_parse_dt(m.get("dataHora")) for m in macros]
        dts_validos = [d for d in dts_macros if d is not None]
        if not dts_validos:
            continue

        # Mantém apenas apontamentos cuja atividade ocorre dentro da janela operacional
        t_min, t_max = min(dts_validos), max(dts_validos)
        if t_max < dt_ini_op or t_min > dt_fim_op:
            continue

        setor_raw = str(item.get("setor") or "").strip()
        setor_resolvido = None
        if setor_raw:
            rota = malha_rotas.resolver_rota_setor(contrato_id, setor_raw, data_iso=data_iso)
            if rota and rota.get("apelido") in setores_validos:
                setor_resolvido = rota["apelido"]
            else:
                setor_up = normalizar_ascii(setor_raw)
                for sv in setores_validos:
                    sv_up = normalizar_ascii(sv)
                    if sv_up and (sv_up in setor_up or setor_up in sv_up):
                        setor_resolvido = sv
                        break
                if not setor_resolvido:
                    cod_rd = malha_rotas.extrair_codigo_setor(setor_raw)
                    if cod_rd:
                        for sv in setores_validos:
                            if malha_rotas.extrair_codigo_setor(sv) == cod_rd:
                                setor_resolvido = sv
                                break

        entry = por_placa.setdefault(placa, {
            "placa": placa,
            "setores_rd": [],
            "setor_principal_rd": None,
            "matricula_motorista": None,
            "peso_total_kg": 0.0,
            "tickets": [],
            "descargas_macro": [],
            "refeicoes_macro": [],
            "sos_macro": [],
            "garagem_sinais_dt": [],
            "aterro_sinais_dt": [],
            "coleta_janelas_macro": [],
            "controles_reais": 0,
            "registros_brutos": [],
        })

        peso_reg = float(item.get("pesoTotalKg") or 0.0)
        entry["peso_total_kg"] += peso_reg
        for tk in item.get("tickets") or []:
            tk_s = str(tk or "").strip()
            if tk_s and tk_s != "0" and tk_s not in entry["tickets"]:
                entry["tickets"].append(tk_s)

        if setor_resolvido and setor_resolvido not in entry["setores_rd"]:
            entry["setores_rd"].append(setor_resolvido)
            if entry["setor_principal_rd"] is None:
                entry["setor_principal_rd"] = setor_resolvido

        # Detecta se o controle tem timestamps reais (não colapsado no mesmo segundo)
        timestamps_distintos = len({d.strftime("%Y-%m-%dT%H:%M") for d in dts_validos})
        tem_tempos_reais = timestamps_distintos >= 3
        if tem_tempos_reais:
            entry["controles_reais"] += 1

        ini_desc: Optional[datetime] = None
        ini_ref: Optional[datetime] = None
        ini_sos: Optional[datetime] = None
        ini_col: Optional[datetime] = None
        ultimo_ticket: Optional[str] = None
        ultimo_peso: float = 0.0

        for m in macros:
            campo = str(m.get("campo") or "").upper().strip()
            valor = str(m.get("valor") or "").strip().rstrip("\x00")
            dt_m = _parse_dt(m.get("dataHora"))
            if campo == "MTR_MOTORISTA" and valor and valor != "0" and not entry["matricula_motorista"]:
                entry["matricula_motorista"] = valor
            if not dt_m:
                continue

            if not tem_tempos_reais:
                continue

            if campo == "TICKET" and valor and valor != "0":
                ultimo_ticket = valor
            elif campo == "PESO":
                try:
                    ultimo_peso = float(str(valor).replace(",", "."))
                except ValueError:
                    ultimo_peso = 0.0
                if entry["descargas_macro"]:
                    ult_d = entry["descargas_macro"][-1]
                    if abs((dt_m - ult_d["fim"]).total_seconds()) <= 900:
                        if ultimo_ticket:
                            ult_d["ticket"] = ultimo_ticket
                        if ultimo_peso > 0:
                            ult_d["peso_kg"] = ultimo_peso

            if campo == "ATIVIDADE":
                if valor in ("SAIDA GARAGEM", "CHEGADA GARAGEM"):
                    entry["garagem_sinais_dt"].append((valor, dt_m))
                elif valor == "INI COLETA":
                    ini_col = dt_m
                elif valor == "FIM COLETA" and ini_col:
                    if dt_m > ini_col:
                        entry["coleta_janelas_macro"].append((ini_col, dt_m, setor_resolvido or setor_raw))
                    ini_col = None
                elif valor == "INI DESCARGA":
                    ini_desc = dt_m
                    entry["aterro_sinais_dt"].append(dt_m)
                elif valor == "FIM DESCARGA":
                    entry["aterro_sinais_dt"].append(dt_m)
                    if ini_desc and dt_m >= ini_desc:
                        entry["descargas_macro"].append({
                            "inicio": ini_desc,
                            "fim": dt_m,
                            "ticket": ultimo_ticket or "",
                            "peso_kg": ultimo_peso,
                        })
                    ini_desc = None
                    ultimo_ticket = None
                    ultimo_peso = 0.0
                elif valor == "INI REFEICAO":
                    ini_ref = dt_m
                elif valor == "FIM REFEICAO" and ini_ref:
                    if dt_m > ini_ref:
                        entry["refeicoes_macro"].append((ini_ref, dt_m))
                    ini_ref = None
                elif valor in ("PEDIDO SOS", "INI SOS"):
                    if ini_sos is None:
                        ini_sos = dt_m
                elif valor == "FIM SOS" and ini_sos:
                    if dt_m > ini_sos:
                        entry["sos_macro"].append((ini_sos, dt_m))
                    ini_sos = None
            elif campo == "LOCAL DE DESCARGA" and "ATERRO" in valor.upper():
                entry["aterro_sinais_dt"].append(dt_m)

        entry["registros_brutos"].append({
            "controle": str(item.get("controle") or ""),
            "setor": setor_resolvido or setor_raw,
            "tem_tempos_reais": tem_tempos_reais,
            "peso_kg": peso_reg,
            "macros": macros if tem_tempos_reais else [],
        })

    return por_placa


def _clusterizar_pontos(
    ocorrencias: List[Tuple[float, float, str]],
    cell_deg: float,
    min_pts: int,
    max_clusters: int = 4,
) -> List[Dict[str, Any]]:
    """Agrupa coordenadas (lat, lon, placa) em zonas circulares centradas na média do cluster."""
    if not ocorrencias:
        return []
    buckets: Dict[Tuple[int, int], List[Tuple[float, float, str]]] = defaultdict(list)
    for lat, lon, pl in ocorrencias:
        buckets[(int(round(lat / cell_deg)), int(round(lon / cell_deg)))].append((lat, lon, pl))

    merged: List[Dict[str, Any]] = []
    usados: Set[Tuple[int, int]] = set()
    for cell, pts in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        if cell in usados:
            continue
        grupo = list(pts)
        usados.add(cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                nb = (cell[0] + dx, cell[1] + dy)
                if nb not in usados and nb in buckets:
                    grupo.extend(buckets[nb])
                    usados.add(nb)
        placas_unicas = {x[2] for x in grupo if x[2]}
        if len(grupo) >= min_pts or len(placas_unicas) >= min(2, min_pts):
            lat_m = sum(x[0] for x in grupo) / len(grupo)
            lon_m = sum(x[1] for x in grupo) / len(grupo)
            merged.append({
                "lat": round(lat_m, 6),
                "lon": round(lon_m, 6),
                "ocorrencias": len(grupo),
                "placas": len(placas_unicas),
            })

    merged.sort(key=lambda c: (-c["placas"], -c["ocorrencias"]))
    return merged[:max_clusters]


def _descobrir_zonas_cidade(
    contrato_id: str,
    gps_filtrado: Dict[str, List[Dict[str, Any]]],
    rd_por_placa: Dict[str, Dict[str, Any]],
    sector_grid: Dict[Tuple[int, int], List[Tuple[float, float, int, List[str]]]],
    ref_lat: float,
    ref_lon: float,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Descobre automaticamente Zonas de Garagem e Zonas de Aterro combinando:
      1) Sinais de macros do Relatório Diário cruzados com GPS;
      2) Padrões GPS de partida/chegada e paradas longas fora dos setores (para cidades sem RD);
      3) Histórico de geofences já aprendidas e salvas em `cidades_geofences`.
    """
    cos_lat = math.cos(math.radians(ref_lat))
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * max(0.1, cos_lat)

    def to_xy(lat: float, lon: float) -> Tuple[float, float]:
        return (lon - ref_lon) * m_per_deg_lon, (lat - ref_lat) * m_per_deg_lat

    gar_salvas, ate_salvas = _carregar_geofences_salvas(contrato_id)
    ocorr_garagem: List[Tuple[float, float, str]] = []
    ocorr_aterro_rd: List[Tuple[float, float, str]] = []
    ocorr_aterro_gps: List[Tuple[float, float, str]] = []

    def _ponto_mais_proximo_gps(pts: List[Dict[str, Any]], alvo_dt: datetime, max_diff_s: float = 900.0) -> Optional[Dict[str, Any]]:
        if not pts:
            return None
        best = min(pts, key=lambda p: abs((p["_dt"] - alvo_dt).total_seconds()))
        if abs((best["_dt"] - alvo_dt).total_seconds()) <= max_diff_s:
            return best
        return None

    for placa, pts in gps_filtrado.items():
        if not pts:
            continue
        rd_p = rd_por_placa.get(placa) or {}

        # 1. Macros de Garagem e Aterro cruzadas com GPS
        for _, dt_g in rd_p.get("garagem_sinais_dt") or []:
            pt = _ponto_mais_proximo_gps(pts, dt_g)
            if pt:
                x, y = to_xy(pt["_lat"], pt["_lon"])
                if not _ponto_perto_de_setor(x, y, sector_grid, 45.0):
                    ocorr_garagem.append((pt["_lat"], pt["_lon"], placa))

        for dt_a in rd_p.get("aterro_sinais_dt") or []:
            pt = _ponto_mais_proximo_gps(pts, dt_a)
            if pt and pt["_vel"] <= VEL_MAX_ATERRO_KMH:
                x, y = to_xy(pt["_lat"], pt["_lon"])
                if not _ponto_perto_de_setor(x, y, sector_grid, 45.0):
                    ocorr_aterro_rd.append((pt["_lat"], pt["_lon"], placa))

        # 2. Pontos extremos (início e fim da trilha do caminhão) fora de setores -> candidato a Garagem
        for pt_ext in (pts[0], pts[-1]):
            if pt_ext["_vel"] <= 15.0:
                x, y = to_xy(pt_ext["_lat"], pt_ext["_lon"])
                if not _ponto_perto_de_setor(x, y, sector_grid, 55.0):
                    ocorr_garagem.append((pt_ext["_lat"], pt_ext["_lon"], placa))

    gar_novas = _clusterizar_pontos(ocorr_garagem, cell_deg=0.0025, min_pts=2, max_clusters=3)
    garagens = gar_novas if gar_novas else gar_salvas
    if not garagens and ocorr_garagem:
        garagens = _clusterizar_pontos(ocorr_garagem, cell_deg=0.0025, min_pts=1, max_clusters=2)

    # Mescla garagens descobertas com salvas sem duplicar (<450m)
    for gs in gar_salvas:
        if not any(_haversine_m(gs["lat"], gs["lon"], g["lat"], g["lon"]) <= 450.0 for g in garagens):
            garagens.append(gs)
    garagens = garagens[:4]

    # 3. Descobre Aterro também por permanência fora da malha e fora da garagem (6 a 90 min)
    for placa, pts in gps_filtrado.items():
        if len(pts) < 6:
            continue
        em_excursao = False
        exc_pts: List[Dict[str, Any]] = []
        for pt in pts:
            na_gar = any(_haversine_m(pt["_lat"], pt["_lon"], g["lat"], g["lon"]) <= 1800.0 for g in garagens)
            x, y = to_xy(pt["_lat"], pt["_lon"])
            no_set = bool(_ponto_perto_de_setor(x, y, sector_grid, 400.0))
            if (not na_gar) and (not no_set) and pt["_vel"] <= 35.0:
                em_excursao = True
                exc_pts.append(pt)
            else:
                if em_excursao and len(exc_pts) >= 4:
                    dur_s = (exc_pts[-1]["_dt"] - exc_pts[0]["_dt"]).total_seconds()
                    if 360.0 <= dur_s <= 5400.0:
                        lat_c = sum(p["_lat"] for p in exc_pts) / len(exc_pts)
                        lon_c = sum(p["_lon"] for p in exc_pts) / len(exc_pts)
                        spread = max(_haversine_m(lat_c, lon_c, p["_lat"], p["_lon"]) for p in exc_pts)
                        if spread <= 750.0:
                            ocorr_aterro_gps.append((lat_c, lon_c, placa))
                em_excursao = False
                exc_pts = []

    ate_rd = _clusterizar_pontos(ocorr_aterro_rd, cell_deg=0.006, min_pts=1, max_clusters=3)
    ate_gps = _clusterizar_pontos(ocorr_aterro_gps, cell_deg=0.006, min_pts=3 if ate_rd else 2, max_clusters=3)

    # Se RD oficial já identificou aterros, só aceita extras GPS se forem claramente distintos (>3km de garagem e >=3 caminhões)
    cands_aterro = list(ate_rd)
    for cg in ate_gps:
        if any(_haversine_m(cg["lat"], cg["lon"], g["lat"], g["lon"]) < 2000.0 for g in garagens):
            continue
        if ate_rd and cg.get("placas", 0) < 3:
            continue
        cands_aterro.append(cg)

    for cs in ate_salvas:
        cx, cy = to_xy(cs["lat"], cs["lon"])
        if _ponto_perto_de_setor(cx, cy, sector_grid, 350.0):
            continue
        if any(_haversine_m(cs["lat"], cs["lon"], g["lat"], g["lon"]) < 1800.0 for g in garagens):
            continue
        cands_aterro.append(cs)

    aterros: List[Dict[str, Any]] = []
    for cand in cands_aterro:
        if any(_haversine_m(cand["lat"], cand["lon"], g["lat"], g["lon"]) <= 1500.0 for g in garagens):
            continue
        if not any(_haversine_m(cand["lat"], cand["lon"], a["lat"], a["lon"]) <= 1100.0 for a in aterros):
            aterros.append(cand)
        if len(aterros) >= 4:
            break

    if garagens or aterros:
        _salvar_geofences(
            contrato_id,
            [{"lat": g["lat"], "lon": g["lon"]} for g in garagens],
            [{"lat": a["lat"], "lon": a["lon"]} for a in aterros],
        )
    return garagens, aterros


def _construir_timeline_caminhao(
    placa: str,
    pts: List[Dict[str, Any]],
    rd_info: Optional[Dict[str, Any]],
    garagens: List[Dict[str, Any]],
    aterros: List[Dict[str, Any]],
    sector_grid: Dict[Tuple[int, int], List[Tuple[float, float, int, List[str]]]],
    ref_lat: float,
    ref_lon: float,
    dt_ini_op: datetime,
    dt_fim_op: datetime,
    metros_por_setor_placa: Optional[Dict[str, float]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Executa a máquina de estados contínua ao longo da janela operacional para um caminhão.
    Retorna (blocos_timeline, estatisticas_caminhao).
    """
    cos_lat = math.cos(math.radians(ref_lat))
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * max(0.1, cos_lat)

    def to_xy(lat: float, lon: float) -> Tuple[float, float]:
        return (lon - ref_lon) * m_per_deg_lon, (lat - ref_lat) * m_per_deg_lat

    setores_rd = set((rd_info or {}).get("setores_rd") or [])
    descargas_macro = (rd_info or {}).get("descargas_macro") or []
    refeicoes_macro = (rd_info or {}).get("refeicoes_macro") or []
    sos_macro = (rd_info or {}).get("sos_macro") or []

    # Caso o caminhão não tenha pontos GPS no dia, tenta reconstruir por RD com tempos reais
    if len(pts) < 2:
        blocos_rd: List[Dict[str, Any]] = []
        if rd_info and rd_info.get("controles_reais", 0) > 0:
            eventos: List[Tuple[datetime, str, str, float]] = []
            for reg in rd_info.get("registros_brutos") or []:
                if not reg.get("tem_tempos_reais"):
                    continue
                set_r = reg.get("setor") or "Setor"
                for m in reg.get("macros") or []:
                    dt_m = _parse_dt(m.get("dataHora"))
                    if not dt_m:
                        continue
                    c = str(m.get("campo") or "").upper().strip()
                    v = str(m.get("valor") or "").strip().rstrip("\x00")
                    dist_m = float(m.get("distancia") or 0.0)
                    if c == "ATIVIDADE":
                        eventos.append((dt_m, v, set_r, dist_m))
            eventos.sort(key=lambda x: x[0])
            estado_atual = "GARAGEM"
            setor_atual = None
            for i in range(len(eventos) - 1):
                t1, ev1, set1, dist_m = eventos[i]
                t2, ev2, _, _ = eventos[i + 1]
                dur_s = (t2 - t1).total_seconds()
                if dur_s <= 0 or dur_s > 43200:
                    continue
                if ev1 in ("CONTROLE", "CHEGADA GARAGEM"):
                    estado_atual = "GARAGEM"
                elif ev1 == "SAIDA GARAGEM":
                    estado_atual = "TRAJETO"
                elif ev1 == "INI COLETA":
                    estado_atual = "SETOR"
                    setor_atual = set1
                elif ev1 == "FIM COLETA":
                    estado_atual = "TRAJETO"
                elif ev1 in ("DESCARGA", "INI DESCARGA"):
                    estado_atual = "ATERRO"
                elif ev1 == "FIM DESCARGA":
                    estado_atual = "TRAJETO"
                elif ev1 == "INI REFEICAO":
                    estado_atual = "REFEICAO"
                elif ev1 == "FIM REFEICAO":
                    estado_atual = "TRAJETO"
                elif ev1 in ("PEDIDO SOS", "INI SOS"):
                    estado_atual = "SOS"
                elif ev1 == "FIM SOS":
                    estado_atual = "TRAJETO"
                blocos_rd.append({
                    "kind": estado_atual,
                    "setor": setor_atual if estado_atual == "SETOR" else None,
                    "start_dt": t1,
                    "end_dt": t2,
                    "dur_s": dur_s,
                    "mov_s": dur_s if estado_atual in ("SETOR", "TRAJETO") else 0.0,
                    "parado_s": 0.0 if estado_atual in ("SETOR", "TRAJETO") else dur_s,
                    "dist_m": dist_m,
                })
        return _finalizar_blocos_caminhao(placa, blocos_rd, rd_info, dt_ini_op)

    # Passo 1: Classifica cada segmento consecutivo [pts[i], pts[i+1]]
    segs_raw: List[Dict[str, Any]] = []
    ultimo_setor: Optional[str] = None

    def _in_intervalos(dt_mid: datetime, lista: List[Tuple[datetime, datetime]]) -> bool:
        for t0, t1 in lista:
            if t0 <= dt_mid <= t1:
                return True
        return False

    def _in_descarga(dt_mid: datetime) -> Optional[Dict[str, Any]]:
        for d in descargas_macro:
            if d["inicio"] <= dt_mid <= d["fim"]:
                return d
        return None

    for i in range(len(pts) - 1):
        p1 = pts[i]
        p2 = pts[i + 1]
        t1: datetime = p1["_dt"]
        t2: datetime = p2["_dt"]
        dt_s = (t2 - t1).total_seconds()
        if dt_s <= 0:
            continue

        dist_m = _haversine_m(p1["_lat"], p1["_lon"], p2["_lat"], p2["_lon"])
        # Proteção contra grandes hiatos de rastreamento (>45min sem sinal fora da garagem ou fim de turno)
        if dt_s > MAX_GAP_GPS_ATIVO_S:
            # Se ambos os pontos estão na garagem, não consideramos um gap rodoviário, e sim pausa/garagem limitada
            na_gar1 = any(_haversine_m(p1["_lat"], p1["_lon"], g["lat"], g["lon"]) <= RAIO_GARAGEM_M for g in garagens)
            na_gar2 = any(_haversine_m(p2["_lat"], p2["_lon"], g["lat"], g["lon"]) <= RAIO_GARAGEM_M for g in garagens)
            if not (na_gar1 and na_gar2 and dist_m <= 250.0):
                continue
            # Limita lacuna longa dentro da garagem a no máximo 15 min se houver salto entre turnos
            if dt_s > 5400.0:
                continue

        vel_calc = (dist_m / dt_s) * 3.6 if dt_s > 0 else p1["_vel"]
        vel_ref = min(max(p1["_vel"], p2["_vel"]), 130.0)
        if vel_ref <= 0.1 and dist_m > 5.0 and dt_s <= 180.0:
            vel_ref = vel_calc

        dt_mid = t1 + timedelta(seconds=dt_s * 0.5)
        lat_mid = (p1["_lat"] + p2["_lat"]) * 0.5
        lon_mid = (p1["_lon"] + p2["_lon"]) * 0.5

        na_gar = (
            vel_ref <= VEL_MAX_GARAGEM_KMH
            and any(_haversine_m(lat_mid, lon_mid, g["lat"], g["lon"]) <= RAIO_GARAGEM_M for g in garagens)
        )
        desc_hit = _in_descarga(dt_mid)
        no_ate = (desc_hit is not None) or (
            vel_ref <= VEL_MAX_ATERRO_KMH
            and any(_haversine_m(lat_mid, lon_mid, a["lat"], a["lon"]) <= RAIO_ATERRO_M for a in aterros)
        )

        if _in_intervalos(dt_mid, sos_macro):
            kind = "SOS"
            setor_escolhido = None
        elif _in_intervalos(dt_mid, refeicoes_macro):
            kind = "REFEICAO"
            setor_escolhido = None
        elif no_ate:
            kind = "ATERRO"
            setor_escolhido = None
        elif na_gar:
            kind = "GARAGEM"
            setor_escolhido = None
        else:
            x_m, y_m = to_xy(lat_mid, lon_mid)
            cand_setores = _ponto_perto_de_setor(x_m, y_m, sector_grid, RAIO_SETOR_M)
            if cand_setores and vel_ref <= VEL_MAX_SETOR_KMH:
                kind = "SETOR"
                if len(cand_setores) == 1:
                    setor_escolhido = next(iter(cand_setores.keys()))
                else:
                    def _score_setor(s_cod: str) -> Tuple[int, int, float]:
                        em_rd = 1 if s_cod in setores_rd else 0
                        eh_ult = 1 if s_cod == ultimo_setor else 0
                        return (eh_ult, em_rd, -cand_setores[s_cod])
                    setor_escolhido = max(cand_setores.keys(), key=_score_setor)
                ultimo_setor = setor_escolhido
            else:
                kind = "TRAJETO"
                setor_escolhido = None

        em_mov = vel_ref > VEL_MOVIMENTO_MIN_KMH and dist_m > 2.0
        segs_raw.append({
            "kind": kind,
            "setor": setor_escolhido,
            "start_dt": t1,
            "end_dt": t2,
            "dur_s": dt_s,
            "mov_s": dt_s if em_mov else 0.0,
            "parado_s": 0.0 if em_mov else dt_s,
            "dist_m": dist_m if dist_m <= 5000.0 else 0.0,
            "lat": lat_mid,
            "lon": lon_mid,
            "vel": vel_ref,
        })

    if not segs_raw:
        return _finalizar_blocos_caminhao(placa, [], rd_info, dt_ini_op)

    # Passo 2: Agrupa segmentos adjacentes de mesmo (kind, setor)
    blocos = _agrupar_blocos(segs_raw)

    # Passo 3: Histerese de Setor (se saiu rapidamente <= 4 min e < 900m entre blocos do mesmo setor, absorve como SETOR)
    alterado = True
    while alterado:
        alterado = False
        novos: List[Dict[str, Any]] = []
        idx = 0
        while idx < len(blocos):
            b = blocos[idx]
            if (
                b["kind"] == "TRAJETO"
                and b["dur_s"] <= HISTERESE_SETOR_MAX_S
                and b["dist_m"] <= HISTERESE_SETOR_MAX_M
                and len(novos) > 0
                and idx + 1 < len(blocos)
            ):
                prev_b = novos[-1]
                next_b = blocos[idx + 1]
                if prev_b["kind"] == "SETOR" and next_b["kind"] == "SETOR" and prev_b["setor"] == next_b["setor"]:
                    b_conv = dict(b)
                    b_conv["kind"] = "SETOR"
                    b_conv["setor"] = prev_b["setor"]
                    novos.append(b_conv)
                    idx += 1
                    alterado = True
                    continue
            novos.append(b)
            idx += 1
        blocos = _agrupar_blocos(novos)

    # Passo 4: Remove passagens rápidas (<3 min) por perímetro de aterro sem macro RD (apenas trânsito na rodovia)
    blocos_limpos_ate: List[Dict[str, Any]] = []
    for b in blocos:
        if b["kind"] == "ATERRO" and b["dur_s"] < MIN_TEMPO_ATERRO_S:
            dt_mid_b = b["start_dt"] + timedelta(seconds=b["dur_s"] * 0.5)
            if not _in_descarga(dt_mid_b):
                b2 = dict(b)
                b2["kind"] = "TRAJETO"
                blocos_limpos_ate.append(b2)
                continue
        blocos_limpos_ate.append(b)
    blocos = _agrupar_blocos(blocos_limpos_ate)

    # Passo 5: Remove cruzamentos breves de passagem por borda de outros setores (<3 min e <=50m coletados)
    tempo_por_setor_raw: Dict[str, float] = defaultdict(float)
    for b in blocos:
        if b["kind"] == "SETOR" and b.get("setor"):
            tempo_por_setor_raw[b["setor"]] += float(b["dur_s"])
    m_map = metros_por_setor_placa or {}
    setores_transito = {
        s_c for s_c, t_s in tempo_por_setor_raw.items()
        if t_s < MIN_TEMPO_SETOR_TRANSITO_S and float(m_map.get(s_c, 0.0)) <= 50.0 and s_c not in setores_rd
    }
    if setores_transito:
        blocos = _agrupar_blocos([
            ({**b, "kind": "TRAJETO", "setor": None} if (b["kind"] == "SETOR" and b.get("setor") in setores_transito) else b)
            for b in blocos
        ])

    # Passo 6: Detecta PARADA_EXTERNA (blocos de TRAJETO parados >= 5 min com deslocamento mínimo)
    blocos_com_parada: List[Dict[str, Any]] = []
    for b in blocos:
        if b["kind"] == "TRAJETO" and b["dur_s"] >= MIN_PARADA_EXTERNA_S and b["parado_s"] >= b["dur_s"] * 0.75 and b["dist_m"] <= 120.0:
            b2 = dict(b)
            b2["kind"] = "PARADA_EXTERNA"
            blocos_com_parada.append(b2)
        else:
            blocos_com_parada.append(b)
    blocos = _agrupar_blocos(blocos_com_parada)

    # Passo 7: Classifica sub-tipos de GARAGEM e TRAJETO
    return _finalizar_blocos_caminhao(placa, blocos, rd_info, dt_ini_op)


def _agrupar_blocos(segs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not segs:
        return []
    out: List[Dict[str, Any]] = []
    for s in segs:
        if not out:
            out.append(dict(s))
            continue
        ult = out[-1]
        gap = (s["start_dt"] - ult["end_dt"]).total_seconds()
        if ult["kind"] == s["kind"] and ult.get("setor") == s.get("setor") and gap <= 300.0:
            ult["end_dt"] = max(ult["end_dt"], s["end_dt"])
            ult["dur_s"] += s["dur_s"]
            ult["mov_s"] += s["mov_s"]
            ult["parado_s"] += s["parado_s"]
            ult["dist_m"] += s["dist_m"]
        else:
            out.append(dict(s))
    return out


def _finalizar_blocos_caminhao(
    placa: str,
    blocos: List[Dict[str, Any]],
    rd_info: Optional[Dict[str, Any]],
    dt_ini_op: datetime,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Classifica os 5 sub-tipos de trajeto, 3 sub-tipos de garagem e consolida viagens ao aterro."""
    if not blocos:
        return [], {
            "placa": placa,
            "jornada_total_s": 0.0,
            "tempo_garagem_s": 0.0,
            "garagem_pre_saida_s": 0.0,
            "garagem_pos_retorno_s": 0.0,
            "garagem_intermediaria_s": 0.0,
            "tempo_setor_s": 0.0,
            "tempo_setor_mov_s": 0.0,
            "tempo_setor_parado_s": 0.0,
            "tempo_trajeto_s": 0.0,
            "trajeto_garagem_setor_s": 0.0,
            "trajeto_setor_aterro_s": 0.0,
            "trajeto_aterro_setor_s": 0.0,
            "trajeto_entre_setores_s": 0.0,
            "trajeto_retorno_garagem_s": 0.0,
            "tempo_aterro_s": 0.0,
            "tempo_refeicao_s": 0.0,
            "tempo_sos_s": 0.0,
            "tempo_parada_externa_s": 0.0,
            "km_rodado_total": 0.0,
            "km_trajeto": 0.0,
            "km_em_setor": 0.0,
            "saida_garagem_dt": None,
            "chegada_garagem_dt": None,
            "primeiro_setor_dt": None,
            "ultimo_setor_dt": None,
            "setores_tempo": {},
            "viagens_aterro": [],
        }

    indices_prod = [i for i, b in enumerate(blocos) if b["kind"] in ("SETOR", "ATERRO")]
    primeiro_prod = indices_prod[0] if indices_prod else None
    ultimo_prod = indices_prod[-1] if indices_prod else None

    # Classifica sub-tipos e acumula somatórios exatos (garantindo soma == jornada_total_s)
    gar_pre = 0.0
    gar_pos = 0.0
    gar_int = 0.0
    set_tot = 0.0
    set_mov = 0.0
    set_par = 0.0
    traj_tot = 0.0
    t_gar_set = 0.0
    t_set_ate = 0.0
    t_ate_set = 0.0
    t_ent_set = 0.0
    t_ret_gar = 0.0
    ate_tot = 0.0
    ref_tot = 0.0
    sos_tot = 0.0
    par_ext_tot = 0.0
    dist_total_m = 0.0
    dist_traj_m = 0.0
    dist_setor_m = 0.0

    setores_tempo: Dict[str, Dict[str, Any]] = {}
    viagens_aterro: List[Dict[str, Any]] = []

    def _vizinho_relevante(idx_start: int, passo: int) -> Optional[Dict[str, Any]]:
        j = idx_start + passo
        while 0 <= j < len(blocos):
            if blocos[j]["kind"] in ("GARAGEM", "SETOR", "ATERRO"):
                return blocos[j]
            j += passo
        return None

    timeline_out: List[Dict[str, Any]] = []
    for i, b in enumerate(blocos):
        kind = b["kind"]
        dur = float(b["dur_s"])
        mov = float(b["mov_s"])
        par = float(b["parado_s"])
        dist = float(b["dist_m"])
        dist_total_m += dist
        subtipo = kind

        if kind == "GARAGEM":
            if primeiro_prod is None or i < primeiro_prod:
                gar_pre += dur
                subtipo = "GARAGEM_PRE_SAIDA"
            elif ultimo_prod is not None and i > ultimo_prod:
                gar_pos += dur
                subtipo = "GARAGEM_POS_RETORNO"
            else:
                gar_int += dur
                subtipo = "GARAGEM_INTERMEDIARIA"
        elif kind == "SETOR":
            set_tot += dur
            set_mov += mov
            set_par += par
            dist_setor_m += dist
            s_cod = str(b.get("setor") or "Setor")
            st_e = setores_tempo.setdefault(s_cod, {
                "setor": s_cod,
                "tempo_s": 0.0,
                "mov_s": 0.0,
                "parado_s": 0.0,
                "dist_m": 0.0,
                "passagens": 0,
                "entrada_dt": b["start_dt"],
                "saida_dt": b["end_dt"],
            })
            st_e["tempo_s"] += dur
            st_e["mov_s"] += mov
            st_e["parado_s"] += par
            st_e["dist_m"] += dist
            st_e["passagens"] += 1
            st_e["entrada_dt"] = min(st_e["entrada_dt"], b["start_dt"])
            st_e["saida_dt"] = max(st_e["saida_dt"], b["end_dt"])
            subtipo = f"SETOR:{s_cod}"
        elif kind == "ATERRO":
            ate_tot += dur
            subtipo = "ATERRO"
        elif kind == "REFEICAO":
            ref_tot += dur
        elif kind == "SOS":
            sos_tot += dur
        elif kind == "PARADA_EXTERNA":
            par_ext_tot += dur
        elif kind == "TRAJETO":
            traj_tot += dur
            dist_traj_m += dist
            prev_v = _vizinho_relevante(i, -1)
            next_v = _vizinho_relevante(i, 1)
            pk = prev_v["kind"] if prev_v else "GARAGEM"
            nk = next_v["kind"] if next_v else "GARAGEM"
            if pk == "GARAGEM" and nk == "SETOR":
                t_gar_set += dur
                subtipo = "TRAJETO_GARAGEM_SETOR"
            elif nk == "ATERRO":
                t_set_ate += dur
                subtipo = "TRAJETO_SETOR_ATERRO"
            elif pk == "ATERRO" and nk == "SETOR":
                t_ate_set += dur
                subtipo = "TRAJETO_ATERRO_SETOR"
            elif pk == "SETOR" and nk == "SETOR":
                t_ent_set += dur
                subtipo = "TRAJETO_ENTRE_SETORES"
            else:
                t_ret_gar += dur
                subtipo = "TRAJETO_RETORNO_GARAGEM"

        timeline_out.append({
            "kind": kind,
            "subtipo": subtipo,
            "setor": b.get("setor"),
            "start": _fmt_dt(b["start_dt"]),
            "end": _fmt_dt(b["end_dt"]),
            "start_hhmm": _fmt_hhmm(b["start_dt"]),
            "end_hhmm": _fmt_hhmm(b["end_dt"]),
            "start_s": round(_sec_na_janela(b["start_dt"], dt_ini_op) or 0.0, 1),
            "end_s": round(_sec_na_janela(b["end_dt"], dt_ini_op) or 0.0, 1),
            "dur_s": round(dur, 1),
            "mov_s": round(mov, 1),
            "parado_s": round(par, 1),
            "dist_km": round(dist / 1000.0, 2),
        })

    # Consolida viagens ao aterro (junta blocos de ATERRO próximos <= 6 min e vincula macro/peso/trajeto)
    descargas_macro = list((rd_info or {}).get("descargas_macro") or [])
    usados_macro: Set[int] = set()
    for i, b in enumerate(blocos):
        if b["kind"] != "ATERRO":
            continue
        if viagens_aterro and (b["start_dt"] - viagens_aterro[-1]["_saida_dt"]).total_seconds() <= 360.0:
            ult_v = viagens_aterro[-1]
            ult_v["_saida_dt"] = max(ult_v["_saida_dt"], b["end_dt"])
            ult_v["duracao_s"] = round(ult_v["duracao_s"] + float(b["dur_s"]), 1)
            continue

        prev_v = _vizinho_relevante(i, -1)
        next_v = _vizinho_relevante(i, 1)
        setor_orig = prev_v.get("setor") if (prev_v and prev_v["kind"] == "SETOR") else (prev_v["kind"] if prev_v else "Garagem")
        dest_pos = next_v.get("setor") if (next_v and next_v["kind"] == "SETOR") else ("Garagem" if (not next_v or next_v["kind"] == "GARAGEM") else next_v["kind"])

        traj_ida_s = 0.0
        for j in range(i - 1, -1, -1):
            if blocos[j]["kind"] in ("SETOR", "GARAGEM", "ATERRO"):
                break
            if blocos[j]["kind"] == "TRAJETO":
                traj_ida_s += float(blocos[j]["dur_s"])

        traj_volta_s = 0.0
        for j in range(i + 1, len(blocos)):
            if blocos[j]["kind"] in ("SETOR", "GARAGEM", "ATERRO"):
                break
            if blocos[j]["kind"] == "TRAJETO":
                traj_volta_s += float(blocos[j]["dur_s"])

        ticket_v = ""
        peso_v = 0.0
        for idx_dm, dm in enumerate(descargas_macro):
            if idx_dm in usados_macro:
                continue
            if abs((dm["inicio"] - b["start_dt"]).total_seconds()) <= 1800.0:
                ticket_v = str(dm.get("ticket") or "")
                peso_v = float(dm.get("peso_kg") or 0.0)
                usados_macro.add(idx_dm)
                break

        viagens_aterro.append({
            "placa": placa,
            "setor_origem": setor_orig or "Não identificado",
            "destino_pos": dest_pos or "Garagem",
            "chegada_hhmm": _fmt_hhmm(b["start_dt"]),
            "saida_hhmm": _fmt_hhmm(b["end_dt"]),
            "chegada": _fmt_dt(b["start_dt"]),
            "saida": _fmt_dt(b["end_dt"]),
            "_saida_dt": b["end_dt"],
            "duracao_s": round(float(b["dur_s"]), 1),
            "trajeto_ida_s": round(traj_ida_s, 1),
            "trajeto_volta_s": round(traj_volta_s, 1),
            "ticket": ticket_v,
            "peso_kg": round(peso_v, 1),
        })

    # Se havia pesos/tickets no RD sem correspondência exata no bloco de aterro, distribui fallback proporcional ou agrega no último
    peso_rd_total = float((rd_info or {}).get("peso_total_kg") or 0.0)
    peso_v_sum = sum(v["peso_kg"] for v in viagens_aterro)
    if peso_rd_total > peso_v_sum and viagens_aterro and peso_v_sum == 0.0:
        por_viagem = round(peso_rd_total / len(viagens_aterro), 1)
        tks_rd = (rd_info or {}).get("tickets") or []
        for idx_v, v in enumerate(viagens_aterro):
            v["peso_kg"] = por_viagem
            if idx_v < len(tks_rd) and not v["ticket"]:
                v["ticket"] = tks_rd[idx_v]

    for v in viagens_aterro:
        v.pop("_saida_dt", None)

    gar_tot = gar_pre + gar_pos + gar_int
    jornada_tot = gar_tot + set_tot + traj_tot + ate_tot + ref_tot + sos_tot + par_ext_tot

    saida_gar_dt = None
    chegada_gar_dt = None
    if primeiro_prod is not None:
        # Primeiro instante em que deixou a garagem rumo à operação
        for i in range(primeiro_prod, -1, -1):
            if blocos[i]["kind"] == "GARAGEM":
                saida_gar_dt = blocos[i]["end_dt"]
                break
        if saida_gar_dt is None:
            saida_gar_dt = blocos[0]["start_dt"]
    if ultimo_prod is not None:
        for i in range(ultimo_prod + 1, len(blocos)):
            if blocos[i]["kind"] == "GARAGEM":
                chegada_gar_dt = blocos[i]["start_dt"]
                break
        if chegada_gar_dt is None:
            chegada_gar_dt = blocos[-1]["end_dt"]

    # Fallback para sinal oficial SAIDA/CHEGADA GARAGEM do RD caso não houvesse bloco GARAGEM antes
    for tipo_g, dt_g in (rd_info or {}).get("garagem_sinais_dt") or []:
        if tipo_g == "SAIDA GARAGEM" and saida_gar_dt is None:
            saida_gar_dt = dt_g
        elif tipo_g == "CHEGADA GARAGEM":
            chegada_gar_dt = dt_g

    setores_dt_ini = [v["entrada_dt"] for v in setores_tempo.values()]
    setores_dt_fim = [v["saida_dt"] for v in setores_tempo.values()]

    return timeline_out, {
        "placa": placa,
        "jornada_total_s": round(jornada_tot, 1),
        "tempo_garagem_s": round(gar_tot, 1),
        "garagem_pre_saida_s": round(gar_pre, 1),
        "garagem_pos_retorno_s": round(gar_pos, 1),
        "garagem_intermediaria_s": round(gar_int, 1),
        "tempo_setor_s": round(set_tot, 1),
        "tempo_setor_mov_s": round(set_mov, 1),
        "tempo_setor_parado_s": round(set_par, 1),
        "tempo_trajeto_s": round(traj_tot, 1),
        "trajeto_garagem_setor_s": round(t_gar_set, 1),
        "trajeto_setor_aterro_s": round(t_set_ate, 1),
        "trajeto_aterro_setor_s": round(t_ate_set, 1),
        "trajeto_entre_setores_s": round(t_ent_set, 1),
        "trajeto_retorno_garagem_s": round(t_ret_gar, 1),
        "tempo_aterro_s": round(ate_tot, 1),
        "tempo_refeicao_s": round(ref_tot, 1),
        "tempo_sos_s": round(sos_tot, 1),
        "tempo_parada_externa_s": round(par_ext_tot, 1),
        "km_rodado_total": round(dist_total_m / 1000.0, 2),
        "km_trajeto": round(dist_traj_m / 1000.0, 2),
        "km_em_setor": round(dist_setor_m / 1000.0, 2),
        "saida_garagem_dt": saida_gar_dt,
        "chegada_garagem_dt": chegada_gar_dt,
        "primeiro_setor_dt": min(setores_dt_ini) if setores_dt_ini else None,
        "ultimo_setor_dt": max(setores_dt_fim) if setores_dt_fim else None,
        "setores_tempo": setores_tempo,
        "viagens_aterro": viagens_aterro,
    }


def _ler_cache_dia(chave: str, janela_fechada: bool, force_refresh: bool) -> Optional[Dict[str, Any]]:
    if force_refresh:
        return None
    conn = None
    try:
        conn = db.get_connection()
        row = conn.execute(
            "SELECT janela_fechada, criado_em, payload_gzip FROM painel_tempos_dia_cache WHERE chave = ?",
            (chave,),
        ).fetchone()
        if not row or not row["payload_gzip"]:
            return None
        if int(row["janela_fechada"] or 0) == 1 and janela_fechada:
            return json.loads(gzip.decompress(bytes(row["payload_gzip"])).decode("utf-8"))
        dt_c = _parse_dt(row["criado_em"])
        if dt_c and (datetime.now() - dt_c).total_seconds() < 90.0:
            return json.loads(gzip.decompress(bytes(row["payload_gzip"])).decode("utf-8"))
    except Exception:
        return None
    finally:
        if conn is not None:
            conn.close()
    return None


def _salvar_cache_dia(chave: str, contrato_id: str, data_iso: str, janela_fechada: bool, payload: Dict[str, Any]) -> None:
    if payload.get("resumo", {}).get("caminhoes_ativos", 0) == 0 and not janela_fechada:
        return
    try:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        gz = gzip.compress(raw, compresslevel=6)
        agora = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with db._write_lock:
            conn = None
            try:
                conn = db.get_connection()
                with conn:
                    conn.execute(
                        """
                        INSERT INTO painel_tempos_dia_cache (chave, contrato_id, data_iso, janela_fechada, criado_em, payload_gzip)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT (chave) DO UPDATE SET
                            janela_fechada = excluded.janela_fechada,
                            criado_em = excluded.criado_em,
                            payload_gzip = excluded.payload_gzip
                        """,
                        (chave, contrato_id, data_iso, 1 if janela_fechada else 0, agora, gz),
                    )
            except Exception as e:
                if db.eh_erro_disco_cheio(e):
                    db.liberar_espaco_emergencia_db(agressivo=False)
            finally:
                if conn is not None:
                    conn.close()
    except Exception as e:
        print(f"[TEMPOS] Aviso ao gravar cache do dia ({contrato_id} {data_iso}): {e}")


def _coletar_frota_local_db(
    inlog: Any,
    contrato_id: str,
    data_iso: str,
    win: Dict[str, Any],
) -> Tuple[List[str], Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """Fallback rápido 100% em banco local quando a API Inlog está offline/sem token."""
    veiculos_rt = inlog._ler_veiculos_tempo_real_db()
    rd_d = inlog._ler_relatorios_diarios_db(data_iso, contrato_id)
    rd_d1: List[Dict[str, Any]] = []
    if win["dia_seguinte"] <= datetime.now().strftime("%Y-%m-%d"):
        rd_d1 = inlog._ler_relatorios_diarios_db(win["dia_seguinte"], contrato_id)
    st_items = inlog._ler_setores_tempo_real_db(contrato_id)

    placas_set: Set[str] = set()
    for v in veiculos_rt:
        if inlog._filtrar_operacao_veiculo(v, contrato_id, "COLETA"):
            pl = formatar_placa_hifen(v.get("placaVeiculo") or v.get("identificadorVeiculo"))
            if pl:
                placas_set.add(pl)
    if not placas_set:
        for v in veiculos_rt:
            if inlog._filtrar_operacao_veiculo(v, contrato_id, "TODAS"):
                grupos = [normalizar_ascii(g.get("descricao")) for g in (v.get("gruposControle") or []) if normalizar_ascii(g.get("descricao"))]
                if any("ONIBUS" not in g and "OFICINA" not in g for g in grupos):
                    pl = formatar_placa_hifen(v.get("placaVeiculo") or v.get("identificadorVeiculo"))
                    if pl:
                        placas_set.add(pl)

    for item in rd_d + rd_d1:
        grupo_op = normalizar_ascii(f"{item.get('grupoControle', '')} {item.get('operacao', '')}")
        if "DOMICILIAR" in grupo_op or "SELETIVA" in grupo_op or "30 DIAS" in grupo_op:
            pl = formatar_placa_hifen(item.get("placa") or item.get("veiculo"))
            if pl:
                placas_set.add(pl)

    for item in st_items:
        macro_setor = normalizar_ascii(f"{item.get('macroSetor', '')} {item.get('setor', '')}")
        if "ENTULHO" not in macro_setor and "CAPINA" not in macro_setor and "ROCO" not in macro_setor:
            pl = formatar_placa_hifen(item.get("placaVeiculo") or item.get("identificadorVeiculo"))
            if pl:
                placas_set.add(pl)

    placas = sorted(placas_set)
    gps_por_placa: Dict[str, List[Dict[str, Any]]] = {}
    for pl in placas:
        pts = inlog._ler_historico_gps_intervalo_db(pl, win["data_inicio"], win["data_fim"])
        if pts:
            gps_por_placa[pl] = pts
    return placas, gps_por_placa, rd_d + rd_d1


def _montar_dia_sem_agendamento(
    cid: str,
    info_cid: Dict[str, Any],
    data_iso: str,
    win: Dict[str, Any],
    duracao_janela_op_s: float,
    janela_fechada: bool,
    dt_ini_op: datetime,
) -> Dict[str, Any]:
    return {
        "resumo": {
            "modo": "dia",
            "contrato_id": cid,
            "contrato_nome": info_cid["nome"],
            "data_iso": data_iso,
            "data_formatada": datetime.strptime(data_iso, "%Y-%m-%d").strftime("%d/%m/%Y"),
            "janela_operacional": {
                "hora_inicial": win["hora_inicial"],
                "hora_final": win["hora_final"],
                "data_inicio": win["data_inicio"],
                "data_fim": win["data_fim"],
                "duracao_s": duracao_janela_op_s,
                "janela_fechada": janela_fechada,
            },
            "primeira_saida_garagem": "--:--",
            "ultimo_retorno_garagem": "--:--",
            "hora_media_saida_diurno": "--:--",
            "hora_media_saida_noturno": "--:--",
            "caminhoes_ativos": 0,
            "setores_previstos": 0,
            "setores_executados": 0,
            "setores_concluidos": 0,
            "km_previsto_setores": 0.0,
            "km_coletado_setores": 0.0,
            "pct_cobertura_geral": 0.0,
            "km_rodado_total": 0.0,
            "km_trajeto_total": 0.0,
            "km_em_setor_total": 0.0,
            "peso_total_kg": 0.0,
            "qtd_viagens_aterro": 0,
            "jornada_total_s": 0.0,
            "jornada_media_caminhao_s": 0.0,
            "tempo_garagem_total_s": 0.0,
            "tempo_garagem_medio_caminhao_s": 0.0,
            "garagem_pre_saida_s": 0.0,
            "garagem_pos_retorno_s": 0.0,
            "garagem_intermediaria_s": 0.0,
            "tempo_setor_total_s": 0.0,
            "tempo_setor_mov_s": 0.0,
            "tempo_setor_parado_s": 0.0,
            "tempo_efetivo_medio_setor_s": 0.0,
            "duracao_janela_media_setor_s": 0.0,
            "tempo_trajeto_total_s": 0.0,
            "tempo_trajeto_medio_caminhao_s": 0.0,
            "trajeto_garagem_setor_s": 0.0,
            "trajeto_setor_aterro_s": 0.0,
            "trajeto_aterro_setor_s": 0.0,
            "trajeto_entre_setores_s": 0.0,
            "trajeto_retorno_garagem_s": 0.0,
            "tempo_aterro_total_s": 0.0,
            "tempo_aterro_medio_caminhao_s": 0.0,
            "tempo_medio_por_descarga_s": 0.0,
            "tempo_refeicao_total_s": 0.0,
            "tempo_sos_total_s": 0.0,
            "tempo_parada_externa_total_s": 0.0,
            "tempo_paradas_total_s": 0.0,
            "pct_tempo_setor": 0.0,
            "pct_tempo_trajeto": 0.0,
            "pct_tempo_aterro": 0.0,
            "pct_tempo_garagem": 0.0,
            "pct_tempo_paradas": 0.0,
            "vel_media_coleta_kmh": 0.0,
            "vel_media_trajeto_kmh": 0.0,
            "ritmo_medio_min_por_km": 0.0,
            "produtividade_media_km_h": 0.0,
            "zonas_detectadas": {"garagens": 0, "aterros": 0},
            "sem_setores_agendados": True,
        },
        "setores": [],
        "caminhoes": [],
        "viagens_aterro": [],
        "atividade_horaria": [
            {
                "hora_idx": h,
                "hora_rotulo": (dt_ini_op + timedelta(hours=h)).strftime("%H:00"),
                "em_garagem_min": 0.0,
                "em_trajeto_min": 0.0,
                "em_setor_min": 0.0,
                "em_aterro_min": 0.0,
                "em_parada_min": 0.0,
            }
            for h in range(25)
        ],
        "destaques": [],
    }


def calcular_painel_dia_cidade(
    engine: Any,
    contrato_id: str,
    data_iso: str,
    force_refresh: bool = False,
    segmentos_cidade: Optional[List[Dict[str, Any]]] = None,
    contrato_info: Optional[Dict[str, Any]] = None,
    usar_apenas_db_local: Optional[bool] = None,
) -> Dict[str, Any]:
    """Calcula o painel completo de métricas de tempo para uma cidade em um dia operacional."""
    cid = str(contrato_id or "").lower().strip()
    win = janela_operacional_cidade(cid, data_iso)
    agora_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    janela_fechada = agora_str >= str(win["data_fim"])
    cache_key = f"{cid}:{data_iso}:{VERSAO_CACHE_TEMPOS}"

    cached = _ler_cache_dia(cache_key, janela_fechada, force_refresh)
    if cached is not None:
        return cached

    info_cid = contrato_info or _obter_info_contrato(cid)
    ref_lat = info_cid["centro_lat"]
    ref_lon = info_cid["centro_lon"]
    segs_cidade = segmentos_cidade if segmentos_cidade is not None else indicadores._carregar_segmentos_cidade(cid)

    dt_ini_op = _parse_dt(win["data_inicio"]) or datetime.strptime(f"{data_iso}T05:00:00", "%Y-%m-%dT%H:%M:%S")
    dt_fim_op = _parse_dt(win["data_fim"]) or (dt_ini_op + timedelta(hours=24))
    duracao_janela_op_s = max(3600.0, (dt_fim_op - dt_ini_op).total_seconds())

    segs_ativos, sector_grid, setores_meta = _indexar_setores_xy(segs_cidade, data_iso, cid, ref_lat, ref_lon)

    if segs_cidade and not segs_ativos:
        vazio = _montar_dia_sem_agendamento(cid, info_cid, data_iso, win, duracao_janela_op_s, janela_fechada, dt_ini_op)
        _salvar_cache_dia(cache_key, cid, data_iso, janela_fechada, vazio)
        return vazio

    if usar_apenas_db_local is None:
        if engine.inlog.is_configured() and engine.inlog.get_token() is None:
            usar_apenas_db_local = True
        else:
            usar_apenas_db_local = False

    if usar_apenas_db_local:
        placas_frota, gps_bruto, rd_itens = _coletar_frota_local_db(engine.inlog, cid, data_iso, win)
    else:
        _, placas_frota, gps_bruto = engine._coletar_frota_e_gps_cidade(
            cid, data_iso, operacao="COLETA", force_refresh=force_refresh
        )
        # Busca Relatórios Diários do dia D e D+1 (turno noturno após meia-noite)
        rd_itens = []
        try:
            rd_itens.extend(engine.inlog.get_relatorio_diario(data_iso, contrato_id=cid, force_refresh=False) or [])
        except Exception:
            pass
        try:
            if win["dia_seguinte"] <= datetime.now().strftime("%Y-%m-%d"):
                rd_itens.extend(engine.inlog.get_relatorio_diario(win["dia_seguinte"], contrato_id=cid, force_refresh=False) or [])
        except Exception:
            pass

    gps_filtrado, placas_rejeitadas = _filtrar_gps_local(gps_bruto, ref_lat, ref_lon)

    rd_por_placa = _parse_rd_dia(
        rd_itens, dt_ini_op, dt_fim_op, cid, data_iso, set(setores_meta.keys())
    )
    for pl_rej in placas_rejeitadas:
        rd_por_placa.pop(pl_rej, None)

    # Inclui placas com RD real no dia mesmo se não vieram no gps_filtrado
    todas_placas_ativas = sorted(set(gps_filtrado.keys()) | {
        pl for pl, info in rd_por_placa.items() if info.get("controles_reais", 0) > 0
    })

    motoristas_map, veiculos_meta, tel_map, alarmes_map = _carregar_auxiliares_locais(
        cid, data_iso, win["dia_seguinte"], todas_placas_ativas
    )

    cobertura_setor, metros_setor_placa = _calcular_cobertura_espacial_setor_placa(
        engine, gps_filtrado, segs_ativos, ref_lat, ref_lon
    )

    garagens, aterros = _descobrir_zonas_cidade(
        cid, gps_filtrado, rd_por_placa, sector_grid, ref_lat, ref_lon
    )

    # Executa máquina de estados para cada caminhão ativo
    caminhoes_out: List[Dict[str, Any]] = []
    viagens_aterro_all: List[Dict[str, Any]] = []
    setor_caminhao_stats: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    atividade_horaria = [
        {
            "hora_idx": h,
            "hora_rotulo": (dt_ini_op + timedelta(hours=h)).strftime("%H:00"),
            "em_garagem_min": 0.0,
            "em_trajeto_min": 0.0,
            "em_setor_min": 0.0,
            "em_aterro_min": 0.0,
            "em_parada_min": 0.0,
        }
        for h in range(25)
    ]

    for placa in todas_placas_ativas:
        pts = gps_filtrado.get(placa, [])
        rd_p = rd_por_placa.get(placa)
        m_placa = {s_k: m_v for (s_k, pl_k), m_v in metros_setor_placa.items() if pl_k == placa}
        timeline, st = _construir_timeline_caminhao(
            placa=placa,
            pts=pts,
            rd_info=rd_p,
            garagens=garagens,
            aterros=aterros,
            sector_grid=sector_grid,
            ref_lat=ref_lat,
            ref_lon=ref_lon,
            dt_ini_op=dt_ini_op,
            dt_fim_op=dt_fim_op,
            metros_por_setor_placa=m_placa,
        )
        if st["jornada_total_s"] <= 0.0:
            continue

        # Preenche curva de atividade horária por hora da janela operacional
        for blk in timeline:
            b0 = blk["start_s"]
            b1 = blk["end_s"]
            h0 = max(0, min(24, int(b0 // 3600)))
            h1 = max(0, min(24, int(b1 // 3600)))
            k = blk["kind"]
            for h in range(h0, h1 + 1):
                hs = h * 3600.0
                he = (h + 1) * 3600.0
                overlap_min = max(0.0, min(b1, he) - max(b0, hs)) / 60.0
                if overlap_min <= 0:
                    continue
                if k == "GARAGEM":
                    atividade_horaria[h]["em_garagem_min"] += overlap_min
                elif k == "TRAJETO":
                    atividade_horaria[h]["em_trajeto_min"] += overlap_min
                elif k == "SETOR":
                    atividade_horaria[h]["em_setor_min"] += overlap_min
                elif k == "ATERRO":
                    atividade_horaria[h]["em_aterro_min"] += overlap_min
                else:
                    atividade_horaria[h]["em_parada_min"] += overlap_min

        v_meta = veiculos_meta.get(placa, {})
        mat = (rd_p or {}).get("matricula_motorista") or v_meta.get("matricula_rt") or ""
        mot_nome = motoristas_map.get(mat, "")
        if not mot_nome and pts:
            for p_g in pts:
                if p_g.get("motorista"):
                    mot_nome = str(p_g["motorista"]).strip()
                    break

        peso_total_kg = float((rd_p or {}).get("peso_total_kg") or 0.0)
        if peso_total_kg <= 0.0 and st["viagens_aterro"]:
            peso_total_kg = sum(float(v.get("peso_kg") or 0.0) for v in st["viagens_aterro"])

        tickets_rd = list((rd_p or {}).get("tickets") or [])
        for va in st["viagens_aterro"]:
            if va.get("ticket") and va["ticket"] not in tickets_rd:
                tickets_rd.append(va["ticket"])
            va_row = dict(va)
            va_row["motorista"] = mot_nome or (f"Mat. {mat}" if mat else "Não informado")
            viagens_aterro_all.append(va_row)

        # Lista de setores atendidos por este caminhão com tempo exato e km coletado
        setores_atendidos_veic: List[Dict[str, Any]] = []
        km_col_veic = 0.0
        for s_cod, s_dt in st["setores_tempo"].items():
            m_col_sp = float(metros_setor_placa.get((s_cod, placa), 0.0))
            km_col_sp = round(m_col_sp / 1000.0, 2)
            km_col_veic += km_col_sp
            dur_sp = float(s_dt["tempo_s"])
            vel_sp = round((s_dt["dist_m"] / 1000.0) / (s_dt["mov_s"] / 3600.0), 1) if s_dt["mov_s"] > 60.0 else 0.0
            setores_atendidos_veic.append({
                "setor": s_cod,
                "turno": (setores_meta.get(s_cod) or {}).get("turno") or "DIURNO",
                "entrada_hhmm": _fmt_hhmm(s_dt["entrada_dt"]),
                "saida_hhmm": _fmt_hhmm(s_dt["saida_dt"]),
                "entrada_s": round(_sec_na_janela(s_dt["entrada_dt"], dt_ini_op) or 0.0, 1),
                "saida_s": round(_sec_na_janela(s_dt["saida_dt"], dt_ini_op) or 0.0, 1),
                "tempo_s": round(dur_sp, 1),
                "mov_s": round(float(s_dt["mov_s"]), 1),
                "parado_s": round(float(s_dt["parado_s"]), 1),
                "passagens": int(s_dt["passagens"]),
                "km_coletado": km_col_sp,
                "vel_media_kmh": vel_sp,
            })
            setor_caminhao_stats[s_cod].append({
                "placa": placa,
                "motorista": mot_nome or (f"Mat. {mat}" if mat else "Não informado"),
                "modelo": v_meta.get("tipo") or v_meta.get("modelo") or "",
                "entrada_dt": s_dt["entrada_dt"],
                "saida_dt": s_dt["saida_dt"],
                "entrada_hhmm": _fmt_hhmm(s_dt["entrada_dt"]),
                "saida_hhmm": _fmt_hhmm(s_dt["saida_dt"]),
                "tempo_s": round(dur_sp, 1),
                "mov_s": round(float(s_dt["mov_s"]), 1),
                "parado_s": round(float(s_dt["parado_s"]), 1),
                "passagens": int(s_dt["passagens"]),
                "km_coletado": km_col_sp,
                "dist_m": float(s_dt["dist_m"]),
                "vel_media_kmh": vel_sp,
            })

        setores_atendidos_veic.sort(key=lambda x: -x["tempo_s"])
        jornada_s = st["jornada_total_s"]
        tempo_set_s = st["tempo_setor_s"]
        tempo_traj_s = st["tempo_trajeto_s"]
        tempo_ate_s = st["tempo_aterro_s"]
        qtd_v_ate = len(st["viagens_aterro"])

        pct_eficiencia = round(tempo_set_s / jornada_s * 100.0, 1) if jornada_s > 0 else 0.0
        pct_km_prod = round(km_col_veic / st["km_rodado_total"] * 100.0, 1) if st["km_rodado_total"] > 0 else 0.0
        vel_coleta = round(st["km_em_setor"] / (st["tempo_setor_mov_s"] / 3600.0), 1) if st["tempo_setor_mov_s"] > 60.0 else 0.0
        vel_trajeto = round(st["km_trajeto"] / (tempo_traj_s / 3600.0), 1) if tempo_traj_s > 60.0 else 0.0
        tel_p = tel_map.get(placa, {})

        # Turno predominante do caminhão pelo horário de saída ou setores atendidos
        turnos_set = [sa["turno"] for sa in setores_atendidos_veic]
        if turnos_set:
            turno_veic = Counter(turnos_set).most_common(1)[0][0]
        elif st["saida_garagem_dt"] and st["saida_garagem_dt"].hour >= 16:
            turno_veic = "NOTURNO"
        else:
            turno_veic = "DIURNO"

        caminhoes_out.append({
            "placa": placa,
            "tipo_veiculo": v_meta.get("tipo") or v_meta.get("modelo") or "Coletor",
            "modelo": f"{v_meta.get('fabricante', '')} {v_meta.get('modelo', '')}".strip(),
            "motorista": mot_nome or (f"Mat. {mat}" if mat else "Não informado"),
            "matricula": mat,
            "turno": turno_veic,
            "saida_garagem": _fmt_dt(st["saida_garagem_dt"]),
            "saida_garagem_hhmm": _fmt_hhmm(st["saida_garagem_dt"]),
            "saida_garagem_s": _sec_na_janela(st["saida_garagem_dt"], dt_ini_op),
            "chegada_garagem": _fmt_dt(st["chegada_garagem_dt"]),
            "chegada_garagem_hhmm": _fmt_hhmm(st["chegada_garagem_dt"]),
            "chegada_garagem_s": _sec_na_janela(st["chegada_garagem_dt"], dt_ini_op),
            "primeiro_setor_hhmm": _fmt_hhmm(st["primeiro_setor_dt"]),
            "ultimo_setor_hhmm": _fmt_hhmm(st["ultimo_setor_dt"]),
            "jornada_total_s": jornada_s,
            "tempo_garagem_s": st["tempo_garagem_s"],
            "garagem_pre_saida_s": st["garagem_pre_saida_s"],
            "garagem_pos_retorno_s": st["garagem_pos_retorno_s"],
            "garagem_intermediaria_s": st["garagem_intermediaria_s"],
            "tempo_setor_s": tempo_set_s,
            "tempo_setor_mov_s": st["tempo_setor_mov_s"],
            "tempo_setor_parado_s": st["tempo_setor_parado_s"],
            "tempo_trajeto_s": tempo_traj_s,
            "trajeto_garagem_setor_s": st["trajeto_garagem_setor_s"],
            "trajeto_setor_aterro_s": st["trajeto_setor_aterro_s"],
            "trajeto_aterro_setor_s": st["trajeto_aterro_setor_s"],
            "trajeto_entre_setores_s": st["trajeto_entre_setores_s"],
            "trajeto_retorno_garagem_s": st["trajeto_retorno_garagem_s"],
            "tempo_aterro_s": tempo_ate_s,
            "qtd_viagens_aterro": qtd_v_ate,
            "tempo_medio_aterro_s": round(tempo_ate_s / qtd_v_ate, 1) if qtd_v_ate > 0 else 0.0,
            "tempo_refeicao_s": st["tempo_refeicao_s"],
            "tempo_sos_s": st["tempo_sos_s"],
            "tempo_parada_externa_s": st["tempo_parada_externa_s"],
            "tempo_paradas_total_s": round(st["tempo_refeicao_s"] + st["tempo_sos_s"] + st["tempo_parada_externa_s"], 1),
            "peso_total_kg": round(peso_total_kg, 1),
            "tickets": tickets_rd,
            "km_rodado_total": st["km_rodado_total"],
            "km_trajeto": st["km_trajeto"],
            "km_em_setor": st["km_em_setor"],
            "km_coletado": round(km_col_veic, 2),
            "pct_eficiencia_produtiva": pct_eficiencia,
            "pct_km_produtivo": pct_km_prod,
            "vel_media_coleta_kmh": vel_coleta,
            "vel_media_trajeto_kmh": vel_trajeto,
            "pct_faixa_verde": tel_p.get("pct_faixa_verde"),
            "qtd_alarmes": alarmes_map.get(placa, 0),
            "qtd_setores": len(setores_atendidos_veic),
            "setores_atendidos": setores_atendidos_veic,
            "viagens_aterro": st["viagens_aterro"],
            "timeline": timeline,
        })

    # Constrói relação completa de Setores (mesmo setores agendados sem caminhão para visibilidade de pendência)
    todos_setores_cod = sorted(set(setores_meta.keys()) | set(setor_caminhao_stats.keys()), key=indicadores._chave_ordenacao_setor)
    setores_out: List[Dict[str, Any]] = []

    # Mapeia intervalos timeline de cada setor por caminhão para montar Gantt por Setor
    setor_timeline_raw: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for cam in caminhoes_out:
        for blk in cam["timeline"]:
            if blk["kind"] == "SETOR" and blk.get("setor"):
                setor_timeline_raw[blk["setor"]].append({
                    "placa": cam["placa"],
                    "start": blk["start"],
                    "end": blk["end"],
                    "start_hhmm": blk["start_hhmm"],
                    "end_hhmm": blk["end_hhmm"],
                    "start_s": blk["start_s"],
                    "end_s": blk["end_s"],
                    "dur_s": blk["dur_s"],
                    "mov_s": blk["mov_s"],
                    "parado_s": blk["parado_s"],
                })

    for s_cod in todos_setores_cod:
        s_meta = setores_meta.get(s_cod, {"codigo": s_cod, "turno": "DIURNO", "agenda_rotulo": ""})
        cob = cobertura_setor.get(s_cod, {
            "km_previsto": 0.0,
            "km_coletado": 0.0,
            "km_manual": 0.0,
            "km_falha_gps": 0.0,
            "km_omissao": 0.0,
            "pct_cobertura": 0.0,
            "pct_manual": 0.0,
            "total_vias": 0,
        })
        cam_list = setor_caminhao_stats.get(s_cod, [])
        tempo_efetivo_s = sum(c["tempo_s"] for c in cam_list)
        tempo_mov_s = sum(c["mov_s"] for c in cam_list)
        tempo_par_s = sum(c["parado_s"] for c in cam_list)
        dist_set_m = sum(c["dist_m"] for c in cam_list)

        if cam_list:
            dt_ini_s = min(c["entrada_dt"] for c in cam_list)
            dt_fim_s = max(c["saida_dt"] for c in cam_list)
            janela_s = max(0.0, (dt_fim_s - dt_ini_s).total_seconds())
            # Intervalo externo = tempo fora do setor entre o início e o fim (ex.: descarga no aterro)
            # Quando há 2 caminhões simultâneos, a soma dos tempos individuais pode superar a janela; limita >= 0
            tl_set = sorted(setor_timeline_raw.get(s_cod, []), key=lambda x: x["start_s"])
            ocupado_uniao_s = 0.0
            cur_a, cur_b = None, None
            for seg_t in tl_set:
                if cur_a is None:
                    cur_a, cur_b = seg_t["start_s"], seg_t["end_s"]
                elif seg_t["start_s"] <= cur_b:
                    cur_b = max(cur_b, seg_t["end_s"])
                else:
                    ocupado_uniao_s += (cur_b - cur_a)
                    cur_a, cur_b = seg_t["start_s"], seg_t["end_s"]
            if cur_a is not None:
                ocupado_uniao_s += (cur_b - cur_a)
            intervalo_externo_s = max(0.0, janela_s - ocupado_uniao_s)
        else:
            dt_ini_s = None
            dt_fim_s = None
            janela_s = 0.0
            intervalo_externo_s = 0.0

        # Ordena caminhões do setor por km coletado e depois por tempo no setor
        cam_ord = sorted(cam_list, key=lambda c: (-c["km_coletado"], -c["tempo_s"], c["placa"]))
        caminhoes_setor_fmt: List[Dict[str, Any]] = []
        for idx_c, c in enumerate(cam_ord):
            pct_tempo = round(c["tempo_s"] / tempo_efetivo_s * 100.0, 1) if tempo_efetivo_s > 0 else 0.0
            pct_cob_c = round(c["km_coletado"] / cob["km_previsto"] * 100.0, 1) if cob["km_previsto"] > 0 else 0.0
            caminhoes_setor_fmt.append({
                "placa": c["placa"],
                "papel": "Principal" if idx_c == 0 else f"Apoio {idx_c}",
                "eh_principal": idx_c == 0,
                "motorista": c["motorista"],
                "modelo": c["modelo"],
                "entrada_hhmm": c["entrada_hhmm"],
                "saida_hhmm": c["saida_hhmm"],
                "tempo_s": round(c["tempo_s"], 1),
                "mov_s": round(c["mov_s"], 1),
                "parado_s": round(c["parado_s"], 1),
                "pct_tempo_setor": pct_tempo,
                "passagens": c["passagens"],
                "km_coletado": c["km_coletado"],
                "pct_cobertura_setor": pct_cob_c,
                "vel_media_kmh": c["vel_media_kmh"],
            })

        km_col = float(cob["km_coletado"])
        prod_km_h = round(km_col / (tempo_efetivo_s / 3600.0), 2) if tempo_efetivo_s > 60.0 and km_col > 0 else 0.0
        ritmo_min_km = round((tempo_efetivo_s / 60.0) / km_col, 1) if km_col > 0.05 and tempo_efetivo_s > 0 else 0.0
        vel_media_s = round((dist_set_m / 1000.0) / (tempo_mov_s / 3600.0), 1) if tempo_mov_s > 60.0 else 0.0

        # Peso estimado do setor (soma de viagens ao aterro cujo setor_origem == s_cod)
        peso_set_kg = sum(float(v.get("peso_kg") or 0.0) for v in viagens_aterro_all if v.get("setor_origem") == s_cod)

        if not cam_list:
            status_exec = "NAO_INICIADO"
        elif cob["pct_cobertura"] >= 80.0:
            status_exec = "CONCLUIDO"
        else:
            status_exec = "PARCIAL"

        setores_out.append({
            "codigo": s_cod,
            "turno": s_meta.get("turno", "DIURNO"),
            "agenda_rotulo": s_meta.get("agenda_rotulo", ""),
            "status_execucao": status_exec,
            "hora_inicio": _fmt_dt(dt_ini_s),
            "hora_inicio_hhmm": _fmt_hhmm(dt_ini_s),
            "hora_inicio_s": _sec_na_janela(dt_ini_s, dt_ini_op),
            "hora_fim": _fmt_dt(dt_fim_s),
            "hora_fim_hhmm": _fmt_hhmm(dt_fim_s),
            "hora_fim_s": _sec_na_janela(dt_fim_s, dt_ini_op),
            "duracao_janela_s": round(janela_s, 1),
            "tempo_efetivo_s": round(tempo_efetivo_s, 1),
            "tempo_movimento_s": round(tempo_mov_s, 1),
            "tempo_parado_s": round(tempo_par_s, 1),
            "tempo_intervalo_externo_s": round(intervalo_externo_s, 1),
            "km_previsto": cob["km_previsto"],
            "km_coletado": km_col,
            "km_omissao": cob["km_omissao"],
            "pct_cobertura": cob["pct_cobertura"],
            "pct_manual": cob["pct_manual"],
            "total_vias": cob["total_vias"],
            "produtividade_km_h": prod_km_h,
            "ritmo_min_por_km": ritmo_min_km,
            "vel_media_kmh": vel_media_s,
            "peso_kg": round(peso_set_kg, 1),
            "qtd_caminhoes": len(caminhoes_setor_fmt),
            "placa_principal": caminhoes_setor_fmt[0]["placa"] if caminhoes_setor_fmt else None,
            "caminhoes": caminhoes_setor_fmt,
            "timeline": sorted(setor_timeline_raw.get(s_cod, []), key=lambda x: x["start_s"]),
        })

    # Consolida resumo executivo do dia
    n_cam = len(caminhoes_out)
    set_com_exec = [s for s in setores_out if s["qtd_caminhoes"] > 0]
    n_set_exec = len(set_com_exec)

    tot_jornada = sum(c["jornada_total_s"] for c in caminhoes_out)
    tot_garagem = sum(c["tempo_garagem_s"] for c in caminhoes_out)
    tot_gar_pre = sum(c["garagem_pre_saida_s"] for c in caminhoes_out)
    tot_gar_pos = sum(c["garagem_pos_retorno_s"] for c in caminhoes_out)
    tot_gar_int = sum(c["garagem_intermediaria_s"] for c in caminhoes_out)
    tot_setor = sum(c["tempo_setor_s"] for c in caminhoes_out)
    tot_setor_mov = sum(c["tempo_setor_mov_s"] for c in caminhoes_out)
    tot_setor_par = sum(c["tempo_setor_parado_s"] for c in caminhoes_out)
    tot_trajeto = sum(c["tempo_trajeto_s"] for c in caminhoes_out)
    tot_t_gar_set = sum(c["trajeto_garagem_setor_s"] for c in caminhoes_out)
    tot_t_set_ate = sum(c["trajeto_setor_aterro_s"] for c in caminhoes_out)
    tot_t_ate_set = sum(c["trajeto_aterro_setor_s"] for c in caminhoes_out)
    tot_t_ent_set = sum(c["trajeto_entre_setores_s"] for c in caminhoes_out)
    tot_t_ret_gar = sum(c["trajeto_retorno_garagem_s"] for c in caminhoes_out)
    tot_aterro = sum(c["tempo_aterro_s"] for c in caminhoes_out)
    tot_refeicao = sum(c["tempo_refeicao_s"] for c in caminhoes_out)
    tot_sos = sum(c["tempo_sos_s"] for c in caminhoes_out)
    tot_par_ext = sum(c["tempo_parada_externa_s"] for c in caminhoes_out)
    tot_paradas = tot_refeicao + tot_sos + tot_par_ext

    tot_viagens_ate = len(viagens_aterro_all)
    tot_peso_kg = sum(c["peso_total_kg"] for c in caminhoes_out)
    tot_km_rodado = sum(c["km_rodado_total"] for c in caminhoes_out)
    tot_km_traj = sum(c["km_trajeto"] for c in caminhoes_out)
    tot_km_em_setor = sum(c["km_em_setor"] for c in caminhoes_out)

    tot_km_prev_set = sum(s["km_previsto"] for s in setores_out)
    tot_km_col_set = sum(s["km_coletado"] for s in setores_out)
    pct_cob_geral = round(tot_km_col_set / tot_km_prev_set * 100.0, 1) if tot_km_prev_set > 0 else 0.0

    saidas_s = [c["saida_garagem_s"] for c in caminhoes_out if c["saida_garagem_s"] is not None]
    chegadas_s = [c["chegada_garagem_s"] for c in caminhoes_out if c["chegada_garagem_s"] is not None]
    saidas_diurno = [c["saida_garagem_s"] for c in caminhoes_out if c["saida_garagem_s"] is not None and c["turno"] == "DIURNO"]
    saidas_noturno = [c["saida_garagem_s"] for c in caminhoes_out if c["saida_garagem_s"] is not None and c["turno"] == "NOTURNO"]

    dur_janela_setores = sum(s["duracao_janela_s"] for s in set_com_exec)
    dur_efetiva_setores = sum(s["tempo_efetivo_s"] for s in set_com_exec)

    saidas_str = [c["saida_garagem"] for c in caminhoes_out if c["saida_garagem"]]
    chegadas_str = [c["chegada_garagem"] for c in caminhoes_out if c["chegada_garagem"]]

    caminhoes_out.sort(key=lambda c: (-c["tempo_setor_s"], c["placa"]))
    viagens_aterro_all.sort(key=lambda v: v["chegada"])

    for ah in atividade_horaria:
        for fk in ("em_garagem_min", "em_trajeto_min", "em_setor_min", "em_aterro_min", "em_parada_min"):
            ah[fk] = round(ah[fk], 1)

    resumo = {
        "modo": "dia",
        "contrato_id": cid,
        "contrato_nome": info_cid["nome"],
        "data_iso": data_iso,
        "data_formatada": datetime.strptime(data_iso, "%Y-%m-%d").strftime("%d/%m/%Y"),
        "janela_operacional": {
            "hora_inicial": win["hora_inicial"],
            "hora_final": win["hora_final"],
            "data_inicio": win["data_inicio"],
            "data_fim": win["data_fim"],
            "duracao_s": duracao_janela_op_s,
            "janela_fechada": janela_fechada,
        },
        "primeira_saida_garagem": min(saidas_str)[11:16] if saidas_str else "--:--",
        "ultimo_retorno_garagem": max(chegadas_str)[11:16] if chegadas_str else "--:--",
        "hora_media_saida_diurno": _media_circular_horas_s(
            [(dt_ini_op + timedelta(seconds=s)).hour * 3600 + (dt_ini_op + timedelta(seconds=s)).minute * 60 for s in saidas_diurno]
        ) or "--:--",
        "hora_media_saida_noturno": _media_circular_horas_s(
            [(dt_ini_op + timedelta(seconds=s)).hour * 3600 + (dt_ini_op + timedelta(seconds=s)).minute * 60 for s in saidas_noturno]
        ) or "--:--",
        "caminhoes_ativos": n_cam,
        "setores_previstos": len(setores_out),
        "setores_executados": n_set_exec,
        "setores_concluidos": sum(1 for s in setores_out if s["status_execucao"] == "CONCLUIDO"),
        "km_previsto_setores": round(tot_km_prev_set, 2),
        "km_coletado_setores": round(tot_km_col_set, 2),
        "pct_cobertura_geral": pct_cob_geral,
        "km_rodado_total": round(tot_km_rodado, 2),
        "km_trajeto_total": round(tot_km_traj, 2),
        "km_em_setor_total": round(tot_km_em_setor, 2),
        "peso_total_kg": round(tot_peso_kg, 1),
        "qtd_viagens_aterro": tot_viagens_ate,
        "jornada_total_s": round(tot_jornada, 1),
        "jornada_media_caminhao_s": round(tot_jornada / n_cam, 1) if n_cam > 0 else 0.0,
        "tempo_garagem_total_s": round(tot_garagem, 1),
        "tempo_garagem_medio_caminhao_s": round(tot_garagem / n_cam, 1) if n_cam > 0 else 0.0,
        "garagem_pre_saida_s": round(tot_gar_pre, 1),
        "garagem_pos_retorno_s": round(tot_gar_pos, 1),
        "garagem_intermediaria_s": round(tot_gar_int, 1),
        "tempo_setor_total_s": round(tot_setor, 1),
        "tempo_setor_mov_s": round(tot_setor_mov, 1),
        "tempo_setor_parado_s": round(tot_setor_par, 1),
        "tempo_efetivo_medio_setor_s": round(dur_efetiva_setores / n_set_exec, 1) if n_set_exec > 0 else 0.0,
        "duracao_janela_media_setor_s": round(dur_janela_setores / n_set_exec, 1) if n_set_exec > 0 else 0.0,
        "tempo_trajeto_total_s": round(tot_trajeto, 1),
        "tempo_trajeto_medio_caminhao_s": round(tot_trajeto / n_cam, 1) if n_cam > 0 else 0.0,
        "trajeto_garagem_setor_s": round(tot_t_gar_set, 1),
        "trajeto_setor_aterro_s": round(tot_t_set_ate, 1),
        "trajeto_aterro_setor_s": round(tot_t_ate_set, 1),
        "trajeto_entre_setores_s": round(tot_t_ent_set, 1),
        "trajeto_retorno_garagem_s": round(tot_t_ret_gar, 1),
        "tempo_aterro_total_s": round(tot_aterro, 1),
        "tempo_aterro_medio_caminhao_s": round(tot_aterro / n_cam, 1) if n_cam > 0 else 0.0,
        "tempo_medio_por_descarga_s": round(tot_aterro / tot_viagens_ate, 1) if tot_viagens_ate > 0 else 0.0,
        "tempo_refeicao_total_s": round(tot_refeicao, 1),
        "tempo_sos_total_s": round(tot_sos, 1),
        "tempo_parada_externa_total_s": round(tot_par_ext, 1),
        "tempo_paradas_total_s": round(tot_paradas, 1),
        "pct_tempo_setor": round(tot_setor / tot_jornada * 100.0, 1) if tot_jornada > 0 else 0.0,
        "pct_tempo_trajeto": round(tot_trajeto / tot_jornada * 100.0, 1) if tot_jornada > 0 else 0.0,
        "pct_tempo_aterro": round(tot_aterro / tot_jornada * 100.0, 1) if tot_jornada > 0 else 0.0,
        "pct_tempo_garagem": round(tot_garagem / tot_jornada * 100.0, 1) if tot_jornada > 0 else 0.0,
        "pct_tempo_paradas": round(tot_paradas / tot_jornada * 100.0, 1) if tot_jornada > 0 else 0.0,
        "vel_media_coleta_kmh": round(tot_km_em_setor / (tot_setor_mov / 3600.0), 1) if tot_setor_mov > 60.0 else 0.0,
        "vel_media_trajeto_kmh": round(tot_km_traj / (tot_trajeto / 3600.0), 1) if tot_trajeto > 60.0 else 0.0,
        "ritmo_medio_min_por_km": round((tot_setor / 60.0) / tot_km_col_set, 1) if tot_km_col_set > 0.1 else 0.0,
        "produtividade_media_km_h": round(tot_km_col_set / (tot_setor / 3600.0), 2) if tot_setor > 60.0 and tot_km_col_set > 0 else 0.0,
        "zonas_detectadas": {
            "garagens": len(garagens),
            "aterros": len(aterros),
        },
        "sem_setores_agendados": len(setores_out) == 0,
    }

    destaques = _gerar_destaques_executivos(resumo, setores_out, caminhoes_out, viagens_aterro_all)

    resultado = {
        "resumo": resumo,
        "setores": setores_out,
        "caminhoes": caminhoes_out,
        "viagens_aterro": viagens_aterro_all,
        "atividade_horaria": atividade_horaria,
        "destaques": destaques,
    }
    _salvar_cache_dia(cache_key, cid, data_iso, janela_fechada, resultado)
    return resultado


def _gerar_destaques_executivos(
    resumo: Dict[str, Any],
    setores: List[Dict[str, Any]],
    caminhoes: List[Dict[str, Any]],
    viagens_aterro: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    destaques: List[Dict[str, str]] = []
    set_exec = [s for s in setores if s.get("tempo_efetivo_s", 0) > 0]
    if set_exec:
        mais_longo = max(set_exec, key=lambda s: s["tempo_efetivo_s"])
        mais_rapido = min(set_exec, key=lambda s: s["tempo_efetivo_s"])
        destaques.append({
            "tipo": "info",
            "titulo": "Setor Mais Extenso em Tempo",
            "texto": f"{mais_longo['codigo']} consumiu {_fmt_duracao(mais_longo['tempo_efetivo_s'])} efetivos ({mais_longo['qtd_caminhoes']} caminhão(ões), {mais_longo['pct_cobertura']}% cobertura).",
        })
        destaques.append({
            "tipo": "ok",
            "titulo": "Setor Mais Ágil",
            "texto": f"{mais_rapido['codigo']} foi concluído em {_fmt_duracao(mais_rapido['tempo_efetivo_s'])} efetivos ({mais_rapido['km_coletado']} km coletados).",
        })
        com_apoio = [s for s in set_exec if s["qtd_caminhoes"] > 1]
        if com_apoio:
            top_ap = max(com_apoio, key=lambda s: s["qtd_caminhoes"])
            destaques.append({
                "tipo": "alerta",
                "titulo": "Setores com Apoio de Frota",
                "texto": f"{len(com_apoio)} setor(es) receberam caminhões de apoio; destaque para {top_ap['codigo']} com {top_ap['qtd_caminhoes']} veículos atuando.",
            })
    if viagens_aterro:
        maior_fila = max(viagens_aterro, key=lambda v: v.get("duracao_s", 0))
        destaques.append({
            "tipo": "aterro",
            "titulo": "Ciclo de Aterro & Descarga",
            "texto": f"{len(viagens_aterro)} descargas registradas com média de {_fmt_duracao(resumo['tempo_medio_por_descarga_s'])} cada; maior permanência: {maior_fila['placa']} ({_fmt_duracao(maior_fila['duracao_s'])} às {maior_fila['chegada_hhmm']}).",
        })
    if caminhoes:
        mais_ef = max(caminhoes, key=lambda c: c.get("pct_eficiencia_produtiva", 0))
        destaques.append({
            "tipo": "eficiencia",
            "titulo": "Eficiência Operacional da Frota",
            "texto": f"{resumo['pct_tempo_setor']}% do tempo da frota em coleta e {resumo['pct_tempo_trajeto']}% em deslocamento. Destaque: {mais_ef['placa']} ({mais_ef['pct_eficiencia_produtiva']}% produtivo).",
        })
    return destaques


def consolidar_painel_mes_cidade(
    engine: Any,
    contrato_id: str,
    mes_str: Optional[str] = None,
    data_str: Optional[str] = None,
    force_refresh: bool = False,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Consolida todas as métricas de tempo de uma cidade ao longo de um mês."""
    cid = str(contrato_id or "").lower().strip()
    periodo = resolver_periodo_tempos(modo="mes", data_str=data_str, mes_str=mes_str)
    dias_iso = periodo["dias_iso"]
    info_cid = _obter_info_contrato(cid)
    segs_cidade = indicadores._carregar_segmentos_cidade(cid)

    total_dias = max(1, len(dias_iso))
    resultados_dia: Dict[str, Dict[str, Any]] = {}
    concluidos = 0

    def _emitir(dt_atual: str) -> None:
        if progress_cb:
            progress_cb({
                "progresso_pct": round(min(99.0, concluidos / total_dias * 100.0), 1),
                "data_atual": dt_atual,
                "etapas_concluidas": concluidos,
                "etapas_total": total_dias,
            })

    _emitir(dias_iso[0] if dias_iso else "")

    usar_db_local = bool(engine.inlog.is_configured() and engine.inlog.get_token() is None)

    with ThreadPoolExecutor(max_workers=min(3, total_dias)) as pool:
        futs = {
            pool.submit(
                calcular_painel_dia_cidade,
                engine,
                cid,
                d_iso,
                force_refresh,
                segs_cidade,
                info_cid,
                usar_db_local,
            ): d_iso
            for d_iso in dias_iso
        }
        for fut in as_completed(futs):
            d_iso = futs[fut]
            try:
                resultados_dia[d_iso] = fut.result()
            except Exception as e:
                print(f"[TEMPOS] Aviso ao processar {cid} {d_iso} no mês: {e}")
            concluidos += 1
            _emitir(d_iso)

    # Agregadores mensais
    set_agg: Dict[str, Dict[str, Any]] = {}
    set_cam_agg: Dict[Tuple[str, str], Dict[str, Any]] = {}
    cam_agg: Dict[str, Dict[str, Any]] = {}
    cam_set_agg: Dict[Tuple[str, str], Dict[str, Any]] = {}
    evolucao_diaria: List[Dict[str, Any]] = []
    viagens_mes: List[Dict[str, Any]] = []
    atividade_horaria_mes = [
        {
            "hora_idx": h,
            "hora_rotulo": f"{h:02d}:00",
            "em_garagem_min": 0.0,
            "em_trajeto_min": 0.0,
            "em_setor_min": 0.0,
            "em_aterro_min": 0.0,
            "em_parada_min": 0.0,
        }
        for h in range(25)
    ]

    dias_operacionais = 0
    cam_dias_total = 0
    set_dias_exec_total = 0

    totais = defaultdict(float)
    saidas_diurno_s: List[float] = []
    saidas_noturno_s: List[float] = []

    for d_iso in dias_iso:
        dia_res = resultados_dia.get(d_iso)
        dt_obj = datetime.strptime(d_iso, "%Y-%m-%d").date()
        dia_sem = _NOMES_DIAS_CURTOS[dt_obj.weekday()]

        if not dia_res:
            evolucao_diaria.append({
                "data_iso": d_iso,
                "data_fmt": dt_obj.strftime("%d/%m"),
                "dia_semana": dia_sem,
                "sem_operacao": True,
                "caminhoes_ativos": 0,
                "setores_previstos": 0,
                "setores_executados": 0,
                "pct_cobertura": 0.0,
                "tempo_setor_s": 0.0,
                "tempo_trajeto_s": 0.0,
                "tempo_aterro_s": 0.0,
                "tempo_garagem_s": 0.0,
                "tempo_paradas_s": 0.0,
                "duracao_media_setor_s": 0.0,
                "peso_total_kg": 0.0,
                "viagens_aterro": 0,
            })
            continue

        r = dia_res["resumo"]
        if r.get("caminhoes_ativos", 0) > 0:
            dias_operacionais += 1
        cam_dias_total += int(r.get("caminhoes_ativos", 0))
        set_dias_exec_total += int(r.get("setores_executados", 0))

        for k in (
            "jornada_total_s", "tempo_garagem_total_s", "garagem_pre_saida_s",
            "garagem_pos_retorno_s", "garagem_intermediaria_s",
            "tempo_setor_total_s", "tempo_setor_mov_s", "tempo_setor_parado_s",
            "tempo_trajeto_total_s", "trajeto_garagem_setor_s", "trajeto_setor_aterro_s",
            "trajeto_aterro_setor_s", "trajeto_entre_setores_s", "trajeto_retorno_garagem_s",
            "tempo_aterro_total_s", "tempo_refeicao_total_s", "tempo_sos_total_s",
            "tempo_parada_externa_total_s", "tempo_paradas_total_s",
            "km_rodado_total", "km_trajeto_total", "km_em_setor_total",
            "km_previsto_setores", "km_coletado_setores", "peso_total_kg", "qtd_viagens_aterro",
        ):
            totais[k] += float(r.get(k) or 0.0)

        for idx_h, ah in enumerate(dia_res.get("atividade_horaria") or []):
            if idx_h < len(atividade_horaria_mes):
                for fk in ("em_garagem_min", "em_trajeto_min", "em_setor_min", "em_aterro_min", "em_parada_min"):
                    atividade_horaria_mes[idx_h][fk] += float(ah.get(fk) or 0.0)

        evolucao_diaria.append({
            "data_iso": d_iso,
            "data_fmt": dt_obj.strftime("%d/%m"),
            "dia_semana": dia_sem,
            "sem_operacao": bool(r.get("sem_setores_agendados") and r.get("caminhoes_ativos", 0) == 0),
            "caminhoes_ativos": int(r.get("caminhoes_ativos") or 0),
            "setores_previstos": int(r.get("setores_previstos") or 0),
            "setores_executados": int(r.get("setores_executados") or 0),
            "pct_cobertura": float(r.get("pct_cobertura_geral") or 0.0),
            "tempo_setor_s": float(r.get("tempo_setor_total_s") or 0.0),
            "tempo_trajeto_s": float(r.get("tempo_trajeto_total_s") or 0.0),
            "tempo_aterro_s": float(r.get("tempo_aterro_total_s") or 0.0),
            "tempo_garagem_s": float(r.get("tempo_garagem_total_s") or 0.0),
            "tempo_paradas_s": float(r.get("tempo_paradas_total_s") or 0.0),
            "duracao_media_setor_s": float(r.get("tempo_efetivo_medio_setor_s") or 0.0),
            "peso_total_kg": float(r.get("peso_total_kg") or 0.0),
            "viagens_aterro": int(r.get("qtd_viagens_aterro") or 0),
        })

        for v in dia_res.get("viagens_aterro") or []:
            v_copy = dict(v)
            v_copy["data_iso"] = d_iso
            v_copy["data_fmt"] = dt_obj.strftime("%d/%m")
            viagens_mes.append(v_copy)

        # Agrega Setores do Dia -> Mês
        for s in dia_res.get("setores") or []:
            sc = s["codigo"]
            sa = set_agg.setdefault(sc, {
                "codigo": sc,
                "turno": s.get("turno", "DIURNO"),
                "agenda_rotulo": s.get("agenda_rotulo", ""),
                "dias_previstos": 0,
                "dias_executados": 0,
                "dias_concluidos": 0,
                "inicios_s": [],
                "fins_s": [],
                "janelas_s": [],
                "efetivos_s": [],
                "tempo_efetivo_total_s": 0.0,
                "tempo_movimento_total_s": 0.0,
                "tempo_parado_total_s": 0.0,
                "intervalo_externo_total_s": 0.0,
                "km_previsto_total": 0.0,
                "km_coletado_total": 0.0,
                "km_omissao_total": 0.0,
                "peso_total_kg": 0.0,
            })
            sa["dias_previstos"] += 1
            sa["km_previsto_total"] += float(s.get("km_previsto") or 0.0)
            sa["km_coletado_total"] += float(s.get("km_coletado") or 0.0)
            sa["km_omissao_total"] += float(s.get("km_omissao") or 0.0)
            sa["peso_total_kg"] += float(s.get("peso_kg") or 0.0)

            if s.get("qtd_caminhoes", 0) > 0:
                sa["dias_executados"] += 1
                if s.get("status_execucao") == "CONCLUIDO":
                    sa["dias_concluidos"] += 1
                if s.get("hora_inicio_s") is not None:
                    sa["inicios_s"].append(float(s["hora_inicio_s"]))
                if s.get("hora_fim_s") is not None:
                    sa["fins_s"].append(float(s["hora_fim_s"]))
                sa["janelas_s"].append(float(s.get("duracao_janela_s") or 0.0))
                sa["efetivos_s"].append(float(s.get("tempo_efetivo_s") or 0.0))
                sa["tempo_efetivo_total_s"] += float(s.get("tempo_efetivo_s") or 0.0)
                sa["tempo_movimento_total_s"] += float(s.get("tempo_movimento_s") or 0.0)
                sa["tempo_parado_total_s"] += float(s.get("tempo_parado_s") or 0.0)
                sa["intervalo_externo_total_s"] += float(s.get("tempo_intervalo_externo_s") or 0.0)

            for cs in s.get("caminhoes") or []:
                key_sc = (sc, cs["placa"])
                sca = set_cam_agg.setdefault(key_sc, {
                    "setor": sc,
                    "placa": cs["placa"],
                    "motorista": cs.get("motorista", ""),
                    "modelo": cs.get("modelo", ""),
                    "dias": 0,
                    "vezes_principal": 0,
                    "tempo_total_s": 0.0,
                    "mov_s": 0.0,
                    "parado_s": 0.0,
                    "passagens": 0,
                    "km_coletado_total": 0.0,
                })
                sca["dias"] += 1
                if cs.get("eh_principal"):
                    sca["vezes_principal"] += 1
                sca["tempo_total_s"] += float(cs.get("tempo_s") or 0.0)
                sca["mov_s"] += float(cs.get("mov_s") or 0.0)
                sca["parado_s"] += float(cs.get("parado_s") or 0.0)
                sca["passagens"] += int(cs.get("passagens") or 0)
                sca["km_coletado_total"] += float(cs.get("km_coletado") or 0.0)

        # Agrega Caminhões do Dia -> Mês
        for c in dia_res.get("caminhoes") or []:
            pl = c["placa"]
            ca = cam_agg.setdefault(pl, {
                "placa": pl,
                "tipo_veiculo": c.get("tipo_veiculo", ""),
                "modelo": c.get("modelo", ""),
                "motoristas": Counter(),
                "turnos": Counter(),
                "dias_ativos": 0,
                "saidas_s": [],
                "chegadas_s": [],
                "jornada_total_s": 0.0,
                "tempo_garagem_s": 0.0,
                "garagem_pre_saida_s": 0.0,
                "garagem_pos_retorno_s": 0.0,
                "garagem_intermediaria_s": 0.0,
                "tempo_setor_s": 0.0,
                "tempo_setor_mov_s": 0.0,
                "tempo_setor_parado_s": 0.0,
                "tempo_trajeto_s": 0.0,
                "trajeto_garagem_setor_s": 0.0,
                "trajeto_setor_aterro_s": 0.0,
                "trajeto_aterro_setor_s": 0.0,
                "trajeto_entre_setores_s": 0.0,
                "trajeto_retorno_garagem_s": 0.0,
                "tempo_aterro_s": 0.0,
                "qtd_viagens_aterro": 0,
                "tempo_refeicao_s": 0.0,
                "tempo_sos_s": 0.0,
                "tempo_parada_externa_s": 0.0,
                "tempo_paradas_total_s": 0.0,
                "peso_total_kg": 0.0,
                "km_rodado_total": 0.0,
                "km_trajeto": 0.0,
                "km_em_setor": 0.0,
                "km_coletado": 0.0,
                "fv_vals": [],
                "qtd_alarmes": 0,
            })
            ca["dias_ativos"] += 1
            if c.get("motorista") and c["motorista"] != "Não informado":
                ca["motoristas"][c["motorista"]] += 1
            ca["turnos"][c.get("turno", "DIURNO")] += 1
            if c.get("saida_garagem_s") is not None:
                ca["saidas_s"].append(float(c["saida_garagem_s"]))
                if c.get("turno") == "DIURNO":
                    saidas_diurno_s.append(float(c["saida_garagem_s"]))
                else:
                    saidas_noturno_s.append(float(c["saida_garagem_s"]))
            if c.get("chegada_garagem_s") is not None:
                ca["chegadas_s"].append(float(c["chegada_garagem_s"]))

            for fk in (
                "jornada_total_s", "tempo_garagem_s", "garagem_pre_saida_s",
                "garagem_pos_retorno_s", "garagem_intermediaria_s",
                "tempo_setor_s", "tempo_setor_mov_s", "tempo_setor_parado_s",
                "tempo_trajeto_s", "trajeto_garagem_setor_s", "trajeto_setor_aterro_s",
                "trajeto_aterro_setor_s", "trajeto_entre_setores_s", "trajeto_retorno_garagem_s",
                "tempo_aterro_s", "tempo_refeicao_s", "tempo_sos_s",
                "tempo_parada_externa_s", "tempo_paradas_total_s",
                "peso_total_kg", "km_rodado_total", "km_trajeto", "km_em_setor", "km_coletado",
            ):
                ca[fk] += float(c.get(fk) or 0.0)
            ca["qtd_viagens_aterro"] += int(c.get("qtd_viagens_aterro") or 0)
            ca["qtd_alarmes"] += int(c.get("qtd_alarmes") or 0)
            if c.get("pct_faixa_verde") is not None:
                ca["fv_vals"].append(float(c["pct_faixa_verde"]))

            for sat in c.get("setores_atendidos") or []:
                key_cs = (pl, sat["setor"])
                csa = cam_set_agg.setdefault(key_cs, {
                    "setor": sat["setor"],
                    "turno": sat.get("turno", "DIURNO"),
                    "dias": 0,
                    "tempo_s": 0.0,
                    "mov_s": 0.0,
                    "parado_s": 0.0,
                    "passagens": 0,
                    "km_coletado": 0.0,
                })
                csa["dias"] += 1
                csa["tempo_s"] += float(sat.get("tempo_s") or 0.0)
                csa["mov_s"] += float(sat.get("mov_s") or 0.0)
                csa["parado_s"] += float(sat.get("parado_s") or 0.0)
                csa["passagens"] += int(sat.get("passagens") or 0)
                csa["km_coletado"] += float(sat.get("km_coletado") or 0.0)

    # Finaliza lista de Setores do Mês
    setores_mes: List[Dict[str, Any]] = []
    for sc in sorted(set_agg.keys(), key=indicadores._chave_ordenacao_setor):
        sa = set_agg[sc]
        d_ex = max(1, sa["dias_executados"])
        km_prev = sa["km_previsto_total"]
        km_col = sa["km_coletado_total"]
        ef_tot = sa["tempo_efetivo_total_s"]
        mov_tot = sa["tempo_movimento_total_s"]

        cams_do_setor = [
            v for (s_k, _), v in set_cam_agg.items() if s_k == sc
        ]
        cams_do_setor.sort(key=lambda x: (-x["km_coletado_total"], -x["tempo_total_s"], x["placa"]))
        cams_fmt: List[Dict[str, Any]] = []
        for idx_c, cs in enumerate(cams_do_setor):
            cams_fmt.append({
                "placa": cs["placa"],
                "papel": "Principal" if idx_c == 0 else f"Apoio {idx_c}",
                "eh_principal": idx_c == 0,
                "motorista": cs["motorista"],
                "modelo": cs["modelo"],
                "dias_no_setor": cs["dias"],
                "tempo_s": round(cs["tempo_total_s"], 1),
                "tempo_medio_dia_s": round(cs["tempo_total_s"] / max(1, cs["dias"]), 1),
                "mov_s": round(cs["mov_s"], 1),
                "parado_s": round(cs["parado_s"], 1),
                "pct_tempo_setor": round(cs["tempo_total_s"] / ef_tot * 100.0, 1) if ef_tot > 0 else 0.0,
                "passagens": cs["passagens"],
                "km_coletado": round(cs["km_coletado_total"], 2),
                "pct_cobertura_setor": round(cs["km_coletado_total"] / km_prev * 100.0, 1) if km_prev > 0 else 0.0,
                "entrada_hhmm": f"{cs['dias']}d",
                "saida_hhmm": f"{cs['dias']}d",
            })

        setores_mes.append({
            "codigo": sc,
            "turno": sa["turno"],
            "agenda_rotulo": sa["agenda_rotulo"],
            "status_execucao": "CONCLUIDO" if (km_prev > 0 and km_col / km_prev >= 0.8) else ("PARCIAL" if sa["dias_executados"] > 0 else "NAO_INICIADO"),
            "dias_previstos": sa["dias_previstos"],
            "dias_executados": sa["dias_executados"],
            "dias_concluidos": sa["dias_concluidos"],
            "hora_inicio_hhmm": _media_circular_horas_s(sa["inicios_s"]) or "--:--",
            "hora_fim_hhmm": _media_circular_horas_s(sa["fins_s"]) or "--:--",
            "duracao_janela_s": round(sum(sa["janelas_s"]) / d_ex, 1) if sa["janelas_s"] else 0.0,
            "duracao_janela_min_s": round(min(sa["janelas_s"]), 1) if sa["janelas_s"] else 0.0,
            "duracao_janela_max_s": round(max(sa["janelas_s"]), 1) if sa["janelas_s"] else 0.0,
            "tempo_efetivo_s": round(ef_tot, 1),
            "tempo_efetivo_medio_s": round(ef_tot / d_ex, 1) if sa["dias_executados"] > 0 else 0.0,
            "tempo_movimento_s": round(mov_tot, 1),
            "tempo_parado_s": round(sa["tempo_parado_total_s"], 1),
            "tempo_intervalo_externo_s": round(sa["intervalo_externo_total_s"] / d_ex, 1) if sa["dias_executados"] > 0 else 0.0,
            "km_previsto": round(km_prev, 2),
            "km_coletado": round(km_col, 2),
            "km_omissao": round(sa["km_omissao_total"], 2),
            "pct_cobertura": round(km_col / km_prev * 100.0, 1) if km_prev > 0 else 0.0,
            "produtividade_km_h": round(km_col / (ef_tot / 3600.0), 2) if ef_tot > 60.0 and km_col > 0 else 0.0,
            "ritmo_min_por_km": round((ef_tot / 60.0) / km_col, 1) if km_col > 0.1 else 0.0,
            "vel_media_kmh": round(km_col / (mov_tot / 3600.0), 1) if mov_tot > 60.0 else 0.0,
            "peso_kg": round(sa["peso_total_kg"], 1),
            "qtd_caminhoes": len(cams_fmt),
            "placa_principal": cams_fmt[0]["placa"] if cams_fmt else None,
            "caminhoes": cams_fmt,
        })

    # Finaliza lista de Caminhões do Mês
    caminhoes_mes: List[Dict[str, Any]] = []
    for pl, ca in cam_agg.items():
        da = max(1, ca["dias_ativos"])
        j_tot = ca["jornada_total_s"]
        s_tot = ca["tempo_setor_s"]
        t_tot = ca["tempo_trajeto_s"]
        a_tot = ca["tempo_aterro_s"]
        q_ate = ca["qtd_viagens_aterro"]
        sets_c = [v for (p_k, _), v in cam_set_agg.items() if p_k == pl]
        sets_c.sort(key=lambda x: (-x["tempo_s"], x["setor"]))
        for sc_item in sets_c:
            sc_item["tempo_medio_dia_s"] = round(sc_item["tempo_s"] / max(1, sc_item["dias"]), 1)
            sc_item["tempo_s"] = round(sc_item["tempo_s"], 1)
            sc_item["km_coletado"] = round(sc_item["km_coletado"], 2)

        caminhoes_mes.append({
            "placa": pl,
            "tipo_veiculo": ca["tipo_veiculo"],
            "modelo": ca["modelo"],
            "motorista": ca["motoristas"].most_common(1)[0][0] if ca["motoristas"] else "Não informado",
            "turno": ca["turnos"].most_common(1)[0][0] if ca["turnos"] else "DIURNO",
            "dias_ativos": ca["dias_ativos"],
            "saida_garagem_hhmm": _media_circular_horas_s(ca["saidas_s"]) or "--:--",
            "chegada_garagem_hhmm": _media_circular_horas_s(ca["chegadas_s"]) or "--:--",
            "jornada_total_s": round(j_tot, 1),
            "jornada_media_dia_s": round(j_tot / da, 1),
            "tempo_garagem_s": round(ca["tempo_garagem_s"], 1),
            "garagem_pre_saida_s": round(ca["garagem_pre_saida_s"], 1),
            "garagem_pos_retorno_s": round(ca["garagem_pos_retorno_s"], 1),
            "garagem_intermediaria_s": round(ca["garagem_intermediaria_s"], 1),
            "tempo_setor_s": round(s_tot, 1),
            "tempo_setor_mov_s": round(ca["tempo_setor_mov_s"], 1),
            "tempo_setor_parado_s": round(ca["tempo_setor_parado_s"], 1),
            "tempo_trajeto_s": round(t_tot, 1),
            "trajeto_garagem_setor_s": round(ca["trajeto_garagem_setor_s"], 1),
            "trajeto_setor_aterro_s": round(ca["trajeto_setor_aterro_s"], 1),
            "trajeto_aterro_setor_s": round(ca["trajeto_aterro_setor_s"], 1),
            "trajeto_entre_setores_s": round(ca["trajeto_entre_setores_s"], 1),
            "trajeto_retorno_garagem_s": round(ca["trajeto_retorno_garagem_s"], 1),
            "tempo_aterro_s": round(a_tot, 1),
            "qtd_viagens_aterro": q_ate,
            "tempo_medio_aterro_s": round(a_tot / q_ate, 1) if q_ate > 0 else 0.0,
            "tempo_refeicao_s": round(ca["tempo_refeicao_s"], 1),
            "tempo_sos_s": round(ca["tempo_sos_s"], 1),
            "tempo_parada_externa_s": round(ca["tempo_parada_externa_s"], 1),
            "tempo_paradas_total_s": round(ca["tempo_paradas_total_s"], 1),
            "peso_total_kg": round(ca["peso_total_kg"], 1),
            "km_rodado_total": round(ca["km_rodado_total"], 2),
            "km_trajeto": round(ca["km_trajeto"], 2),
            "km_em_setor": round(ca["km_em_setor"], 2),
            "km_coletado": round(ca["km_coletado"], 2),
            "pct_eficiencia_produtiva": round(s_tot / j_tot * 100.0, 1) if j_tot > 0 else 0.0,
            "pct_km_produtivo": round(ca["km_coletado"] / ca["km_rodado_total"] * 100.0, 1) if ca["km_rodado_total"] > 0 else 0.0,
            "vel_media_coleta_kmh": round(ca["km_em_setor"] / (ca["tempo_setor_mov_s"] / 3600.0), 1) if ca["tempo_setor_mov_s"] > 60.0 else 0.0,
            "vel_media_trajeto_kmh": round(ca["km_trajeto"] / (t_tot / 3600.0), 1) if t_tot > 60.0 else 0.0,
            "pct_faixa_verde": round(sum(ca["fv_vals"]) / len(ca["fv_vals"]), 1) if ca["fv_vals"] else None,
            "qtd_alarmes": ca["qtd_alarmes"],
            "qtd_setores": len(sets_c),
            "setores_atendidos": sets_c,
        })

    caminhoes_mes.sort(key=lambda c: (-c["tempo_setor_s"], c["placa"]))
    viagens_mes.sort(key=lambda v: (v.get("data_iso", ""), v.get("chegada", "")))

    for ah in atividade_horaria_mes:
        for fk in ("em_garagem_min", "em_trajeto_min", "em_setor_min", "em_aterro_min", "em_parada_min"):
            ah[fk] = round(ah[fk] / max(1, dias_operacionais), 1)

    j_tot = totais["jornada_total_s"]
    s_tot = totais["tempo_setor_total_s"]
    t_tot = totais["tempo_trajeto_total_s"]
    a_tot = totais["tempo_aterro_total_s"]
    g_tot = totais["tempo_garagem_total_s"]
    p_tot = totais["tempo_paradas_total_s"]
    q_ate = int(totais["qtd_viagens_aterro"])
    km_prev_m = totais["km_previsto_setores"]
    km_col_m = totais["km_coletado_setores"]
    n_cam_dias = max(1, cam_dias_total)
    n_set_dias = max(1, set_dias_exec_total)

    resumo_mes = {
        "modo": "mes",
        "contrato_id": cid,
        "contrato_nome": info_cid["nome"],
        "mes_ref": periodo["mes_ref"],
        "data_inicial": periodo["data_inicial_iso"],
        "data_final": periodo["data_final_iso"],
        "data_formatada": f"{periodo['data_inicial'].strftime('%d/%m/%Y')} a {periodo['data_final'].strftime('%d/%m/%Y')}",
        "qtd_dias": len(dias_iso),
        "dias_operacionais": dias_operacionais,
        "mes_em_andamento": periodo["mes_em_andamento"],
        "hora_media_saida_diurno": _media_circular_horas_s(saidas_diurno_s) or "--:--",
        "hora_media_saida_noturno": _media_circular_horas_s(saidas_noturno_s) or "--:--",
        "caminhoes_ativos": len(caminhoes_mes),
        "caminhoes_dia_total": cam_dias_total,
        "setores_previstos": len(setores_mes),
        "setores_executados": sum(1 for s in setores_mes if s["dias_executados"] > 0),
        "setores_concluidos": sum(1 for s in setores_mes if s["status_execucao"] == "CONCLUIDO"),
        "km_previsto_setores": round(km_prev_m, 2),
        "km_coletado_setores": round(km_col_m, 2),
        "pct_cobertura_geral": round(km_col_m / km_prev_m * 100.0, 1) if km_prev_m > 0 else 0.0,
        "km_rodado_total": round(totais["km_rodado_total"], 2),
        "km_trajeto_total": round(totais["km_trajeto_total"], 2),
        "km_em_setor_total": round(totais["km_em_setor_total"], 2),
        "peso_total_kg": round(totais["peso_total_kg"], 1),
        "qtd_viagens_aterro": q_ate,
        "jornada_total_s": round(j_tot, 1),
        "jornada_media_caminhao_s": round(j_tot / n_cam_dias, 1),
        "tempo_garagem_total_s": round(g_tot, 1),
        "tempo_garagem_medio_caminhao_s": round(g_tot / n_cam_dias, 1),
        "garagem_pre_saida_s": round(totais["garagem_pre_saida_s"], 1),
        "garagem_pos_retorno_s": round(totais["garagem_pos_retorno_s"], 1),
        "garagem_intermediaria_s": round(totais["garagem_intermediaria_s"], 1),
        "tempo_setor_total_s": round(s_tot, 1),
        "tempo_setor_mov_s": round(totais["tempo_setor_mov_s"], 1),
        "tempo_setor_parado_s": round(totais["tempo_setor_parado_s"], 1),
        "tempo_efetivo_medio_setor_s": round(s_tot / n_set_dias, 1),
        "duracao_janela_media_setor_s": round(
            sum(s["duracao_janela_s"] for s in setores_mes if s["dias_executados"] > 0) / max(1, sum(1 for s in setores_mes if s["dias_executados"] > 0)),
            1,
        ),
        "tempo_trajeto_total_s": round(t_tot, 1),
        "tempo_trajeto_medio_caminhao_s": round(t_tot / n_cam_dias, 1),
        "trajeto_garagem_setor_s": round(totais["trajeto_garagem_setor_s"], 1),
        "trajeto_setor_aterro_s": round(totais["trajeto_setor_aterro_s"], 1),
        "trajeto_aterro_setor_s": round(totais["trajeto_aterro_setor_s"], 1),
        "trajeto_entre_setores_s": round(totais["trajeto_entre_setores_s"], 1),
        "trajeto_retorno_garagem_s": round(totais["trajeto_retorno_garagem_s"], 1),
        "tempo_aterro_total_s": round(a_tot, 1),
        "tempo_aterro_medio_caminhao_s": round(a_tot / n_cam_dias, 1),
        "tempo_medio_por_descarga_s": round(a_tot / q_ate, 1) if q_ate > 0 else 0.0,
        "tempo_refeicao_total_s": round(totais["tempo_refeicao_total_s"], 1),
        "tempo_sos_total_s": round(totais["tempo_sos_total_s"], 1),
        "tempo_parada_externa_total_s": round(totais["tempo_parada_externa_total_s"], 1),
        "tempo_paradas_total_s": round(p_tot, 1),
        "pct_tempo_setor": round(s_tot / j_tot * 100.0, 1) if j_tot > 0 else 0.0,
        "pct_tempo_trajeto": round(t_tot / j_tot * 100.0, 1) if j_tot > 0 else 0.0,
        "pct_tempo_aterro": round(a_tot / j_tot * 100.0, 1) if j_tot > 0 else 0.0,
        "pct_tempo_garagem": round(g_tot / j_tot * 100.0, 1) if j_tot > 0 else 0.0,
        "pct_tempo_paradas": round(p_tot / j_tot * 100.0, 1) if j_tot > 0 else 0.0,
        "vel_media_coleta_kmh": round(totais["km_em_setor_total"] / (totais["tempo_setor_mov_s"] / 3600.0), 1) if totais["tempo_setor_mov_s"] > 60.0 else 0.0,
        "vel_media_trajeto_kmh": round(totais["km_trajeto_total"] / (t_tot / 3600.0), 1) if t_tot > 60.0 else 0.0,
        "ritmo_medio_min_por_km": round((s_tot / 60.0) / km_col_m, 1) if km_col_m > 0.1 else 0.0,
        "produtividade_media_km_h": round(km_col_m / (s_tot / 3600.0), 2) if s_tot > 60.0 and km_col_m > 0 else 0.0,
    }

    destaques = _gerar_destaques_executivos(resumo_mes, setores_mes, caminhoes_mes, viagens_mes)
    if progress_cb:
        progress_cb({"progresso_pct": 100.0, "data_atual": periodo["data_final_iso"], "etapas_concluidas": total_dias, "etapas_total": total_dias})

    return {
        "resumo": resumo_mes,
        "setores": setores_mes,
        "caminhoes": caminhoes_mes,
        "viagens_aterro": viagens_mes,
        "atividade_horaria": atividade_horaria_mes,
        "evolucao_diaria": evolucao_diaria,
        "destaques": destaques,
    }


def gerar_excel_painel_tempos(dados: Dict[str, Any]) -> Tuple[bytes, str]:
    """Gera workbook Excel (.xlsx) executivo multi-abas com todas as métricas de tempo da cidade."""
    resumo = dados.get("resumo") or {}
    modo = resumo.get("modo", "dia")
    cid = resumo.get("contrato_id", "cidade")
    nome_cid = resumo.get("contrato_nome", cid.title())
    ref_tag = resumo.get("data_iso") if modo == "dia" else resumo.get("mes_ref", "mes")

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    def _estilizar_aba(ws, larguras: List[int]) -> None:
        ws.freeze_panes = "A2"
        if ws.max_column >= 1 and ws.max_row >= 1:
            ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"
        for idx, w in enumerate(larguras, start=1):
            c = ws.cell(row=1, column=idx)
            c.font = _HEADER_FONT
            c.fill = _HEADER_FILL
            c.alignment = _HEADER_ALIGN
            ws.column_dimensions[get_column_letter(idx)].width = w

    # Aba 1: Resumo Operacional
    ws_res = wb.create_sheet("Resumo Operacional")
    ws_res.append(["Indicador", "Valor", "Detalhe / Observação"])
    linhas_res = [
        ("Cidade / Contrato", nome_cid, f"Modo: {modo.upper()} ({resumo.get('data_formatada', '')})"),
        ("Caminhões Ativos", resumo.get("caminhoes_ativos", 0), f"Setores executados: {resumo.get('setores_executados', 0)}/{resumo.get('setores_previstos', 0)}"),
        ("Cobertura Geral", f"{resumo.get('pct_cobertura_geral', 0)}%", f"{resumo.get('km_coletado_setores', 0)} / {resumo.get('km_previsto_setores', 0)} km"),
        ("Jornada Operacional Total", _fmt_duracao(resumo.get("jornada_total_s", 0)), f"Média por caminhão/dia: {_fmt_duracao(resumo.get('jornada_media_caminhao_s', 0))}"),
        ("Tempo em Setor (Coleta)", _fmt_duracao(resumo.get("tempo_setor_total_s", 0)), f"{resumo.get('pct_tempo_setor', 0)}% da jornada | Média/setor: {_fmt_duracao(resumo.get('tempo_efetivo_medio_setor_s', 0))}"),
        ("Tempo em Movimento vs Parado (Setor)", f"{_fmt_duracao(resumo.get('tempo_setor_mov_s', 0))} / {_fmt_duracao(resumo.get('tempo_setor_parado_s', 0))}", f"Vel. média coleta: {resumo.get('vel_media_coleta_kmh', 0)} km/h"),
        ("Tempo Total de Trajeto", _fmt_duracao(resumo.get("tempo_trajeto_total_s", 0)), f"{resumo.get('pct_tempo_trajeto', 0)}% da jornada | Média/caminhão: {_fmt_duracao(resumo.get('tempo_trajeto_medio_caminhao_s', 0))}"),
        ("Trajeto: Garagem → 1º Setor", _fmt_duracao(resumo.get("trajeto_garagem_setor_s", 0)), "Deslocamento inicial"),
        ("Trajeto: Setor → Aterro", _fmt_duracao(resumo.get("trajeto_setor_aterro_s", 0)), "Ida carregado para descarga"),
        ("Trajeto: Aterro → Setor", _fmt_duracao(resumo.get("trajeto_aterro_setor_s", 0)), "Retorno vazio à coleta"),
        ("Trajeto: Entre Setores", _fmt_duracao(resumo.get("trajeto_entre_setores_s", 0)), "Apoios e transições"),
        ("Trajeto: Retorno → Garagem", _fmt_duracao(resumo.get("trajeto_retorno_garagem_s", 0)), "Recolhimento final"),
        ("Tempo na Garagem", _fmt_duracao(resumo.get("tempo_garagem_total_s", 0)), f"Pré-saída: {_fmt_duracao(resumo.get('garagem_pre_saida_s', 0))} | Pós-retorno: {_fmt_duracao(resumo.get('garagem_pos_retorno_s', 0))}"),
        ("Tempo no Aterro / Descarga", _fmt_duracao(resumo.get("tempo_aterro_total_s", 0)), f"{resumo.get('qtd_viagens_aterro', 0)} viagens | Média/descarga: {_fmt_duracao(resumo.get('tempo_medio_por_descarga_s', 0))} | Peso: {round(resumo.get('peso_total_kg', 0)/1000.0, 2)} t"),
        ("Refeição / SOS / Paradas Externas", _fmt_duracao(resumo.get("tempo_paradas_total_s", 0)), f"Refeição: {_fmt_duracao(resumo.get('tempo_refeicao_total_s', 0))} | SOS: {_fmt_duracao(resumo.get('tempo_sos_total_s', 0))} | Outras >5m: {_fmt_duracao(resumo.get('tempo_parada_externa_total_s', 0))}"),
    ]
    for lr in linhas_res:
        ws_res.append(list(lr))
    _estilizar_aba(ws_res, [34, 24, 58])

    # Aba 2: Relação de Setores
    ws_set = wb.create_sheet("Relacao de Setores")
    ws_set.append([
        "Setor", "Turno", "Status", "Início", "Fim", "Duração Janela (h)",
        "Tempo Efetivo (h)", "Movimento (h)", "Parado (h)", "Intervalo Externo (h)",
        "Km Previsto", "Km Coletado", "% Cobertura", "Produtividade (km/h)",
        "Ritmo (min/km)", "Qtd Caminhões", "Placa Principal", "Peso (kg)"
    ])
    for s in dados.get("setores") or []:
        ws_set.append([
            s.get("codigo"), s.get("turno"), s.get("status_execucao"),
            s.get("hora_inicio_hhmm"), s.get("hora_fim_hhmm"),
            round(float(s.get("duracao_janela_s", 0)) / 3600.0, 2),
            round(float(s.get("tempo_efetivo_s", 0)) / 3600.0, 2),
            round(float(s.get("tempo_movimento_s", 0)) / 3600.0, 2),
            round(float(s.get("tempo_parado_s", 0)) / 3600.0, 2),
            round(float(s.get("tempo_intervalo_externo_s", 0)) / 3600.0, 2),
            s.get("km_previsto"), s.get("km_coletado"), s.get("pct_cobertura"),
            s.get("produtividade_km_h"), s.get("ritmo_min_por_km"),
            s.get("qtd_caminhoes"), s.get("placa_principal") or "-", s.get("peso_kg", 0),
        ])
    _estilizar_aba(ws_set, [28, 12, 14, 10, 10, 16, 16, 14, 13, 18, 13, 13, 13, 18, 15, 14, 16, 13])

    # Aba 3: Caminhões por Setor
    ws_sc = wb.create_sheet("Caminhoes por Setor")
    ws_sc.append([
        "Setor", "Turno", "Placa", "Papel", "Motorista", "Entrada", "Saída",
        "Tempo no Setor (h)", "Tempo Formatado", "% Tempo do Setor", "Movimento (h)",
        "Parado (h)", "Passagens", "Km Coletado", "% Cobertura Setor"
    ])
    for s in dados.get("setores") or []:
        for c in s.get("caminhoes") or []:
            ws_sc.append([
                s.get("codigo"), s.get("turno"), c.get("placa"), c.get("papel"),
                c.get("motorista"), c.get("entrada_hhmm"), c.get("saida_hhmm"),
                round(float(c.get("tempo_s", 0)) / 3600.0, 2),
                _fmt_duracao(c.get("tempo_s", 0)),
                c.get("pct_tempo_setor"),
                round(float(c.get("mov_s", 0)) / 3600.0, 2),
                round(float(c.get("parado_s", 0)) / 3600.0, 2),
                c.get("passagens"), c.get("km_coletado"), c.get("pct_cobertura_setor"),
            ])
    _estilizar_aba(ws_sc, [28, 12, 14, 13, 28, 10, 10, 16, 16, 16, 14, 13, 11, 13, 16])

    # Aba 4: Frota e Tempos
    ws_cam = wb.create_sheet("Frota e Tempos")
    ws_cam.append([
        "Placa", "Turno", "Motorista", "Saída Garagem", "Chegada Garagem",
        "Jornada Total (h)", "Garagem (h)", "Garagem Pré (h)", "Garagem Pós (h)",
        "Em Setor (h)", "Trajeto Total (h)", "Gar→Setor (h)", "Setor→Aterro (h)",
        "Aterro→Setor (h)", "Entre Setores (h)", "Retorno→Gar (h)",
        "Aterro (h)", "Viagens Aterro", "Peso (kg)", "Paradas/Ref/SOS (h)",
        "% Eficiência", "Km Rodado", "Km Coletado", "Vel Coleta (km/h)"
    ])
    for c in dados.get("caminhoes") or []:
        ws_cam.append([
            c.get("placa"), c.get("turno"), c.get("motorista"),
            c.get("saida_garagem_hhmm"), c.get("chegada_garagem_hhmm"),
            round(float(c.get("jornada_total_s", 0)) / 3600.0, 2),
            round(float(c.get("tempo_garagem_s", 0)) / 3600.0, 2),
            round(float(c.get("garagem_pre_saida_s", 0)) / 3600.0, 2),
            round(float(c.get("garagem_pos_retorno_s", 0)) / 3600.0, 2),
            round(float(c.get("tempo_setor_s", 0)) / 3600.0, 2),
            round(float(c.get("tempo_trajeto_s", 0)) / 3600.0, 2),
            round(float(c.get("trajeto_garagem_setor_s", 0)) / 3600.0, 2),
            round(float(c.get("trajeto_setor_aterro_s", 0)) / 3600.0, 2),
            round(float(c.get("trajeto_aterro_setor_s", 0)) / 3600.0, 2),
            round(float(c.get("trajeto_entre_setores_s", 0)) / 3600.0, 2),
            round(float(c.get("trajeto_retorno_garagem_s", 0)) / 3600.0, 2),
            round(float(c.get("tempo_aterro_s", 0)) / 3600.0, 2),
            c.get("qtd_viagens_aterro", 0), c.get("peso_total_kg", 0),
            round(float(c.get("tempo_paradas_total_s", 0)) / 3600.0, 2),
            c.get("pct_eficiencia_produtiva", 0), c.get("km_rodado_total", 0),
            c.get("km_coletado", 0), c.get("vel_media_coleta_kmh", 0),
        ])
    _estilizar_aba(ws_cam, [13, 11, 28, 14, 15, 15, 13, 14, 14, 13, 15, 14, 15, 15, 16, 15, 12, 14, 12, 18, 13, 12, 12, 16])

    # Aba 5: Viagens ao Aterro (e Evolução Diária no modo mês)
    ws_ate = wb.create_sheet("Viagens Aterro")
    ws_ate.append([
        "Data", "Placa", "Motorista", "Setor Origem", "Trajeto Ida (min)",
        "Chegada Aterro", "Saída Aterro", "Permanência Aterro (min)",
        "Trajeto Volta (min)", "Destino Pós-Aterro", "Ticket", "Peso (kg)"
    ])
    for v in dados.get("viagens_aterro") or []:
        ws_ate.append([
            v.get("data_iso") or resumo.get("data_iso", ""),
            v.get("placa"), v.get("motorista"), v.get("setor_origem"),
            round(float(v.get("trajeto_ida_s", 0)) / 60.0, 1),
            v.get("chegada_hhmm"), v.get("saida_hhmm"),
            round(float(v.get("duracao_s", 0)) / 60.0, 1),
            round(float(v.get("trajeto_volta_s", 0)) / 60.0, 1),
            v.get("destino_pos"), v.get("ticket") or "-", v.get("peso_kg", 0),
        ])
    _estilizar_aba(ws_ate, [13, 13, 28, 26, 16, 15, 14, 21, 17, 22, 13, 13])

    if dados.get("evolucao_diaria"):
        ws_ev = wb.create_sheet("Evolucao Diaria")
        ws_ev.append([
            "Data", "Dia Semana", "Caminhões", "Setores Exec.", "Setores Prev.",
            "% Cobertura", "Em Setor (h)", "Trajeto (h)", "Aterro (h)",
            "Garagem (h)", "Paradas (h)", "Duração Média Setor", "Viagens Aterro", "Peso (kg)"
        ])
        for ev in dados["evolucao_diaria"]:
            ws_ev.append([
                ev["data_iso"], ev["dia_semana"], ev["caminhoes_ativos"],
                ev["setores_executados"], ev["setores_previstos"], ev["pct_cobertura"],
                round(ev["tempo_setor_s"] / 3600.0, 2),
                round(ev["tempo_trajeto_s"] / 3600.0, 2),
                round(ev["tempo_aterro_s"] / 3600.0, 2),
                round(ev["tempo_garagem_s"] / 3600.0, 2),
                round(ev["tempo_paradas_s"] / 3600.0, 2),
                _fmt_duracao(ev["duracao_media_setor_s"]),
                ev["viagens_aterro"], ev["peso_total_kg"],
            ])
        _estilizar_aba(ws_ev, [13, 12, 12, 14, 14, 13, 14, 13, 12, 13, 13, 18, 15, 13])

    buf = io.BytesIO()
    wb.save(buf)
    filename = f"Gestao_Tempos_{cid}_{ref_tag}.xlsx".replace(" ", "_")
    return buf.getvalue(), filename


def _limpar_jobs_expirados() -> None:
    agora = time.time()
    expirados = [k for k, v in _JOBS_TEMPOS.items() if (agora - float(v.get("criado_ts", 0))) > _JOB_TEMPOS_TTL_S]
    for k in expirados:
        _JOBS_TEMPOS.pop(k, None)


def iniciar_job_tempos(
    engine: Any,
    contrato_id: str,
    modo: str,
    data_str: Optional[str] = None,
    mes_str: Optional[str] = None,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """
    Inicia o cálculo do Painel de Tempos.
    Se o modo 'dia' já estiver rápido ou em cache, conclui de forma síncrona na hora;
    se for 'mes' (múltiplos dias), executa em background com acompanhamento de progresso.
    """
    cid = str(contrato_id or "").lower().strip()
    if not cid or cid in ("all", "todas"):
        raise ValueError("Selecione uma cidade específica para abrir o Painel de Tempos.")

    periodo = resolver_periodo_tempos(modo=modo, data_str=data_str, mes_str=mes_str)
    job_id = secrets.token_hex(10)

    if periodo["modo"] == "dia":
        resultado = calcular_painel_dia_cidade(
            engine=engine,
            contrato_id=cid,
            data_iso=periodo["data_inicial_iso"],
            force_refresh=force_refresh,
        )
        with _JOBS_TEMPOS_LOCK:
            _limpar_jobs_expirados()
            _JOBS_TEMPOS[job_id] = {
                "job_id": job_id,
                "contrato_id": cid,
                "modo": "dia",
                "status": "done",
                "progresso_pct": 100.0,
                "data_atual": periodo["data_inicial_iso"],
                "etapas_concluidas": 1,
                "etapas_total": 1,
                "criado_ts": time.time(),
                "resultado": resultado,
                "erro": None,
            }
        return {
            "job_id": job_id,
            "contrato_id": cid,
            "status": "done",
            "progresso_pct": 100.0,
            "concluido_imediato": True,
            "resultado": resultado,
        }

    # Modo Mês: verifica se todos os dias já estão cacheados e prontos
    with _JOBS_TEMPOS_LOCK:
        _limpar_jobs_expirados()
        _JOBS_TEMPOS[job_id] = {
            "job_id": job_id,
            "contrato_id": cid,
            "modo": "mes",
            "status": "running",
            "progresso_pct": 0.0,
            "data_atual": periodo["data_inicial_iso"],
            "etapas_concluidas": 0,
            "etapas_total": periodo["qtd_dias"],
            "criado_ts": time.time(),
            "resultado": None,
            "erro": None,
        }

    def _on_prog(upd: Dict[str, Any]) -> None:
        with _JOBS_TEMPOS_LOCK:
            job = _JOBS_TEMPOS.get(job_id)
            if not job:
                return
            job["progresso_pct"] = upd.get("progresso_pct", job["progresso_pct"])
            job["data_atual"] = upd.get("data_atual", job["data_atual"])
            job["etapas_concluidas"] = upd.get("etapas_concluidas", job["etapas_concluidas"])
            job["etapas_total"] = upd.get("etapas_total", job["etapas_total"])

    def _worker() -> None:
        try:
            res_mes = consolidar_painel_mes_cidade(
                engine=engine,
                contrato_id=cid,
                mes_str=mes_str,
                data_str=data_str,
                force_refresh=force_refresh,
                progress_cb=_on_prog,
            )
            with _JOBS_TEMPOS_LOCK:
                job = _JOBS_TEMPOS.get(job_id)
                if job:
                    job["status"] = "done"
                    job["progresso_pct"] = 100.0
                    job["resultado"] = res_mes
        except Exception as e:
            print(f"[TEMPOS] Erro no job mensal {job_id}: {e}")
            with _JOBS_TEMPOS_LOCK:
                job = _JOBS_TEMPOS.get(job_id)
                if job:
                    job["status"] = "error"
                    job["erro"] = str(e)

    threading.Thread(target=_worker, daemon=True).start()
    return {
        "job_id": job_id,
        "contrato_id": cid,
        "status": "running",
        "progresso_pct": 0.0,
        "etapas_total": periodo["qtd_dias"],
        "concluido_imediato": False,
    }


def obter_status_job_tempos(job_id: str) -> Optional[Dict[str, Any]]:
    with _JOBS_TEMPOS_LOCK:
        job = _JOBS_TEMPOS.get(str(job_id or "").strip())
        if not job:
            return None
        return dict(job)
