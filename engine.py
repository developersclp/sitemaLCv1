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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Any, Tuple, Optional, Set

import database as db
import malha_rotas
from inlog_service import InlogService, formatar_placa_hifen, normalizar_ascii


class HeatmapEngine:
    def __init__(self, inlog_service: Optional[InlogService] = None):
        self.inlog = inlog_service or InlogService()

    def _ler_regras_operacionais(self) -> Dict[str, float]:
        """Lê parâmetros operacionais configurados no .env."""
        buffer_m = max(20.0, float(os.getenv("GPS_BUFFER_METROS", "25.0")))
        vel_min = float(os.getenv("VELOCIDADE_MIN_COLETA", "2.0"))
        vel_max = float(os.getenv("VELOCIDADE_MAX_COLETA", "25.0"))
        dwell_s = float(os.getenv("DWELL_TIME_MIN_SEGUNDOS", "180.0"))
        return {
            "buffer_m": buffer_m,
            "vel_min": vel_min,
            "vel_max": vel_max,
            "vel_passagem_max": max(vel_max, 38.0),
            "dwell_s": dwell_s,
        }

    def _coletar_frota_e_gps_cidade(
        self, contrato_id: str, data_iso: str, operacao: str = "COLETA", force_refresh: bool = False
    ) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, List[Dict[str, Any]]]]:
        """Descobre as placas do contrato/operação e busca seus pontos GPS reais em paralelo."""
        veiculos_rt = self.inlog.get_veiculos_tempo_real(
            contrato_id=contrato_id, operacao=operacao, force_refresh=force_refresh
        )
        placas_set: Set[str] = set()
        for v in veiculos_rt:
            pl = formatar_placa_hifen(v.get("placaVeiculo") or v.get("identificadorVeiculo"))
            if pl:
                placas_set.add(pl)

        # Complementa com placas registradas no Relatório Diário daquela cidade/data
        op_norm = normalizar_ascii(operacao)
        try:
            rd_items = self.inlog.get_relatorio_diario(
                data_iso, contrato_id=contrato_id, force_refresh=force_refresh
            )
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
        except Exception as e:
            print(f"[ENGINE] Aviso ao consultar RD para complementar placas: {e}")

        placas = sorted(placas_set)
        gps_por_placa: Dict[str, List[Dict[str, Any]]] = {}

        if placas:
            workers = min(5, len(placas))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futuros = {
                    pool.submit(self.inlog.get_historico_gps, placa, data_iso, force_refresh): placa
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

                if dist <= 300.0 and dt_s <= 240.0 and vel_calc <= vel_pass_max:
                    n_steps = max(1, int(dist // 10.0))
                    for s in range(1, n_steps):
                        frac = s / n_steps
                        add_point(pass_grid, x1 + (x2 - x1) * frac, y1 + (y2 - y1) * frac, placa)
                elif 300.0 < dist <= 1500.0 and 0 < dt_s <= 900.0:
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
    ) -> Dict[str, Any]:
        """Executa a auditoria espacial real da cidade na data informada ou recupera do SQLite."""
        contrato_id = contrato_id.lower().strip()
        operacao_norm = (operacao or "COLETA").upper().strip()
        turno_chave = turno if operacao_norm == "COLETA" else f"{turno}:{operacao_norm}"

        conn = db.get_connection()
        cur = conn.cursor()

        # 1. Garante que a malha viária da cidade esteja importada se disponível
        db.importar_malha_cidade_se_necessario(contrato_id)

        # 2. Obtém dados do contrato e segmentos locais
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

        # Regra padrão de frequência por dia da semana para todas as cidades:
        # Segunda, Quarta e Sexta: só exibe os setores PAR (3ª letra 'P', ex: SCPD, SCPN).
        # Terça, Quinta e Sábado: só exibe os setores ÍMPAR (3ª letra 'I', ex: SCID, SCIN).
        # Domingo: só exibe coletas especiais de domingo (DOM, FEIRA).
        segmentos = []
        for seg in todos_segmentos:
            setor_primario = seg["setor"]
            try:
                setores_seg = json.loads(seg["setores_json"]) if seg["setores_json"] else [setor_primario]
                if not isinstance(setores_seg, list) or not setores_seg:
                    setores_seg = [setor_primario]
            except Exception:
                setores_seg = [setor_primario]

            if any(malha_rotas.setor_visivel_na_data(s, data_iso) for s in setores_seg):
                segmentos.append(seg)

        if not segmentos and todos_segmentos:
            segmentos = todos_segmentos

        # 3. Se já existe auditoria em cache no SQLite (e há malha local e não pediu force), retorna rápido
        if segmentos and not forcar_reprocessamento:
            cur.execute(
                """
                SELECT * FROM auditorias
                WHERE contrato_id = ? AND data_iso = ? AND turno = ?
                """,
                (contrato_id, data_iso, turno_chave),
            )
            audit_existente = cur.fetchone()
            if audit_existente:
                res = self._montar_resposta_geojson(conn, audit_existente["id"], dict(audit_existente))
                conn.close()
                return res

        # 4. Coleta frota real e pontos GPS reais da Inlog em paralelo
        veiculos_rt, placas, gps_por_placa = self._coletar_frota_e_gps_cidade(
            contrato_id, data_iso, operacao=operacao_norm, force_refresh=forcar_reprocessamento
        )
        total_pontos_gps = sum(len(pts) for pts in gps_por_placa.values())
        placas_com_gps = {p: len(pts) for p, pts in gps_por_placa.items() if len(pts) > 0}

        # Caso a cidade não possua malha viária cadastrada no SQLite, retorna visão completa da Inlog sem erro
        if not segmentos:
            conn.close()
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
                matched_dwell, placas_dwell = self._consultar_grade(amostras, dwell_grid, 45.0)
                if matched_dwell >= 1 or (matched_pass >= 1 and 0.0 < ratio < 0.30):
                    st = "possible_manual"
                    p_resp = sorted(placas_dwell | placas_pass)
                    coletado_m += ext
                else:
                    matched_gap, placas_gap = self._consultar_grade(amostras, gap_grid, 30.0)
                    if matched_gap >= 1:
                        st = "possible_gps_gap"
                        p_resp = sorted(placas_gap)
                    else:
                        st = "not_collected"
                        p_resp = []
                        nao_coletado_m += ext

            detalhes_inserir.append((seg["id"], st, json.dumps(p_resp)))

        pct_cobertura = (coletado_m / total_m * 100.0) if total_m > 0 else 0.0
        km_total = round(total_m / 1000.0, 2)
        km_coletado = round(coletado_m / 1000.0, 2)
        km_omissao = round(nao_coletado_m / 1000.0, 2)
        vias_omissao = sum(1 for d in detalhes_inserir if d[1] == "not_collected")

        # 6. Grava auditoria determinística no SQLite com transação atômica
        with db._write_lock:
            with conn:
                cur.execute(
                    "DELETE FROM auditorias WHERE contrato_id = ? AND data_iso = ? AND turno = ?",
                    (contrato_id, data_iso, turno_chave),
                )
                cur.execute(
                    """
                    INSERT INTO auditorias (
                        contrato_id, data_iso, turno, pct_cobertura, km_total, km_coletado,
                        km_omissao, total_vias, vias_omissao, placas_ativas_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        contrato_id,
                        data_iso,
                        turno_chave,
                        round(pct_cobertura, 2),
                        km_total,
                        km_coletado,
                        km_omissao,
                        len(segmentos),
                        vias_omissao,
                        json.dumps(placas),
                    ),
                )
                audit_id = cur.lastrowid
                detalhes_com_id = [(audit_id, seg_id, st, p_resp) for seg_id, st, p_resp in detalhes_inserir]
                cur.executemany(
                    """
                    INSERT INTO auditoria_detalhes (auditoria_id, segmento_id, status, placas_responsaveis)
                    VALUES (?, ?, ?, ?)
                    """,
                    detalhes_com_id,
                )

        cur.execute("SELECT * FROM auditorias WHERE id = ?", (audit_id,))
        audit_row = dict(cur.fetchone())
        res = self._montar_resposta_geojson(
            conn,
            audit_id,
            audit_row,
            extra_resumo={
                "total_pontos_gps": total_pontos_indexados if total_pontos_indexados else total_pontos_gps,
                "placas_com_gps": placas_com_gps,
                "operacao": operacao_norm,
                "sem_malha_viaria": False,
                "regras_auditoria": regras,
            },
        )
        conn.close()
        return res

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
        # Filtra setores conforme a frequência do dia (Regra Par / Ímpar)
        setores_inlog_filtrados = [
            s for s in setores_inlog
            if malha_rotas.setor_visivel_na_data(s.get("apelido") or s.get("descricao") or s.get("identificador"), data_iso)
        ]
        if not setores_inlog_filtrados and setores_inlog:
            setores_inlog_filtrados = setores_inlog

        setores_lista = [
            {
                "codigo": s.get("apelido") or s.get("descricao") or s.get("identificador"),
                "descricao": s.get("descricao"),
                "identificador": s.get("identificador"),
                "macroSetor": s.get("macroSetor"),
                "centro": [0.0, 0.0],
                "extensao_km": 0.0,
                "km_coletado": 0.0,
                "pct_cobertura": 0.0,
                "vias_omissao": 0,
                "total_vias": 0,
                "veiculos": [],
            }
            for s in setores_inlog_filtrados
        ]
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
            "possible_manual": "#ff9900",
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

            # Regra padrão para todas as cidades:
            # Segunda, Quarta e Sexta só exibe setores PAR (3ª letra 'P').
            # Terça, Quinta e Sábado só exibe setores ÍMPAR (3ª letra 'I').
            setores_ativos_dia = [s for s in setores_seg if malha_rotas.setor_visivel_na_data(s, data_iso_audit)]
            if not setores_ativos_dia:
                # Via não opera na data informada
                continue

            setor_exibicao = setores_ativos_dia[0]
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

            setores_lista.append(
                {
                    "codigo": s_cod,
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

        # Conta pontos GPS já cacheados no banco para esta frota/data (rastreabilidade mesmo em leitura de cache)
        total_pontos_gps = 0
        placas_com_gps: Dict[str, int] = {}
        if placas_ativas:
            placeholders = ",".join(["?"] * len(placas_ativas))
            cur.execute(
                f"""
                SELECT placa, COUNT(*) AS qtd
                FROM inlog_historico_gps
                WHERE data_iso = ? AND placa IN ({placeholders})
                GROUP BY placa
                """,
                [audit_row["data_iso"]] + placas_ativas,
            )
            for row_gps in cur.fetchall():
                placas_com_gps[row_gps["placa"]] = row_gps["qtd"]
                total_pontos_gps += row_gps["qtd"]

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
        cur = conn.cursor()

        cur.execute(
            "SELECT a.*, c.nome as cidade_nome FROM auditorias a JOIN contratos c ON c.id = a.contrato_id WHERE a.id = ?",
            (audit_id,),
        )
        audit = cur.fetchone()
        if not audit:
            conn.close()
            return "", "relatorio.csv"

        cur.execute(
            """
            SELECT s.nome_rua, s.setor, s.extensao_m
            FROM segmentos_viarios s
            JOIN auditoria_detalhes d ON d.segmento_id = s.id
            WHERE d.auditoria_id = ? AND d.status = 'not_collected'
            ORDER BY s.setor, s.nome_rua
            """,
            (audit_id,),
        )
        linhas = cur.fetchall()
        conn.close()

        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Data", "Cidade", "Setor", "Logradouro / Rua", "Extensão (Metros)", "Situação", "Ação Recomendada"])

        data_fmt = datetime.strptime(audit["data_iso"], "%Y-%m-%d").strftime("%d/%m/%Y")
        for r in linhas:
            writer.writerow(
                [
                    data_fmt,
                    audit["cidade_nome"],
                    r["setor"],
                    r["nome_rua"],
                    round(r["extensao_m"], 1),
                    "NÃO COLETADA (ÁREA VERMELHA)",
                    "Enviar caminhão de repasse imediato",
                ]
            )

        nome_arquivo = f"omissoes_{audit['contrato_id']}_{audit['data_iso']}.csv"
        return output.getvalue(), nome_arquivo
