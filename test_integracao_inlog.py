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
import malha_rotas
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
        freq_esperada = malha_rotas.info_frequencia_data(self.hoje)["frequencia"]
        self.assertEqual(res1["resumo"]["frequencia_dia"]["frequencia"], freq_esperada)

    def test_05_persistencia_gis_sqlite(self):
        criado = db.salvar_ou_atualizar_segmento({
            "contrato_id": "cabo",
            "nome_rua": "Rua Teste Integração Inlog",
            "setor": "SETOR-TST",
            "tipo": "COLETA",
            "is_manual": False,
            "is_priority": True,
            "coordinates": [[-35.0333, -8.2833], [-35.0323, -8.2833]],
        })
        seg_id = criado["id"]
        self.assertGreater(seg_id, 0)
        self.assertGreater(criado["extensao_m"], 90.0)
        self.assertEqual(criado["is_priority"], 1)

        atualizado = db.salvar_ou_atualizar_segmento({
            "id": seg_id,
            "contrato_id": "cabo",
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

    def test_08_auditoria_todas_as_cidades(self):
        """Valida a consolidação de todas as cidades e o centro nacional do mapa."""
        res_all = self.engine.processar_ou_obter_auditoria("all", self.hoje, forcar_reprocessamento=False)
        self.assertIn("resumo", res_all)
        self.assertIn("geojson", res_all)
        self.assertEqual(res_all["resumo"]["contrato_id"], "all")
        self.assertEqual(res_all["resumo"]["centro"], [-14.2350, -51.9253])
        self.assertEqual(res_all["resumo"]["zoom"], 4)
        self.assertGreater(len(res_all["geojson"]["features"]), 10000)
        self.assertGreater(res_all["resumo"]["km_total"], 0)
        self.assertIn("resumos_cidades", res_all["resumo"])
        self.assertGreaterEqual(len(res_all["resumo"]["resumos_cidades"]), 10)

    def test_09_nomenclatura_setores_completa(self):
        """Valida rigorosamente todas as regras de nomenclatura, sufixos, ordinais, QZ, prefixos e turnos."""
        # Novembro 2026:
        # 2026-11-02 (Seg, 1ª seg), 2026-11-03 (Ter, 1ª ter), 2026-11-04 (Qua, 1ª qua),
        # 2026-11-05 (Qui, 1ª qui), 2026-11-06 (Sex, 1ª sex), 2026-11-07 (Sab, 1º sab), 2026-11-08 (Dom, 2º dom)
        # 1. Dias individuais
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCAD 25 SEG", "2026-11-02"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCAD 25 SEG", "2026-11-03"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCAD 26 TER", "2026-11-03"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCPD 99 QUA", "2026-11-04"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCPD 99 QUA", "2026-11-02"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCDIST 11 QUI", "2026-11-05"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCPD 100 SEX", "2026-11-06"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCFEIRA 12 SAB", "2026-11-07"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCDOM 28 DOM", "2026-11-08"))

        # 2. Combinações com espaço e com "E"
        for d in ("2026-11-02", "2026-11-04", "2026-11-06"):
            self.assertTrue(malha_rotas.setor_visivel_na_data("SCPD 01 SEG QUA SEX", d))
            self.assertTrue(malha_rotas.setor_visivel_na_data("SCPD 01 SEG E QUA E SEX", d))
            self.assertFalse(malha_rotas.setor_visivel_na_data("SCID 01 TER QUI SAB", d))
        for d in ("2026-11-03", "2026-11-05", "2026-11-07"):
            self.assertTrue(malha_rotas.setor_visivel_na_data("SCID 01 TER QUI SAB", d))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCDD 04 TER e SAB", "2026-11-03"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCDD 04 TER e SAB", "2026-11-07"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCDD 04 TER e SAB", "2026-11-04"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCPD 39 QUA e SEX", "2026-11-04"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCSBD 01 TER e QUI", "2026-11-05"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCSCD 05 QUA e SEX", "2026-11-06"))

        # 3. Intervalos com "A"
        for d in ("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"):
            self.assertTrue(malha_rotas.setor_visivel_na_data("SCDN 19 SEG A SEX", d))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCDN 19 SEG A SEX", "2026-11-07"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCDN 01 SEG A SAB", "2026-11-07"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCDN 01 SEG A SAB", "2026-11-08"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCDD 01 TER A QUI", "2026-11-04"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCDD 01 TER A QUI", "2026-11-06"))

        # 4. Ordinais no mês (1, 2, 3, 4, L)
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCRURAL 1 SAB 1", "2026-11-07"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCRURAL 1 SAB 1", "2026-11-14"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCRURAL 2 SAB 2", "2026-11-14"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCRURAL 2 SAB 2", "2026-11-07"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCRURAL 3 SAB L", "2026-11-28"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCRURAL 3 SAB L", "2026-11-21"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCAD 10 TER 1 3", "2026-11-03"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCAD 10 TER 1 3", "2026-11-10"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCAD 10 TER 1 3", "2026-11-17"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCAD 11 QUA L SAB 3 L", "2026-11-25"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCAD 11 QUA L SAB 3 L", "2026-11-21"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCAD 11 QUA L SAB 3 L", "2026-11-28"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCAD 11 QUA L SAB 3 L", "2026-11-14"))

        # 5. Quinzenal (QZ)
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCAD 20 SEG QZ 06.04.2026", "2026-04-06"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCAD 20 SEG QZ 06.04.2026", "2026-04-13"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCAD 20 SEG QZ 06.04.2026", "2026-04-20"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCAD 21 SAB QZ 04.04.2026", "2026-04-04"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCAD 21 SAB QZ 04.04.2026", "2026-04-11"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCAD 21 SAB QZ 04.04.2026", "2026-04-18"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCDIST 12 QUI QZ 07.05.2026", "2026-05-07"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCDIST 12 QUI QZ 07.05.2026", "2026-05-14"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCDIST 09 QUI QZ 14.05.2026", "2026-05-14"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCDIST 09 QUI QZ 14.05.2026", "2026-05-07"))

        # 6. Padrão por prefixo (sem sufixo explícito) + caso DOM BOSCO não confundir DOM
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCDD 01", "2026-11-08"))  # SCD* é diário (todos os dias)
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCDN 22", "2026-11-08"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCPD 06", "2026-11-02"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCPD 06", "2026-11-03"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCPV 16", "2026-11-04"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCID 04", "2026-11-03"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCID 04", "2026-11-04"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("CUIABA - SCID 54 - REGIAO DO DOM BOSCO", "2026-11-03"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("CUIABA - SCID 54 - REGIAO DO DOM BOSCO", "2026-11-08"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCDOM 31", "2026-11-08"))
        self.assertFalse(malha_rotas.setor_visivel_na_data("SCDOM 31", "2026-11-07"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCDOMD 43", "2026-11-08"))
        self.assertTrue(malha_rotas.setor_visivel_na_data("SCDOMN 46", "2026-11-08"))
        self.assertEqual(malha_rotas.analisar_nomenclatura_setor("SCSBD 01")["regra_tipo"], "SEM_AGENDA_PADRAO")

        # 7. Turnos (DIURNO vs NOTURNO)
        self.assertEqual(malha_rotas.analisar_nomenclatura_setor("SCDOMD 43")["turno"], "DIURNO")
        self.assertEqual(malha_rotas.analisar_nomenclatura_setor("SCDOMN 46")["turno"], "NOTURNO")
        self.assertEqual(malha_rotas.analisar_nomenclatura_setor("SCPV 16")["turno"], "DIURNO")
        self.assertEqual(malha_rotas.analisar_nomenclatura_setor("SCPM 45")["turno"], "NOTURNO")
        self.assertEqual(malha_rotas.analisar_nomenclatura_setor("SCSBD 01 TER e QUI")["turno"], "DIURNO")

    def test_10_janela_operacional_e_velocidade_cidades(self):
        """Valida janela operacional por cidade (05:00->05:00 + exceções Cuiabá/Olinda/Paulista) e cobertura real."""
        from inlog_service import janela_operacional_cidade

        wc = janela_operacional_cidade("cuiaba", "2026-11-16")
        self.assertEqual(wc["data_inicio"], "2026-11-16T04:00:00")
        self.assertEqual(wc["data_fim"], "2026-11-17T05:00:00")
        self.assertEqual(wc["diurno_fim"], "2026-11-16T17:00:00")

        wo = janela_operacional_cidade("olinda", "2026-11-16")
        self.assertEqual(wo["data_inicio"], "2026-11-16T05:00:00")
        self.assertEqual(wo["data_fim"], "2026-11-17T07:00:00")

        wp = janela_operacional_cidade("paulista", "2026-11-16")
        self.assertEqual(wp["data_inicio"], "2026-11-16T05:00:00")
        self.assertEqual(wp["data_fim"], "2026-11-17T07:00:00")

        wk = janela_operacional_cidade("cabo", "2026-11-16")
        self.assertEqual(wk["data_inicio"], "2026-11-16T05:00:00")
        self.assertEqual(wk["data_fim"], "2026-11-17T05:00:00")

        res_cui = self.engine.processar_ou_obter_auditoria("cuiaba", "2026-11-16", forcar_reprocessamento=False)
        mapa_setores = {s["codigo"]: s["pct_cobertura"] for s in res_cui["setores"]}
        self.assertGreaterEqual(mapa_setores.get("SCPM 50", 0), 98.0)
        self.assertGreaterEqual(mapa_setores.get("SCPM 49", 0), 98.0)
        self.assertGreaterEqual(mapa_setores.get("SCPD 12 SEG", 0), 95.0)

    def test_11_pesqueira_veiculo_regional_e_setores(self):
        """Valida inclusão de veículos emprestados entre regionais (ex: PCE-7467 de Garanhuns operando Pesqueira)."""
        res_pesq = self.engine.processar_ou_obter_auditoria(
            "pesqueira", "2026-11-16", turno="TODOS", forcar_reprocessamento=False
        )
        self.assertIn("PCE-7467", res_pesq["resumo"]["placas_ativas"])
        mapa = {s["codigo"]: s["pct_cobertura"] for s in res_pesq["setores"]}
        self.assertGreaterEqual(mapa.get("SCDIST 13 SEG", 0), 99.0)
        self.assertGreaterEqual(mapa.get("SCPD 01", 0), 98.0)
        self.assertGreaterEqual(mapa.get("SCPD 02", 0), 95.0)
        self.assertGreaterEqual(mapa.get("SCPN 03", 0), 95.0)

    def test_12_link_periodo_quinzenal_mensal_e_manual_azul(self):
        """Valida geração de link quinzenal/mensal, trava contra alteração na URL e paleta azul para coleta manual."""
        from engine import HeatmapEngine

        # 1. Paleta por % (Normal e Manual Azul)
        self.assertEqual(HeatmapEngine._cor_por_percentual(95.0, False), "#00E600")
        self.assertEqual(HeatmapEngine._cor_por_percentual(70.0, False), "#FFFF00")
        self.assertEqual(HeatmapEngine._cor_por_percentual(50.0, False), "#FF9900")
        self.assertEqual(HeatmapEngine._cor_por_percentual(30.0, False), "#FF0000")
        self.assertEqual(HeatmapEngine._cor_por_percentual(10.0, False), "#660000")

        self.assertEqual(HeatmapEngine._cor_por_percentual(95.0, True), "#00FFFF")
        self.assertEqual(HeatmapEngine._cor_por_percentual(70.0, True), "#00BFFF")
        self.assertEqual(HeatmapEngine._cor_por_percentual(50.0, True), "#0080FF")
        self.assertEqual(HeatmapEngine._cor_por_percentual(30.0, True), "#0040FF")
        self.assertEqual(HeatmapEngine._cor_por_percentual(10.0, True), "#001F66")

        # 2. UI possui modalSharePeriodo, legendPeriodo e Coleta Manual em azul (#0284C7)
        session = requests.Session()
        session.post(f"{self.local_url}/api/auth/login", json={"password": "filialsaopaulo01"}, timeout=10)
        html = session.get(f"{self.local_url}/", timeout=15).text
        for item in ("modalSharePeriodo", "legendPeriodo", "#0284C7"):
            self.assertIn(item, html)

        # 3. Gera link de 2ª Quinzena travado (quem acessa não consegue forçar periodo=diario na URL)
        gen = session.post(
            f"{self.local_url}/api/compartilhar/gerar",
            json={
                "cidade": "pesqueira",
                "data": "2026-11-16",
                "periodo": "quinzena2",
                "turno": "all",
                "coleta": "all",
                "setores": [],
            },
            timeout=10,
        ).json()
        self.assertTrue(gen["sucesso"])
        token = gen["token"]

        sess_view = requests.Session()
        view_html = sess_view.get(f"{self.local_url}{gen['path']}", timeout=15).text
        self.assertIn('"periodo": "quinzena2"', view_html)

        aud_view = sess_view.get(
            f"{self.local_url}/api/auditoria?cidade=cabo&data=2026-11-10&periodo=diario&token={token}",
            timeout=60,
        ).json()
        self.assertEqual(aud_view["resumo"]["contrato_id"], "pesqueira")
        self.assertEqual(aud_view["resumo"]["periodo"], "quinzena2")
        self.assertEqual(aud_view["resumo"]["periodo_inicio"], "2026-11-16")
        self.assertGreaterEqual(aud_view["resumo"]["dias_avaliados"], 1)
        self.assertGreater(len(aud_view["geojson"]["features"]), 0)
        f0 = aud_view["geojson"]["features"][0]["properties"]
        self.assertIn("pct_periodo", f0)
        self.assertIn("dias_previstos", f0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
