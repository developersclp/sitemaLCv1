"""
Suíte de Testes de Integração e Veracidade de Dados Inlog (test_integracao_inlog.py)
Desenvolvido para sitemaLCv1 - Locar Saneamento Ambiental.

Valida todos os 10 endpoints da API oficial Inlog, persistência SQLite,
motor geométrico determinístico (zero random), operações GIS e rotas HTTP de app.py.
"""

import os
import sys
import json
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import database as db
from inlog_service import InlogService, identificar_contrato, formatar_placa_hifen
from engine import HeatmapEngine
import app as locar_app


class TestIntegracaoInlog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inlog = InlogService()
        cls.engine = HeatmapEngine(inlog_service=cls.inlog)
        cls.hoje = datetime.now().strftime("%Y-%m-%d")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 18089), locar_app.LocarRequestHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.local_url = "http://127.0.0.1:18089"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_01_autenticacao_oauth2(self):
        self.assertTrue(self.inlog.is_configured(), "Credenciais Inlog devem estar configuradas no .env")
        token = self.inlog.get_token(force=True)
        self.assertIsNotNone(token)
        self.assertNotEqual(token, "TOKEN_SIMULADO_LOCAR")
        self.assertGreater(len(token), 20)

    def test_02_normalizacao_contratos_e_placas(self):
        self.assertEqual(identificar_contrato("JABOATAO - DOMICILIAR"), "jaboatao")
        self.assertEqual(identificar_contrato("CABO DE SANTO AGOSTINHO - CAIXA BROOKS"), "cabo")
        self.assertEqual(identificar_contrato("VITORIA DE SANTO ANTAO - DOMICILIAR"), "vitoria")
        self.assertEqual(identificar_contrato("CUIABÁ - ENTULHO"), "cuiaba")
        self.assertEqual(identificar_contrato("JEQUIE-DOMICILIAR"), "jequie")
        self.assertEqual(identificar_contrato("VÁRZEA GRANDE - DOMICILIAR"), "varzea")
        self.assertEqual(identificar_contrato("NOVO HAMBURGO - DOMICILIAR"), "novo hamburgo")

        self.assertEqual(formatar_placa_hifen("RIQ7B84"), "RIQ-7B84")
        self.assertEqual(formatar_placa_hifen("riq-7b84"), "RIQ-7B84")
        self.assertEqual(formatar_placa_hifen(" PDC-5809 "), "PDC-5809")

    def test_03_todos_10_endpoints_inlog(self):
        # 1. Veiculos/TempoReal
        veic_cabo = self.inlog.get_veiculos_tempo_real("cabo", operacao="COLETA", force_refresh=True)
        self.assertGreater(len(veic_cabo), 0, "Cabo deve retornar veículos reais em Veiculos/TempoReal")
        veic_jab = self.inlog.get_veiculos_tempo_real("jaboatao", operacao="COLETA")
        self.assertGreater(len(veic_jab), 0, "Jaboatão deve retornar veículos reais em Veiculos/TempoReal")
        self.assertTrue(any(v["placaVeiculo"] in ("LVE-5J16", "DGG-8F39") for v in veic_jab))

        # 2. Setores/TempoReal (usa cache se recente ou busca na API com timeout 60s)
        setores_rt = self.inlog.get_setores_tempo_real("cabo")
        self.assertIsInstance(setores_rt, list)
        self.assertGreater(len(setores_rt), 0, "Setores/TempoReal deve retornar setores de Cabo")

        # 3. RelatorioDiario
        rd_gar = self.inlog.get_relatorio_diario("2026-11-15", "garanhuns")
        self.assertIsInstance(rd_gar, list)

        # 4. Veiculos (Cadastro)
        cad_veic = self.inlog.get_cadastro_veiculos()
        self.assertGreater(len(cad_veic), 100)

        # 5. Setores (Cadastro)
        cad_set = self.inlog.get_cadastro_setores("cabo")
        self.assertGreater(len(cad_set), 0)

        # 6. Motoristas (Cadastro)
        cad_mot = self.inlog.get_cadastro_motoristas()
        self.assertGreater(len(cad_mot), 100)

        # 7. Alarmes (inclui teste com data de hoje respeitando regra dos 5 minutos)
        alarmes_hoje = self.inlog.get_alarmes(self.hoje)
        self.assertIsInstance(alarmes_hoje, list)
        alarmes_ontem = self.inlog.get_alarmes("2026-11-15", "olinda")
        self.assertIsInstance(alarmes_ontem, list)

        # 8. Historico GPS (placa ativa hoje em Cabo)
        placas_ativas = [v["placaVeiculo"] for v in veic_cabo if self.hoje in str(v.get("dataHoraAtualizacao", ""))]
        self.assertGreater(len(placas_ativas), 0, "Deve haver ao menos 1 veículo de Cabo ativo hoje")
        gps_pts = self.inlog.get_historico_gps(placas_ativas[0], self.hoje)
        self.assertIsInstance(gps_pts, list)
        self.assertGreater(len(gps_pts), 0, f"Placa {placas_ativas[0]} deve retornar pontos GPS reais hoje")
        self.assertIn("latitude", gps_pts[0])
        self.assertIn("longitude", gps_pts[0])
        self.assertIn("velocidade", gps_pts[0])

        # 9. Falha
        falhas = self.inlog.get_falhas(self.hoje)
        self.assertIsInstance(falhas, list)

        # 10. Telemetria
        tel = self.inlog.get_telemetria("2026-11-15", contrato_id="cabo")
        self.assertIsInstance(tel, list)

    def test_04_motor_espacial_deterministico_e_reconciliacao(self):
        # Valida geometria unitária determinística da grade espacial
        regras = {"buffer_m": 25.0, "vel_min": 2.0, "vel_max": 25.0, "vel_passagem_max": 38.0, "dwell_s": 180.0}
        gps_sim = {
            "TST-1234": [
                {"dataHora": f"{self.hoje}T08:00:00", "latitude": -8.283300, "longitude": -35.033300, "velocidade": 12.0},
                {"dataHora": f"{self.hoje}T08:00:10", "latitude": -8.283300, "longitude": -35.032900, "velocidade": 12.0},
            ]
        }
        pass_g, _, _, qtd = self.engine._construir_grade_espacial(gps_sim, -8.2833, -35.0333, regras)
        self.assertEqual(qtd, 2)
        amostras_rua = self.engine._amostrar_segmento_xy(
            [[-35.0333, -8.2833], [-35.0329, -8.2833]], -8.2833, -35.0333, passo_m=10.0
        )
        matched, placas_match = self.engine._consultar_grade(amostras_rua, pass_g, 25.0)
        self.assertGreaterEqual(matched, 1)
        self.assertEqual(placas_match, {"TST-1234"})

        # Rua distante deve ter 0 match
        amostras_longe = self.engine._amostrar_segmento_xy(
            [[-35.0500, -8.3000], [-35.0490, -8.3000]], -8.2833, -35.0333, passo_m=10.0
        )
        matched_longe, placas_longe = self.engine._consultar_grade(amostras_longe, pass_g, 25.0)
        self.assertEqual(matched_longe, 0)
        self.assertEqual(placas_longe, set())

        # Valida auditoria real em Cabo (determinística e com filtro de frequência do dia Par/Ímpar)
        conn = db.get_connection()
        qtd_cabo_total = conn.execute("SELECT COUNT(*) FROM segmentos_viarios WHERE contrato_id = 'cabo'").fetchone()[0]
        conn.close()
        self.assertGreater(qtd_cabo_total, 10000)

        res1 = self.engine.processar_ou_obter_auditoria("cabo", self.hoje, forcar_reprocessamento=False)
        res2 = self.engine.processar_ou_obter_auditoria("cabo", self.hoje, forcar_reprocessamento=False)
        self.assertEqual(res1["resumo"]["pct_cobertura"], res2["resumo"]["pct_cobertura"])
        self.assertGreater(res1["resumo"]["total_vias"], 10000)
        self.assertLessEqual(res1["resumo"]["total_vias"], qtd_cabo_total)
        self.assertGreater(res1["resumo"]["total_pontos_gps"], 0)
        self.assertEqual(res1["resumo"]["status_inlog"], "ONLINE (INLOG OFICIAL)")
        self.assertIn("planejado_vs_executado", res1)
        self.assertIn("frequencia_dia", res1["resumo"])
        self.assertEqual(res1["resumo"]["frequencia_dia"]["frequencia"], "PAR")

    def test_05_persistencia_gis_sqlite(self):
        criado = db.salvar_ou_atualizar_segmento({
            "contrato_id": "varzea",
            "nome_rua": "Rua Teste Integração Inlog",
            "setor": "SETOR-TST",
            "tipo": "COLETA",
            "is_manual": False,
            "is_priority": True,
            "coordinates": [[-56.1325, -15.6467], [-56.1315, -15.6467]],
        })
        seg_id = criado["id"]
        self.assertGreater(seg_id, 0)
        self.assertGreater(criado["extensao_m"], 90.0)
        self.assertEqual(criado["is_priority"], 1)

        atualizado = db.salvar_ou_atualizar_segmento({
            "id": seg_id,
            "contrato_id": "varzea",
            "nome_rua": "Rua Teste Atualizada",
            "setor": "SETOR-TST-2",
            "tipo": "MANUAL",
            "is_manual": True,
            "is_priority": False,
        })
        self.assertEqual(atualizado["nome_rua"], "Rua Teste Atualizada")
        self.assertEqual(atualizado["is_manual"], 1)

        removido = db.excluir_segmento(seg_id)
        self.assertTrue(removido)

    def test_06_rotas_http_app_py(self):
        session = requests.Session()
        login_resp = session.post(
            f"{self.local_url}/api/auth/login",
            json={"password": "filialsaopaulo01"},
            timeout=10,
        )
        self.assertEqual(login_resp.status_code, 200, "Falha ao autenticar no painel central")

        rotas_get = [
            ("/", "text/html"),
            ("/api/status", "application/json"),
            (f"/api/auditoria?cidade=cabo&data={self.hoje}", "application/json"),
            (f"/api/inlog/visao-geral?data={self.hoje}", "application/json"),
            ("/api/inlog/veiculos-tempo-real?cidade=cabo", "application/json"),
            (f"/api/inlog/rastro-gps?cidade=cabo&data={self.hoje}", "application/json"),
            ("/api/inlog/setores?cidade=cabo", "application/json"),
            ("/api/inlog/relatorio-diario?cidade=garanhuns&data=2026-11-15", "application/json"),
            ("/api/inlog/alarmes-falhas?cidade=olinda&data=2026-11-15", "application/json"),
            ("/api/inlog/telemetria?cidade=cabo&data=2026-11-15", "application/json"),
            ("/api/inlog/motoristas", "application/json"),
            ("/api/inlog/logs", "application/json"),
            ("/api/rota-planejada?cidade=caruaru&setor=SCPD%2020", "application/json"),
            ("/api/malha-viaria?cidade=caruaru&bbox=-35.99,-8.30,-35.96,-8.27", "application/json"),
            ("/api/exportar-geojson?cidade=cabo", "application/geo+json"),
        ]

        audit_id = None
        for path, expected_ct in rotas_get:
            resp = session.get(f"{self.local_url}{path}", timeout=65)
            self.assertEqual(resp.status_code, 200, f"Falha na rota {path}: HTTP {resp.status_code}")
            self.assertIn(expected_ct, resp.headers.get("Content-Type", ""))

            if expected_ct.startswith("application/") and path != "/api/exportar-geojson":
                js = resp.json()
                if path.startswith("/api/auditoria"):
                    audit_id = js["resumo"]["id"]
                    self.assertGreater(js["resumo"]["total_vias"], 10000)
                    self.assertGreater(js["resumo"]["total_pontos_gps"], 0)
                    self.assertIn("planejado_vs_executado", js)
                elif path.startswith("/api/rota-planejada"):
                    self.assertTrue(js["sucesso"])
                    self.assertGreater(js["km_coleta"], 0)
                    self.assertGreater(len(js["geojson"]["features"]), 0)
                elif path.startswith("/api/malha-viaria"):
                    self.assertGreater(len(js["features"]), 0)
                elif path.startswith("/api/inlog/visao-geral"):
                    self.assertGreater(js["total_veiculos_frota"], 200)
                elif path.startswith("/api/inlog/veiculos-tempo-real"):
                    self.assertGreater(js["total"], 0)
                elif path.startswith("/api/inlog/rastro-gps"):
                    self.assertGreater(js["total_pontos"], 0)
                elif path.startswith("/api/inlog/logs"):
                    self.assertGreater(len(js["logs"]), 0)

        self.assertIsNotNone(audit_id)
        csv_resp = session.get(f"{self.local_url}/api/exportar-csv?id={audit_id}", timeout=15)
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn("text/csv", csv_resp.headers.get("Content-Type", ""))
        self.assertIn("Logradouro / Rua", csv_resp.text)

    def test_07_link_seguro_turno_e_coleta_manual(self):
        import malha_rotas

        # 1. Classificação Diurno/Noturno pela sigla
        self.assertEqual(malha_rotas.classificar_setor_sigla("SCPD 10")["turno"], "DIURNO")
        self.assertEqual(malha_rotas.classificar_setor_sigla("SCPN 02")["turno"], "NOTURNO")

        # 2. Presença dos novos componentes e métricas na interface HTML autenticada
        session = requests.Session()
        session.post(
            f"{self.local_url}/api/auth/login",
            json={"password": "filialsaopaulo01"},
            timeout=10,
        )
        html_resp = session.get(f"{self.local_url}/", timeout=15)
        self.assertEqual(html_resp.status_code, 200)
        for elemento in (
            "filterTurno",
            "selectTurno",
            "btnBaixarCsvLink",
            "secCamadasInlog",
            "Coleta Manual",
            "Coleta (Domiciliar + Manual)",
        ):
            self.assertIn(elemento, html_resp.text)

        # 3. Sem autenticação /api/auditoria bloqueia (401); com sessão autenticada funciona
        unauth_aud = requests.get(f"{self.local_url}/api/auditoria?cidade=cabo&data={self.hoje}", timeout=15)
        self.assertEqual(unauth_aud.status_code, 401)

        aud = session.get(f"{self.local_url}/api/auditoria?cidade=cabo&data={self.hoje}", timeout=30).json()
        self.assertIn("km_manual", aud["resumo"])
        self.assertIn("vias_manual", aud["resumo"])
        self.assertIn("pct_manual", aud["resumo"])
        audit_id = aud["resumo"]["id"]

        # 4. Link Assinado Exclusivo (/v/<token>) e Bloqueio do Painel Central
        gen = session.post(
            f"{self.local_url}/api/compartilhar/gerar",
            json={"cidade": "cabo", "data": self.hoje, "turno": "DIURNO", "coleta": "all", "setores": []},
            timeout=10,
        )
        self.assertEqual(gen.status_code, 200)
        gen_js = gen.json()
        self.assertTrue(gen_js["sucesso"])
        link_path = gen_js["path"]
        self.assertTrue(link_path.startswith("/v/"))

        # Sessão de destinatário abrindo o link
        sess = requests.Session()
        view_resp = sess.get(f"{self.local_url}{link_path}", timeout=15)
        self.assertEqual(view_resp.status_code, 200)
        self.assertIn('"active": true', view_resp.text)
        self.assertIn('"cidade": "cabo"', view_resp.text)

        # Token adulterado deve retornar 403
        tampered = requests.get(f"{self.local_url}{link_path}X", timeout=10)
        self.assertEqual(tampered.status_code, 403)

        # Destinatário tentando apagar parte da URL e ir para raiz (/) é bloqueado
        root_try = sess.get(f"{self.local_url}/", allow_redirects=False, timeout=10)
        self.assertEqual(root_try.status_code, 302)
        self.assertIn("/login?locked=1", root_try.headers.get("Location", ""))

        # Rotas centrais/escrita bloqueadas na sessão de link
        vg_try = sess.get(f"{self.local_url}/api/inlog/visao-geral", timeout=10)
        self.assertEqual(vg_try.status_code, 403)

        # CSV da cidade do link continua acessível para o destinatário
        csv_view = sess.get(f"{self.local_url}/api/exportar-csv?id={audit_id}", timeout=15)
        self.assertEqual(csv_view.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
