"""
Motor de Auditoria Espacial Real e Heatmap com SQLite (engine.py)
Desenvolvido para sitemaLCv1 - Locar Saneamento Ambiental.

Executa o cruzamento geométrico determinístico da telemetria GPS real da Inlog
contra a malha viária do projeto no SQLite, calculando métricas reais de cobertura,
omissões, coletas manuais (dwell time) e possíveis falhas de sinal GPS.
"""

import os
import io
import csv
import json
import math
import calendar
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List, Any, Tuple, Optional, Set

import database as db
import malha_rotas
from inlog_service import InlogService, formatar_placa_hifen, janela_operacional_cidade, normalizar_ascii


NOMENCLATURA_VERSION = "v5_contrato_por_setor"


class HeatmapEngine:
    _cache_nomenclatura_validado = False

    def __init__(self, inlog_service: Optional[InlogService] = None):
        self.inlog = inlog_service or InlogService()
        self._garantir_cache_nomenclatura_atualizado()

    def _garantir_cache_nomenclatura_atualizado(self) -> None:
        """Invalida uma única vez auditorias em cache geradas antes da regra completa de nomenclatura."""
        if HeatmapEngine._cache_nomenclatura_validado:
            return
        with db._write_lock:
            if HeatmapEngine._cache_nomenclatura_validado:
                return
            conn = db.get_connection()
            try:
                with conn:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS engine_meta (chave TEXT PRIMARY KEY, valor TEXT)"
                    )
                    row = conn.execute(
                        "SELECT valor FROM engine_meta WHERE chave = 'nomenclatura_version'"
                    ).fetchone()
                    if not row or row["valor"] != NOMENCLATURA_VERSION:
                        if db.IS_POSTGRES:
                            conn.execute("TRUNCATE TABLE auditoria_detalhes, auditorias RESTART IDENTITY")
                        else:
                            conn.execute("DELETE FROM auditoria_detalhes")
                            conn.execute("DELETE FROM auditorias")
                        conn.execute(
                            "INSERT INTO engine_meta (chave, valor) VALUES ('nomenclatura_version', ?) ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor",
                            (NOMENCLATURA_VERSION,),
                        )
                HeatmapEngine._cache_nomenclatura_validado = True
            except Exception as e:
                print(f"[ENGINE] Aviso ao validar versão de nomenclatura: {e}")
            finally:
                conn.close()

    def _ler_regras_operacionais(self) -> Dict[str, float]:
        """Lê parâmetros operacionais configurados no .env."""
        buffer_m = max(25.0, float(os.getenv("GPS_BUFFER_METROS", "25.0")))
        vel_min = float(os.getenv("VELOCIDADE_MIN_COLETA", "2.0"))
        vel_max = float(os.getenv("VELOCIDADE_MAX_COLETA", "25.0"))
        vel_passagem_max = max(float(os.getenv("VELOCIDADE_MAX_PASSAGEM", "110.0")), 110.0)
        dwell_s = float(os.getenv("DWELL_TIME_MIN_SEGUNDOS", "180.0"))
        return {
            "buffer_m": buffer_m,
            "vel_min": vel_min,
            "vel_max": vel_max,
            "vel_passagem_max": vel_passagem_max,
            "dwell_s": dwell_s,
        }

    def _coletar_frota_e_gps_cidade(
        self, contrato_id: str, data_iso: str, operacao: str = "COLETA", force_refresh: bool = False
    ) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, List[Dict[str, Any]]]]:
        """Descobre as placas do contrato/operação (em paralelo) e busca seus pontos GPS na janela operacional."""
        veiculos_rt: List[Dict[str, Any]] = []
        rd_items: List[Dict[str, Any]] = []
        st_items: List[Dict[str, Any]] = []

        hoje_iso = datetime.now().strftime("%Y-%m-%d")
        usar_setores_cache_local = (str(data_iso)[:10] < hoje_iso) and (not force_refresh)
        cid_st = (contrato_id or "").lower().strip() or None
        if cid_st in ("todas", "all"):
            cid_st = None

        with ThreadPoolExecutor(max_workers=3) as pool:
            f_rt = pool.submit(
                self.inlog.get_veiculos_tempo_real,
                contrato_id=contrato_id,
                operacao=operacao,
                force_refresh=force_refresh,
            )
            f_rd = pool.submit(
                self.inlog.get_relatorio_diario,
                data_iso,
                contrato_id=contrato_id,
                force_refresh=force_refresh,
            )
            if usar_setores_cache_local:
                f_st = pool.submit(self.inlog._ler_setores_tempo_real_db, cid_st)
            else:
                f_st = pool.submit(
                    self.inlog.get_setores_tempo_real,
                    contrato_id=contrato_id,
                    force_refresh=force_refresh,
                )
            try:
                veiculos_rt = f_rt.result() or []
            except Exception as e:
                print(f"[ENGINE] Aviso ao consultar Veiculos/TempoReal: {e}")
            try:
                rd_items = f_rd.result() or []
            except Exception as e:
                print(f"[ENGINE] Aviso ao consultar RD para complementar placas: {e}")
            try:
                st_items = f_st.result() or []
            except Exception as e:
                print(f"[ENGINE] Aviso ao consultar Setores/TempoReal para complementar placas: {e}")

        placas_set: Set[str] = set()
        for v in veiculos_rt:
            pl = formatar_placa_hifen(v.get("placaVeiculo") or v.get("identificadorVeiculo"))
            if pl:
                placas_set.add(pl)

        # Complementa com placas registradas no Relatório Diário daquela cidade/data
        op_norm = normalizar_ascii(operacao)
        for item in rd_items:
            grupo_op = normalizar_ascii(f"{item.get('grupoControle', '')} {item.get('operacao', '')}")
            if op_norm in ("", "ALL", "TODAS"):
                incluir = True
            elif op_norm == "COLETA":
                incluir = ("DOMICILIAR" in grupo_op or "SELETIVA" in grupo_op or "30 DIAS" in grupo_op)
            else:
                incluir = op_norm in grupo_op

            if incluir:
                pl = formatar_placa_hifen(item.get("placa") or item.get("veiculo"))
                if pl:
                    placas_set.add(pl)

        # Complementa com placas atribuídas aos setores da cidade em Setores/TempoReal
        for item in st_items:
            macro_setor = normalizar_ascii(f"{item.get('macroSetor', '')} {item.get('setor', '')}")
            if op_norm in ("", "ALL", "TODAS"):
                incluir = True
            elif op_norm == "COLETA":
                incluir = (
                    "DOMICILIAR" in macro_setor
                    or "SELETIVA" in macro_setor
                    or "30 DIAS" in macro_setor
                    or ("ENTULHO" not in macro_setor and "CAPINA" not in macro_setor and "ROCO" not in macro_setor)
                )
            else:
                incluir = op_norm in macro_setor
            if incluir:
                pl = formatar_placa_hifen(item.get("placaVeiculo") or item.get("identificadorVeiculo"))
                if pl:
                    placas_set.add(pl)

        placas = sorted(placas_set)
        gps_por_placa: Dict[str, List[Dict[str, Any]]] = {}

        if placas:
            workers = min(5, len(placas))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futuros = {
                    pool.submit(
                        self.inlog.get_historico_gps_janela_operacional,
                        placa,
                        data_iso,
                        contrato_id,
                        force_refresh,
                    ): placa
                    for placa in placas
                }
                for fut in as_completed(futuros):
                    placa = futuros[fut]
                    try:
                        pts = fut.result() or []
                        gps_por_placa[placa] = pts
                    except Exception as e:
                        print(f"[ENGINE] Erro ao buscar GPS de {placa}: {e}")
                        gps_por_placa[placa] = []

        return veiculos_rt, placas, gps_por_placa

    def _construir_grade_espacial(
        self,
        gps_por_placa: Dict[str, List[Dict[str, Any]]],
        ref_lat: float,
        ref_lon: float,
        regras: Dict[str, float],
    ) -> Tuple[
        Dict[Tuple[int, int], List[Tuple[float, float, str]]],
        Dict[Tuple[int, int], List[Tuple[float, float, str]]],
        Dict[Tuple[int, int], List[Tuple[float, float, str]]],
        int,
    ]:
        """Converte trilhas GPS reais para plano métrico local e indexa em células de 50x50m."""
        cos_lat = math.cos(math.radians(ref_lat))
        m_per_deg_lat = 111320.0
        m_per_deg_lon = 111320.0 * max(0.1, cos_lat)
        cell_size = 50.0

        def to_xy(lat: float, lon: float) -> Tuple[float, float]:
            return (lon - ref_lon) * m_per_deg_lon, (lat - ref_lat) * m_per_deg_lat

        def add_point(grid: Dict, x: float, y: float, placa: str):
            cell = (int(math.floor(x / cell_size)), int(math.floor(y / cell_size)))
            grid[cell].append((x, y, placa))

        pass_grid: Dict[Tuple[int, int], List[Tuple[float, float, str]]] = defaultdict(list)
        dwell_grid: Dict[Tuple[int, int], List[Tuple[float, float, str]]] = defaultdict(list)
        gap_grid: Dict[Tuple[int, int], List[Tuple[float, float, str]]] = defaultdict(list)

        total_pontos = 0
        vel_pass_max = regras["vel_passagem_max"]
        vel_min = regras["vel_min"]
        dwell_min_s = regras["dwell_s"]

        for placa, pontos_raw in gps_por_placa.items():
            if not pontos_raw:
                continue

            pontos_ord = sorted(pontos_raw, key=lambda p: str(p.get("dataHora") or ""))
            pts_proc: List[Tuple[float, float, datetime, float]] = []

            for p in pontos_ord:
                lat = float(p.get("latitude") or 0.0)
                lon = float(p.get("longitude") or 0.0)
                dt_str = str(p.get("dataHora") or "")[:19]
                if (lat == 0.0 and lon == 0.0) or len(dt_str) < 19:
                    continue
                try:
                    dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    continue
                vel = float(p.get("velocidade") or 0.0)
                x, y = to_xy(lat, lon)
                pts_proc.append((x, y, dt, vel))
                total_pontos += 1

                if vel <= vel_pass_max:
                    add_point(pass_grid, x, y, placa)

            if not pts_proc:
                continue

            # Detecção de paradas (dwell time >= DWELL_TIME_MIN_SEGUNDOS em raio de 35m)
            anchor_x, anchor_y, t_start, t_end = pts_proc[0][0], pts_proc[0][1], pts_proc[0][2], pts_proc[0][2]
            for x, y, dt, vel in pts_proc[1:]:
                dist_anchor = math.hypot(x - anchor_x, y - anchor_y)
                if dist_anchor <= 35.0 and vel <= max(vel_min, 3.0):
                    t_end = dt
                else:
                    if (t_end - t_start).total_seconds() >= dwell_min_s:
                        add_point(dwell_grid, anchor_x, anchor_y, placa)
                    anchor_x, anchor_y, t_start, t_end = x, y, dt, dt
            if (t_end - t_start).total_seconds() >= dwell_min_s:
                add_point(dwell_grid, anchor_x, anchor_y, placa)

            # Interpolação entre pontos consecutivos e detecção de GPS gaps
            for i in range(len(pts_proc) - 1):
                x1, y1, t1, v1 = pts_proc[i]
                x2, y2, t2, v2 = pts_proc[i + 1]
                dist = math.hypot(x2 - x1, y2 - y1)
                dt_s = (t2 - t1).total_seconds()
                if dist < 1.0 or dt_s < 0:
                    continue

                vel_calc = (dist / dt_s) * 3.6 if dt_s > 0 else max(v1, v2)

                if dist <= 220.0 and dt_s <= 150.0 and vel_calc <= vel_pass_max:
                    n_steps = max(1, int(dist // 10.0))
                    for s in range(1, n_steps):
                        frac = s / n_steps
                        add_point(pass_grid, x1 + (x2 - x1) * frac, y1 + (y2 - y1) * frac, placa)
                elif (220.0 < dist <= 1800.0 and 0 < dt_s <= 900.0) or (40.0 < dist <= 1800.0 and 150.0 < dt_s <= 900.0):
                    n_steps = max(2, int(dist // 15.0))
                    for s in range(1, n_steps):
                        frac = s / n_steps
                        add_point(gap_grid, x1 + (x2 - x1) * frac, y1 + (y2 - y1) * frac, placa)

        return pass_grid, dwell_grid, gap_grid, total_pontos

    def _amostrar_segmento_xy(
        self, coords: List[List[float]], ref_lat: float, ref_lon: float, passo_m: float = 10.0
    ) -> List[Tuple[float, float]]:
        """Gera amostras métricas (x, y) a cada `passo_m` metros ao longo de uma LineString [lon, lat]."""
        if not coords:
            return []
        cos_lat = math.cos(math.radians(ref_lat))
        m_per_deg_lat = 111320.0
        m_per_deg_lon = 111320.0 * max(0.1, cos_lat)

        xy_pts = [
            ((float(pt[0]) - ref_lon) * m_per_deg_lon, (float(pt[1]) - ref_lat) * m_per_deg_lat)
            for pt in coords
            if isinstance(pt, (list, tuple)) and len(pt) >= 2
        ]
        if len(xy_pts) < 2:
            return xy_pts

        amostras: List[Tuple[float, float]] = [xy_pts[0]]
        for i in range(len(xy_pts) - 1):
            x1, y1 = xy_pts[i]
            x2, y2 = xy_pts[i + 1]
            dist = math.hypot(x2 - x1, y2 - y1)
            if dist > passo_m:
                steps = int(dist // passo_m)
                for s in range(1, steps + 1):
                    frac = s / (steps + 1)
                    amostras.append((x1 + (x2 - x1) * frac, y1 + (y2 - y1) * frac))
            amostras.append((x2, y2))
        return amostras

    def _consultar_grade(
        self,
        amostras: List[Tuple[float, float]],
        grid: Dict[Tuple[int, int], List[Tuple[float, float, str]]],
        raio_m: float,
    ) -> Tuple[int, Set[str]]:
        """Retorna quantos pontos amostrais da rua têm ponto GPS dentro do raio e quais placas."""
        if not grid or not amostras:
            return 0, set()

        cell_size = 50.0
        raio_sq = raio_m * raio_m
        matched = 0
        placas: Set[str] = set()

        for sx, sy in amostras:
            gx = int(math.floor(sx / cell_size))
            gy = int(math.floor(sy / cell_size))
            achou_nesta_amostra = False
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    bucket = grid.get((gx + dx, gy + dy))
                    if not bucket:
                        continue
                    for px, py, placa in bucket:
                        if (px - sx) ** 2 + (py - sy) ** 2 <= raio_sq:
                            achou_nesta_amostra = True
                            placas.add(placa)
            if achou_nesta_amostra:
                matched += 1

        return matched, placas

    def processar_ou_obter_auditoria(
        self,
        contrato_id: str,
        data_iso: str,
        turno: str = "DIURNO",
        forcar_reprocessamento: bool = False,
        operacao: str = "COLETA",
        _retry_malha: int = 0,
        incluir_geojson: bool = True,
    ) -> Dict[str, Any]:
        """Executa a auditoria espacial real da cidade na data informada ou recupera do SQLite."""
        contrato_id = contrato_id.lower().strip()
        operacao_norm = (operacao or "COLETA").upper().strip()
        turno_chave = turno if operacao_norm == "COLETA" else f"{turno}:{operacao_norm}"

        if contrato_id in ("all", "todas", "todas_as_cidades"):
            return self._processar_auditoria_todas_cidades(
                data_iso=data_iso,
                turno=turno,
                forcar_reprocessamento=forcar_reprocessamento,
                operacao=operacao_norm,
            )

        # 1. Garante que a malha viária da cidade esteja importada se disponível (antes de abrir conexão)
        db.importar_malha_cidade_se_necessario(contrato_id)

        # 2. Obtém dados do contrato, segmentos locais e verifica cache de auditoria (conexão de curta duração)
        win_op = janela_operacional_cidade(contrato_id, data_iso)
        agora_local_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        c_row = None
        todos_segmentos = []
        ref_lat = -8.1930
        ref_lon = -34.9364

        conn = db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM contratos WHERE id = ?", (contrato_id,))
            c_row = cur.fetchone()
            ref_lat = float(c_row["centro_lat"]) if c_row and c_row["centro_lat"] else -8.1930
            ref_lon = float(c_row["centro_lon"]) if c_row and c_row["centro_lon"] else -34.9364

            cur.execute(
                """
                SELECT id, fid, nome_rua, setor, setores_json, bairro, edge_key, tipo, is_manual, is_priority, extensao_m, geom_json
                FROM segmentos_viarios
                WHERE contrato_id = ?
                """,
                (contrato_id,),
            )
            todos_segmentos = cur.fetchall()

            # 3. Se já existe auditoria em cache no banco (e foi gerada após o fechamento da janela ou sem atualização pendente), retorna rápido
            if todos_segmentos and not forcar_reprocessamento:
                cur.execute(
                    """
                    SELECT * FROM auditorias
                    WHERE contrato_id = ? AND data_iso = ? AND turno = ?
                    """,
                    (contrato_id, data_iso, turno_chave),
                )
                audit_existente = cur.fetchone()
                if audit_existente:
                    criado_em_str = str(audit_existente["criado_em"] or "").strip().replace(" ", "T")[:19]
                    try:
                        placas_salvas = json.loads(audit_existente["placas_ativas_json"] or "[]")
                        if not isinstance(placas_salvas, list):
                            placas_salvas = []
                    except Exception:
                        placas_salvas = []

                    placas_simuladas = {"LOC-1010", "LOC-2020"}
                    somente_placas_simuladas = (
                        self.inlog.is_configured()
                        and bool(placas_salvas)
                        and all(str(p).upper().strip() in placas_simuladas for p in placas_salvas)
                    )
                    sem_placas_reais = (len(placas_salvas) == 0) or somente_placas_simuladas
                    auditoria_vazia = (
                        float(audit_existente["pct_cobertura"] or 0.0) <= 0.0
                        and (sem_placas_reais or self.inlog.is_configured())
                        and data_iso <= datetime.now().strftime("%Y-%m-%d")
                    )
                    idade_cache_seg = 999999.0
                    criado_em_valido = len(criado_em_str) == 19 and criado_em_str[10] == "T"
                    if criado_em_valido:
                        try:
                            dt_c = datetime.strptime(criado_em_str, "%Y-%m-%dT%H:%M:%S")
                            idade_cache_seg = (datetime.now() - dt_c).total_seconds()
                        except Exception:
                            criado_em_valido = False

                    cache_obsoleto = (
                        not criado_em_valido
                        or (criado_em_str < win_op["data_fim"] and agora_local_str > criado_em_str)
                        or (auditoria_vazia and idade_cache_seg >= 60.0)
                    )
                    if not cache_obsoleto:
                        if not incluir_geojson:
                            return {
                                "resumo": {
                                    "id": int(audit_existente["id"]),
                                    "contrato_id": contrato_id,
                                    "data_iso": data_iso,
                                    "pct_cobertura": float(audit_existente["pct_cobertura"] or 0.0),
                                }
                            }
                        return self._montar_resposta_geojson(conn, audit_existente["id"], dict(audit_existente))
        finally:
            conn.close()

        # Filtra segmentos pela agenda oficial da nomenclatura de cada setor na data consultada (em memória, sem conexão aberta)
        segmentos = []
        setores_visibilidade_cache: Dict[str, bool] = {}
        for seg in todos_segmentos:
            setor_primario = seg["setor"]
            try:
                setores_seg = json.loads(seg["setores_json"]) if seg["setores_json"] else [setor_primario]
                if not isinstance(setores_seg, list) or not setores_seg:
                    setores_seg = [setor_primario]
            except Exception:
                setores_seg = [setor_primario]

            visivel = False
            for s in setores_seg:
                s_str = str(s or "")
                if s_str not in setores_visibilidade_cache:
                    setores_visibilidade_cache[s_str] = malha_rotas.setor_visivel_na_data(
                        s_str, data_iso, contrato_id=contrato_id
                    )
                if setores_visibilidade_cache[s_str]:
                    visivel = True
                    break
            if visivel:
                segmentos.append(seg)

        if not segmentos and todos_segmentos:
            if all(str(s["setor"] or "S/S").upper() == "S/S" for s in todos_segmentos):
                segmentos = todos_segmentos

        # 4. Coleta frota real e pontos GPS reais da Inlog em paralelo (sem segurar conexão do banco)
        veiculos_rt, placas, gps_por_placa = self._coletar_frota_e_gps_cidade(
            contrato_id, data_iso, operacao=operacao_norm, force_refresh=forcar_reprocessamento
        )
        total_pontos_gps = sum(len(pts) for pts in gps_por_placa.values())
        placas_com_gps = {p: len(pts) for p, pts in gps_por_placa.items() if len(pts) > 0}

        # Caso a cidade não possua malha viária cadastrada no banco, retorna visão completa da Inlog sem erro
        if not todos_segmentos:
            return self._montar_resposta_sem_malha(
                contrato_id=contrato_id,
                c_row=dict(c_row) if c_row else None,
                data_iso=data_iso,
                turno=turno_chave,
                operacao=operacao_norm,
                placas=placas,
                placas_com_gps=placas_com_gps,
                total_pontos_gps=total_pontos_gps,
                veiculos_rt=veiculos_rt,
            )

        # 5. Cruzamento Espacial Real GPS x Segmentos Viários
        regras = self._ler_regras_operacionais()
        pass_grid, dwell_grid, gap_grid, total_pontos_indexados = self._construir_grade_espacial(
            gps_por_placa, ref_lat, ref_lon, regras
        )

        # Passo 5.1: Pré-amostragem e primeira passagem de coleta direta por GPS
        info_segmentos = []
        seg_coletados_ids = set()
        ponto_coletado_grid: Dict[Tuple[int, int], List[Tuple[float, float, str]]] = {}
        buffer_m = regras["buffer_m"]

        for seg in segmentos:
            ext = float(seg["extensao_m"] or 0.0)
            try:
                coords = json.loads(seg["geom_json"])
            except Exception:
                coords = []

            amostras = self._amostrar_segmento_xy(coords, ref_lat, ref_lon, passo_m=10.0)
            n_amostras = len(amostras)
            matched_pass, placas_pass = self._consultar_grade(amostras, pass_grid, buffer_m)
            ratio = (matched_pass / n_amostras) if n_amostras > 0 else 0.0

            eh_via_manual = bool(seg["is_manual"]) or (str(seg["tipo"] or "").upper() == "MANUAL")
            coberta_por_passagem = (n_amostras <= 4 and matched_pass >= 1) or (ratio >= 0.30 and matched_pass >= 1)

            if coberta_por_passagem and not eh_via_manual:
                seg_coletados_ids.add(seg["id"])
                # Indexa pontos de vias coletadas para avaliar adjacência/conexão topológica com vias manuais
                for x, y in amostras:
                    cx = int(math.floor(x / 50.0))
                    cy = int(math.floor(y / 50.0))
                    ponto_coletado_grid.setdefault((cx, cy), []).append((x, y, list(placas_pass)[0] if placas_pass else ""))

            info_segmentos.append({
                "seg": seg,
                "ext": ext,
                "coords": coords,
                "amostras": amostras,
                "n_amostras": n_amostras,
                "matched_pass": matched_pass,
                "placas_pass": placas_pass,
                "ratio": ratio,
                "eh_via_manual": eh_via_manual,
                "coberta_por_passagem": coberta_por_passagem,
            })

        total_m = 0.0
        coletado_m = 0.0
        nao_coletado_m = 0.0
        detalhes_inserir = []
        detalhes_map: Dict[int, Tuple[str, List[str]]] = {}

        for item in info_segmentos:
            seg = item["seg"]
            ext = item["ext"]
            amostras = item["amostras"]
            n_amostras = item["n_amostras"]
            matched_pass = item["matched_pass"]
            placas_pass = item["placas_pass"]
            ratio = item["ratio"]
            eh_via_manual = item["eh_via_manual"]
            coberta_por_passagem = item["coberta_por_passagem"]

            total_m += ext

            if eh_via_manual:
                # Regra Inlog / Locar de Coleta Manual:
                # 1. Veículo passou ou parou dentro de raio de esquina/proximidade (50m)
                # 2. OU via conectada/adjacente foi coletada (caminhão recolheu a pé a partir da esquina conectada)
                matched_man_pass, placas_man_pass = self._consultar_grade(amostras, pass_grid, 50.0)
                matched_man_dwell, placas_man_dwell = self._consultar_grade(amostras, dwell_grid, 50.0)

                conectada_a_via_coletada = False
                placas_adj: Set[str] = set()
                if not (matched_man_pass >= 1 or matched_man_dwell >= 1):
                    # Checa proximidade direta das pontas/amostras com vias conectadas já coletadas (até 35m)
                    for x, y in amostras:
                        cx0 = int(math.floor(x / 50.0))
                        cy0 = int(math.floor(y / 50.0))
                        for dx in (-1, 0, 1):
                            for dy in (-1, 0, 1):
                                for px, py, p_placa in ponto_coletado_grid.get((cx0 + dx, cy0 + dy), []):
                                    if math.hypot(x - px, y - py) <= 35.0:
                                        conectada_a_via_coletada = True
                                        if p_placa:
                                            placas_adj.add(p_placa)
                                        break
                                if conectada_a_via_coletada:
                                    break
                            if conectada_a_via_coletada:
                                break
                        if conectada_a_via_coletada:
                            break

                if matched_man_pass >= 1 or matched_man_dwell >= 1 or conectada_a_via_coletada:
                    st = "possible_manual"
                    p_resp = sorted(placas_man_pass | placas_man_dwell | placas_adj)
                    coletado_m += ext
                else:
                    matched_gap, placas_gap = self._consultar_grade(amostras, gap_grid, 35.0)
                    if matched_gap >= 1:
                        st = "possible_gps_gap"
                        p_resp = sorted(placas_gap)
                    else:
                        st = "not_collected"
                        p_resp = []
                        nao_coletado_m += ext
            elif coberta_por_passagem:
                st = "collected"
                p_resp = sorted(placas_pass)
                coletado_m += ext
            else:
                matched_gap, placas_gap = self._consultar_grade(amostras, gap_grid, 35.0)
                matched_dwell, placas_dwell = self._consultar_grade(amostras, dwell_grid, 45.0)
                if matched_gap >= 1 and matched_dwell == 0:
                    st = "possible_gps_gap"
                    p_resp = sorted(placas_gap | placas_pass)
                elif matched_dwell >= 1 or (matched_pass >= 1 and 0.0 < ratio < 0.30):
                    st = "possible_manual"
                    p_resp = sorted(placas_dwell | placas_pass)
                    coletado_m += ext
                elif matched_gap >= 1:
                    st = "possible_gps_gap"
                    p_resp = sorted(placas_gap)
                else:
                    st = "not_collected"
                    p_resp = []
                    nao_coletado_m += ext

            seg_id_int = int(seg["id"])
            detalhes_inserir.append((seg_id_int, st, json.dumps(p_resp)))
            detalhes_map[seg_id_int] = (st, p_resp)

        pct_cobertura = (coletado_m / total_m * 100.0) if total_m > 0 else 0.0
        km_total = round(total_m / 1000.0, 2)
        km_coletado = round(coletado_m / 1000.0, 2)
        km_omissao = round(nao_coletado_m / 1000.0, 2)

        # 6. Grava auditoria determinística no banco com transação atômica e proteção contra disco cheio
        malha_alterada = False
        audit_id: Optional[int] = None
        nao_persistir_zero = (
            self.inlog.is_configured()
            and total_pontos_gps == 0
            and pct_cobertura <= 0.0
            and len(segmentos) > 0
        )

        if not nao_persistir_zero:
            for tentativa_db in range(2):
                conn_w = None
                try:
                    with db._write_lock:
                        conn_w = db.get_connection()
                        cur_w = conn_w.cursor()
                        cur_w.execute("SELECT id FROM segmentos_viarios WHERE contrato_id = ?", (contrato_id,))
                        ids_validos = {int(r["id"]) for r in cur_w.fetchall()}
                        detalhes_validos = [
                            (seg_id, st, p_resp)
                            for seg_id, st, p_resp in detalhes_inserir
                            if int(seg_id) in ids_validos
                        ]

                        if detalhes_inserir and len(detalhes_validos) < len(detalhes_inserir) and _retry_malha < 1:
                            malha_alterada = True
                        else:
                            with conn_w:
                                cur_w.execute(
                                    "DELETE FROM auditorias WHERE contrato_id = ? AND data_iso = ? AND turno = ?",
                                    (contrato_id, data_iso, turno_chave),
                                )
                                cur_w.execute(
                                    """
                                    INSERT INTO auditorias (
                                        contrato_id, data_iso, turno, pct_cobertura, km_total, km_coletado,
                                        km_omissao, total_vias, vias_omissao, placas_ativas_json, criado_em
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        contrato_id,
                                        data_iso,
                                        turno_chave,
                                        round(pct_cobertura, 2),
                                        km_total,
                                        km_coletado,
                                        km_omissao,
                                        len(detalhes_validos),
                                        sum(1 for d in detalhes_validos if d[1] == "not_collected"),
                                        json.dumps(placas),
                                        datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                                    ),
                                )
                                audit_id = cur_w.lastrowid
                                detalhes_com_id = [
                                    (audit_id, seg_id, st, p_resp)
                                    for seg_id, st, p_resp in detalhes_validos
                                ]
                                if detalhes_com_id:
                                    cur_w.executemany(
                                        """
                                        INSERT INTO auditoria_detalhes (auditoria_id, segmento_id, status, placas_responsaveis)
                                        VALUES (?, ?, ?, ?)
                                        """,
                                        detalhes_com_id,
                                    )
                    break
                except Exception as e:
                    if tentativa_db == 0 and db.eh_erro_disco_cheio(e):
                        db.liberar_espaco_emergencia_db(agressivo=True)
                        continue
                    print(f"[ENGINE] Aviso ao gravar cache de auditoria ({contrato_id} {data_iso}): {e}")
                    break
                finally:
                    if conn_w is not None:
                        conn_w.close()

        if malha_alterada:
            return self.processar_ou_obter_auditoria(
                contrato_id=contrato_id,
                data_iso=data_iso,
                turno=turno,
                forcar_reprocessamento=False,
                operacao=operacao_norm,
                _retry_malha=_retry_malha + 1,
                incluir_geojson=incluir_geojson,
            )

        if not incluir_geojson:
            return {
                "resumo": {
                    "id": int(audit_id) if audit_id else None,
                    "contrato_id": contrato_id,
                    "data_iso": data_iso,
                    "pct_cobertura": round(pct_cobertura, 2),
                },
                "detalhes_map": detalhes_map,
            }

        conn_r = db.get_connection()
        try:
            cur_r = conn_r.cursor()
            if audit_id:
                cur_r.execute("SELECT * FROM auditorias WHERE id = ?", (audit_id,))
                row_a = cur_r.fetchone()
                audit_row = dict(row_a) if row_a else None
            else:
                audit_row = None

            if not audit_row:
                audit_row = {
                    "id": 0,
                    "contrato_id": contrato_id,
                    "data_iso": data_iso,
                    "turno": turno_chave,
                    "pct_cobertura": round(pct_cobertura, 2),
                    "km_total": km_total,
                    "km_coletado": km_coletado,
                    "km_omissao": km_omissao,
                    "total_vias": len(detalhes_inserir),
                    "vias_omissao": sum(1 for d in detalhes_inserir if d[1] == "not_collected"),
                    "placas_ativas_json": json.dumps(placas),
                    "criado_em": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                }
            return self._montar_resposta_geojson(
                conn_r,
                int(audit_id or 0),
                audit_row,
                extra_resumo={
                    "total_pontos_gps": total_pontos_indexados if total_pontos_indexados else total_pontos_gps,
                    "placas_com_gps": placas_com_gps,
                    "operacao": operacao_norm,
                    "sem_malha_viaria": False,
                    "regras_auditoria": regras,
                },
            )
        finally:
            conn_r.close()

    def _montar_resposta_sem_malha(
        self,
        contrato_id: str,
        c_row: Optional[Dict[str, Any]],
        data_iso: str,
        turno: str,
        operacao: str,
        placas: List[str],
        placas_com_gps: Dict[str, int],
        total_pontos_gps: int,
        veiculos_rt: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Monta resposta completa para cidades que ainda não têm malha viária GeoJSON importada."""
        setores_inlog = self.inlog.get_cadastro_setores(contrato_id=contrato_id)
        # Filtra setores conforme nomenclatura/sufixo e prefixo oficial na data
        setores_inlog_filtrados = [
            s for s in setores_inlog
            if malha_rotas.setor_visivel_na_data(
                s.get("descricao") or s.get("apelido") or s.get("identificador"),
                data_iso,
                contrato_id=contrato_id,
            )
        ]
        if not setores_inlog_filtrados and setores_inlog:
            setores_inlog_filtrados = setores_inlog

        setores_lista = []
        for s in setores_inlog_filtrados:
            cod = s.get("apelido") or s.get("descricao") or s.get("identificador")
            nom = malha_rotas.analisar_nomenclatura_setor(
                s.get("descricao") or cod, contrato_id=contrato_id
            )
            setores_lista.append(
                {
                    "codigo": cod,
                    "descricao": s.get("descricao"),
                    "identificador": s.get("identificador"),
                    "macroSetor": s.get("macroSetor"),
                    "turno": nom["turno"],
                    "agenda_rotulo": nom["agenda_rotulo"],
                    "regra_tipo": nom["regra_tipo"],
                    "centro": [0.0, 0.0],
                    "extensao_km": 0.0,
                    "km_coletado": 0.0,
                    "pct_cobertura": 0.0,
                    "vias_omissao": 0,
                    "total_vias": 0,
                    "veiculos": [],
                }
            )
        data_formatada = datetime.strptime(data_iso, "%Y-%m-%d").strftime("%d/%m/%Y")
        return {
            "resumo": {
                "id": 0,
                "contrato_id": contrato_id,
                "contrato_nome": c_row["nome"] if c_row else contrato_id.title(),
                "data_iso": data_iso,
                "data_formatada": data_formatada,
                "turno": turno,
                "operacao": operacao,
                "pct_cobertura": 0.0,
                "km_total": 0.0,
                "km_coletado": 0.0,
                "km_omissao": 0.0,
                "km_manual": 0.0,
                "vias_manual": 0,
                "pct_manual": 0.0,
                "total_vias": 0,
                "vias_omissao": 0,
                "placas_ativas": placas,
                "placas_com_gps": placas_com_gps,
                "total_pontos_gps": total_pontos_gps,
                "centro": [c_row["centro_lat"], c_row["centro_lon"]] if c_row else [-8.1930, -34.9364],
                "zoom": c_row["zoom_padrao"] if c_row else 12,
                "status_inlog": "ONLINE (INLOG OFICIAL)" if self.inlog.is_configured() else "SIMULAÇÃO COM DADOS LOCAIS",
                "sem_malha_viaria": True,
                "frequencia_dia": malha_rotas.info_frequencia_data(data_iso),
            },
            "setores": setores_lista,
            "veiculos_tempo_real": veiculos_rt,
            "geojson": {
                "type": "FeatureCollection",
                "features": [],
            },
        }

    def _montar_resposta_geojson(
        self,
        conn: Any,
        audit_id: int,
        audit_row: Dict[str, Any],
        extra_resumo: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Gera o GeoJSON com IDs reais e estatísticas consolidadas."""
        cur = conn.cursor()

        cur.execute("SELECT * FROM contratos WHERE id = ?", (audit_row["contrato_id"],))
        c_row = cur.fetchone()

        cur.execute(
            """
            SELECT s.id, s.fid, s.nome_rua, s.setor, s.setores_json, s.bairro, s.edge_key,
                   s.tipo, s.is_manual, s.is_priority, s.extensao_m, s.geom_json,
                   d.status, d.placas_responsaveis
            FROM segmentos_viarios s
            JOIN auditoria_detalhes d ON d.segmento_id = s.id
            WHERE d.auditoria_id = ?
            """,
            (audit_id,),
        )
        rows = cur.fetchall()

        cores_map = {
            "collected": "#00ff00",
            "not_collected": "#ff0000",
            "possible_gps_gap": "#ffff00",
            "possible_manual": "#0284c7",
        }

        features = []
        setores_dados = {}
        detalhes_por_seg = {}

        total_m_ativo = 0.0
        coletado_m_ativo = 0.0
        nao_coletado_m_ativo = 0.0
        manual_m_ativo = 0.0
        vias_omissao_ativa = 0
        vias_manuais_ativas = 0
        data_iso_audit = audit_row["data_iso"]
        contrato_audit = audit_row["contrato_id"]
        visivel_cache: Dict[str, bool] = {}
        nom_cache: Dict[str, Dict[str, Any]] = {}

        def _get_nom(s_nome: str) -> Dict[str, Any]:
            if s_nome not in nom_cache:
                nom_cache[s_nome] = malha_rotas.analisar_nomenclatura_setor(
                    s_nome, contrato_id=contrato_audit
                )
            return nom_cache[s_nome]

        def _is_visivel(s_nome: str) -> bool:
            if s_nome not in visivel_cache:
                visivel_cache[s_nome] = malha_rotas.setor_visivel_na_data(
                    s_nome, data_iso_audit, contrato_id=contrato_audit
                )
            return visivel_cache[s_nome]

        for r in rows:
            placas_list = json.loads(r["placas_responsaveis"]) if r["placas_responsaveis"] else []
            coords = json.loads(r["geom_json"])
            setor_primario = r["setor"] or "S/S"
            try:
                setores_seg = json.loads(r["setores_json"]) if r["setores_json"] else [setor_primario]
                if not isinstance(setores_seg, list) or not setores_seg:
                    setores_seg = [setor_primario]
            except Exception:
                setores_seg = [setor_primario]

            setores_ativos_dia = [s for s in setores_seg if _is_visivel(str(s or ""))]
            if not setores_ativos_dia:
                # Via não opera na data informada
                continue

            setor_exibicao = setores_ativos_dia[0]
            nom_exib = _get_nom(str(setor_exibicao))
            turnos_via = sorted(set(_get_nom(str(s))["turno"] for s in setores_ativos_dia))
            ext = float(r["extensao_m"] or 0.0)
            st = r["status"]
            detalhes_por_seg[r["id"]] = (r, st, set(placas_list))

            total_m_ativo += ext
            if st in ("collected", "possible_manual"):
                coletado_m_ativo += ext
                if st == "possible_manual" or bool(r["is_manual"]):
                    manual_m_ativo += ext
                    vias_manuais_ativas += 1
            elif st == "not_collected":
                nao_coletado_m_ativo += ext
                vias_omissao_ativa += 1

            for setor in setores_ativos_dia:
                if setor not in setores_dados:
                    setores_dados[setor] = {
                        "codigo": setor,
                        "pontos": [],
                        "extensao_m": 0.0,
                        "coletado_m": 0.0,
                        "total_vias": 0,
                        "vias_omissao": 0,
                        "veiculos": set(),
                    }
                setores_dados[setor]["extensao_m"] += ext
                setores_dados[setor]["total_vias"] += 1
                if st in ("collected", "possible_manual"):
                    setores_dados[setor]["coletado_m"] += ext
                elif st == "not_collected":
                    setores_dados[setor]["vias_omissao"] += 1

                for p in placas_list:
                    setores_dados[setor]["veiculos"].add(p)

                if coords:
                    pt_mid = coords[len(coords) // 2]
                    setores_dados[setor]["pontos"].append((pt_mid[1], pt_mid[0]))

            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": coords,
                    },
                    "properties": {
                        "id": r["id"],
                        "fid": r["fid"],
                        "edge_key": r["edge_key"] or "",
                        "street_name": r["nome_rua"],
                        "bairro": r["bairro"] or "",
                        "sector": setor_exibicao,
                        "sectors": setores_ativos_dia,
                        "turnos": turnos_via,
                        "agenda_rotulo": nom_exib["agenda_rotulo"],
                        "tipo": r["tipo"] or "COLETA",
                        "is_manual": bool(r["is_manual"]),
                        "is_priority": bool(r["is_priority"]),
                        "len_m": ext,
                        "status": st,
                        "color": cores_map.get(st, "#00ff00"),
                        "vehicles": placas_list,
                    },
                }
            )

        setores_lista = []
        for s_cod, s_info in sorted(setores_dados.items(), key=lambda x: x[0]):
            pts = s_info["pontos"]
            c_lat = sum(p[0] for p in pts) / len(pts) if pts else 0.0
            c_lon = sum(p[1] for p in pts) / len(pts) if pts else 0.0
            total_m = s_info["extensao_m"]
            col_m = s_info["coletado_m"]
            pct = round((col_m / total_m * 100), 2) if total_m > 0 else 0.0
            info_nom = _get_nom(str(s_cod))

            setores_lista.append(
                {
                    "codigo": s_cod,
                    "turno": info_nom["turno"],
                    "agenda_rotulo": info_nom["agenda_rotulo"],
                    "regra_tipo": info_nom["regra_tipo"],
                    "centro": [round(c_lat, 6), round(c_lon, 6)],
                    "extensao_km": round(total_m / 1000.0, 2),
                    "km_coletado": round(col_m / 1000.0, 2),
                    "pct_cobertura": pct,
                    "vias_omissao": s_info["vias_omissao"],
                    "total_vias": s_info["total_vias"],
                    "veiculos": sorted(list(s_info["veiculos"])),
                }
            )

        placas_ativas = json.loads(audit_row.get("placas_ativas_json") or "[]")
        data_formatada = datetime.strptime(audit_row["data_iso"], "%Y-%m-%d").strftime("%d/%m/%Y")

        # Conta pontos GPS cacheados na janela operacional da cidade (rastreabilidade mesmo em leitura de cache)
        win_op_res = janela_operacional_cidade(audit_row["contrato_id"], audit_row["data_iso"])
        total_pontos_gps = 0
        placas_com_gps: Dict[str, int] = {}
        if placas_ativas:
            try:
                placeholders = ",".join(["?"] * len(placas_ativas))
                cur.execute(
                    f"""
                    SELECT placa, COUNT(*) AS qtd
                    FROM inlog_historico_gps
                    WHERE data_hora >= ? AND substr(data_hora, 1, 19) <= ? AND placa IN ({placeholders})
                    GROUP BY placa
                    """,
                    [win_op_res["data_inicio"], win_op_res["data_fim"][:19]] + placas_ativas,
                )
                for row_gps in cur.fetchall():
                    placas_com_gps[row_gps["placa"]] = row_gps["qtd"]
                    total_pontos_gps += row_gps["qtd"]
            except Exception:
                pass
            if not placas_com_gps:
                for pl in placas_ativas:
                    placas_com_gps[pl] = 1

        pct_cobertura_ativo = round((coletado_m_ativo / total_m_ativo * 100.0), 2) if total_m_ativo > 0 else 0.0
        km_total_ativo = round(total_m_ativo / 1000.0, 2)
        km_coletado_ativo = round(coletado_m_ativo / 1000.0, 2)
        km_omissao_ativo = round(nao_coletado_m_ativo / 1000.0, 2)
        km_manual_ativo = round(manual_m_ativo / 1000.0, 2)
        pct_manual_ativo = round((manual_m_ativo / total_m_ativo * 100.0), 2) if total_m_ativo > 0 else 0.0

        resumo = {
            "id": audit_id,
            "contrato_id": audit_row["contrato_id"],
            "contrato_nome": c_row["nome"] if c_row else audit_row["contrato_id"].title(),
            "data_iso": audit_row["data_iso"],
            "data_formatada": data_formatada,
            "turno": audit_row["turno"],
            "janela_operacional": {
                "inicial": win_op_res["hora_inicial"],
                "final": win_op_res["hora_final"],
                "data_inicio": win_op_res["data_inicio"],
                "data_fim": win_op_res["data_fim"],
                "diurno": f"{win_op_res['hora_inicial']} → 17:00",
                "noturno": f"17:00 → {win_op_res['hora_final']} (+1d)",
            },
            "pct_cobertura": pct_cobertura_ativo,
            "km_total": km_total_ativo,
            "km_coletado": km_coletado_ativo,
            "km_omissao": km_omissao_ativo,
            "km_manual": km_manual_ativo,
            "vias_manual": vias_manuais_ativas,
            "pct_manual": pct_manual_ativo,
            "total_vias": len(features),
            "vias_omissao": vias_omissao_ativa,
            "placas_ativas": placas_ativas,
            "placas_com_gps": placas_com_gps,
            "total_pontos_gps": total_pontos_gps,
            "centro": [c_row["centro_lat"], c_row["centro_lon"]] if c_row else [-8.1930, -34.9364],
            "zoom": c_row["zoom_padrao"] if c_row else 12,
            "status_inlog": "ONLINE (INLOG OFICIAL)" if self.inlog.is_configured() else "SIMULAÇÃO COM DADOS LOCAIS",
            "sem_malha_viaria": False,
            "frequencia_dia": malha_rotas.info_frequencia_data(audit_row["data_iso"]),
        }
        if extra_resumo:
            resumo.update(extra_resumo)

        planejado_vs_executado = self._calcular_planejado_vs_executado(
            contrato_id=audit_row["contrato_id"],
            data_iso=audit_row["data_iso"],
            operacao_norm=(extra_resumo or {}).get("operacao", "COLETA"),
            placas_ativas=placas_ativas,
            placas_com_gps=placas_com_gps,
            detalhes_por_seg=detalhes_por_seg,
            setores_lista=setores_lista,
        )
        proj_info = malha_rotas.status_projeto(audit_row["contrato_id"])
        if proj_info:
            resumo["malha_arquivo"] = os.path.basename(proj_info.get("malha_arquivo") or "")
            resumo["n_rotas_planejadas"] = proj_info.get("n_rotas", 0)

        return {
            "resumo": resumo,
            "setores": setores_lista,
            "planejado_vs_executado": planejado_vs_executado,
            "geojson": {
                "type": "FeatureCollection",
                "features": features,
            },
        }

    def _processar_auditoria_todas_cidades(
        self,
        data_iso: str,
        turno: str = "DIURNO",
        forcar_reprocessamento: bool = False,
        operacao: str = "COLETA",
    ) -> Dict[str, Any]:
        """Consolida as auditorias de todas as cidades cadastradas no sistema."""
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, nome, centro_lat, centro_lon, zoom_padrao FROM contratos ORDER BY nome")
            contratos = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

        todas_features = []
        todos_setores = []
        setor_codes_seen = set()
        total_km = 0.0
        total_coletado_km = 0.0
        total_omissao_km = 0.0
        total_manual_km = 0.0
        total_vias_manuais = 0
        total_vias_omissao = 0
        placas_ativas_set = set()
        total_pontos_gps = 0
        placas_com_gps: Dict[str, int] = {}
        resumos_cidades = []
        pve_km_planejado = 0.0
        pve_km_executado = 0.0
        pve_veiculos = []
        pve_setores = []

        # Pré-aquece os 3 endpoints globais da Inlog uma única vez em paralelo antes do pool de cidades
        if forcar_reprocessamento:
            with ThreadPoolExecutor(max_workers=3) as pre_pool:
                f1 = pre_pool.submit(self.inlog.get_veiculos_tempo_real, force_refresh=True)
                f2 = pre_pool.submit(self.inlog.get_relatorio_diario, data_iso, force_refresh=True)
                f3 = pre_pool.submit(self.inlog.get_setores_tempo_real, force_refresh=True)
                for f in (f1, f2, f3):
                    try:
                        f.result()
                    except Exception:
                        pass

        audits_por_cid: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=2) as city_pool:
            futuros_cid = {
                city_pool.submit(
                    self.processar_ou_obter_auditoria,
                    c["id"],
                    data_iso,
                    turno=turno,
                    forcar_reprocessamento=forcar_reprocessamento,
                    operacao=operacao,
                ): c["id"]
                for c in contratos
            }
            for fut in as_completed(futuros_cid):
                cid_f = futuros_cid[fut]
                try:
                    audits_por_cid[cid_f] = fut.result()
                except Exception as e:
                    print(f"[ENGINE] Erro ao auditar cidade {cid_f}: {e}")

        for c in contratos:
            c_id = c["id"]
            try:
                audit = audits_por_cid.get(c_id)
                if audit is None:
                    continue
                r = audit.get("resumo", {})
                km_tot = float(r.get("km_total", 0.0) or 0.0)
                km_col = float(r.get("km_coletado", 0.0) or 0.0)
                km_omis = float(r.get("km_omissao", 0.0) or 0.0)
                km_man = float(r.get("km_manual", 0.0) or 0.0)
                v_man = int(r.get("vias_manual", 0) or 0)
                v_omis = int(r.get("vias_omissao", 0) or 0)

                total_km += km_tot
                total_coletado_km += km_col
                total_omissao_km += km_omis
                total_manual_km += km_man
                total_vias_manuais += v_man
                total_vias_omissao += v_omis

                for p in r.get("placas_ativas", []):
                    placas_ativas_set.add(p)

                total_pontos_gps += int(r.get("total_pontos_gps", 0) or 0)
                for p, qtd in r.get("placas_com_gps", {}).items():
                    placas_com_gps[p] = placas_com_gps.get(p, 0) + qtd

                for feat in audit.get("geojson", {}).get("features", []):
                    props = feat.get("properties", {})
                    props["cidade_id"] = c_id
                    props["cidade_nome"] = c["nome"]
                    todas_features.append(feat)

                for s in audit.get("setores", []):
                    s_cod = s.get("codigo", "")
                    s_key = f"{c_id}:{s_cod}"
                    if s_key not in setor_codes_seen:
                        setor_codes_seen.add(s_key)
                        s_copy = dict(s)
                        s_copy["cidade_id"] = c_id
                        s_copy["cidade_nome"] = c["nome"]
                        todos_setores.append(s_copy)

                pve = audit.get("planejado_vs_executado")
                if isinstance(pve, list):
                    for pv in pve:
                        pv_copy = dict(pv)
                        pv_copy["cidade"] = c["nome"]
                        pve_veiculos.append(pv_copy)
                        pve_km_planejado += float(pv.get("km_planejado", 0.0) or 0.0)
                        pve_km_executado += float(pv.get("km_executado_planejado", 0.0) or 0.0)
                elif isinstance(pve, dict):
                    pve_km_planejado += float(pve.get("km_planejado", 0.0) or 0.0)
                    pve_km_executado += float(pve.get("km_executado", 0.0) or 0.0)
                    for pv in pve.get("veiculos", []):
                        pv_copy = dict(pv)
                        pv_copy["cidade"] = c["nome"]
                        pve_veiculos.append(pv_copy)

                resumos_cidades.append({
                    "id": c_id,
                    "nome": c["nome"],
                    "pct_cobertura": r.get("pct_cobertura", 0.0),
                    "km_total": km_tot,
                    "km_coletado": km_col,
                    "km_omissao": km_omis,
                    "km_manual": km_man,
                    "total_vias": len(audit.get("geojson", {}).get("features", [])),
                    "placas_ativas": len(r.get("placas_ativas", [])),
                })
            except Exception as e:
                print(f"[ENGINE] Aviso ao consolidar cidade {c_id}: {e}")

        pct_cobertura = round((total_coletado_km / total_km * 100.0), 2) if total_km > 0 else 0.0
        pct_manual = round((total_manual_km / total_km * 100.0), 2) if total_km > 0 else 0.0
        try:
            dt_fmt = datetime.strptime(data_iso, "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            dt_fmt = data_iso

        aderencia_geral = round((pve_km_executado / pve_km_planejado * 100.0), 2) if pve_km_planejado > 0 else 0.0

        return {
            "resumo": {
                "id": 0,
                "contrato_id": "all",
                "contrato_nome": "Todas as Cidades (Visão Geral)",
                "data_iso": data_iso,
                "data_formatada": dt_fmt,
                "turno": turno,
                "pct_cobertura": pct_cobertura,
                "km_total": round(total_km, 2),
                "km_coletado": round(total_coletado_km, 2),
                "km_omissao": round(total_omissao_km, 2),
                "km_manual": round(total_manual_km, 2),
                "vias_manual": total_vias_manuais,
                "pct_manual": pct_manual,
                "total_vias": len(todas_features),
                "vias_omissao": total_vias_omissao,
                "placas_ativas": sorted(list(placas_ativas_set)),
                "placas_com_gps": placas_com_gps,
                "total_pontos_gps": total_pontos_gps,
                "centro": [-14.2350, -51.9253],
                "zoom": 4,
                "status_inlog": "ONLINE (INLOG OFICIAL)" if self.inlog.is_configured() else "CONSOLIDADO NACIONAL",
                "sem_malha_viaria": False,
                "resumos_cidades": resumos_cidades,
                "frequencia_dia": malha_rotas.info_frequencia_data(data_iso),
            },
            "setores": todos_setores,
            "planejado_vs_executado": pve_veiculos,
            "geojson": {
                "type": "FeatureCollection",
                "features": todas_features,
            },
        }

    def _calcular_planejado_vs_executado(
        self,
        contrato_id: str,
        data_iso: str,
        operacao_norm: str,
        placas_ativas: List[str],
        placas_com_gps: Dict[str, int],
        detalhes_por_seg: Dict[int, Tuple[Any, str, Set[str]]],
        setores_lista: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Cruza o planejamento de setores da Inlog (Setores/TempoReal + RelatorioDiario)
        com as rotas planejadas (.gpkg) e a execução GPS real de cada veículo.
        """
        hoje_str = datetime.now().strftime("%Y-%m-%d")
        veic_setores: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        setor_vinculo: Dict[str, Dict[str, Any]] = {}

        # 1. Relatório Diário (histórico oficial do dia: setor, placa, peso, tickets)
        try:
            rd_items = self.inlog.get_relatorio_diario(data_iso, contrato_id=contrato_id, force_refresh=False)
        except Exception:
            rd_items = []

        for item in rd_items:
            pl = formatar_placa_hifen(item.get("placa") or item.get("veiculo"))
            setor_nome = str(item.get("setor") or "").strip()
            if not pl or not setor_nome:
                continue
            rota = malha_rotas.resolver_rota_setor(contrato_id, setor_nome, data_iso=data_iso)
            apelido = rota["apelido"] if rota else setor_nome
            veic_setores[pl].append({
                "setor_inlog": setor_nome,
                "rota_apelido": apelido,
                "rota": rota,
                "turno": rota["turno"] if rota else "",
                "fonte": "Relatório Diário",
                "situacao": "APONTADO",
                "pct_inlog": None,
                "peso_kg": float(item.get("pesoTotalKg") or 0.0),
                "motorista": "",
            })
            if rota:
                setor_vinculo.setdefault(apelido, {"placas": set(), "situacao": "APONTADO", "pct_inlog": None})["placas"].add(pl)

        # 2. Setores em Tempo Real (dia atual: veículo vinculado ao setor, conclusão Inlog, motorista)
        if data_iso == hoje_str:
            try:
                setores_rt = self.inlog.get_setores_tempo_real(contrato_id=contrato_id, force_refresh=False)
            except Exception:
                setores_rt = []
            for s in setores_rt:
                pl = formatar_placa_hifen(s.get("placaVeiculo") or s.get("identificadorVeiculo"))
                setor_nome = str(s.get("setor") or "").strip()
                turno = str(s.get("turno") or "").strip()
                if not setor_nome:
                    continue
                rota = malha_rotas.resolver_rota_setor(contrato_id, setor_nome, data_iso=data_iso, turno=turno)
                apelido = rota["apelido"] if rota else setor_nome
                if rota:
                    sv = setor_vinculo.setdefault(apelido, {"placas": set(), "situacao": s.get("situacao") or "", "pct_inlog": s.get("percentualConclusao")})
                    if pl:
                        sv["placas"].add(pl)
                    sv["situacao"] = s.get("situacao") or sv["situacao"]
                    sv["pct_inlog"] = s.get("percentualConclusao")
                if not pl:
                    continue
                ja_existe = any(x["setor_inlog"] == setor_nome for x in veic_setores[pl])
                if ja_existe:
                    for x in veic_setores[pl]:
                        if x["setor_inlog"] == setor_nome:
                            x["situacao"] = s.get("situacao") or x["situacao"]
                            x["pct_inlog"] = s.get("percentualConclusao")
                            x["motorista"] = s.get("motorista") or x["motorista"]
                else:
                    veic_setores[pl].append({
                        "setor_inlog": setor_nome,
                        "rota_apelido": apelido,
                        "rota": rota,
                        "turno": turno or (rota["turno"] if rota else ""),
                        "fonte": "Setores/TempoReal",
                        "situacao": s.get("situacao") or "",
                        "pct_inlog": s.get("percentualConclusao"),
                        "peso_kg": 0.0,
                        "motorista": s.get("motorista") or "",
                    })

        # Enriquece lista de setores com vínculo Inlog
        for s_item in setores_lista:
            sv = setor_vinculo.get(s_item["codigo"])
            if sv:
                s_item["placas_inlog"] = sorted(sv["placas"])
                s_item["situacao_inlog"] = sv["situacao"]
                s_item["pct_inlog"] = sv["pct_inlog"]

        # Índice rápido de segmentos auditados por edge_key
        seg_por_ek: Dict[str, List[Tuple[Any, str, Set[str]]]] = defaultdict(list)
        for seg_row, st, placas_set in detalhes_por_seg.values():
            ek = seg_row["edge_key"] or ""
            if ek:
                seg_por_ek[ek].append((seg_row, st, placas_set))

        todas_placas = sorted(set(placas_ativas) | set(veic_setores.keys()) | set(placas_com_gps.keys()))
        comparacoes: List[Dict[str, Any]] = []

        for placa in todas_placas:
            atribuicoes = veic_setores.get(placa, [])
            rotas_unicas: Dict[str, Dict[str, Any]] = {}
            setores_inlog_nomes: List[str] = []
            peso_total = 0.0
            motorista = ""
            situacao = ""
            pct_inlog_max = None

            for attr in atribuicoes:
                setores_inlog_nomes.append(attr["setor_inlog"])
                peso_total += attr.get("peso_kg") or 0.0
                if attr.get("motorista") and not motorista:
                    motorista = attr["motorista"]
                if attr.get("situacao") and not situacao:
                    situacao = attr["situacao"]
                if attr.get("pct_inlog") is not None:
                    pct_inlog_max = max(pct_inlog_max or 0.0, float(attr["pct_inlog"]))
                if attr["rota"]:
                    rotas_unicas[attr["rota"]["path"]] = attr["rota"]

            planned_eks: Set[str] = set()
            planned_m = 0.0
            for rota in rotas_unicas.values():
                for a in malha_rotas.carregar_rota(rota):
                    if a["is_collection"] and a["edge_key"] not in planned_eks:
                        planned_eks.add(a["edge_key"])
                        planned_m += a["len_m"]

            exec_placa_no_plan_m = 0.0
            coberto_setor_no_plan_m = 0.0
            trechos_exec_placa = 0
            trechos_faltantes = 0
            faltante_m = 0.0

            for ek in planned_eks:
                matches = seg_por_ek.get(ek, [])
                if not matches:
                    trechos_faltantes += 1
                    continue
                seg_row, st, placas_set = matches[0]
                ext = float(seg_row["extensao_m"] or 0.0)
                if placa in placas_set:
                    exec_placa_no_plan_m += ext
                    trechos_exec_placa += 1
                if st in ("collected", "possible_manual"):
                    coberto_setor_no_plan_m += ext
                else:
                    trechos_faltantes += 1
                    faltante_m += ext

            # Trechos executados por este veículo fora da rota planejada
            fora_rota_m = 0.0
            trechos_fora_rota = 0
            for seg_row, st, placas_set in detalhes_por_seg.values():
                if placa in placas_set:
                    ek = seg_row["edge_key"] or ""
                    if not planned_eks or ek not in planned_eks:
                        fora_rota_m += float(seg_row["extensao_m"] or 0.0)
                        trechos_fora_rota += 1

            km_plan = round(planned_m / 1000.0, 2)
            km_exec_plan = round(exec_placa_no_plan_m / 1000.0, 2)
            km_cob_setor = round(coberto_setor_no_plan_m / 1000.0, 2)
            km_falt = round(faltante_m / 1000.0, 2)
            km_fora = round(fora_rota_m / 1000.0, 2)
            pct_aderencia = round((exec_placa_no_plan_m / planned_m * 100.0), 1) if planned_m > 0 else 0.0
            pct_setor = round((coberto_setor_no_plan_m / planned_m * 100.0), 1) if planned_m > 0 else 0.0

            comparacoes.append({
                "placa": placa,
                "motorista": motorista,
                "situacao_inlog": situacao or ("COM GPS" if placas_com_gps.get(placa, 0) > 0 else "SEM SINAL"),
                "pct_inlog": pct_inlog_max,
                "peso_kg": round(peso_total, 1),
                "pontos_gps": placas_com_gps.get(placa, 0),
                "setores_inlog": sorted(set(setores_inlog_nomes)),
                "rotas_planejadas": [r["apelido"] for r in rotas_unicas.values()],
                "tem_rota_planejada": len(planned_eks) > 0,
                "trechos_planejados": len(planned_eks),
                "trechos_executados": trechos_exec_placa,
                "trechos_faltantes": trechos_faltantes,
                "trechos_fora_rota": trechos_fora_rota,
                "km_planejado": km_plan,
                "km_executado_planejado": km_exec_plan,
                "km_coberto_setor": km_cob_setor,
                "km_faltante": km_falt,
                "km_fora_rota": km_fora,
                "pct_aderencia": pct_aderencia,
                "pct_cobertura_setor": pct_setor,
            })

        comparacoes.sort(key=lambda c: (not c["tem_rota_planejada"], -c["km_planejado"], c["placa"]))
        return comparacoes

    def gerar_csv_omissoes_string(self, audit_id: int) -> Tuple[str, str]:
        """Gera o CSV de vias não coletadas diretamente como string em memória."""
        conn = db.get_connection()
        try:
            cur = conn.cursor()

            cur.execute(
                "SELECT a.*, c.nome as cidade_nome FROM auditorias a JOIN contratos c ON c.id = a.contrato_id WHERE a.id = ?",
                (audit_id,),
            )
            audit = cur.fetchone()
            if not audit:
                return "", "relatorio.csv"

            cur.execute(
                """
                SELECT s.nome_rua, s.setor, s.setores_json, s.extensao_m
                FROM segmentos_viarios s
                JOIN auditoria_detalhes d ON d.segmento_id = s.id
                WHERE d.auditoria_id = ? AND d.status = 'not_collected'
                ORDER BY s.setor, s.nome_rua
                """,
                (audit_id,),
            )
            linhas = cur.fetchall()
        finally:
            conn.close()

        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Data", "Cidade", "Setor", "Logradouro / Rua", "Extensão (Metros)", "Situação", "Ação Recomendada"])

        data_iso_aud = audit["data_iso"]
        contrato_aud = audit["contrato_id"]
        vis_cache: Dict[str, bool] = {}
        data_fmt = datetime.strptime(data_iso_aud, "%Y-%m-%d").strftime("%d/%m/%Y")
        for r in linhas:
            setor_primario = r["setor"] or "S/S"
            try:
                setores_seg = json.loads(r["setores_json"]) if r["setores_json"] else [setor_primario]
                if not isinstance(setores_seg, list) or not setores_seg:
                    setores_seg = [setor_primario]
            except Exception:
                setores_seg = [setor_primario]
            ativos = []
            for s_nome in setores_seg:
                s_str = str(s_nome or "")
                if s_str not in vis_cache:
                    vis_cache[s_str] = malha_rotas.setor_visivel_na_data(
                        s_str, data_iso_aud, contrato_id=contrato_aud
                    )
                if vis_cache[s_str]:
                    ativos.append(s_str)
            if not ativos:
                continue
            writer.writerow(
                [
                    data_fmt,
                    audit["cidade_nome"],
                    ativos[0],
                    r["nome_rua"],
                    round(r["extensao_m"], 1),
                    "NÃO COLETADA (ÁREA VERMELHA)",
                    "Enviar caminhão de repasse imediato",
                ]
            )

        nome_arquivo = f"omissoes_{audit['contrato_id']}_{audit['data_iso']}.csv"
        return output.getvalue(), nome_arquivo

    @staticmethod
    def _cor_por_percentual(pct: float, eh_manual: bool = False) -> str:
        """Retorna a cor da legenda por faixa percentual (normal ou manual em tons de azul)."""
        if eh_manual:
            if pct >= 80.0:
                return "#00FFFF"
            if pct >= 60.0:
                return "#00BFFF"
            if pct >= 40.0:
                return "#0080FF"
            if pct >= 20.0:
                return "#0040FF"
            return "#001F66"
        if pct >= 80.0:
            return "#00E600"
        if pct >= 60.0:
            return "#FFFF00"
        if pct >= 40.0:
            return "#FF9900"
        if pct >= 20.0:
            return "#FF0000"
        return "#660000"

    def processar_auditoria_periodo(
        self,
        contrato_id: str,
        data_iso: str,
        periodo: str = "mensal",
        turno: str = "TODOS",
        operacao: str = "COLETA",
    ) -> Dict[str, Any]:
        """Consolida cobertura por rua e setor em janela quinzenal (1ª/2ª) ou mensal."""
        contrato_id = str(contrato_id or "cabo").lower().strip()
        periodo_norm = str(periodo or "mensal").lower().strip()
        if periodo_norm not in ("quinzena1", "quinzena2", "mensal"):
            return self.processar_ou_obter_auditoria(
                contrato_id, data_iso, turno=turno, forcar_reprocessamento=False, operacao=operacao
            )

        dt_ref = datetime.strptime(str(data_iso)[:10], "%Y-%m-%d")
        ano, mes = dt_ref.year, dt_ref.month
        ultimo_dia = calendar.monthrange(ano, mes)[1]
        if periodo_norm == "quinzena1":
            dia_ini, dia_fim = 1, 15
            rotulo_periodo = "1ª Quinzena"
        elif periodo_norm == "quinzena2":
            dia_ini, dia_fim = 16, ultimo_dia
            rotulo_periodo = "2ª Quinzena"
        else:
            dia_ini, dia_fim = 1, ultimo_dia
            rotulo_periodo = "Mensal"

        inicio_iso = f"{ano:04d}-{mes:02d}-{dia_ini:02d}"
        fim_teorico_iso = f"{ano:04d}-{mes:02d}-{dia_fim:02d}"
        hoje_iso = datetime.now().strftime("%Y-%m-%d")
        fim_iso = min(fim_teorico_iso, hoje_iso)

        dias: List[str] = []
        if inicio_iso <= fim_iso:
            cur_dt = datetime.strptime(inicio_iso, "%Y-%m-%d")
            end_dt = datetime.strptime(fim_iso, "%Y-%m-%d")
            while cur_dt <= end_dt:
                dias.append(cur_dt.strftime("%Y-%m-%d"))
                cur_dt += timedelta(days=1)

        operacao_norm = (operacao or "COLETA").upper().strip()
        turno_chave = "TODOS" if operacao_norm == "COLETA" else f"TODOS:{operacao_norm}"
        cache_key = f"{contrato_id}:{periodo_norm}:{inicio_iso}:{fim_iso}:{operacao_norm}:{NOMENCLATURA_VERSION}"

        # 0. Cache consolidado de período no SQLite (evita reprocessar 15-30 dias a cada acesso)
        conn_c = db.get_connection()
        try:
            conn_c.execute(
                "CREATE TABLE IF NOT EXISTS auditorias_periodo_cache (chave TEXT PRIMARY KEY, criado_em TEXT, payload_json TEXT)"
            )
            row_c = conn_c.execute(
                "SELECT criado_em, payload_json FROM auditorias_periodo_cache WHERE chave = ?",
                (cache_key,),
            ).fetchone()
            if row_c and row_c["payload_json"]:
                cache_valido = fim_iso < hoje_iso
                if not cache_valido and row_c["criado_em"]:
                    try:
                        dt_c = datetime.strptime(row_c["criado_em"], "%Y-%m-%d %H:%M:%S")
                        if (datetime.now() - dt_c).total_seconds() < 300:
                            cache_valido = True
                    except Exception:
                        pass
                if cache_valido:
                    return json.loads(row_c["payload_json"])
        except Exception:
            pass
        finally:
            conn_c.close()

        db.importar_malha_cidade_se_necessario(contrato_id)
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            c_row = cur.execute("SELECT * FROM contratos WHERE id = ?", (contrato_id,)).fetchone()
            segs_rows = cur.execute(
                """
                SELECT id, fid, nome_rua, setor, setores_json, bairro, edge_key,
                       tipo, is_manual, is_priority, extensao_m, geom_json
                FROM segmentos_viarios
                WHERE contrato_id = ?
                """,
                (contrato_id,),
            ).fetchall()

            # Verifica quais dias já estão auditados no SQLite para evitar GeoJSON completo e paralelizar apenas faltantes
            aud_por_dia: Dict[str, Any] = {}
            if dias:
                placeholders = ",".join(["?"] * len(dias))
                rows_aud = cur.execute(
                    f"SELECT id, data_iso, placas_ativas_json FROM auditorias WHERE contrato_id = ? AND turno = ? AND data_iso IN ({placeholders})",
                    [contrato_id, turno_chave] + dias,
                ).fetchall()
                for ra in rows_aud:
                    if ra["data_iso"] != hoje_iso:
                        aud_por_dia[ra["data_iso"]] = ra
        finally:
            conn.close()

        dias_faltantes = [d for d in dias if d not in aud_por_dia]
        if dias_faltantes:
            workers = min(4, len(dias_faltantes))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {
                    pool.submit(
                        self.processar_ou_obter_auditoria,
                        contrato_id,
                        d_iso,
                        "TODOS",
                        False,
                        operacao_norm,
                    ): d_iso
                    for d_iso in dias_faltantes
                }
                for fut in as_completed(futs):
                    try:
                        fut.result()
                    except Exception as e:
                        print(f"[ENGINE] Aviso ao processar dia {futs[fut]} no período: {e}")

            conn2 = db.get_connection()
            try:
                placeholders = ",".join(["?"] * len(dias))
                rows_aud2 = conn2.execute(
                    f"SELECT id, data_iso, placas_ativas_json FROM auditorias WHERE contrato_id = ? AND turno = ? AND data_iso IN ({placeholders})",
                    [contrato_id, turno_chave] + dias,
                ).fetchall()
                for ra in rows_aud2:
                    aud_por_dia[ra["data_iso"]] = ra
            finally:
                conn2.close()

        seg_map: Dict[int, Dict[str, Any]] = {}
        for r in segs_rows:
            setor_prim = r["setor"] or "S/S"
            try:
                setores_seg = json.loads(r["setores_json"]) if r["setores_json"] else [setor_prim]
                if not isinstance(setores_seg, list) or not setores_seg:
                    setores_seg = [setor_prim]
            except Exception:
                setores_seg = [setor_prim]
            seg_map[r["id"]] = {
                "row": r,
                "setor_primario": setor_prim,
                "setores_seg": [str(s or "") for s in setores_seg],
                "dias_previstos": 0,
                "dias_coletados": 0,
                "dias_manuais": 0,
                "placas": set(),
                "setores_ativos": set(),
                "turnos": set(),
            }

        placas_ativas_periodo: Set[str] = set()
        total_pontos_gps_periodo = 0
        setores_acc: Dict[str, Dict[str, Any]] = {}
        vis_cache: Dict[Tuple[str, str], bool] = {}
        nom_cache: Dict[str, Dict[str, Any]] = {}

        def _eh_visivel(s_nome: str, d_iso: str) -> bool:
            key = (s_nome, d_iso)
            if key not in vis_cache:
                vis_cache[key] = malha_rotas.setor_visivel_na_data(
                    s_nome, d_iso, contrato_id=contrato_id
                )
            return vis_cache[key]

        def _get_nom(s_nome: str) -> Dict[str, Any]:
            if s_nome not in nom_cache:
                nom_cache[s_nome] = malha_rotas.analisar_nomenclatura_setor(
                    s_nome, contrato_id=contrato_id
                )
            return nom_cache[s_nome]

        conn_det = db.get_connection()
        try:
            for d_iso in dias:
                ra = aud_por_dia.get(d_iso)
                status_por_seg: Dict[int, Tuple[str, List[str]]] = {}
                if ra:
                    try:
                        for pl in json.loads(ra["placas_ativas_json"] or "[]"):
                            placas_ativas_periodo.add(pl)
                    except Exception:
                        pass
                    det_rows = conn_det.execute(
                        "SELECT segmento_id, status, placas_responsaveis FROM auditoria_detalhes WHERE auditoria_id = ?",
                        (ra["id"],),
                    ).fetchall()
                    for dr in det_rows:
                        try:
                            veics = json.loads(dr["placas_responsaveis"] or "[]")
                        except Exception:
                            veics = []
                        status_por_seg[int(dr["segmento_id"])] = (
                            str(dr["status"] or "not_collected"),
                            veics,
                        )

                for sid, info in seg_map.items():
                    ativos_dia = [s for s in info["setores_seg"] if _eh_visivel(s, d_iso)]
                    if not ativos_dia:
                        continue
                    info["dias_previstos"] += 1
                    for s_at in ativos_dia:
                        info["setores_ativos"].add(s_at)
                        info["turnos"].add(_get_nom(s_at)["turno"])

                    st_dia, veics_dia = status_por_seg.get(sid, ("not_collected", []))
                    for v in veics_dia:
                        info["placas"].add(v)
                    eh_man_dia = (st_dia == "possible_manual") or (
                        bool(info["row"]["is_manual"]) and st_dia != "not_collected"
                    )
                    coletado_dia = st_dia in ("collected", "possible_manual")
                    if coletado_dia:
                        info["dias_coletados"] += 1
                    if eh_man_dia:
                        info["dias_manuais"] += 1

                    ext_m = float(info["row"]["extensao_m"] or 0.0)
                    for s_at in ativos_dia:
                        if s_at not in setores_acc:
                            nom = _get_nom(s_at)
                            setores_acc[s_at] = {
                                "codigo": s_at,
                                "turno": nom["turno"],
                                "agenda_rotulo": nom.get("agenda_rotulo", ""),
                                "regra_tipo": nom["regra_tipo"],
                                "ext_prev_m": 0.0,
                                "ext_col_m": 0.0,
                                "seg_ids": set(),
                                "seg_baixos": set(),
                                "veiculos": set(),
                            }
                        sa = setores_acc[s_at]
                        sa["ext_prev_m"] += ext_m
                        if coletado_dia:
                            sa["ext_col_m"] += ext_m
                        sa["seg_ids"].add(sid)
                        for v in veics_dia:
                            sa["veiculos"].add(v)
        finally:
            conn_det.close()

        features: List[Dict[str, Any]] = []
        total_m_prev_sum = 0.0
        coletado_m_sum = 0.0
        manual_m_sum = 0.0
        omissao_m_sum = 0.0
        vias_abaixo_80 = 0
        vias_manuais = 0

        for sid, info in seg_map.items():
            if info["dias_previstos"] <= 0:
                continue
            r = info["row"]
            ext_m = float(r["extensao_m"] or 0.0)
            dias_prev = info["dias_previstos"]
            dias_col = info["dias_coletados"]
            dias_man = info["dias_manuais"]

            pct_seg = round((dias_col / dias_prev) * 100.0, 2)
            eh_manual = bool(r["is_manual"]) or (dias_man > 0)
            cor = self._cor_por_percentual(pct_seg, eh_manual=eh_manual)

            total_m_prev_sum += ext_m * dias_prev
            coletado_m_sum += ext_m * dias_col
            if eh_manual:
                manual_m_sum += ext_m * dias_col
                vias_manuais += 1
            else:
                omissao_m_sum += ext_m * (dias_prev - dias_col)

            if pct_seg < 80.0:
                vias_abaixo_80 += 1
                for s_at in info["setores_ativos"]:
                    if s_at in setores_acc:
                        setores_acc[s_at]["seg_baixos"].add(sid)

            status_sint = "possible_manual" if eh_manual else ("collected" if pct_seg >= 80.0 else "not_collected")
            setores_list = sorted(info["setores_ativos"]) or info["setores_seg"]

            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": json.loads(r["geom_json"])},
                    "properties": {
                        "id": sid,
                        "fid": r["fid"],
                        "street_name": r["nome_rua"],
                        "sector": setores_list[0],
                        "sectors": setores_list,
                        "turnos": sorted(info["turnos"]) or [_get_nom(setores_list[0])["turno"]],
                        "bairro": r["bairro"] or "",
                        "tipo": r["tipo"] or "COLETA",
                        "is_manual": eh_manual,
                        "is_priority": bool(r["is_priority"]),
                        "status": status_sint,
                        "pct_periodo": pct_seg,
                        "dias_previstos": dias_prev,
                        "dias_coletados": dias_col,
                        "dias_manuais": dias_man,
                        "color": cor,
                        "len_m": round(ext_m, 2),
                        "vehicles": sorted(info["placas"]),
                    },
                }
            )

        pct_total = round((coletado_m_sum / total_m_prev_sum * 100.0), 2) if total_m_prev_sum > 0 else 0.0
        pct_manual = round((manual_m_sum / total_m_prev_sum * 100.0), 2) if total_m_prev_sum > 0 else 0.0

        setores_out = []
        for s_cod in sorted(setores_acc.keys()):
            sa = setores_acc[s_cod]
            pct_s = round((sa["ext_col_m"] / sa["ext_prev_m"] * 100.0), 2) if sa["ext_prev_m"] > 0 else 0.0
            setores_out.append(
                {
                    "codigo": s_cod,
                    "turno": sa["turno"],
                    "agenda_rotulo": sa["agenda_rotulo"],
                    "regra_tipo": sa["regra_tipo"],
                    "extensao_km": round(sa["ext_prev_m"] / 1000.0, 2),
                    "km_coletado": round(sa["ext_col_m"] / 1000.0, 2),
                    "pct_cobertura": pct_s,
                    "vias_omissao": len(sa["seg_baixos"]),
                    "total_vias": len(sa["seg_ids"]),
                    "veiculos": sorted(sa["veiculos"]),
                }
            )

        resultado_periodo = {
            "resumo": {
                "id": None,
                "contrato_id": contrato_id,
                "contrato_nome": c_row["nome"] if c_row else contrato_id.title(),
                "data_iso": data_iso,
                "data_formatada": f"{datetime.strptime(inicio_iso, '%Y-%m-%d').strftime('%d/%m/%Y')} a {datetime.strptime(fim_iso, '%Y-%m-%d').strftime('%d/%m/%Y')}" if dias else inicio_iso,
                "turno": turno,
                "periodo": periodo_norm,
                "periodo_rotulo": rotulo_periodo,
                "periodo_inicio": inicio_iso,
                "periodo_fim": fim_iso,
                "dias_avaliados": len(dias),
                "pct_cobertura": pct_total,
                "km_total": round(total_m_prev_sum / 1000.0, 2),
                "km_coletado": round(coletado_m_sum / 1000.0, 2),
                "km_omissao": round(omissao_m_sum / 1000.0, 2),
                "km_manual": round(manual_m_sum / 1000.0, 2),
                "vias_manual": vias_manuais,
                "pct_manual": pct_manual,
                "total_vias": len(features),
                "vias_omissao": vias_abaixo_80,
                "placas_ativas": sorted(placas_ativas_periodo),
                "total_pontos_gps": total_pontos_gps_periodo,
                "centro": [c_row["centro_lat"], c_row["centro_lon"]] if c_row else [-8.1930, -34.9364],
                "zoom": c_row["zoom"] if (c_row and "zoom" in c_row.keys() and c_row["zoom"]) else 13.0,
                "status_inlog": "ONLINE (INLOG OFICIAL)" if self.inlog.is_configured() else "OFFLINE",
                "sem_malha_viaria": len(segs_rows) == 0,
                "operacao": operacao,
            },
            "setores": setores_out,
            "planejado_vs_executado": [],
            "geojson": {"type": "FeatureCollection", "features": features},
        }
        try:
            with db._write_lock:
                conn_sv = db.get_connection()
                try:
                    with conn_sv:
                        conn_sv.execute(
                            "INSERT INTO auditorias_periodo_cache (chave, criado_em, payload_json) VALUES (?, ?, ?) ON CONFLICT (chave) DO UPDATE SET criado_em = excluded.criado_em, payload_json = excluded.payload_json",
                            (
                                cache_key,
                                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                json.dumps(resultado_periodo, ensure_ascii=False),
                            ),
                        )
                finally:
                    conn_sv.close()
        except Exception as e:
            print(f"[ENGINE] Aviso ao salvar cache de período: {e}")
        return resultado_periodo
