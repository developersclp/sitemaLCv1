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
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import database as db
from inlog_service import normalizar_ascii

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DIR_MALHA_TRANSITAVEIS = "Malhas Viárias - Somente Transitáveis"
DIR_MALHA_COMPLETA = "Malhas Viárias - Completas"

DIAS_SEMANA = {"SEG": 0, "TER": 1, "QUA": 2, "QUI": 3, "SEX": 4, "SAB": 5, "DOM": 6}
ORDEM_DIAS = ["SEG", "TER", "QUA", "QUI", "SEX", "SAB", "DOM"]
NOMES_DIAS_PT = [
    "Segunda-feira",
    "Terça-feira",
    "Quarta-feira",
    "Quinta-feira",
    "Sexta-feira",
    "Sábado",
    "Domingo",
]
DIAS_ALIASES = {
    "SEGUNDA": "SEG",
    "TERCA": "TER",
    "QUARTA": "QUA",
    "QUINTA": "QUI",
    "SEXTA": "SEX",
    "SABADO": "SAB",
    "DOMINGO": "DOM",
}
_NUM_DIA_INLOG = {"2": "SEG", "3": "TER", "4": "QUA", "5": "QUI", "6": "SEX", "7": "SAB"}

_PREFIXOS_CONHECIDOS_ORDENADOS = (
    "SCDOMD", "SCDOMN", "SCDOM",
    "SCDIST", "SCFEIRA", "SCRURAL", "SCEXTRA",
    "SCSBD", "SCSCD",
    "SCAD", "SCDD", "SCDN", "SCDM", "SCDV",
    "SCPD", "SCPN", "SCPM", "SCPV",
    "SCID", "SCIN", "SCIM", "SCIV",
)

_RE_CODIGO = re.compile(
    r"\b(SCDOMD|SCDOMN|SCDOM|SCDIST|SCFEIRA|SCRURAL|SCEXTRA|SCSBD|SCSCD|SC[A-Z]{1,6}|FEIRA)\s*0*(\d{1,4})\b"
)
_RE_PREF_SEM_NUM = re.compile(
    r"\b(SCDOMD|SCDOMN|SCDOM|SCDIST|SCFEIRA|SCRURAL|SCEXTRA|SCSBD|SCSCD|SC[A-Z]{1,6})\b"
)
_RE_QZ = re.compile(
    r"\b((?:(?:SEG|TER|QUA|QUI|SEX|SAB|DOM)\s*(?:E\s+)?)+)QZ\s+(\d{2})[./-](\d{2})[./-](\d{4})\b"
)
_RE_ORDINAL = re.compile(
    r"\b(SEG|TER|QUA|QUI|SEX|SAB|DOM)\s+((?:[1-5L](?:\s+|$))+)"
)
_RE_INTERVALO_A = re.compile(
    r"\b(SEG|TER|QUA|QUI|SEX|SAB|DOM)\s+A\s+(SEG|TER|QUA|QUI|SEX|SAB|DOM)\b"
)
_RE_DIA_SIGLA = re.compile(r"\b(SEG|TER|QUA|QUI|SEX|SAB|DOM)\b")


def _parse_data_iso(data_iso: Any) -> Optional[date]:
    if isinstance(data_iso, datetime):
        return data_iso.date()
    if isinstance(data_iso, date):
        return data_iso
    try:
        return datetime.strptime(str(data_iso)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _normalizar_trecho_agenda(texto: Any) -> str:
    """
    Normaliza um texto para análise de nomenclatura/agenda preservando datas QZ
    e separando prefixo de número (ex.: 'SCDD04 3E7' -> 'SCDD 04 TER E SAB').
    """
    raw = str(texto or "").upper().strip()
    raw = re.sub(r"\.GPKG$", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^ROTA[_\s-]*FINAL[_\s-]*", "", raw, flags=re.IGNORECASE)
    t = normalizar_ascii(raw)
    t = re.sub(r"([A-Z])(\d)", r"\1 \2", t)
    t = re.sub(r"\b(\d+)([A-Z]+)", r"\1 \2", t)

    # Mapeia variações descritivas da Inlog (ex.: SC DISTRITO QUINTA 09 -> SCDIST 09 QUI)
    t = re.sub(r"\bSC\s+DISTRITO\b", "SCDIST", t)
    t = re.sub(r"\bDISTRI(?:TO)?\b", "SCDIST", t)
    t = re.sub(r"\bDOMINGO\s+FEIRA\b", "SCFEIRA", t)
    # Expande nomes de dias por extenso preservando datas DD.MM.AAAA
    for longo, curto in DIAS_ALIASES.items():
        t = re.sub(rf"\b{longo}(?:\s*-?\s*FEIRA)?\b", curto, t)

    # Expande abreviações numéricas Inlog de dias (ex.: 2E4 -> SEG E QUA, 2A6 -> SEG A SEX, 3E7 -> TER E SAB)
    def _repl_num_op(m: re.Match) -> str:
        d1 = _NUM_DIA_INLOG.get(m.group(1), m.group(1))
        op = m.group(2)
        d2 = _NUM_DIA_INLOG.get(m.group(3), m.group(3))
        return f"{d1} {op} {d2}"

    t = re.sub(r"\b([2-7])\s*([AE])\s*([2-7])\b", _repl_num_op, t)
    return re.sub(r"\s+", " ", t).strip()


def _extrair_bloco_identificacao_e_sufixo(texto_norm: str) -> Tuple[str, Optional[int], str, str]:
    """
    Extrai (prefixo, numero, sufixo_agenda, trecho_completo_identificado).
    Isola o sufixo da agenda antes da descrição de bairros/ruas (' - BAIRRO...').
    """
    # Normaliza caso Inlog "SCDIST <DIA> <NUM>" -> "SCDIST <NUM> <DIA>"
    texto_norm = re.sub(
        r"\b(SCDIST|SCFEIRA|SCRURAL|SCEXTRA)\s+(SEG|TER|QUA|QUI|SEX|SAB|DOM)\s+0*(\d{1,4})\b",
        r"\1 \3 \2",
        texto_norm,
    )

    m = _RE_CODIGO.search(texto_norm)
    if not m:
        # Tenta localizar prefixo sem número (ex.: VITORIA - SCDOMD - DOMICILIAR)
        m2 = _RE_PREF_SEM_NUM.search(texto_norm)
        if not m2:
            # Fallback para descrições antigas sem SC* (ex.: "CARUARU - DOMINGO FEIRA 74 MANHA - CENTRO")
            partes_dash = [p.strip() for p in texto_norm.split(" - ") if p.strip()]
            bloco = partes_dash[1] if len(partes_dash) >= 2 else texto_norm
            return "OUTRO", None, bloco, bloco
        pref = m2.group(1)
        cauda = texto_norm[m2.end():].strip()
        sufixo = cauda.split(" - ")[0].strip() if " - " in cauda else cauda
        return pref, None, sufixo, f"{pref} {sufixo}".strip()

    pref = m.group(1)
    if pref == "FEIRA":
        pref = "SCFEIRA"
    num = int(m.group(2))
    cauda = texto_norm[m.end():].strip()

    # Se houver " - " logo após o código (ex: "SCDIST 10 - TER E SAB" ou "SCID 54 - BOSQUE DA SAUDE - DOM BOSCO"),
    # pega apenas o primeiro bloco caso o resto após " - " seja só agenda, senão pega antes do " - "
    if cauda.startswith("- "):
        prox_blocos = [p.strip() for p in cauda[2:].split(" - ") if p.strip()]
        if prox_blocos:
            primeiro = prox_blocos[0]
            tokens_p = [tok for tok in primeiro.split() if tok not in ("E", "A", "QZ")]
            # Só considera bloco após "-" como sufixo de agenda se ele for composto apenas de tokens de agenda
            if tokens_p and all(
                tok in DIAS_SEMANA or tok in ("1", "2", "3", "4", "5", "L") or re.match(r"^\d{2}[./-]\d{2}[./-]\d{4}$", tok)
                for tok in tokens_p
            ):
                sufixo = primeiro
            else:
                sufixo = ""
        else:
            sufixo = ""
    elif " - " in cauda:
        sufixo = cauda.split(" - ")[0].strip()
    else:
        sufixo = cauda

    return pref, num, sufixo, f"{pref} {num} {sufixo}".strip()


def _classificar_turno(pref: str, texto_norm: str, turno_forcado: Optional[str] = None) -> str:
    if turno_forcado:
        tf = normalizar_ascii(turno_forcado)
        if "NOTUR" in tf or tf == "N":
            return "NOTURNO"
        if "DIUR" in tf or tf == "D":
            return "DIURNO"

    if pref in ("SCDOMN", "SCDN", "SCPN", "SCIN", "SCDM", "SCPM", "SCIM"):
        return "NOTURNO"
    if pref in ("SCDOMD", "SCDD", "SCPD", "SCID", "SCAD", "SCSBD", "SCSCD", "SCPV", "SCIV", "SCDV"):
        return "DIURNO"
    if re.search(r"\b(NOTURNO|NOTURNA|NOITE)\b", texto_norm):
        return "NOTURNO"
    if re.search(r"\b(DIURNO|DIURNA|MANHA|TARDE)\b", texto_norm):
        return "DIURNO"

    # Padrão de heurística para 4ª letra (SCxN / SCxM -> NOTURNO)
    if pref.startswith("SC") and len(pref) == 4 and pref not in ("SCDOM",):
        if pref[-1] in ("N", "M"):
            return "NOTURNO"
        if pref[-1] in ("D", "V"):
            return "DIURNO"

    # SCDOM 28 / 29 / 38 (Olinda Noturno) sem palavra NOTURNO no apelido
    if pref == "SCDOM":
        m_num = re.search(r"\bSCDOM\s*0*(\d+)\b", texto_norm)
        if m_num and int(m_num.group(1)) in (28, 29, 38):
            return "NOTURNO"

    return "DIURNO"


def _expandir_intervalo_dias(d1: str, d2: str) -> List[str]:
    i1 = DIAS_SEMANA[d1]
    i2 = DIAS_SEMANA[d2]
    if i1 <= i2:
        return [ORDEM_DIAS[i] for i in range(i1, i2 + 1)]
    return [ORDEM_DIAS[i] for i in list(range(i1, 7)) + list(range(0, i2 + 1))]


def _ordinal_dia_no_mes(dt: date) -> Tuple[int, bool]:
    nth = (dt.day - 1) // 7 + 1
    eh_ultimo = (dt + timedelta(days=7)).month != dt.month
    return nth, eh_ultimo


def _formatar_ordinal_pt(dia_sigla: str, ords: List[str]) -> str:
    mapa = {"1": "1º", "2": "2º", "3": "3º", "4": "4º", "5": "5º", "L": "Últ."}
    nomes = [mapa.get(o, o) for o in ords]
    return f"{'/'.join(nomes)} {dia_sigla}"


def analisar_nomenclatura_setor(
    setor_nome: Any,
    contrato_id: Optional[str] = None,
    turno_forcado: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Analisa a nomenclatura completa de um setor seguindo estritamente o Manual de Rotas e Setores:
      1. Sufixos explícitos:
         - Quinzenal: `<DIA> QZ DD.MM.AAAA` (ex.: SEG QZ 06.04.2026, SAB QZ 04.04.2026)
         - Ordinal mensal: `SAB 1`, `TER 2`, `QUI 3`, `SEX 4`, `SAB L`, `TER 1 3`, `QUA L SAB 3 L`
         - Intervalo consecutivo com 'A': `SEG A SEX`, `SEG A SAB`, `TER A QUI`
         - Combinações com espaço ou 'E' / Dias individuais: `SEG`, `TER`, `SEG QUA SEX`, `TER E QUI`, `TER QUI SAB`
      2. Padrão por prefixo (quando nenhum sufixo de dia é informado):
         - `SCD*` (exceto SCDIST*, SCDOM*): Diário (todos os dias)
         - `SCP*`: Dias de frequência Par (Seg, Qua, Sex — 2ª, 4ª, 6ª)
         - `SCI*`: Dias de frequência Ímpar (Ter, Qui, Sáb — 3ª, 5ª, Sáb)
         - `SCDOM*`: Apenas Domingos
         - `SCA*`, `SCS*`, `SCDIST*`, `SCFEIRA*`, `SCRURAL*`, `SCEXTRA*`: Sem agenda padrão se não houver sufixo
    """
    texto_norm = _normalizar_trecho_agenda(setor_nome)
    pref, num, sufixo, ident_str = _extrair_bloco_identificacao_e_sufixo(texto_norm)

    # Se vier apenas prefixo+num da Inlog sem sufixo e tivermos contrato_id, tenta enriquecer pelo arquivo de rota
    turno_arquivo = None
    if contrato_id and pref != "OUTRO" and num is not None:
        codigo_canon = ("FEIRA" if pref == "SCFEIRA" else pref, num)
        rotas_cidade = listar_rotas_cidade(contrato_id)
        cands_rota = [r for r in rotas_cidade if r.get("codigo") == codigo_canon]
        if len(cands_rota) == 1:
            turno_arquivo = cands_rota[0].get("turno")
            if not sufixo:
                _, _, sufixo_rota, _ = _extrair_bloco_identificacao_e_sufixo(
                    _normalizar_trecho_agenda(cands_rota[0].get("apelido", ""))
                )
                if sufixo_rota:
                    sufixo = sufixo_rota

    turno = _classificar_turno(pref, texto_norm, turno_forcado=turno_forcado or turno_arquivo)

    # Limpa pontuação solta no sufixo mantendo pontos/barras de datas
    sufixo_clean = re.sub(r"[,;()]+", " ", sufixo)
    sufixo_clean = re.sub(r"\s+", " ", sufixo_clean).strip()

    # 1. Quinzenal (`<DIAS> QZ <DD.MM.AAAA>`)
    m_qz = _RE_QZ.search(sufixo_clean)
    if m_qz:
        dias_raw = _RE_DIA_SIGLA.findall(m_qz.group(1))
        dias_ord = sorted(set(dias_raw), key=lambda d: DIAS_SEMANA[d])
        dd, mm, yyyy = int(m_qz.group(2)), int(m_qz.group(3)), int(m_qz.group(4))
        try:
            ref_dt = date(yyyy, mm, dd)
        except ValueError:
            ref_dt = None
        rotulo_data = f"{dd:02d}.{mm:02d}.{yyyy:04d}"
        return {
            "prefixo": pref,
            "numero": num,
            "turno": turno,
            "regra_tipo": "QUINZENAL",
            "frequencia": "QUINZENAL",
            "dias_semana": dias_ord,
            "ordinais": {},
            "qz_ref": ref_dt.isoformat() if ref_dt else None,
            "tem_sufixo_explicito": True,
            "agenda_rotulo": f"{'/'.join(dias_ord)} QZ {rotulo_data}",
            "descricao": f"Quinzenal ({'/'.join(dias_ord)}) ref. {dd:02d}/{mm:02d}/{yyyy:04d}",
        }

    # 2. Ordinal mensal (`SAB 1`, `TER 2`, `QUI 3`, `SEX 4`, `SAB L`, `TER 1 3`, `QUA L SAB 3 L`)
    ord_matches = list(_RE_ORDINAL.finditer(sufixo_clean))
    if ord_matches:
        ord_map: Dict[str, List[str]] = {}
        partes_rotulo: List[str] = []
        for om in ord_matches:
            d_tok = om.group(1)
            ords = re.findall(r"[1-5L]", om.group(2))
            if d_tok not in ord_map:
                ord_map[d_tok] = []
            for o in ords:
                if o not in ord_map[d_tok]:
                    ord_map[d_tok].append(o)
        # Caso existam dias extras no sufixo fora dos blocos ordinais (sem número ordinal)
        sufixo_sem_ord = _RE_ORDINAL.sub(" ", sufixo_clean)
        dias_extra = [d for d in _RE_DIA_SIGLA.findall(sufixo_sem_ord) if d not in ord_map]
        for d_tok, ords in ord_map.items():
            partes_rotulo.append(_formatar_ordinal_pt(d_tok, ords))
        for d_ext in dias_extra:
            partes_rotulo.append(d_ext)
        dias_ord = sorted(set(list(ord_map.keys()) + dias_extra), key=lambda d: DIAS_SEMANA[d])
        return {
            "prefixo": pref,
            "numero": num,
            "turno": turno,
            "regra_tipo": "ORDINAL",
            "frequencia": "ORDINAL",
            "dias_semana": dias_ord,
            "ordinais": ord_map,
            "dias_sem_ordinal": dias_extra,
            "qz_ref": None,
            "tem_sufixo_explicito": True,
            "agenda_rotulo": " · ".join(partes_rotulo),
            "descricao": f"Ordinal mensal: {' · '.join(partes_rotulo)}",
        }

    # 3. Intervalo consecutivo com "A" (`SEG A SEX`, `SEG A SAB`, `TER A QUI`)
    int_matches = list(_RE_INTERVALO_A.finditer(sufixo_clean))
    if int_matches:
        dias_set: Set[str] = set()
        for im in int_matches:
            dias_set.update(_expandir_intervalo_dias(im.group(1), im.group(2)))
        sufixo_sem_int = _RE_INTERVALO_A.sub(" ", sufixo_clean)
        dias_set.update(_RE_DIA_SIGLA.findall(sufixo_sem_int))
        dias_ord = sorted(dias_set, key=lambda d: DIAS_SEMANA[d])
        rotulo = (
            f"{int_matches[0].group(1)} A {int_matches[0].group(2)}"
            if len(int_matches) == 1 and not _RE_DIA_SIGLA.findall(sufixo_sem_int)
            else "/".join(dias_ord)
        )
        return {
            "prefixo": pref,
            "numero": num,
            "turno": turno,
            "regra_tipo": "INTERVALO",
            "frequencia": rotulo,
            "dias_semana": dias_ord,
            "ordinais": {},
            "qz_ref": None,
            "tem_sufixo_explicito": True,
            "agenda_rotulo": rotulo,
            "descricao": f"Intervalo de coleta: {' / '.join(dias_ord)}",
        }

    # 4. Combinações com espaço, "E" ou dia individual (`SEG`, `TER E SAB`, `SEG QUA SEX`, etc.)
    dias_explicitos = _RE_DIA_SIGLA.findall(sufixo_clean)
    if dias_explicitos:
        dias_ord = sorted(set(dias_explicitos), key=lambda d: DIAS_SEMANA[d])
        rotulo = (
            " E ".join(dias_ord)
            if (len(dias_ord) == 2 and re.search(r"\bE\b", sufixo_clean))
            else "/".join(dias_ord)
        )
        freq = (
            "PAR"
            if dias_ord == ["SEG", "QUA", "SEX"]
            else ("IMPAR" if dias_ord == ["TER", "QUI", "SAB"] else ("DOMINGO" if dias_ord == ["DOM"] else rotulo))
        )
        return {
            "prefixo": pref,
            "numero": num,
            "turno": turno,
            "regra_tipo": "DIAS_FIXOS",
            "frequencia": freq,
            "dias_semana": dias_ord,
            "ordinais": {},
            "qz_ref": None,
            "tem_sufixo_explicito": True,
            "agenda_rotulo": rotulo,
            "descricao": f"Dias fixos: {', '.join(dias_ord)}",
        }

    # 5. Padrão deduzido pelo prefixo do código (quando nenhum sufixo explícito foi informado)
    if pref.startswith("SCDOM"):
        return {
            "prefixo": pref,
            "numero": num,
            "turno": turno,
            "regra_tipo": "PADRAO_DOMINGO",
            "frequencia": "DOMINGO",
            "dias_semana": ["DOM"],
            "ordinais": {},
            "qz_ref": None,
            "tem_sufixo_explicito": False,
            "agenda_rotulo": "DOM",
            "descricao": "Apenas domingos (padrão SCDOM*)",
        }

    if pref.startswith("SCD") and not pref.startswith("SCDIST"):
        return {
            "prefixo": pref,
            "numero": num,
            "turno": turno,
            "regra_tipo": "PADRAO_DIARIO",
            "frequencia": "DIARIO",
            "dias_semana": ORDEM_DIAS[:],
            "ordinais": {},
            "qz_ref": None,
            "tem_sufixo_explicito": False,
            "agenda_rotulo": "DIÁRIO",
            "descricao": "Diário — todos os dias (padrão SCD*)",
        }

    if pref.startswith("SCP"):
        return {
            "prefixo": pref,
            "numero": num,
            "turno": turno,
            "regra_tipo": "PADRAO_PAR",
            "frequencia": "PAR",
            "dias_semana": ["SEG", "QUA", "SEX"],
            "ordinais": {},
            "qz_ref": None,
            "tem_sufixo_explicito": False,
            "agenda_rotulo": "PAR (SEG/QUA/SEX)",
            "descricao": "Frequência PAR — Seg/Qua/Sex (padrão SCP*)",
        }

    if pref.startswith("SCI"):
        return {
            "prefixo": pref,
            "numero": num,
            "turno": turno,
            "regra_tipo": "PADRAO_IMPAR",
            "frequencia": "IMPAR",
            "dias_semana": ["TER", "QUI", "SAB"],
            "ordinais": {},
            "qz_ref": None,
            "tem_sufixo_explicito": False,
            "agenda_rotulo": "ÍMPAR (TER/QUI/SÁB)",
            "descricao": "Frequência ÍMPAR — Ter/Qui/Sáb (padrão SCI*)",
        }

    # Prefixos especiais sem sufixo explícito (`SCA*`, `SCS*`, `SCDIST*`, `SCFEIRA*`, `SCRURAL*`, `SCEXTRA*`)
    return {
        "prefixo": pref,
        "numero": num,
        "turno": turno,
        "regra_tipo": "SEM_AGENDA_PADRAO",
        "frequencia": "MANUAL",
        "dias_semana": ORDEM_DIAS[:],
        "ordinais": {},
        "qz_ref": None,
        "tem_sufixo_explicito": False,
        "agenda_rotulo": "AGENDA MANUAL",
        "descricao": "Sem agenda padrão definida no código",
    }


def classificar_setor_sigla(setor_nome: Any, contrato_id: Optional[str] = None) -> Dict[str, Any]:
    """Compatibilidade com chamadas existentes: retorna o diagnóstico completo do setor."""
    info = analisar_nomenclatura_setor(setor_nome, contrato_id=contrato_id)
    pref = info["prefixo"]
    l3 = pref[2] if (pref.startswith("SC") and len(pref) >= 4) else pref
    l4 = pref[3] if (pref.startswith("SC") and len(pref) >= 4) else ""
    return {
        "sigla": pref,
        "l3": l3,
        "l4": l4,
        "freq": info["frequencia"],
        "regra_tipo": info["regra_tipo"],
        "turno": info["turno"],
        "agenda_rotulo": info["agenda_rotulo"],
        "dias_semana": info["dias_semana"],
        "descricao": info["descricao"],
    }


def setor_visivel_na_data(
    setor_nome: Any,
    data_iso: str,
    contrato_id: Optional[str] = None,
    turno_forcado: Optional[str] = None,
    incluir_diario_domingo: bool = True,
) -> bool:
    """
    Avalia rigorosamente se o setor deve ser exibido e contabilizado na data consultada,
    seguindo a nomenclatura oficial de sufixos, intervalos, combinações, ordinais mensais,
    quinzenais (QZ) e padrões por prefixo.

    Quando `incluir_diario_domingo=False` (regra oficial do relatório de Indicadores),
    setores diários padrão (`SCD*` sem sufixo explícito `DOM`) operam de Segunda a Sábado
    (`SEG`..`SAB`), ficando aos Domingos apenas setores explícitos de Domingo (`SCDOM*`, `DOM`).
    """
    dt = _parse_data_iso(data_iso)
    if dt is None:
        return True

    nome_str = str(setor_nome or "").strip()
    if not nome_str or nome_str.upper() == "S/S":
        return True

    info = analisar_nomenclatura_setor(nome_str, contrato_id=contrato_id, turno_forcado=turno_forcado)
    regra = info["regra_tipo"]
    dia_sigla = ORDEM_DIAS[dt.weekday()]

    if regra == "QUINZENAL":
        if dia_sigla not in info["dias_semana"]:
            return False
        ref_str = info.get("qz_ref")
        if not ref_str:
            return True
        ref_dt = _parse_data_iso(ref_str)
        if not ref_dt:
            return True
        dt_monday = dt - timedelta(days=dt.weekday())
        ref_monday = ref_dt - timedelta(days=ref_dt.weekday())
        semanas_diff = (dt_monday - ref_monday).days // 7
        return (semanas_diff % 2) == 0

    if regra == "ORDINAL":
        if dia_sigla in info.get("dias_sem_ordinal", []):
            return True
        ords = info.get("ordinais", {}).get(dia_sigla)
        if not ords:
            return False
        nth, eh_ultimo = _ordinal_dia_no_mes(dt)
        return (str(nth) in ords) or ("L" in ords and eh_ultimo)

    if regra == "PADRAO_DIARIO":
        if not incluir_diario_domingo and dia_sigla == "DOM":
            return False
        return dia_sigla in info["dias_semana"]

    if regra in ("INTERVALO", "DIAS_FIXOS", "PADRAO_DOMINGO", "PADRAO_PAR", "PADRAO_IMPAR"):
        return dia_sigla in info["dias_semana"]

    # SEM_AGENDA_PADRAO (ex.: SCA*, SCS*, SCDIST*, SCFEIRA*, SCRURAL*, SCEXTRA* sem sufixo)
    return True


def info_frequencia_data(data_iso: str) -> Dict[str, Any]:
    """Retorna metadados operacionais completos da data selecionada para exibição no painel."""
    dt = _parse_data_iso(data_iso)
    if dt is None:
        return {"dia_semana": "", "frequencia": "TODOS", "rotulo": "Todos os setores"}

    wd = dt.weekday()
    dia_nome = NOMES_DIAS_PT[wd]
    dia_sigla = ORDEM_DIAS[wd]
    nth, eh_ultimo = _ordinal_dia_no_mes(dt)
    ord_tag = f"{nth}º ({'último' if eh_ultimo else 'ordinário'})"

    if wd in (0, 2, 4):
        return {
            "weekday": wd,
            "dia_semana": dia_nome,
            "dia_sigla": dia_sigla,
            "ocorrencia_mes": nth,
            "eh_ultima_ocorrencia": eh_ultimo,
            "frequencia": "PAR",
            "siglas_esperadas": ["SCPD", "SCPN", "SCD*", dia_sigla],
            "rotulo": f"{dia_nome} · Agenda {dia_sigla} / PAR (SCP*) + Diários (SCD*)",
            "descricao": f"{dia_nome} ({ord_tag}): exibe setores com sufixo {dia_sigla}/QZ/Ordinal do dia, padrão PAR (SCP*) e Diários (SCD*).",
        }
    elif wd in (1, 3, 5):
        return {
            "weekday": wd,
            "dia_semana": dia_nome,
            "dia_sigla": dia_sigla,
            "ocorrencia_mes": nth,
            "eh_ultima_ocorrencia": eh_ultimo,
            "frequencia": "IMPAR",
            "siglas_esperadas": ["SCID", "SCIN", "SCD*", dia_sigla],
            "rotulo": f"{dia_nome} · Agenda {dia_sigla} / ÍMPAR (SCI*) + Diários (SCD*)",
            "descricao": f"{dia_nome} ({ord_tag}): exibe setores com sufixo {dia_sigla}/QZ/Ordinal do dia, padrão ÍMPAR (SCI*) e Diários (SCD*).",
        }
    else:
        return {
            "weekday": wd,
            "dia_semana": dia_nome,
            "dia_sigla": dia_sigla,
            "ocorrencia_mes": nth,
            "eh_ultima_ocorrencia": eh_ultimo,
            "frequencia": "DOMINGO",
            "siglas_esperadas": ["SCDOM*", "SCFEIRA DOM", "SCD*"],
            "rotulo": f"{dia_nome} · Agenda DOM / SCDOM* + Diários (SCD*)",
            "descricao": f"{dia_nome} ({ord_tag}): exibe setores de Domingo (DOM / SCDOM* / SCFEIRA DOM) e rotas Diárias sem restrição (SCD*).",
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
    ]
    for c in candidatos:
        if c and os.path.isdir(c):
            return os.path.abspath(c)
    return None


def _norm_txt(texto: Any) -> str:
    return _normalizar_trecho_agenda(texto)


def extrair_codigo_setor(texto: Any) -> Optional[Tuple[str, int]]:
    """
    Extrai o par canônico (PREFIXO, NUMERO) para casamento com as rotas planejadas.
    Ex.: 'CARUARU - SCPD 85 - SEG e QUA' -> ('SCPD', 85);
         'DOMINGO FEIRA 74' / 'SCFEIRA 74 DOM' -> ('FEIRA', 74);
         'PESQUEIRA - SC DISTRITO QUINTA 09' -> ('SCDIST', 9).
    """
    t = _normalizar_trecho_agenda(texto)
    pref, num, _, _ = _extrair_bloco_identificacao_e_sufixo(t)
    if num is None or pref == "OUTRO":
        m = _RE_CODIGO.search(t)
        if not m:
            return None
        pref = m.group(1)
        num = int(m.group(2))
    if pref.endswith("FEIRA"):
        pref = "FEIRA"
    return pref, int(num)


def extrair_dias_setor(texto: Any) -> Set[str]:
    """Retorna os dias da semana explicitamente informados no sufixo de agenda do setor."""
    info = analisar_nomenclatura_setor(texto)
    if info["tem_sufixo_explicito"]:
        return set(info["dias_semana"])
    return set()


def listar_rotas_cidade(contrato_id: str) -> List[Dict[str, Any]]:
    """Indexa os arquivos de rota planejada da cidade: [{apelido, turno, path, codigo, dias, agenda, mtime}]."""
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
    -> agenda ativa na data consultada -> rota sem restrição de dia.
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
        ativas_dia = [
            r for r in cands
            if r["dias"] and setor_visivel_na_data(r["apelido"], data_iso, turno_forcado=r["turno"])
        ]
        if ativas_dia:
            return sorted(ativas_dia, key=lambda r: len(r["dias"]))[0]
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
    """Assinatura determinística baseada no tamanho dos arquivos (estável entre deploys Docker)."""
    malha = arquivo_malha(contrato_id)
    malha_sig = float(os.path.getsize(malha)) if malha and os.path.isfile(malha) else 0.0
    rotas = listar_rotas_cidade(contrato_id)
    rotas_sig = float(sum(os.path.getsize(r["path"]) for r in rotas if os.path.isfile(r["path"])))
    return malha, malha_sig, rotas_sig, len(rotas)


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
                    INSERT INTO projetos_malha
                        (contrato_id, malha_arquivo, malha_mtime, rotas_dir, rotas_mtime, n_rotas,
                         n_segmentos, km_total, n_prioritarias, n_preservados, construido_em)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT (contrato_id) DO UPDATE SET
                        malha_arquivo = excluded.malha_arquivo,
                        malha_mtime = excluded.malha_mtime,
                        rotas_dir = excluded.rotas_dir,
                        rotas_mtime = excluded.rotas_mtime,
                        n_rotas = excluded.n_rotas,
                        n_segmentos = excluded.n_segmentos,
                        km_total = excluded.km_total,
                        n_prioritarias = excluded.n_prioritarias,
                        n_preservados = excluded.n_preservados,
                        construido_em = CURRENT_TIMESTAMP
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
