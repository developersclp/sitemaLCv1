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
import re
import secrets
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

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


def _sanitizar_nome_aba(nome: str) -> str:
    """Garante nome de aba válido no Excel (máx. 31 caracteres, sem caracteres proibidos)."""
    limpo = re.sub(r"[:\\/?*\[\]]", " ", str(nome or "Cidade")).strip()
    limpo = re.sub(r"\s+", " ", limpo)
    return (limpo or "Cidade")[:31]


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
    hoje = date.today()

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


def listar_contratos_indicadores(cidades_filtro: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """Retorna os contratos ordenados alfabeticamente pelo nome oficial da cidade."""
    conn = db.get_connection()
    try:
        rows = conn.execute("SELECT id, nome FROM contratos ORDER BY nome").fetchall()
    finally:
        conn.close()

    filtro_norm = None
    if cidades_filtro:
        filtro_norm = {str(c).lower().strip() for c in cidades_filtro if str(c).strip()}
        if "all" in filtro_norm or "todas" in filtro_norm:
            filtro_norm = None

    contratos: List[Dict[str, str]] = []
    for r in rows:
        cid = str(r["id"]).lower().strip()
        if filtro_norm is not None and cid not in filtro_norm:
            continue
        contratos.append({
            "id": cid,
            "nome": str(r["nome"]),
            "contrato_codigo": normalizar_ascii(cid),
            "aba": _sanitizar_nome_aba(str(r["nome"])),
        })
    return contratos


def _carregar_segmentos_cidade(contrato_id: str) -> List[Dict[str, Any]]:
    """Retorna os segmentos da cidade já carregados no banco (importando malha apenas se vazia)."""
    conn = db.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, setor, setores_json, extensao_m, is_manual
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
                SELECT id, setor, setores_json, extensao_m, is_manual
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
            "setor_primario": setor_prim,
            "setores": setores_limpos,
            "extensao_m": float(r["extensao_m"] or 0.0),
            "is_manual": bool(r["is_manual"]),
        })
    return segs


def _auditoria_em_cache_valida(contrato_id: str, data_iso: str) -> Optional[int]:
    """
    Retorna o audit_id já salvo no banco (turno DIURNO ou TODOS) caso seja válido
    e não obsoleto, evitando reprocessamento redundante na API Inlog.
    """
    win_op = janela_operacional_cidade(contrato_id, data_iso)
    agora_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    hoje_str = datetime.now().strftime("%Y-%m-%d")

    conn = db.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, pct_cobertura, placas_ativas_json, criado_em
            FROM auditorias
            WHERE contrato_id = ? AND data_iso = ? AND turno IN ('DIURNO', 'TODOS')
            ORDER BY criado_em DESC, id DESC
            """,
            (contrato_id, data_iso),
        ).fetchall()
    finally:
        conn.close()

    for aud in rows:
        criado_em = str(aud["criado_em"] or "")
        if len(criado_em) < 19 or "T" not in criado_em:
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
        auditoria_vazia = (
            float(aud["pct_cobertura"] or 0.0) <= 0.0
            and len(placas) == 0
            and data_iso <= hoje_str
        )
        if auditoria_vazia:
            try:
                dt_c = datetime.strptime(criado_em[:19], "%Y-%m-%dT%H:%M:%S")
                if (datetime.now() - dt_c).total_seconds() >= 60.0:
                    continue
            except Exception:
                continue
        return int(aud["id"])
    return None


def _garantir_auditoria_dia(engine: Any, contrato_id: str, data_iso: str) -> Optional[int]:
    """Obtém o audit_id válido do cache ou executa o motor espacial para o dia."""
    audit_id = _auditoria_em_cache_valida(contrato_id, data_iso)
    if audit_id is not None:
        return audit_id

    res = engine.processar_ou_obter_auditoria(
        contrato_id=contrato_id,
        data_iso=data_iso,
        turno="DIURNO",
        forcar_reprocessamento=False,
        operacao="COLETA",
    )
    aid = (res.get("resumo") or {}).get("id")
    if aid:
        return int(aid)

    # Fallback: consulta diretamente no banco
    conn = db.get_connection()
    try:
        row = conn.execute(
            """
            SELECT id FROM auditorias
            WHERE contrato_id = ? AND data_iso = ? AND turno IN ('DIURNO', 'TODOS')
            ORDER BY id DESC LIMIT 1
            """,
            (contrato_id, data_iso),
        ).fetchone()
        return int(row["id"]) if row else None
    finally:
        conn.close()


def _carregar_detalhes_auditoria(audit_id: Optional[int]) -> Dict[int, Tuple[str, List[str]]]:
    """Carrega {segmento_id: (status, [placas])} da tabela auditoria_detalhes."""
    if not audit_id:
        return {}
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT segmento_id, status, placas_responsaveis FROM auditoria_detalhes WHERE auditoria_id = ?",
            (int(audit_id),),
        ).fetchall()
    finally:
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
    contrato_codigo = contrato["contrato_codigo"]
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
        ext_m = seg["extensao_m"]
        for s_nome in ativos:
            segs_por_setor[s_nome].append((ext_m, st, placas))

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
            ms["setor"],
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

    etapas_total = max(1, len(contratos) * len(dias_iso))
    etapas_concluidas = 0

    wb = openpyxl.Workbook()
    # Remove aba padrão inicial
    wb.remove(wb.active)

    resumo_cidades: List[Dict[str, Any]] = []
    total_linhas_geral = 0

    for contrato in contratos:
        cid = contrato["id"]
        nome_aba = contrato["aba"]
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

        # Garante auditorias dos dias agendados (paralelizando apenas dias faltantes no cache)
        audit_ids_por_dia: Dict[str, Optional[int]] = {}
        dias_faltantes: List[str] = []
        for d_iso in dias_com_agenda:
            aid_cache = _auditoria_em_cache_valida(cid, d_iso)
            if aid_cache is not None:
                audit_ids_por_dia[d_iso] = aid_cache
            else:
                dias_faltantes.append(d_iso)

        if dias_faltantes:
            workers = min(3, len(dias_faltantes))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {
                    pool.submit(_garantir_auditoria_dia, engine, cid, d_iso): d_iso
                    for d_iso in dias_faltantes
                }
                for fut in as_completed(futs):
                    d_f = futs[fut]
                    try:
                        audit_ids_por_dia[d_f] = fut.result()
                    except Exception as e:
                        print(f"[INDICADORES] Aviso ao auditar {cid} em {d_f}: {e}")
                        audit_ids_por_dia[d_f] = None

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
            if progress_cb:
                progress_cb({
                    "cidade_atual": contrato["nome"],
                    "data_atual": d_iso,
                    "etapas_concluidas": etapas_concluidas,
                    "etapas_total": etapas_total,
                    "progresso_pct": round(etapas_concluidas / etapas_total * 100.0, 1),
                    "resumo_cidades": list(resumo_cidades),
                })

            detalhes = _carregar_detalhes_auditoria(audit_ids_por_dia.get(d_iso)) if d_iso in audit_ids_por_dia else {}
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

            etapas_concluidas += 1

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

    buf = io.BytesIO()
    wb.save(buf)
    xlsx_bytes = buf.getvalue()
    filename = f"Indicadores - Mapa de Execucao - {periodo['rotulo_arquivo']}.xlsx"

    if progress_cb:
        progress_cb({
            "cidade_atual": "Concluído",
            "data_atual": periodo["data_final_iso"],
            "etapas_concluidas": etapas_total,
            "etapas_total": etapas_total,
            "progresso_pct": 100.0,
            "resumo_cidades": list(resumo_cidades),
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
