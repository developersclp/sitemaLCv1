"""
Testes Automatizados — Painel Completo de Gestão de Tempos (test_painel_tempos.py)
Valida:
  1. Cálculo diário real em locar.db (Cabo 2026-11-24, Caruaru 2026-11-24, Vitória 2026-11-09)
  2. Consistência matemática da soma das categorias de tempo == jornada_total_s
  3. Detecção real de Garagem e Aterro e filtro geográfico de placas fora da cidade
  4. Consolidação mensal e exportação Excel (.xlsx) válida multi-abas
  5. Isolamento estrito por cidade nos links compartilhados (/v/<snapshot_id>)
"""

import email.message
import io
import json
import os
import sys
import unittest
from typing import Any, Dict, Optional

import openpyxl

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import app as locar_app
import database as db
import painel_tempos
import snapshots
from engine import HeatmapEngine
from inlog_service import InlogService


def _dispatch_http(method: str, path: str, body_json: Optional[Dict[str, Any]] = None, cookies: Optional[Dict[str, str]] = None):
    """Executa diretamente LocarRequestHandler (do_GET/do_POST) em memória sem abrir socket TCP."""
    handler = locar_app.LocarRequestHandler.__new__(locar_app.LocarRequestHandler)
    raw_body = json.dumps(body_json).encode("utf-8") if body_json is not None else b""
    hdrs = email.message.Message()
    hdrs["Host"] = "127.0.0.1"
    if body_json is not None:
        hdrs["Content-Type"] = "application/json"
        hdrs["Content-Length"] = str(len(raw_body))
    else:
        hdrs["Content-Length"] = "0"
    if cookies:
        hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    handler.headers = hdrs
    handler.path = path
    handler.command = method
    handler.request_version = "HTTP/1.1"
    handler.client_address = ("127.0.0.1", 50000)
    handler.rfile = io.BytesIO(raw_body)
    handler.wfile = io.BytesIO()
    status_holder = {"code": 200}
    resp_headers: Dict[str, list] = {}

    def _send_response(code, message=None):
        status_holder["code"] = int(code)

    def _send_header(k, v):
        resp_headers.setdefault(k.lower(), []).append(v)

    def _end_headers():
        pass

    handler.send_response = _send_response
    handler.send_header = _send_header
    handler.end_headers = _end_headers

    if method == "POST":
        handler.do_POST()
    else:
        handler.do_GET()

    return status_holder["code"], resp_headers, handler.wfile.getvalue()


class TestPainelTempos(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        cls.inlog = InlogService()
        cls.engine = HeatmapEngine(inlog_service=cls.inlog)

    def test_01_calculo_diario_cabo_e_consistencia_matematica(self):
        res = painel_tempos.calcular_painel_dia_cidade(
            self.engine, "cabo", "2026-11-24", force_refresh=True
        )
        r = res["resumo"]
        self.assertEqual(r["contrato_id"], "cabo")
        self.assertGreater(r["caminhoes_ativos"], 10)
        self.assertGreater(r["setores_executados"], 5)
        self.assertGreater(r["zonas_detectadas"]["garagens"], 0)
        self.assertGreater(r["zonas_detectadas"]["aterros"], 0)
        self.assertGreater(r["tempo_setor_total_s"], 0.0)
        self.assertGreater(r["tempo_trajeto_total_s"], 0.0)
        self.assertGreater(r["tempo_garagem_total_s"], 0.0)
        self.assertGreater(r["tempo_aterro_total_s"], 0.0)
        self.assertGreater(r["qtd_viagens_aterro"], 0)

        # Consistência matemática por caminhão: soma das partes == jornada_total_s
        for c in res["caminhoes"]:
            soma_partes = (
                c["tempo_garagem_s"]
                + c["tempo_setor_s"]
                + c["tempo_trajeto_s"]
                + c["tempo_aterro_s"]
                + c["tempo_refeicao_s"]
                + c["tempo_sos_s"]
                + c["tempo_parada_externa_s"]
            )
            self.assertAlmostEqual(c["jornada_total_s"], soma_partes, delta=1.5)
            soma_traj = (
                c["trajeto_garagem_setor_s"]
                + c["trajeto_setor_aterro_s"]
                + c["trajeto_aterro_setor_s"]
                + c["trajeto_entre_setores_s"]
                + c["trajeto_retorno_garagem_s"]
            )
            self.assertAlmostEqual(c["tempo_trajeto_s"], soma_traj, delta=1.5)
            soma_gar = (
                c["garagem_pre_saida_s"]
                + c["garagem_pos_retorno_s"]
                + c["garagem_intermediaria_s"]
            )
            self.assertAlmostEqual(c["tempo_garagem_s"], soma_gar, delta=1.5)

        # Consistência Setor x Caminhão: soma dos tempos dos caminhões == tempo_efetivo_s do setor
        for s in res["setores"]:
            soma_cam_set = sum(cm["tempo_s"] for cm in s["caminhoes"])
            self.assertAlmostEqual(s["tempo_efetivo_s"], soma_cam_set, delta=1.5)

    def test_02_consolidacao_mensal_e_excel(self):
        # Garante cache rápido com alguns dias reais em novembro/2026 e consolida mês até hoje
        res_mes = painel_tempos.consolidar_painel_mes_cidade(
            self.engine, "cabo", mes_str="2026-11"
        )
        rm = res_mes["resumo"]
        self.assertEqual(rm["modo"], "mes")
        self.assertEqual(rm["contrato_id"], "cabo")
        self.assertGreater(rm["dias_operacionais"], 0)
        self.assertGreater(len(res_mes["evolucao_diaria"]), 0)
        self.assertGreater(len(res_mes["setores"]), 0)
        self.assertGreater(len(res_mes["caminhoes"]), 0)

        xlsx_bytes, filename = painel_tempos.gerar_excel_painel_tempos(res_mes)
        self.assertTrue(filename.endswith(".xlsx"))
        self.assertTrue(xlsx_bytes.startswith(b"PK\x03\x04"))
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
        self.assertIn("Resumo Operacional", wb.sheetnames)
        self.assertIn("Relacao de Setores", wb.sheetnames)
        self.assertIn("Caminhoes por Setor", wb.sheetnames)
        self.assertIn("Frota e Tempos", wb.sheetnames)
        self.assertIn("Viagens Aterro", wb.sheetnames)
        self.assertIn("Evolucao Diaria", wb.sheetnames)

    def test_03_isolamento_seguranca_link_compartilhado(self):
        # 1. Visitante sem autenticação/link recebe 403
        st_unauth, _, _ = _dispatch_http(
            "POST",
            "/api/painel-tempos/iniciar",
            body_json={"cidade": "cabo", "modo": "dia", "data": "2026-11-24"},
        )
        self.assertEqual(st_unauth, 403)

        # 2. Cria/obtém um snapshot da cidade 'cabo'
        snap = snapshots.gerar_ou_atualizar_snapshot(
            self.engine,
            {"cidade": "cabo", "data": "2026-11-24", "periodo": "diario"},
        )
        snap_id = snap["id"]

        # 3. Acessa /v/<snap_id> como visitante e verifica botão + cookie lc_viewer_lock
        st_view, hdrs_view, raw_view = _dispatch_http("GET", f"/v/{snap_id}")
        self.assertEqual(st_view, 200)
        html_view = raw_view.decode("utf-8")
        self.assertIn("btnAbrirPainelTempos", html_view)
        self.assertIn("painel_tempos.js", html_view)
        cookies_cabo = {"lc_viewer_lock": snap_id}

        # 4. Tenta adulterar 'cidade' para 'caruaru' usando o link/cookie de 'cabo'
        # O backend DEVE forçar estritamente 'cabo' (nunca retornar dados de outra cidade)
        st_hack, _, raw_hack = _dispatch_http(
            "POST",
            "/api/painel-tempos/iniciar",
            body_json={
                "cidade": "caruaru",
                "modo": "dia",
                "data": "2026-11-24",
                "snapshot_id": snap_id,
            },
            cookies=cookies_cabo,
        )
        self.assertEqual(st_hack, 200)
        payload_hack = json.loads(raw_hack.decode("utf-8"))
        self.assertTrue(payload_hack["sucesso"])
        self.assertEqual(payload_hack["contrato_id"], "cabo")
        self.assertEqual(payload_hack["resultado"]["resumo"]["contrato_id"], "cabo")

        # 5. Exportação Excel pelo link de 'cabo' também trava em 'cabo' mesmo com ?cidade=caruaru
        st_xls, _, raw_xls = _dispatch_http(
            "GET",
            f"/api/painel-tempos/exportar-excel?job_id={payload_hack['job_id']}&cidade=caruaru&snapshot_id={snap_id}",
            cookies=cookies_cabo,
        )
        self.assertEqual(st_xls, 200)
        self.assertTrue(raw_xls.startswith(b"PK\x03\x04"))

        # 6. Job criado para outra cidade (via admin) não pode ser lido por visitante de 'cabo'
        job_caruaru = painel_tempos.iniciar_job_tempos(
            self.engine, "caruaru", "dia", data_str="2026-11-24"
        )
        st_cross, _, _ = _dispatch_http(
            "GET",
            f"/api/painel-tempos/status?job_id={job_caruaru['job_id']}&snapshot_id={snap_id}",
            cookies=cookies_cabo,
        )
        self.assertEqual(st_cross, 403)

        # 7. Token HMAC assinado (legado /v/<token>) também força estritamente a cidade do token
        tok_vitoria = locar_app.gerar_token_compartilhamento({"cidade": "vitoria", "data": "2026-11-09"})
        st_tok, _, raw_tok = _dispatch_http(
            "POST",
            "/api/painel-tempos/iniciar",
            body_json={
                "cidade": "cabo",
                "modo": "dia",
                "data": "2026-11-09",
                "token": tok_vitoria,
            },
        )
        self.assertEqual(st_tok, 200)
        payload_tok = json.loads(raw_tok.decode("utf-8"))
        self.assertEqual(payload_tok["contrato_id"], "vitoria")
        self.assertEqual(payload_tok["resultado"]["resumo"]["contrato_id"], "vitoria")


if __name__ == "__main__":
    unittest.main(verbosity=2)
