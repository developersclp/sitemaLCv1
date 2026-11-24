"""
Módulo de Painel de Indicadores e Exportação Excel (.xlsx) (indicadores.py)
Desenvolvido para sitemaLCv1 - Locar Saneamento Ambiental.

Gera o relatório oficial "Indicadores - Mapa de Execução.xlsx" com:
  - Uma aba por cidade (ordenadas alfabeticamente pelo nome do contrato)
  - Exatamente as 36 colunas oficiais na ordem exata do sistema legado
  - Formatação visual idêntica (cabeçalho verde #1F5C3A negrito branco centralizado,
    datas nativas Excel numFmtId=14, congelamento na linha 1, AutoFilter e larguras ajustadas)
  - Suporte a extração por Dia Único ou por Mês (com corte automático até hoje se o mês
    estiver em andamento)
  - Execução assíncrona em background com progresso em tempo real e resumo consolidado
"""

import calendar
import io
import json
import math
import re
import secrets
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import database as db
import malha_rotas
from inlog_service import janela_operacional_cidade, normalizar_ascii


COLUNAS_INDICADORES: List[str] = [
    "data_inicial",
    "data_final",
    "qtd_dias",
    "gerado_em",
    "data",
    "sem_setores_agendados",
    "contrato",
    "pct_coleta_contrato",
    "pct_coleta_contrato_com_falha_gps",
    "qtd_trechos_falha_gps_contrato",
    "metros_falha_gps_contrato",
    "pct_falha_gps_contrato",
    "qtd_trechos_possivel_manual_contrato",
    "metros_possivel_manual_contrato",
    "pct_possivel_manual_contrato",
    "qtd_setores",
    "setor",
    "pct_coleta_setor",
    "metros_previstos_setor",
    "metros_coletados_setor",
    "qtd_trechos_falha_gps_setor",
    "metros_falha_gps_setor",
    "pct_falha_gps_setor",
    "qtd_trechos_possivel_manual_setor",
    "metros_possivel_manual_setor",
    "pct_possivel_manual_setor",
    "placa_principal",
    "pct_coleta_principal",
    "metros_coletados_principal",
    "qtd_veiculos_apoio",
    "pct_coleta_apoio_soma",
    "placa_apoio",
    "pct_coleta_apoio",
    "metros_coletados_apoio",
    "pct_coleta_sem_veiculo",
    "metros_coletados_sem_veiculo",
]

LARGURAS_COLUNAS: List[int] = [
    14, 12, 11, 20, 12, 23, 15, 21, 34, 32, 27, 24,
    34, 33, 30, 13, 14, 18, 24, 24, 29, 24, 21, 34,
    30, 27, 17, 22, 28, 20, 23, 14, 18, 24, 24, 30,
]

# Índices (1-based) das colunas de data no Excel (A, B, E)
_COLUNAS_DATA_IDX = (1, 2, 5)
# numFmtId=14 no Excel (formato de data curta respeitando localidade pt-BR -> dd/mm/aaaa)
_DATE_NUM_FMT = "mm-dd-yy"

_HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFFFF")
_HEADER_FILL = PatternFill(fill_type="solid", start_color="FF1F5C3A", end_color="FF1F5C3A")
_HEADER_ALIGN = Alignment(horizontal="center")

_JOBS_LOCK = threading.Lock()
_JOBS: Dict[str, Dict[str, Any]] = {}
_JOB_TTL_SECONDS = 3600  # Mantém arquivos gerados em memória por 1 hora

_RE_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_PLACAS_SIMULADAS_FALLBACK = {"LOC-1010", "LOC-2020"}


def _sanitizar_texto_excel(valor: Any) -> Optional[str]:
    """Remove caracteres de controle ilegais em XML 1.0 para evitar corrupção do XLSX."""
    if valor is None:
        return None
    return _RE_XML_INVALID.sub("", str(valor))


def _sanitizar_nome_aba(nome: str) -> str:
    """Garante nome de aba válido no Excel (máx. 31 caracteres, sem caracteres proibidos)."""
    limpo = _sanitizar_texto_excel(nome) or "Cidade"
    limpo = re.sub(r"[:\\/?*\[\]]", " ", limpo).strip()
    limpo = re.sub(r"\s+", " ", limpo)
    return (limpo or "Cidade")[:31]


def _garantir_nome_aba_unico(nome: str, usados_lower: Set[str]) -> str:
    """Garante nome de aba único (case-insensitive) dentro do limite de 31 caracteres do Excel."""
    base = _sanitizar_nome_aba(nome)
    cand = base
    suf_idx = 2
    while cand.lower() in usados_lower:
        suf = f" ({suf_idx})"
        cand = f"{base[:max(1, 31 - len(suf))]}{suf}"
        suf_idx += 1
    usados_lower.add(cand.lower())
    return cand


def _chave_ordenacao_setor(setor_nome: str) -> Tuple[int, str, str]:
    """Ordena setores pelo número principal e depois pelo código (padrão do relatório oficial)."""
    cod = malha_rotas.extrair_codigo_setor(setor_nome)
    if cod is not None:
        pref, num = cod
        return (int(num), pref, str(setor_nome))
    m = re.search(r"(\d+)", str(setor_nome or ""))
    num = int(m.group(1)) if m else 999999
    return (num, str(setor_nome or ""), str(setor_nome or ""))


def resolver_periodo_indicadores(
    modo: str,
    data_str: Optional[str] = None,
    mes_str: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Resolve o intervalo [data_inicial, data_final] e a lista de datas ISO.
      - modo='dia': apenas o dia informado (data_str='YYYY-MM-DD')
      - modo='mes': mês informado (mes_str='YYYY-MM'); se o mês ainda não terminou,
                    corta automaticamente até a data de hoje.
    """
    modo_norm = str(modo or "dia").lower().strip()
    hoje = datetime.now().date()

    if modo_norm == "mes":
        raw_mes = str(mes_str or "").strip()
        if not raw_mes and data_str:
            raw_mes = str(data_str).strip()[:7]
        m = re.match(r"^(\d{4})-(\d{2})", raw_mes)
        if not m:
            raise ValueError("Informe um mês válido no formato AAAA-MM.")
        ano, mes = int(m.group(1)), int(m.group(2))
        if mes < 1 or mes > 12:
            raise ValueError("Mês inválido.")
        dt_ini = date(ano, mes, 1)
        if dt_ini > hoje:
            raise ValueError("O mês selecionado ainda não iniciou.")
        ultimo_dia = calendar.monthrange(ano, mes)[1]
        dt_fim_teorico = date(ano, mes, ultimo_dia)
        dt_fim = min(dt_fim_teorico, hoje)
        mes_em_andamento = dt_fim < dt_fim_teorico
        rotulo_arquivo = f"{ano:04d}-{mes:02d}"
        if mes_em_andamento:
            rotulo_arquivo = f"{ano:04d}-{mes:02d}_ate_{dt_fim.strftime('%d-%m-%Y')}"
    else:
        modo_norm = "dia"
        raw_data = str(data_str or "").strip()[:10]
        try:
            dt_ini = datetime.strptime(raw_data, "%Y-%m-%d").date()
        except Exception:
            raise ValueError("Informe uma data válida no formato AAAA-MM-DD.")
        if dt_ini > hoje:
            raise ValueError("A data selecionada é futura e ainda não possui operação.")
        dt_fim = dt_ini
        mes_em_andamento = False
        rotulo_arquivo = dt_ini.strftime("%Y-%m-%d")

    dias_iso: List[str] = []
    cur = dt_ini
    while cur <= dt_fim:
        dias_iso.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)

    return {
        "modo": modo_norm,
        "data_inicial": dt_ini,
        "data_final": dt_fim,
        "data_inicial_iso": dt_ini.strftime("%Y-%m-%d"),
        "data_final_iso": dt_fim.strftime("%Y-%m-%d"),
        "qtd_dias": len(dias_iso),
        "dias_iso": dias_iso,
        "mes_em_andamento": mes_em_andamento,
        "rotulo_arquivo": rotulo_arquivo,
    }


def listar_contratos_indicadores(cidades_filtro: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Retorna os contratos ordenados alfabeticamente pelo nome oficial da cidade."""
    conn = db.get_connection()
    try:
        rows = conn.execute("SELECT id, nome, centro_lat, centro_lon FROM contratos ORDER BY nome").fetchall()
    finally:
        conn.close()

    filtro_norm = None
    if cidades_filtro:
        filtro_norm = {str(c).lower().strip() for c in cidades_filtro if str(c).strip()}
        if "all" in filtro_norm or "todas" in filtro_norm:
            filtro_norm = None

    contratos: List[Dict[str, Any]] = []
    for r in rows:
        cid = str(r["id"]).lower().strip()
        if filtro_norm is not None and cid not in filtro_norm:
            continue
        c_lat = float(r["centro_lat"]) if r["centro_lat"] is not None else -8.1930
        c_lon = float(r["centro_lon"]) if r["centro_lon"] is not None else -34.9364
        contratos.append({
            "id": cid,
            "nome": str(r["nome"]),
            "contrato_codigo": normalizar_ascii(cid),
            "aba": _sanitizar_nome_aba(str(r["nome"])),
            "centro": (c_lat, c_lon),
        })
    return contratos


def _carregar_segmentos_cidade(contrato_id: str) -> List[Dict[str, Any]]:
    """Retorna os segmentos da cidade já carregados no banco (importando malha apenas se vazia)."""
    conn = db.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, setor, setores_json, extensao_m, is_manual, tipo, geom_json
            FROM segmentos_viarios
            WHERE contrato_id = ?
            """,
            (contrato_id,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        db.importar_malha_cidade_se_necessario(contrato_id)
        conn = db.get_connection()
        try:
            rows = conn.execute(
                """
                SELECT id, setor, setores_json, extensao_m, is_manual, tipo, geom_json
                FROM segmentos_viarios
                WHERE contrato_id = ?
                """,
                (contrato_id,),
            ).fetchall()
        finally:
            conn.close()

    segs: List[Dict[str, Any]] = []
    for r in rows:
        setor_prim = str(r["setor"] or "S/S").strip() or "S/S"
        try:
            s_list = json.loads(r["setores_json"]) if r["setores_json"] else [setor_prim]
            if not isinstance(s_list, list) or not s_list:
                s_list = [setor_prim]
        except Exception:
            s_list = [setor_prim]
        setores_limpos = [str(s or "").strip() for s in s_list if str(s or "").strip()] or [setor_prim]
        segs.append({
            "id": int(r["id"]),
            "setor": setor_prim,
            "setor_primario": setor_prim,
            "setores": setores_limpos,
            "extensao_m": float(r["extensao_m"] or 0.0),
            "is_manual": bool(r["is_manual"]),
            "tipo": str(r["tipo"] or "COLETA"),
            "geom_json": r["geom_json"],
        })
    return segs


def _auditoria_em_cache_valida(
    contrato_id: str,
    data_iso: str,
    inlog_configurado: bool = True,
) -> Optional[int]:
    """
    Retorna o audit_id já salvo no banco (turno DIURNO ou TODOS) caso seja válido
    e não obsoleto, evitando reprocessamento redundante na API Inlog.
    """
    win_op = janela_operacional_cidade(contrato_id, data_iso)
    agora_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    hoje_str = datetime.now().strftime("%Y-%m-%d")

    conn = None
    try:
        conn = db.get_connection()
        rows = conn.execute(
            """
            SELECT id, pct_cobertura, placas_ativas_json, criado_em
            FROM auditorias
            WHERE contrato_id = ? AND data_iso = ? AND turno IN ('DIURNO', 'TODOS')
            ORDER BY criado_em DESC, id DESC
            """,
            (contrato_id, data_iso),
        ).fetchall()
    except Exception as e:
        print(f"[INDICADORES] Aviso ao consultar cache de auditoria ({contrato_id} {data_iso}): {e}")
        return None
    finally:
        if conn is not None:
            conn.close()

    for aud in rows:
        criado_em = str(aud["criado_em"] or "").strip().replace(" ", "T")[:19]
        if len(criado_em) != 19 or criado_em[10] != "T":
            continue
        # Se a janela operacional ainda não fechou quando a auditoria foi gerada e já avançou
        if criado_em < win_op["data_fim"] and agora_str > criado_em:
            continue
        try:
            placas = json.loads(aud["placas_ativas_json"] or "[]")
            if not isinstance(placas, list):
                placas = []
        except Exception:
            placas = []
        somente_placas_simuladas = (
            inlog_configurado
            and bool(placas)
            and all(str(p).upper().strip() in _PLACAS_SIMULADAS_FALLBACK for p in placas)
        )
        sem_placas_reais = (len(placas) == 0) or somente_placas_simuladas
        auditoria_vazia = (
            float(aud["pct_cobertura"] or 0.0) <= 0.0
            and (sem_placas_reais or inlog_configurado)
            and data_iso <= hoje_str
        )
        if auditoria_vazia:
            try:
                dt_c = datetime.strptime(criado_em, "%Y-%m-%dT%H:%M:%S")
                if (datetime.now() - dt_c).total_seconds() >= 60.0:
                    continue
            except Exception:
                continue
        return int(aud["id"])
    return None


def _garantir_auditoria_e_detalhes_dia(
    engine: Any,
    contrato_id: str,
    data_iso: str,
    inlog_configurado: bool = True,
    segmentos_cidade: Optional[List[Dict[str, Any]]] = None,
    centro_cidade: Optional[Tuple[float, float]] = None,
    verificar_cache: bool = True,
) -> Tuple[Optional[int], Optional[Dict[int, Tuple[str, List[str]]]]]:
    """
    Executa o motor espacial para o dia 100% em memória (persistir_banco=False),
    sem gravar índices ou detalhes de auditoria no banco de dados.
    """
    if verificar_cache and not db.IS_POSTGRES:
        audit_id = _auditoria_em_cache_valida(
            contrato_id,
            data_iso,
            inlog_configurado=inlog_configurado,
        )
        if audit_id is not None:
            return audit_id, None

    try:
        res = engine.processar_ou_obter_auditoria(
            contrato_id=contrato_id,
            data_iso=data_iso,
            turno="DIURNO",
            forcar_reprocessamento=False,
            operacao="COLETA",
            incluir_geojson=False,
            persistir_banco=False,
            segmentos_precarregados=segmentos_cidade,
            centro_precarregado=centro_cidade,
        )
    except TypeError:
        try:
            res = engine.processar_ou_obter_auditoria(
                contrato_id=contrato_id,
                data_iso=data_iso,
                turno="DIURNO",
                forcar_reprocessamento=False,
                operacao="COLETA",
                incluir_geojson=False,
            )
        except TypeError:
            res = engine.processar_ou_obter_auditoria(
                contrato_id=contrato_id,
                data_iso=data_iso,
                turno="DIURNO",
                forcar_reprocessamento=False,
                operacao="COLETA",
            )

    aid = None
    det_mem = None
    if isinstance(res, dict):
        raw_id = (res.get("resumo") or {}).get("id")
        if raw_id:
            aid = int(raw_id)
        if isinstance(res.get("detalhes_map"), dict):
            det_mem = res["detalhes_map"]

    if aid is not None or det_mem is not None:
        return aid, det_mem

    if db.IS_POSTGRES:
        return None, None

    # Fallback apenas para SQLite local / stubs de teste
    conn = None
    try:
        conn = db.get_connection()
        row = conn.execute(
            """
            SELECT id FROM auditorias
            WHERE contrato_id = ? AND data_iso = ? AND turno IN ('DIURNO', 'TODOS')
            ORDER BY id DESC LIMIT 1
            """,
            (contrato_id, data_iso),
        ).fetchone()
        return (int(row["id"]) if row else None), None
    except Exception:
        return None, None
    finally:
        if conn is not None:
            conn.close()


def _garantir_auditoria_dia(
    engine: Any,
    contrato_id: str,
    data_iso: str,
    inlog_configurado: bool = True,
) -> Optional[int]:
    """Obtém o audit_id válido do cache ou executa o motor espacial para o dia."""
    aid, _ = _garantir_auditoria_e_detalhes_dia(
        engine=engine,
        contrato_id=contrato_id,
        data_iso=data_iso,
        inlog_configurado=inlog_configurado,
    )
    return aid


def _carregar_detalhes_auditoria(audit_id: Optional[int]) -> Dict[int, Tuple[str, List[str]]]:
    """Carrega {segmento_id: (status, [placas])} da tabela auditoria_detalhes."""
    if not audit_id:
        return {}
    conn = None
    try:
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT segmento_id, status, placas_responsaveis FROM auditoria_detalhes WHERE auditoria_id = ?",
            (int(audit_id),),
        ).fetchall()
    except Exception as e:
        print(f"[INDICADORES] Aviso ao ler detalhes da auditoria {audit_id}: {e}")
        return {}
    finally:
        if conn is not None:
            conn.close()

    detalhes: Dict[int, Tuple[str, List[str]]] = {}
    for r in rows:
        try:
            placas = json.loads(r["placas_responsaveis"] or "[]")
            if not isinstance(placas, list):
                placas = []
        except Exception:
            placas = []
        detalhes[int(r["segmento_id"])] = (
            str(r["status"] or "not_collected"),
            [str(p).strip() for p in placas if str(p).strip()],
        )
    return detalhes


def _arred1(val: float) -> float:
    return round(float(val), 1)


def calcular_linhas_dia_cidade(
    contrato: Dict[str, str],
    data_iso: str,
    dt_ini: date,
    dt_fim: date,
    qtd_dias: int,
    gerado_em: str,
    segmentos_cidade: List[Dict[str, Any]],
    detalhes_auditoria: Dict[int, Tuple[str, List[str]]],
    vis_cache: Dict[Tuple[str, str], bool],
) -> Tuple[List[List[Any]], Dict[str, Any]]:
    """
    Calcula todas as linhas do relatório de Indicadores para uma cidade em um dia específico,
    seguindo 100% as regras matemáticas e de nulidade do arquivo oficial de referência.
    """
    contrato_id = contrato["id"]
    contrato_codigo = _sanitizar_texto_excel(contrato["contrato_codigo"]) or ""
    dt_atual = datetime.strptime(data_iso, "%Y-%m-%d").date()

    def _eh_agendado(setor_nome: str) -> bool:
        chave = (setor_nome, data_iso)
        if chave not in vis_cache:
            vis_cache[chave] = malha_rotas.setor_visivel_na_data(
                setor_nome,
                data_iso,
                contrato_id=contrato_id,
                incluir_diario_domingo=False,
            )
        return vis_cache[chave]

    # Agrupa segmentos ativos por setor agendado na data
    segs_por_setor: Dict[str, List[Tuple[float, str, List[str]]]] = defaultdict(list)
    for seg in segmentos_cidade:
        ativos = [s for s in seg["setores"] if _eh_agendado(s)]
        if not ativos:
            continue
        st, placas = detalhes_auditoria.get(seg["id"], ("not_collected", []))
        ext_m_raw = float(seg["extensao_m"] or 0.0)
        ext_m = ext_m_raw if (math.isfinite(ext_m_raw) and ext_m_raw >= 0.0) else 0.0
        placas_unicas: List[str] = []
        placas_vistas: Set[str] = set()
        for pl in placas:
            pl_limpa = (_sanitizar_texto_excel(pl) or "").strip()
            if pl_limpa and pl_limpa not in placas_vistas:
                placas_vistas.add(pl_limpa)
                placas_unicas.append(pl_limpa)
        for s_nome in ativos:
            segs_por_setor[s_nome].append((ext_m, st, placas_unicas))

    # Caso não haja setores agendados na data (ex.: domingos sem SCDOM* / DOM)
    if not segs_por_setor:
        linha_sem = [dt_ini, dt_fim, qtd_dias, gerado_em, dt_atual, True] + [None] * 30
        resumo_dia = {
            "sem_setores_agendados": True,
            "qtd_setores": 0,
            "prev_m": 0.0,
            "col_m": 0.0,
            "fgps_m": 0.0,
            "man_m": 0.0,
            "pct_coleta": None,
            "pct_com_fgps": None,
            "pct_fgps": None,
            "pct_manual": None,
        }
        return [linha_sem], resumo_dia

    setores_ordenados = sorted(segs_por_setor.keys(), key=_chave_ordenacao_setor)
    qtd_setores = len(setores_ordenados)

    # Calcula métricas por setor e soma exata para o contrato
    metricas_setores: List[Dict[str, Any]] = []
    tot_prev_contrato = 0.0
    tot_col_contrato = 0.0
    tot_fgps_q_contrato = 0
    tot_fgps_m_contrato = 0.0
    tot_man_q_contrato = 0
    tot_man_m_contrato = 0.0

    for setor_nome in setores_ordenados:
        segs = segs_por_setor[setor_nome]
        prev_m = 0.0
        col_m = 0.0
        fgps_q = 0
        fgps_m = 0.0
        man_q = 0
        man_m = 0.0

        # Passo 1: Acumula totais do setor e pontuação de presença de cada placa
        presenca_m: Dict[str, float] = defaultdict(float)
        exclusiva_m: Dict[str, float] = defaultdict(float)
        collected_segs: List[Tuple[float, List[str]]] = []

        for ext_m, st, placas in segs:
            prev_m += ext_m
            if st == "collected":
                col_m += ext_m
                collected_segs.append((ext_m, placas))
                if len(placas) == 1:
                    exclusiva_m[placas[0]] += ext_m
                for pl in placas:
                    presenca_m[pl] += ext_m
            elif st == "possible_gps_gap":
                fgps_q += 1
                fgps_m += ext_m
            elif st == "possible_manual":
                man_q += 1
                man_m += ext_m

        # Passo 2: Atribuição exata sem dupla contagem (cada segmento coletado -> 1 placa ou sem_veiculo)
        atribuido_por_placa: Dict[str, float] = defaultdict(float)
        sem_veiculo_m = 0.0
        for ext_m, placas in collected_segs:
            if not placas:
                sem_veiculo_m += ext_m
            else:
                melhor_placa = max(placas, key=lambda p: (presenca_m[p], exclusiva_m[p], p))
                atribuido_por_placa[melhor_placa] += ext_m

        # Ordena veículos do setor por metros coletados decrescente (tie-break alfabético)
        ranking_veic = sorted(
            [(pl, m) for pl, m in atribuido_por_placa.items() if m > 0.0],
            key=lambda kv: (-kv[1], kv[0]),
        )

        tot_prev_contrato += prev_m
        tot_col_contrato += col_m
        tot_fgps_q_contrato += fgps_q
        tot_fgps_m_contrato += fgps_m
        tot_man_q_contrato += man_q
        tot_man_m_contrato += man_m

        metricas_setores.append({
            "setor": setor_nome,
            "prev_m": prev_m,
            "col_m": col_m,
            "fgps_q": fgps_q,
            "fgps_m": fgps_m,
            "man_q": man_q,
            "man_m": man_m,
            "ranking_veic": ranking_veic,
            "sem_veiculo_m": sem_veiculo_m,
        })

    # Métricas consolidadas do contrato no dia
    if tot_prev_contrato > 0.0:
        pct_col_contrato = _arred1(tot_col_contrato / tot_prev_contrato * 100.0)
        pct_col_com_fgps_contrato = _arred1(
            (tot_col_contrato + tot_fgps_m_contrato + tot_man_m_contrato) / tot_prev_contrato * 100.0
        )
        pct_fgps_contrato = _arred1(tot_fgps_m_contrato / tot_prev_contrato * 100.0)
        pct_man_contrato = _arred1(tot_man_m_contrato / tot_prev_contrato * 100.0)
    else:
        pct_col_contrato = 0.0
        pct_col_com_fgps_contrato = 0.0
        pct_fgps_contrato = 0.0
        pct_man_contrato = 0.0

    metros_fgps_contrato_r = _arred1(tot_fgps_m_contrato)
    metros_man_contrato_r = _arred1(tot_man_m_contrato)

    linhas: List[List[Any]] = []
    for ms in metricas_setores:
        prev_m = ms["prev_m"]
        col_m = ms["col_m"]
        fgps_q = ms["fgps_q"]
        fgps_m = ms["fgps_m"]
        man_q = ms["man_q"]
        man_m = ms["man_m"]
        ranking_veic = ms["ranking_veic"]
        sem_veic_m = ms["sem_veiculo_m"]

        pct_col_setor = _arred1(col_m / prev_m * 100.0) if prev_m > 0.0 else 0.0
        pct_fgps_setor = _arred1(fgps_m / prev_m * 100.0) if prev_m > 0.0 else 0.0
        pct_man_setor = _arred1(man_m / prev_m * 100.0) if prev_m > 0.0 else 0.0

        if ranking_veic:
            placa_principal, metros_principal = ranking_veic[0]
            pct_principal = _arred1(metros_principal / prev_m * 100.0) if prev_m > 0.0 else 0.0
            metros_principal_r = _arred1(metros_principal)
            apoios = ranking_veic[1:]
        else:
            placa_principal = None
            pct_principal = None
            metros_principal_r = None
            apoios = []

        qtd_apoios = len(apoios)
        if qtd_apoios > 0 and prev_m > 0.0:
            pct_apoio_soma = _arred1(sum(m for _, m in apoios) / prev_m * 100.0)
        else:
            pct_apoio_soma = 0

        if sem_veic_m > 0.0 and prev_m > 0.0:
            pct_sem_veic: Optional[float] = _arred1(sem_veic_m / prev_m * 100.0)
            metros_sem_veic_r: Optional[float] = _arred1(sem_veic_m)
        else:
            pct_sem_veic = None
            metros_sem_veic_r = None

        base_cols = [
            dt_ini,
            dt_fim,
            qtd_dias,
            gerado_em,
            dt_atual,
            None,  # sem_setores_agendados (vazio quando há setores agendados)
            contrato_codigo,
            pct_col_contrato,
            pct_col_com_fgps_contrato,
            tot_fgps_q_contrato,
            metros_fgps_contrato_r,
            pct_fgps_contrato,
            tot_man_q_contrato,
            metros_man_contrato_r,
            pct_man_contrato,
            qtd_setores,
            _sanitizar_texto_excel(ms["setor"]),
            pct_col_setor,
            _arred1(prev_m),
            _arred1(col_m),
            fgps_q,
            _arred1(fgps_m),
            pct_fgps_setor,
            man_q,
            _arred1(man_m),
            pct_man_setor,
            placa_principal,
            pct_principal,
            metros_principal_r,
            qtd_apoios,
            pct_apoio_soma,
        ]

        if apoios:
            for placa_ap, metros_ap in apoios:
                pct_ap = _arred1(metros_ap / prev_m * 100.0) if prev_m > 0.0 else 0.0
                linhas.append(
                    base_cols + [placa_ap, pct_ap, _arred1(metros_ap), pct_sem_veic, metros_sem_veic_r]
                )
        else:
            linhas.append(
                base_cols + [None, None, None, pct_sem_veic, metros_sem_veic_r]
            )

    resumo_dia = {
        "sem_setores_agendados": False,
        "qtd_setores": qtd_setores,
        "prev_m": tot_prev_contrato,
        "col_m": tot_col_contrato,
        "fgps_m": tot_fgps_m_contrato,
        "man_m": tot_man_m_contrato,
        "pct_coleta": pct_col_contrato,
        "pct_com_fgps": pct_col_com_fgps_contrato,
        "pct_fgps": pct_fgps_contrato,
        "pct_manual": pct_man_contrato,
    }
    return linhas, resumo_dia


def gerar_excel_indicadores(
    engine: Any,
    modo: str,
    data_str: Optional[str] = None,
    mes_str: Optional[str] = None,
    cidades: Optional[List[str]] = None,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """
    Gera o arquivo Excel (.xlsx) completo em memória com uma aba por cidade
    e retorna os bytes do arquivo, nome sugerido e resumo consolidado por cidade.
    """
    periodo = resolver_periodo_indicadores(modo=modo, data_str=data_str, mes_str=mes_str)
    contratos = listar_contratos_indicadores(cidades_filtro=cidades)
    if not contratos:
        raise ValueError("Nenhuma cidade encontrada para gerar o relatório.")

    dt_ini: date = periodo["data_inicial"]
    dt_fim: date = periodo["data_final"]
    qtd_dias: int = periodo["qtd_dias"]
    dias_iso: List[str] = periodo["dias_iso"]
    gerado_em = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if db.IS_POSTGRES:
        try:
            db.liberar_espaco_emergencia_db(agressivo=False)
        except Exception:
            pass

    inlog_obj = getattr(engine, "inlog", None)
    if inlog_obj is not None and callable(getattr(inlog_obj, "is_configured", None)):
        inlog_configurado = bool(inlog_obj.is_configured())
    else:
        inlog_configurado = True

    if inlog_obj is not None and inlog_configurado and callable(getattr(inlog_obj, "get_veiculos_tempo_real", None)):
        try:
            inlog_obj.get_veiculos_tempo_real()
        except Exception as e:
            print(f"[INDICADORES] Aviso ao pré-aquecer frota Inlog: {e}")

    etapas_total = max(1, len(contratos) * len(dias_iso))
    unidades_total = max(1, etapas_total * 2)
    unidades_concluidas = 0

    wb = openpyxl.Workbook()
    # Remove aba padrão inicial
    wb.remove(wb.active)

    resumo_cidades: List[Dict[str, Any]] = []
    avisos: List[str] = []
    abas_usadas: Set[str] = set()
    total_linhas_geral = 0

    def _emitir_progresso(cidade_atual: str, data_atual: str, pct_override: Optional[float] = None) -> None:
        if not progress_cb:
            return
        etapas_est = min(etapas_total, unidades_concluidas // 2)
        pct_calc = round(min(99.5, unidades_concluidas / unidades_total * 100.0), 1)
        progress_cb({
            "cidade_atual": cidade_atual,
            "data_atual": data_atual,
            "etapas_concluidas": etapas_est,
            "etapas_total": etapas_total,
            "progresso_pct": pct_override if pct_override is not None else pct_calc,
            "resumo_cidades": list(resumo_cidades),
            "avisos": list(avisos),
        })

    for contrato in contratos:
        cid = contrato["id"]
        nome_aba = _garantir_nome_aba_unico(contrato["aba"], abas_usadas)
        segs_cidade = _carregar_segmentos_cidade(cid)
        vis_cache: Dict[Tuple[str, str], bool] = {}

        # Pré-verifica quais dias têm setores agendados para evitar chamadas Inlog em dias vazios (ex.: domingos)
        dias_com_agenda: List[str] = []
        for d_iso in dias_iso:
            tem_algum = False
            for seg in segs_cidade:
                for s_nome in seg["setores"]:
                    chave = (s_nome, d_iso)
                    if chave not in vis_cache:
                        vis_cache[chave] = malha_rotas.setor_visivel_na_data(
                            s_nome, d_iso, contrato_id=cid, incluir_diario_domingo=False
                        )
                    if vis_cache[chave]:
                        tem_algum = True
                        break
                if tem_algum:
                    break
            if tem_algum:
                dias_com_agenda.append(d_iso)

        # Em PostgreSQL, processa 100% em memória direto para o Excel sem consultar nem gravar auditorias no banco.
        audit_ids_por_dia: Dict[str, Optional[int]] = {}
        detalhes_mem_por_dia: Dict[str, Dict[int, Tuple[str, List[str]]]] = {}
        dias_faltantes: List[str] = []
        if db.IS_POSTGRES:
            dias_faltantes = list(dias_com_agenda)
        else:
            for d_iso in dias_com_agenda:
                aid_cache = _auditoria_em_cache_valida(
                    cid, d_iso, inlog_configurado=inlog_configurado
                )
                if aid_cache is not None:
                    audit_ids_por_dia[d_iso] = aid_cache
                else:
                    dias_faltantes.append(d_iso)

        # Dias sem agenda ou já em cache válido contam imediatamente na fase de auditoria
        unidades_concluidas += max(0, len(dias_iso) - len(dias_faltantes))

        if dias_faltantes:
            _emitir_progresso(contrato["nome"], f"Calculando {dias_faltantes[0]}...")
            centro_cid = contrato.get("centro")
            workers = min(2, len(dias_faltantes))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {
                    pool.submit(
                        _garantir_auditoria_e_detalhes_dia,
                        engine,
                        cid,
                        d_iso,
                        inlog_configurado,
                        segs_cidade,
                        centro_cid,
                        False,
                    ): d_iso
                    for d_iso in dias_faltantes
                }
                for fut in as_completed(futs):
                    d_f = futs[fut]
                    try:
                        aid_res, det_mem_res = fut.result()
                        audit_ids_por_dia[d_f] = aid_res
                        if det_mem_res is not None:
                            detalhes_mem_por_dia[d_f] = det_mem_res
                    except Exception as e:
                        msg_aviso = f"{contrato['nome']} ({d_f}): {e}"
                        print(f"[INDICADORES] Aviso ao auditar {msg_aviso}")
                        avisos.append(msg_aviso)
                        audit_ids_por_dia[d_f] = None
                    unidades_concluidas += 1
                    _emitir_progresso(contrato["nome"], d_f)
        else:
            _emitir_progresso(contrato["nome"], dias_iso[0] if dias_iso else periodo["data_inicial_iso"])

        # Cria aba da cidade com cabeçalho idêntico ao arquivo oficial
        ws = wb.create_sheet(title=nome_aba)
        ws.append(COLUNAS_INDICADORES)
        for col_i in range(1, len(COLUNAS_INDICADORES) + 1):
            c_hdr = ws.cell(row=1, column=col_i)
            c_hdr.font = _HEADER_FONT
            c_hdr.fill = _HEADER_FILL
            c_hdr.alignment = _HEADER_ALIGN
            ws.column_dimensions[get_column_letter(col_i)].width = LARGURAS_COLUNAS[col_i - 1]

        dias_operacao = 0
        dias_sem_agendamento = 0
        soma_prev_m = 0.0
        soma_col_m = 0.0
        soma_fgps_m = 0.0
        soma_man_m = 0.0
        linhas_aba = 0

        for d_iso in dias_iso:
            if d_iso in detalhes_mem_por_dia:
                detalhes = detalhes_mem_por_dia[d_iso]
            elif d_iso in audit_ids_por_dia:
                detalhes = _carregar_detalhes_auditoria(audit_ids_por_dia.get(d_iso))
            else:
                detalhes = {}
            linhas_dia, res_dia = calcular_linhas_dia_cidade(
                contrato=contrato,
                data_iso=d_iso,
                dt_ini=dt_ini,
                dt_fim=dt_fim,
                qtd_dias=qtd_dias,
                gerado_em=gerado_em,
                segmentos_cidade=segs_cidade,
                detalhes_auditoria=detalhes,
                vis_cache=vis_cache,
            )

            for linha in linhas_dia:
                ws.append(linha)
                r_idx = ws.max_row
                for dc in _COLUNAS_DATA_IDX:
                    ws.cell(row=r_idx, column=dc).number_format = _DATE_NUM_FMT
                linhas_aba += 1

            if res_dia["sem_setores_agendados"]:
                dias_sem_agendamento += 1
            else:
                dias_operacao += 1
                soma_prev_m += res_dia["prev_m"]
                soma_col_m += res_dia["col_m"]
                soma_fgps_m += res_dia["fgps_m"]
                soma_man_m += res_dia["man_m"]

            unidades_concluidas += 1
            _emitir_progresso(contrato["nome"], d_iso)

        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:AJ{max(1, ws.max_row)}"

        pct_coleta_cid = _arred1(soma_col_m / soma_prev_m * 100.0) if soma_prev_m > 0.0 else 0.0
        pct_com_fgps_cid = (
            _arred1((soma_col_m + soma_fgps_m + soma_man_m) / soma_prev_m * 100.0)
            if soma_prev_m > 0.0 else 0.0
        )
        pct_fgps_cid = _arred1(soma_fgps_m / soma_prev_m * 100.0) if soma_prev_m > 0.0 else 0.0
        pct_man_cid = _arred1(soma_man_m / soma_prev_m * 100.0) if soma_prev_m > 0.0 else 0.0

        resumo_cidades.append({
            "id": cid,
            "nome": contrato["nome"],
            "aba": nome_aba,
            "contrato": contrato["contrato_codigo"],
            "dias_operacao": dias_operacao,
            "dias_sem_agendamento": dias_sem_agendamento,
            "pct_coleta": pct_coleta_cid,
            "pct_com_falha_gps": pct_com_fgps_cid,
            "pct_falha_gps": pct_fgps_cid,
            "pct_possivel_manual": pct_man_cid,
            "km_previsto": round(soma_prev_m / 1000.0, 1),
            "km_coletado": round(soma_col_m / 1000.0, 1),
            "linhas": linhas_aba,
        })
        total_linhas_geral += linhas_aba

    if wb.worksheets:
        wb.active = 0
        for idx, sheet in enumerate(wb.worksheets):
            if sheet.views and sheet.views.sheetView:
                sheet.views.sheetView[0].tabSelected = (idx == 0)

    buf = io.BytesIO()
    wb.save(buf)
    xlsx_bytes = buf.getvalue()
    if not xlsx_bytes.startswith(b"PK\x03\x04"):
        raise RuntimeError("Falha ao gerar arquivo Excel válido (assinatura ZIP ausente).")
    filename = f"Indicadores - Mapa de Execucao - {periodo['rotulo_arquivo']}.xlsx"

    if progress_cb:
        progress_cb({
            "cidade_atual": "Concluído",
            "data_atual": periodo["data_final_iso"],
            "etapas_concluidas": etapas_total,
            "etapas_total": etapas_total,
            "progresso_pct": 100.0,
            "resumo_cidades": list(resumo_cidades),
            "avisos": list(avisos),
        })

    return {
        "xlsx_bytes": xlsx_bytes,
        "filename": filename,
        "tamanho_kb": round(len(xlsx_bytes) / 1024.0, 1),
        "gerado_em": gerado_em,
        "periodo": {
            "modo": periodo["modo"],
            "data_inicial": periodo["data_inicial_iso"],
            "data_final": periodo["data_final_iso"],
            "qtd_dias": qtd_dias,
            "mes_em_andamento": periodo["mes_em_andamento"],
        },
        "total_abas": len(contratos),
        "total_linhas": total_linhas_geral,
        "resumo_cidades": resumo_cidades,
        "avisos": avisos,
    }


def _limpar_jobs_antigos() -> None:
    agora = time.time()
    expirados = [
        jid for jid, info in _JOBS.items()
        if (agora - float(info.get("criado_ts", 0))) > _JOB_TTL_SECONDS
    ]
    for jid in expirados:
        _JOBS.pop(jid, None)


def iniciar_job_indicadores(
    engine: Any,
    modo: str,
    data_str: Optional[str] = None,
    mes_str: Optional[str] = None,
    cidades: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Inicia geração assíncrona do Excel de Indicadores e retorna imediatamente o job_id."""
    # Valida período antes de disparar thread
    periodo = resolver_periodo_indicadores(modo=modo, data_str=data_str, mes_str=mes_str)
    contratos = listar_contratos_indicadores(cidades_filtro=cidades)
    if not contratos:
        raise ValueError("Selecione ao menos uma cidade para exportar.")

    job_id = secrets.token_hex(10)
    estado_inicial: Dict[str, Any] = {
        "job_id": job_id,
        "status": "running",
        "criado_ts": time.time(),
        "progresso_pct": 0.0,
        "cidade_atual": contratos[0]["nome"],
        "data_atual": periodo["data_inicial_iso"],
        "etapas_concluidas": 0,
        "etapas_total": len(contratos) * periodo["qtd_dias"],
        "periodo": {
            "modo": periodo["modo"],
            "data_inicial": periodo["data_inicial_iso"],
            "data_final": periodo["data_final_iso"],
            "qtd_dias": periodo["qtd_dias"],
            "mes_em_andamento": periodo["mes_em_andamento"],
        },
        "resumo_cidades": [],
        "avisos": [],
        "filename": None,
        "tamanho_kb": 0.0,
        "total_abas": len(contratos),
        "total_linhas": 0,
        "gerado_em": None,
        "erro": None,
        "_xlsx_bytes": None,
    }

    with _JOBS_LOCK:
        _limpar_jobs_antigos()
        _JOBS[job_id] = estado_inicial

    def _on_progress(upd: Dict[str, Any]) -> None:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if not job:
                return
            job["cidade_atual"] = upd.get("cidade_atual", job["cidade_atual"])
            job["data_atual"] = upd.get("data_atual", job["data_atual"])
            job["etapas_concluidas"] = upd.get("etapas_concluidas", job["etapas_concluidas"])
            job["etapas_total"] = upd.get("etapas_total", job["etapas_total"])
            job["progresso_pct"] = upd.get("progresso_pct", job["progresso_pct"])
            if "resumo_cidades" in upd:
                job["resumo_cidades"] = upd["resumo_cidades"]
            if "avisos" in upd:
                job["avisos"] = upd["avisos"]

    def _worker() -> None:
        try:
            resultado = gerar_excel_indicadores(
                engine=engine,
                modo=modo,
                data_str=data_str,
                mes_str=mes_str,
                cidades=cidades,
                progress_cb=_on_progress,
            )
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                if job:
                    job["status"] = "done"
                    job["progresso_pct"] = 100.0
                    job["filename"] = resultado["filename"]
                    job["tamanho_kb"] = resultado["tamanho_kb"]
                    job["total_abas"] = resultado["total_abas"]
                    job["total_linhas"] = resultado["total_linhas"]
                    job["gerado_em"] = resultado["gerado_em"]
                    job["resumo_cidades"] = resultado["resumo_cidades"]
                    job["avisos"] = resultado.get("avisos", [])
                    job["_xlsx_bytes"] = resultado["xlsx_bytes"]
        except Exception as e:
            print(f"[INDICADORES] Erro no job {job_id}: {e}")
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                if job:
                    job["status"] = "error"
                    job["erro"] = str(e)

    threading.Thread(target=_worker, daemon=True).start()
    return obter_status_job(job_id) or {}


def obter_status_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Retorna o estado público (serializável em JSON) de um job de indicadores."""
    with _JOBS_LOCK:
        job = _JOBS.get(str(job_id or "").strip())
        if not job:
            return None
        publico = {k: v for k, v in job.items() if not k.startswith("_")}
        if publico["status"] == "done":
            publico["download_url"] = f"/api/indicadores/download?job_id={publico['job_id']}"
        return publico


def obter_arquivo_job(job_id: str) -> Optional[Tuple[bytes, str]]:
    """Retorna (xlsx_bytes, filename) de um job concluído."""
    with _JOBS_LOCK:
        job = _JOBS.get(str(job_id or "").strip())
        if not job or job.get("status") != "done" or not job.get("_xlsx_bytes"):
            return None
        return bytes(job["_xlsx_bytes"]), str(job.get("filename") or "Indicadores_Mapa_Execucao.xlsx")
