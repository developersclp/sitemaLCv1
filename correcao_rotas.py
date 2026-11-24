"""
Módulo de Solicitações de Correção de Rotas - LOCAR Saneamento Ambiental.
Gerencia o recebimento automático dos PDFs + GeoJSON + pedidos gerados
pelas empresas/equipes no Link Compartilhado (ou internamente), bem como
a análise, download de PDF, extração de GeoJSON para QGIS e controle de status
no Painel Central Administrativo.
"""

import base64
import gzip
import json
import re
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import database as db
import malha_rotas

STATUS_VALIDOS = ("PENDENTE", "EM_ANALISE", "APROVADA", "CONCLUIDA", "REJEITADA")


def _agora_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _gerar_id_e_protocolo() -> Tuple[str, str]:
    agora = datetime.now()
    sol_id = f"sc_{agora.strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}"
    protocolo = f"CR-{agora.strftime('%Y%m%d')}-{secrets.token_hex(2).upper()}"
    return sol_id, protocolo


def extrair_geojson_de_pdf_bytes(pdf_bytes: bytes) -> Optional[Dict[str, Any]]:
    """Extrai o objeto GeoJSON embutido (/Type /EmbeddedFile) de um PDF gerado pelo sistema."""
    try:
        txt = pdf_bytes.decode("latin1")
        k = txt.find("/Type /EmbeddedFile")
        if k < 0:
            return None
        m_len = re.search(r"/Length\s+(\d+)", txt[k : k + 350])
        if not m_len:
            return None
        stream_idx = txt.find("stream\n", k)
        if stream_idx >= 0:
            ini = stream_idx + 7
        else:
            stream_idx = txt.find("stream\r\n", k)
            if stream_idx < 0:
                return None
            ini = stream_idx + 8
        tam = int(m_len.group(1))
        raw_geo = pdf_bytes[ini : ini + tam]
        return json.loads(raw_geo.decode("utf-8"))
    except Exception:
        return None


def salvar_solicitacao(dados: Dict[str, Any]) -> Dict[str, Any]:
    """Salva uma nova solicitação de correção de rotas com PDF e GeoJSON comprimidos."""
    solicitante = str(dados.get("solicitante") or "").strip()
    if not solicitante:
        raise ValueError("O nome do solicitante é obrigatório.")

    contrato_id = str(dados.get("contrato_id") or "").lower().strip() or "desconhecido"
    contrato_nome = str(dados.get("contrato_nome") or contrato_id.title()).strip()
    snapshot_id = str(dados.get("snapshot_id") or "").strip() or None
    periodo = str(dados.get("periodo") or "diario").lower().strip()
    data_ref = str(dados.get("data_ref") or "").strip()
    obs_gerais = str(dados.get("observacoes_gerais") or "").strip()

    pedidos = dados.get("pedidos")
    if not isinstance(pedidos, list) or len(pedidos) == 0:
        raise ValueError("Nenhuma alteração informada na solicitação.")

    setores_set = set()
    for p in pedidos:
        if isinstance(p, dict):
            if p.get("setor"):
                setores_set.add(str(p["setor"]).strip())
            if p.get("destino"):
                setores_set.add(str(p["destino"]).strip())
    setores_lista = sorted(setores_set)

    geojson_obj = dados.get("geojson")
    geojson_gz: Optional[bytes] = None
    if isinstance(geojson_obj, dict):
        raw_geo = json.dumps(geojson_obj, ensure_ascii=False, indent=1).encode("utf-8")
        geojson_gz = gzip.compress(raw_geo, compresslevel=6)

    pdf_b64 = str(dados.get("pdf_base64") or "").strip()
    pdf_gz: Optional[bytes] = None
    tamanho_pdf = 0
    if pdf_b64:
        if "," in pdf_b64 and pdf_b64.startswith("data:"):
            pdf_b64 = pdf_b64.split(",", 1)[1]
        pdf_bytes = base64.b64decode(pdf_b64)
        tamanho_pdf = len(pdf_bytes)
        pdf_gz = gzip.compress(pdf_bytes, compresslevel=6)
        if geojson_gz is None:
            geo_ext = extrair_geojson_de_pdf_bytes(pdf_bytes)
            if geo_ext:
                raw_geo = json.dumps(geo_ext, ensure_ascii=False, indent=1).encode("utf-8")
                geojson_gz = gzip.compress(raw_geo, compresslevel=6)

    pdf_filename = str(dados.get("pdf_filename") or "").strip()
    if not pdf_filename:
        safe_c = re.sub(r"[^A-Za-z0-9_-]+", "_", contrato_nome)
        pdf_filename = f"Solicitacao_Correcao_{safe_c}_{datetime.now().strftime('%Y-%m-%d')}.pdf"

    sol_id, protocolo = _gerar_id_e_protocolo()
    agora = _agora_iso()

    with db._write_lock:
        conn = db.get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO solicitacoes_correcao_rotas (
                        id, protocolo, contrato_id, contrato_nome, snapshot_id,
                        periodo, data_ref, solicitante, observacoes_gerais,
                        qtd_alteracoes, qtd_setores, setores_json, pedidos_json,
                        geojson_gzip, pdf_gzip, pdf_filename, tamanho_pdf,
                        status, notas_analise, criado_em, atualizado_em
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDENTE', '', ?, ?)
                    """,
                    (
                        sol_id,
                        protocolo,
                        contrato_id,
                        contrato_nome,
                        snapshot_id,
                        periodo,
                        data_ref,
                        solicitante,
                        obs_gerais,
                        len(pedidos),
                        len(setores_lista),
                        json.dumps(setores_lista, ensure_ascii=False),
                        json.dumps(pedidos, ensure_ascii=False),
                        geojson_gz,
                        pdf_gz,
                        pdf_filename,
                        tamanho_pdf,
                        agora,
                        agora,
                    ),
                )
        finally:
            conn.close()

    return {
        "id": sol_id,
        "protocolo": protocolo,
        "criado_em": agora,
        "qtd_alteracoes": len(pedidos),
        "qtd_setores": len(setores_lista),
        "pdf_filename": pdf_filename,
    }


def importar_pdf_externo(pdf_b64: str, filename: str = "Solicitacao.pdf") -> Dict[str, Any]:
    """Importa um PDF avulso enviado externamente (ex.: WhatsApp) extraindo seu GeoJSON técnico."""
    if not pdf_b64:
        raise ValueError("Arquivo PDF vazio.")
    if "," in pdf_b64 and pdf_b64.startswith("data:"):
        pdf_b64 = pdf_b64.split(",", 1)[1]
    pdf_bytes = base64.b64decode(pdf_b64)
    if not pdf_bytes.startswith(b"%PDF"):
        raise ValueError("O arquivo enviado não é um PDF válido.")

    geo = extrair_geojson_de_pdf_bytes(pdf_bytes)
    meta = (geo or {}).get("metadata") or {}
    contrato_nome = str(meta.get("contrato") or "Contrato Importado").strip()
    contrato_id = contrato_nome.lower().strip()

    # Tenta casar com id oficial do contrato no banco
    conn = db.get_connection()
    try:
        rows_c = conn.execute("SELECT id, nome FROM contratos").fetchall()
        for rc in rows_c:
            if rc["id"].lower() == contrato_id or rc["nome"].lower() == contrato_nome.lower():
                contrato_id = rc["id"]
                contrato_nome = rc["nome"]
                break
    finally:
        conn.close()

    solicitante = str(meta.get("solicitante") or "Importado via PDF").strip()
    obs_gerais = str(meta.get("observacoes_gerais") or "").strip()
    data_ref = str(meta.get("mapa") or "").strip()

    # Reconstrói lista agrupada de pedidos a partir das features do GeoJSON, caso presente
    pedidos_map: Dict[str, Dict[str, Any]] = {}
    if geo and isinstance(geo.get("features"), list):
        for feat in geo["features"]:
            pr = (feat or {}).get("properties") or {}
            pid = str(pr.get("pedido_id") or f"imp_{len(pedidos_map)+1}")
            if pid not in pedidos_map:
                pedidos_map[pid] = {
                    "id": pid,
                    "tipo": pr.get("acao") or "manual",
                    "acao_nome": pr.get("acao_nome") or pr.get("acao") or "Alteração",
                    "setor": pr.get("setor") or "S/S",
                    "destino": pr.get("setor_destino"),
                    "de": pr.get("interdicao_de"),
                    "ate": pr.get("interdicao_ate"),
                    "obs": pr.get("observacao") or "",
                    "ruas_nomes": [],
                    "trechos_qtd": 0,
                    "extensao_m": 0.0,
                }
            rua_n = str(pr.get("rua") or "Rua sem nome").strip()
            if rua_n and rua_n not in pedidos_map[pid]["ruas_nomes"]:
                pedidos_map[pid]["ruas_nomes"].append(rua_n)
            pedidos_map[pid]["trechos_qtd"] += 1
            pedidos_map[pid]["extensao_m"] = round(
                pedidos_map[pid]["extensao_m"] + float(pr.get("len_m") or 0.0), 1
            )

    pedidos_lista = list(pedidos_map.values()) if pedidos_map else [
        {
            "id": "pdf_ext_1",
            "tipo": "documento",
            "acao_nome": "PDF Importado",
            "setor": "Geral",
            "obs": f"Arquivo importado manualmente ({filename})",
            "ruas_nomes": [],
            "trechos_qtd": 1,
            "extensao_m": 0.0,
        }
    ]

    return salvar_solicitacao({
        "contrato_id": contrato_id,
        "contrato_nome": contrato_nome,
        "periodo": "importado",
        "data_ref": data_ref,
        "solicitante": solicitante,
        "observacoes_gerais": obs_gerais,
        "pedidos": pedidos_lista,
        "geojson": geo,
        "pdf_base64": base64.b64encode(pdf_bytes).decode("ascii"),
        "pdf_filename": filename or "Solicitacao_Importada.pdf",
    })


def listar_solicitacoes(
    contrato_id: Optional[str] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """Lista solicitações recebidas (sem trafegar os binários pesados) + contadores."""
    conn = db.get_connection()
    try:
        wheres: List[str] = []
        params: List[Any] = []
        if contrato_id and contrato_id.lower() not in ("all", "todas", ""):
            wheres.append("contrato_id = ?")
            params.append(contrato_id.lower().strip())
        if status and status.upper() not in ("ALL", "TODAS", ""):
            wheres.append("status = ?")
            params.append(status.upper().strip())

        sql_where = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        rows = conn.execute(
            f"""
            SELECT id, protocolo, contrato_id, contrato_nome, snapshot_id,
                   periodo, data_ref, solicitante, observacoes_gerais,
                   qtd_alteracoes, qtd_setores, setores_json,
                   pdf_filename, tamanho_pdf,
                   (CASE WHEN pdf_gzip IS NOT NULL THEN 1 ELSE 0 END) AS tem_pdf,
                   (CASE WHEN geojson_gzip IS NOT NULL THEN 1 ELSE 0 END) AS tem_geojson,
                   status, notas_analise, criado_em, atualizado_em
            FROM solicitacoes_correcao_rotas
            {sql_where}
            ORDER BY criado_em DESC
            LIMIT 300
            """,
            tuple(params),
        ).fetchall()

        row_pend = conn.execute(
            "SELECT COUNT(*) AS cnt FROM solicitacoes_correcao_rotas WHERE status = 'PENDENTE'"
        ).fetchone()
        pendentes_total = int(row_pend["cnt"] if row_pend else 0)
    finally:
        conn.close()

    itens: List[Dict[str, Any]] = []
    for r in rows:
        try:
            setores = json.loads(r["setores_json"] or "[]")
        except Exception:
            setores = []
        itens.append({
            "id": r["id"],
            "protocolo": r["protocolo"],
            "contrato_id": r["contrato_id"],
            "contrato_nome": r["contrato_nome"],
            "snapshot_id": r["snapshot_id"],
            "periodo": r["periodo"],
            "data_ref": r["data_ref"],
            "solicitante": r["solicitante"],
            "observacoes_gerais": r["observacoes_gerais"] or "",
            "qtd_alteracoes": int(r["qtd_alteracoes"] or 0),
            "qtd_setores": int(r["qtd_setores"] or 0),
            "setores": setores,
            "pdf_filename": r["pdf_filename"] or "Solicitacao.pdf",
            "tamanho_kb": round(int(r["tamanho_pdf"] or 0) / 1024.0, 1),
            "tem_pdf": bool(r["tem_pdf"]),
            "tem_geojson": bool(r["tem_geojson"]),
            "status": r["status"],
            "notas_analise": r["notas_analise"] or "",
            "criado_em": r["criado_em"],
            "atualizado_em": r["atualizado_em"],
        })

    return {
        "pendentes_total": pendentes_total,
        "total": len(itens),
        "solicitacoes": itens,
    }


def obter_detalhe_solicitacao(sol_id: str) -> Optional[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        r = conn.execute(
            """
            SELECT id, protocolo, contrato_id, contrato_nome, snapshot_id,
                   periodo, data_ref, solicitante, observacoes_gerais,
                   qtd_alteracoes, qtd_setores, setores_json, pedidos_json,
                   geojson_gzip, pdf_filename, tamanho_pdf,
                   (CASE WHEN pdf_gzip IS NOT NULL THEN 1 ELSE 0 END) AS tem_pdf,
                   status, notas_analise, criado_em, atualizado_em
            FROM solicitacoes_correcao_rotas
            WHERE id = ?
            """,
            (sol_id,),
        ).fetchone()
    finally:
        conn.close()

    if not r:
        return None

    try:
        setores = json.loads(r["setores_json"] or "[]")
    except Exception:
        setores = []
    try:
        pedidos = json.loads(r["pedidos_json"] or "[]")
    except Exception:
        pedidos = []

    geojson_obj = None
    if r["geojson_gzip"]:
        try:
            raw_geo = gzip.decompress(bytes(r["geojson_gzip"]))
            geojson_obj = json.loads(raw_geo.decode("utf-8"))
        except Exception:
            geojson_obj = None

    return {
        "id": r["id"],
        "protocolo": r["protocolo"],
        "contrato_id": r["contrato_id"],
        "contrato_nome": r["contrato_nome"],
        "snapshot_id": r["snapshot_id"],
        "periodo": r["periodo"],
        "data_ref": r["data_ref"],
        "solicitante": r["solicitante"],
        "observacoes_gerais": r["observacoes_gerais"] or "",
        "qtd_alteracoes": int(r["qtd_alteracoes"] or 0),
        "qtd_setores": int(r["qtd_setores"] or 0),
        "setores": setores,
        "pedidos": pedidos,
        "geojson": geojson_obj,
        "pdf_filename": r["pdf_filename"] or "Solicitacao.pdf",
        "tamanho_kb": round(int(r["tamanho_pdf"] or 0) / 1024.0, 1),
        "tem_pdf": bool(r["tem_pdf"]),
        "tem_geojson": geojson_obj is not None,
        "status": r["status"],
        "notas_analise": r["notas_analise"] or "",
        "criado_em": r["criado_em"],
        "atualizado_em": r["atualizado_em"],
    }


def obter_pdf_solicitacao(sol_id: str) -> Optional[Tuple[bytes, str]]:
    conn = db.get_connection()
    try:
        r = conn.execute(
            "SELECT pdf_gzip, pdf_filename FROM solicitacoes_correcao_rotas WHERE id = ?",
            (sol_id,),
        ).fetchone()
    finally:
        conn.close()

    if not r or not r["pdf_gzip"]:
        return None
    pdf_bytes = gzip.decompress(bytes(r["pdf_gzip"]))
    filename = str(r["pdf_filename"] or f"{sol_id}.pdf")
    return pdf_bytes, filename


def obter_geojson_solicitacao(sol_id: str) -> Optional[Tuple[bytes, str]]:
    conn = db.get_connection()
    try:
        r = conn.execute(
            "SELECT geojson_gzip, pdf_gzip, pdf_filename FROM solicitacoes_correcao_rotas WHERE id = ?",
            (sol_id,),
        ).fetchone()
    finally:
        conn.close()

    if not r:
        return None
    base_name = re.sub(r"\.pdf$", "", str(r["pdf_filename"] or sol_id), flags=re.IGNORECASE) + ".geojson"
    if r["geojson_gzip"]:
        return gzip.decompress(bytes(r["geojson_gzip"])), base_name
    if r["pdf_gzip"]:
        pdf_bytes = gzip.decompress(bytes(r["pdf_gzip"]))
        geo = extrair_geojson_de_pdf_bytes(pdf_bytes)
        if geo:
            return json.dumps(geo, ensure_ascii=False, indent=1).encode("utf-8"), base_name
    return None


def atualizar_status_solicitacao(sol_id: str, status: str, notas_analise: Optional[str] = None) -> bool:
    st_norm = str(status or "").upper().strip()
    if st_norm not in STATUS_VALIDOS:
        raise ValueError(f"Status inválido: {status}")

    agora = _agora_iso()
    with db._write_lock:
        conn = db.get_connection()
        try:
            with conn:
                if notas_analise is not None:
                    cur = conn.execute(
                        "UPDATE solicitacoes_correcao_rotas SET status = ?, notas_analise = ?, atualizado_em = ? WHERE id = ?",
                        (st_norm, str(notas_analise).strip(), agora, sol_id),
                    )
                else:
                    cur = conn.execute(
                        "UPDATE solicitacoes_correcao_rotas SET status = ?, atualizado_em = ? WHERE id = ?",
                        (st_norm, agora, sol_id),
                    )
                return (cur.rowcount or 0) > 0
        finally:
            conn.close()


def excluir_solicitacao(sol_id: str) -> bool:
    with db._write_lock:
        conn = db.get_connection()
        try:
            with conn:
                cur = conn.execute("DELETE FROM solicitacoes_correcao_rotas WHERE id = ?", (sol_id,))
                return (cur.rowcount or 0) > 0
        finally:
            conn.close()


def listar_setores_contrato(contrato_id: str) -> List[Dict[str, str]]:
    """Retorna todos os setores cadastrados no projeto da cidade (inclusive de outros dias da semana)."""
    cid = str(contrato_id or "").lower().strip()
    if not cid or cid in ("all", "todas"):
        return []
    db.importar_malha_cidade_se_necessario(cid)
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT setor, setores_json FROM segmentos_viarios WHERE contrato_id = ?",
            (cid,),
        ).fetchall()
    finally:
        conn.close()

    codigos = set()
    for r in rows:
        if r["setor"]:
            codigos.add(str(r["setor"]).strip())
        if r["setores_json"]:
            try:
                lst = json.loads(r["setores_json"])
                if isinstance(lst, list):
                    for item in lst:
                        if item and str(item).strip():
                            codigos.add(str(item).strip())
            except Exception:
                pass

    codigos.discard("")
    codigos.discard("S/S")
    saida: List[Dict[str, str]] = []
    for cod in sorted(codigos):
        nom = malha_rotas.analisar_nomenclatura_setor(cod, contrato_id=cid)
        saida.append({
            "codigo": cod,
            "agenda": nom.get("agenda_rotulo") or "",
            "turno": "Noturno" if nom.get("turno") == "NOTURNO" else "Diurno",
        })
    return saida
