"""
Módulo de Snapshots Congelados para Links Compartilháveis (snapshots.py)
Desenvolvido para sitemaLCv1 - Locar Saneamento Ambiental.

Permite gerar um snapshot pré-processado e pré-comprimido (GZIP) no momento em que
o link é criado, eliminando chamadas à API Inlog e reprocessamento espacial no
acesso do visitante (/v/<id> -> /api/snapshot/<id>).
"""

import calendar
import csv
import gzip
import hashlib
import io
import json
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import database as db
from inlog_service import janela_operacional_cidade


def normalizar_parametros(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normaliza e padroniza os parâmetros de um link de visualização."""
    cidade = str(payload.get("cidade") or "cabo").lower().strip()
    data_iso = str(payload.get("data") or "").strip()[:10]
    if not data_iso:
        data_iso = datetime.now().strftime("%Y-%m-%d")

    periodo = str(payload.get("periodo") or "diario").lower().strip()
    if periodo not in ("diario", "quinzena1", "quinzena2", "mensal"):
        periodo = "diario"

    turno = str(payload.get("turno") or "all").strip() or "all"
    coleta = str(payload.get("coleta") or "all").strip() or "all"
    setores_raw = payload.get("setores") or []
    if not isinstance(setores_raw, list):
        setores_raw = []
    setores = sorted({str(s).strip() for s in setores_raw if str(s).strip()})

    return {
        "cidade": cidade,
        "data": data_iso,
        "periodo": periodo,
        "turno": turno,
        "coleta": coleta,
        "setores": setores,
    }


def calcular_chave_params(params: Dict[str, Any]) -> str:
    """Calcula chave determinística para identificar a mesma combinação de filtros."""
    raw = json.dumps(params, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def janela_esta_fechada(contrato_id: str, data_iso: str, periodo: str) -> bool:
    """Verifica se a janela operacional da data/período já encerrou."""
    try:
        dt_ref = datetime.strptime(str(data_iso)[:10], "%Y-%m-%d")
    except Exception:
        return False

    if periodo == "diario":
        data_fim_ref = dt_ref.strftime("%Y-%m-%d")
    else:
        ano, mes = dt_ref.year, dt_ref.month
        ultimo_dia = calendar.monthrange(ano, mes)[1]
        dia_fim = 15 if periodo == "quinzena1" else ultimo_dia
        data_fim_ref = f"{ano:04d}-{mes:02d}-{dia_fim:02d}"

    win = janela_operacional_cidade(contrato_id, data_fim_ref)
    agora_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    return agora_str >= str(win.get("data_fim") or "")


def _arredondar_coords(coords: Any, casas: int = 5) -> Any:
    """Arredonda coordenadas GeoJSON para 5 casas decimais (~1.1m de precisão)."""
    if not isinstance(coords, list):
        return coords
    if coords and isinstance(coords[0], (int, float)):
        return [round(float(c), casas) for c in coords]
    return [_arredondar_coords(item, casas) for item in coords]


def _otimizar_payload_snapshot(resultado: Dict[str, Any], meta_snap: Dict[str, Any]) -> Dict[str, Any]:
    """Enxuga campos não utilizados pelo viewer mantendo 100% de fidelidade visual e KPIs."""
    resumo = dict(resultado.get("resumo") or {})
    resumo["snapshot"] = meta_snap

    geojson_orig = resultado.get("geojson") or {}
    features_orig = geojson_orig.get("features") or []
    features_otim: List[Dict[str, Any]] = []

    for f in features_orig:
        geom = f.get("geometry") or {}
        props = dict(f.get("properties") or {})
        for k_drop in ("edge_key", "fid", "color"):
            props.pop(k_drop, None)
        features_otim.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": geom.get("type", "LineString"),
                    "coordinates": _arredondar_coords(geom.get("coordinates") or [], 5),
                },
                "properties": props,
            }
        )

    return {
        "resumo": resumo,
        "setores": resultado.get("setores") or [],
        "planejado_vs_executado": resultado.get("planejado_vs_executado") or [],
        "geojson": {
            "type": "FeatureCollection",
            "features": features_otim,
        },
    }


def _gerar_csv_from_features(resultado: Dict[str, Any], params: Dict[str, Any]) -> Tuple[str, str]:
    """Gera CSV de omissões/vias diretamente a partir do snapshot para períodos ou sem audit_id."""
    resumo = resultado.get("resumo") or {}
    cidade = params.get("cidade") or resumo.get("contrato_id") or "cidade"
    data_iso = params.get("data") or resumo.get("data_iso") or ""
    periodo = params.get("periodo") or "diario"

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(["CIDADE", "ID_VIA", "NOME_RUA", "BAIRRO", "SETOR", "STATUS_COLETA", "EXTENSAO_METROS", "VEICULOS_RESPONSAVEIS"])

    for f in (resultado.get("geojson") or {}).get("features") or []:
        p = f.get("properties") or {}
        writer.writerow([
            resumo.get("contrato_nome") or cidade.upper(),
            p.get("id") or "",
            p.get("street_name") or "",
            p.get("bairro") or "",
            p.get("sector") or "",
            p.get("status") or "",
            f"{float(p.get('len_m') or 0.0):.2f}",
            ";".join(p.get("vehicles") or []),
        ])

    filename = f"auditoria_{cidade}_{periodo}_{data_iso}.csv".replace(" ", "_")
    return "\ufeff" + buf.getvalue(), filename


def gerar_ou_atualizar_snapshot(
    engine: Any,
    payload: Dict[str, Any],
    forcar_recriar: bool = False,
) -> Dict[str, Any]:
    """
    Gera ou reutiliza um snapshot congelado para os parâmetros informados.
    - Se a janela operacional já fechou e já existe snapshot ativo, reutiliza-o instantaneamente
      (a menos que `forcar_recriar=True`).
    - Se o dia/período ainda está em andamento e o link for gerado novamente, recria o snapshot
      atualizando o mesmo ID com os dados mais recentes.
    - Validade padrão: sem expiração (`expira_em = NULL`).
    """
    params = normalizar_parametros(payload)
    chave_params = calcular_chave_params(params)
    cidade = params["cidade"]
    data_iso = params["data"]
    periodo = params["periodo"]
    fechada = janela_esta_fechada(cidade, data_iso, periodo)

    conn = db.get_connection()
    try:
        existente = conn.execute(
            """
            SELECT id, sha256, tamanho_raw, tamanho_gzip, janela_fechada, criado_em, atualizado_em, acessos
            FROM links_snapshot
            WHERE chave_params = ? AND revogado = 0
              AND (expira_em IS NULL OR expira_em > ?)
            ORDER BY atualizado_em DESC
            LIMIT 1
            """,
            (chave_params, datetime.now().strftime("%Y-%m-%dT%H:%M:%S")),
        ).fetchone()
    finally:
        conn.close()

    if existente and not forcar_recriar and fechada and int(existente["janela_fechada"] or 0) == 1:
        return {
            "id": existente["id"],
            "path": f"/v/{existente['id']}",
            "params": params,
            "sha256": existente["sha256"],
            "tamanho_raw": existente["tamanho_raw"],
            "tamanho_gzip": existente["tamanho_gzip"],
            "janela_fechada": True,
            "criado_em": existente["criado_em"],
            "atualizado_em": existente["atualizado_em"],
            "reutilizado": True,
            "recriado": False,
        }

    # Reaproveita o mesmo ID se o dia estava em andamento e está sendo gerado novamente
    snap_id = existente["id"] if existente else secrets.token_hex(12)
    agora_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    criado_em = existente["criado_em"] if existente else agora_iso

    # Se já existia snapshot em andamento e foi gerado novamente, força reprocessamento para trazer GPS novo
    forcar_engine = bool(forcar_recriar or (existente is not None and not fechada))

    if periodo in ("quinzena1", "quinzena2", "mensal"):
        resultado = engine.processar_auditoria_periodo(
            cidade, data_iso, periodo=periodo, operacao="COLETA"
        )
    else:
        resultado = engine.processar_ou_obter_auditoria(
            cidade, data_iso, forcar_reprocessamento=forcar_engine, operacao="COLETA"
        )

    audit_id = (resultado.get("resumo") or {}).get("id")
    if audit_id:
        try:
            csv_str, csv_filename = engine.gerar_csv_omissoes_string(int(audit_id))
        except Exception:
            csv_str, csv_filename = _gerar_csv_from_features(resultado, params)
    else:
        csv_str, csv_filename = _gerar_csv_from_features(resultado, params)
    csv_gzip = gzip.compress(csv_str.encode("utf-8-sig"), compresslevel=6)

    meta_snap = {
        "id": snap_id,
        "congelado_em": agora_iso,
        "janela_fechada": bool(fechada),
        "recriado": bool(existente is not None),
    }
    payload_otim = _otimizar_payload_snapshot(resultado, meta_snap)
    raw_bytes = json.dumps(payload_otim, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sha256_hex = hashlib.sha256(raw_bytes).hexdigest()
    payload_gzip = gzip.compress(raw_bytes, compresslevel=6)
    parametros_json = json.dumps(params, ensure_ascii=False, separators=(",", ":"))

    with db._write_lock:
        conn_w = db.get_connection()
        try:
            with conn_w:
                conn_w.execute(
                    """
                    INSERT INTO links_snapshot (
                        id, chave_params, contrato_id, data_iso, periodo, parametros_json,
                        payload_gzip, csv_gzip, csv_filename, sha256,
                        tamanho_raw, tamanho_gzip, janela_fechada,
                        criado_em, atualizado_em, expira_em, revogado, acessos
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 0)
                    ON CONFLICT(id) DO UPDATE SET
                        payload_gzip = excluded.payload_gzip,
                        csv_gzip = excluded.csv_gzip,
                        csv_filename = excluded.csv_filename,
                        sha256 = excluded.sha256,
                        tamanho_raw = excluded.tamanho_raw,
                        tamanho_gzip = excluded.tamanho_gzip,
                        janela_fechada = excluded.janela_fechada,
                        atualizado_em = excluded.atualizado_em
                    """,
                    (
                        snap_id,
                        chave_params,
                        cidade,
                        data_iso,
                        periodo,
                        parametros_json,
                        payload_gzip,
                        csv_gzip,
                        csv_filename,
                        sha256_hex,
                        len(raw_bytes),
                        len(payload_gzip),
                        1 if fechada else 0,
                        criado_em,
                        agora_iso,
                    ),
                )
        finally:
            conn_w.close()

    return {
        "id": snap_id,
        "path": f"/v/{snap_id}",
        "params": params,
        "sha256": sha256_hex,
        "tamanho_raw": len(raw_bytes),
        "tamanho_gzip": len(payload_gzip),
        "janela_fechada": bool(fechada),
        "criado_em": criado_em,
        "atualizado_em": agora_iso,
        "reutilizado": False,
        "recriado": bool(existente is not None),
    }


def obter_snapshot_meta(snapshot_id: str) -> Optional[Dict[str, Any]]:
    """Retorna metadados do snapshot (sem carregar os blobs) se válido e não revogado."""
    if not snapshot_id or "." in snapshot_id:
        return None
    conn = db.get_connection()
    try:
        row = conn.execute(
            """
            SELECT id, contrato_id, data_iso, periodo, parametros_json, sha256,
                   tamanho_raw, tamanho_gzip, janela_fechada, criado_em, atualizado_em,
                   expira_em, revogado, acessos
            FROM links_snapshot
            WHERE id = ?
            """,
            (snapshot_id.strip(),),
        ).fetchone()
    finally:
        conn.close()

    if not row or int(row["revogado"] or 0) != 0:
        return None
    if row["expira_em"] and row["expira_em"] <= datetime.now().strftime("%Y-%m-%dT%H:%M:%S"):
        return None

    try:
        params = json.loads(row["parametros_json"] or "{}")
    except Exception:
        params = {}

    return {
        "id": row["id"],
        "contrato_id": row["contrato_id"],
        "data_iso": row["data_iso"],
        "periodo": row["periodo"],
        "params": params,
        "sha256": row["sha256"],
        "tamanho_raw": row["tamanho_raw"],
        "tamanho_gzip": row["tamanho_gzip"],
        "janela_fechada": bool(row["janela_fechada"]),
        "criado_em": row["criado_em"],
        "atualizado_em": row["atualizado_em"],
    }


def obter_snapshot_payload(snapshot_id: str) -> Optional[Dict[str, Any]]:
    """Retorna o blob gzip pronto do snapshot e contabiliza o acesso."""
    if not snapshot_id or "." in snapshot_id:
        return None
    agora = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn = db.get_connection()
    try:
        row = conn.execute(
            """
            SELECT id, payload_gzip, sha256, atualizado_em, expira_em, revogado
            FROM links_snapshot
            WHERE id = ?
            """,
            (snapshot_id.strip(),),
        ).fetchone()
        if not row or int(row["revogado"] or 0) != 0:
            return None
        if row["expira_em"] and row["expira_em"] <= agora:
            return None
    finally:
        conn.close()

    try:
        with db._write_lock:
            conn_w = db.get_connection()
            try:
                with conn_w:
                    conn_w.execute(
                        "UPDATE links_snapshot SET acessos = COALESCE(acessos, 0) + 1, ultimo_acesso = ? WHERE id = ?",
                        (agora, snapshot_id.strip()),
                    )
            finally:
                conn_w.close()
    except Exception:
        pass

    return {
        "id": row["id"],
        "payload_gzip": bytes(row["payload_gzip"]),
        "sha256": row["sha256"],
        "atualizado_em": row["atualizado_em"],
    }


def obter_snapshot_csv(snapshot_id: str) -> Optional[Tuple[bytes, str]]:
    """Retorna (csv_gzip, csv_filename) de um snapshot válido."""
    if not snapshot_id or "." in snapshot_id:
        return None
    conn = db.get_connection()
    try:
        row = conn.execute(
            """
            SELECT csv_gzip, csv_filename, expira_em, revogado
            FROM links_snapshot
            WHERE id = ?
            """,
            (snapshot_id.strip(),),
        ).fetchone()
    finally:
        conn.close()

    if not row or int(row["revogado"] or 0) != 0 or not row["csv_gzip"]:
        return None
    if row["expira_em"] and row["expira_em"] <= datetime.now().strftime("%Y-%m-%dT%H:%M:%S"):
        return None
    return bytes(row["csv_gzip"]), str(row["csv_filename"] or f"auditoria_{snapshot_id}.csv")


def listar_snapshots(contrato_id: Optional[str] = None, limite: int = 40) -> List[Dict[str, Any]]:
    """Lista snapshots gerados recentemente para gerenciamento no painel admin."""
    conn = db.get_connection()
    try:
        if contrato_id and contrato_id not in ("all", "todas"):
            rows = conn.execute(
                """
                SELECT id, contrato_id, data_iso, periodo, parametros_json, tamanho_gzip,
                       janela_fechada, criado_em, atualizado_em, revogado, acessos, ultimo_acesso
                FROM links_snapshot
                WHERE contrato_id = ? AND revogado = 0
                ORDER BY atualizado_em DESC
                LIMIT ?
                """,
                (contrato_id.lower().strip(), int(limite)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, contrato_id, data_iso, periodo, parametros_json, tamanho_gzip,
                       janela_fechada, criado_em, atualizado_em, revogado, acessos, ultimo_acesso
                FROM links_snapshot
                WHERE revogado = 0
                ORDER BY atualizado_em DESC
                LIMIT ?
                """,
                (int(limite),),
            ).fetchall()
    finally:
        conn.close()

    saida: List[Dict[str, Any]] = []
    for r in rows:
        try:
            params = json.loads(r["parametros_json"] or "{}")
        except Exception:
            params = {}
        saida.append(
            {
                "id": r["id"],
                "path": f"/v/{r['id']}",
                "contrato_id": r["contrato_id"],
                "data_iso": r["data_iso"],
                "periodo": r["periodo"],
                "turno": params.get("turno", "all"),
                "coleta": params.get("coleta", "all"),
                "setores": params.get("setores", []),
                "tamanho_kb": round((r["tamanho_gzip"] or 0) / 1024.0, 1),
                "janela_fechada": bool(r["janela_fechada"]),
                "criado_em": r["criado_em"],
                "atualizado_em": r["atualizado_em"],
                "acessos": int(r["acessos"] or 0),
                "ultimo_acesso": r["ultimo_acesso"],
            }
        )
    return saida


def revogar_snapshot(snapshot_id: str) -> bool:
    """Marca um snapshot como revogado."""
    if not snapshot_id:
        return False
    with db._write_lock:
        conn = db.get_connection()
        try:
            with conn:
                cur = conn.execute("UPDATE links_snapshot SET revogado = 1 WHERE id = ?", (snapshot_id.strip(),))
                return cur.rowcount > 0
        finally:
            conn.close()
